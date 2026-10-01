"""Telegram handlers: main menu, input form, GitHub inspection, interactive file browser."""
import asyncio
import html
import logging
import re

from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import ALLOWED_USER_IDS, PAGE_SIZE
from github.client import GitHubError, inspect_repository, parse_github_url

log = logging.getLogger(__name__)
SESSION_KEY = "copy_session"
FORM_KEY = "copy_form"
HTML = ParseMode.HTML

MENU_TEXT = "🏠 <b>CopyPaste</b>\nChoose an action:"
HELP_TEXT = (
    "ℹ️ <b>Help</b>\n\n"
    "1. Press <b>New Copy</b>\n"
    "2. Tap the URL field and send a GitHub link\n"
    "3. Optionally set a branch\n"
    "4. Press <b>OK</b> and pick your files\n\n"
    "Shortcuts: /start opens this menu, /copy opens the form."
)


# ----------------------------------------------------------------- helpers
def _allowed(update: Update) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    user = update.effective_user
    return bool(user and user.id in ALLOWED_USER_IDS)


def _btn(label: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=data)


def _fmt_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def _short(name: str, limit: int = 34) -> str:
    return name if len(name) <= limit else name[: limit - 1] + "…"


async def _edit(query_or_bot, text, markup=None, chat_id=None, message_id=None):
    """Edit a message, ignoring Telegram's 'message is not modified' error."""
    try:
        if chat_id is None:
            await query_or_bot.edit_message_text(text, reply_markup=markup, parse_mode=HTML)
        else:
            await query_or_bot.edit_message_text(
                text, chat_id=chat_id, message_id=message_id, reply_markup=markup, parse_mode=HTML
            )
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


async def _safe_delete(bot, chat_id, message_id) -> None:
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


# -------------------------------------------------------------- main menu
def _menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_btn("📥 New Copy", "menu:new")],
        [_btn("ℹ️ Help", "menu:help")],
    ])


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        await update.message.reply_text("⛔ You are not allowed to use this bot.")
        return
    await update.message.reply_text(MENU_TEXT, reply_markup=_menu_markup(), parse_mode=HTML)


# ------------------------------------------------------------------- form
def _form_view(form: dict, notice: str = ""):
    url, branch = form["url"], form["branch"]
    url_line = f"<code>{html.escape(url)}</code>" if url else "<i>not set</i>"
    branch_line = f"<code>{html.escape(branch)}</code>" if branch else "<i>auto (default branch)</i>"
    text = (
        "📝 <b>New Copy</b>\n"
        "Tap a field to fill it in, then press OK.\n\n"
        f"🔗 GitHub URL: {url_line}\n"
        f"🌿 Branch: {branch_line}"
    )
    if notice:
        text += f"\n\n{notice}"

    rows = [
        [_btn(f"🔗 URL: {_short(url, 28) if url else 'tap to set'}", "form:url")],
        [_btn(f"🌿 Branch: {_short(branch, 24) if branch else 'auto'}", "form:branch")],
    ]
    if branch:
        rows.append([_btn("🔄 Reset branch to auto", "form:resetbranch")])
    rows.append([_btn("✅ OK", "form:ok"), _btn("❌ Cancel", "form:cancel")])
    return text, InlineKeyboardMarkup(rows)


async def _refresh_form(context: ContextTypes.DEFAULT_TYPE, form: dict, notice: str = "") -> None:
    text, markup = _form_view(form, notice)
    if form.get("msg_id"):
        await _edit(context.bot, text, markup, chat_id=form["chat_id"], message_id=form["msg_id"])
    else:
        msg = await context.bot.send_message(form["chat_id"], text, reply_markup=markup, parse_mode=HTML)
        form["msg_id"] = msg.message_id


def _new_form(chat_id: int, msg_id=None, url: str = "") -> dict:
    return {"chat_id": chat_id, "msg_id": msg_id, "url": url, "branch": "", "awaiting": None, "prompt_id": None}


async def copy_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/copy opens the form. A URL after the command pre-fills the URL field."""
    if not _allowed(update):
        await update.message.reply_text("⛔ You are not allowed to use this bot.")
        return
    prefill = context.args[0] if context.args else ""
    notice = ""
    if prefill:
        try:
            parse_github_url(prefill)
        except GitHubError as exc:
            notice = f"❌ {html.escape(str(exc))}"
            prefill = ""
    form = _new_form(update.effective_chat.id, url=prefill)
    context.user_data[FORM_KEY] = form
    await _refresh_form(context, form, notice)


async def _ask_field(context: ContextTypes.DEFAULT_TYPE, form: dict, field: str) -> None:
    await _safe_delete(context.bot, form["chat_id"], form.get("prompt_id"))
    prompts = {
        "url": ("🔗 Send the GitHub repository URL:", "https://github.com/owner/repo"),
        "branch": ("🌿 Send the branch name:", "main"),
    }
    text, placeholder = prompts[field]
    msg = await context.bot.send_message(
        form["chat_id"], text,
        reply_markup=ForceReply(selective=True, input_field_placeholder=placeholder),
    )
    form["awaiting"] = field
    form["prompt_id"] = msg.message_id


async def text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Receives the value for whichever form field is waiting for input."""
    if not _allowed(update):
        return
    form = context.user_data.get(FORM_KEY)
    if not form or not form.get("awaiting"):
        return

    field = form["awaiting"]
    value = update.message.text.strip()
    error = ""
    if field == "url":
        try:
            parse_github_url(value)
        except GitHubError as exc:
            error = str(exc)
    elif field == "branch" and not re.fullmatch(r"[\w./-]+", value):
        error = "Branch names may only contain letters, numbers, . _ - /"

    if error:
        await update.message.reply_text(
            f"❌ {error}\nTry again:",
            reply_markup=ForceReply(selective=True, input_field_placeholder="Send the value again"),
        )
        return

    form[field] = value
    form["awaiting"] = None
    await _safe_delete(context.bot, form["chat_id"], update.message.message_id)
    await _safe_delete(context.bot, form["chat_id"], form.get("prompt_id"))
    form["prompt_id"] = None
    await _refresh_form(context, form)


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the main menu and the form buttons."""
    query = update.callback_query
    if not _allowed(update):
        await query.answer("Not allowed.", show_alert=True)
        return

    group, action = query.data.split(":", 1)

    if group == "menu":
        await query.answer()
        if action == "new":
            form = _new_form(query.message.chat_id, query.message.message_id)
            context.user_data[FORM_KEY] = form
            await _refresh_form(context, form)
        elif action == "help":
            await _edit(query, HELP_TEXT, InlineKeyboardMarkup([[_btn("🏠 Menu", "menu:home")]]))
        else:  # home
            context.user_data.pop(FORM_KEY, None)
            await _edit(query, MENU_TEXT, _menu_markup())
        return

    form = context.user_data.get(FORM_KEY)
    if not form:
        await query.answer("Session expired. Send /start.", show_alert=True)
        return

    if action in ("url", "branch"):
        await query.answer()
        await _ask_field(context, form, action)
    elif action == "resetbranch":
        form["branch"] = ""
        await query.answer("Branch reset to auto.")
        await _refresh_form(context, form)
    elif action == "cancel":
        await query.answer()
        await _safe_delete(context.bot, form["chat_id"], form.get("prompt_id"))
        context.user_data.pop(FORM_KEY, None)
        await _edit(query, "🚫 Cancelled.\n\n" + MENU_TEXT, _menu_markup())
    elif action == "ok":
        if not form["url"]:
            await query.answer("Please set the GitHub URL first.", show_alert=True)
            return
        await query.answer("Inspecting…")
        await _safe_delete(context.bot, form["chat_id"], form.get("prompt_id"))
        form["awaiting"] = None
        await _edit(query, "🔎 Inspecting repository…")
        await _run_inspection(update, context, form)


async def _run_inspection(update: Update, context: ContextTypes.DEFAULT_TYPE, form: dict) -> None:
    url = form["url"]
    if form["branch"]:
        owner, repo, _ = parse_github_url(url)
        url = f"https://github.com/{owner}/{repo}/tree/{form['branch']}"
    try:
        insp = await asyncio.to_thread(inspect_repository, url)
    except GitHubError as exc:
        await _refresh_form(context, form, f"❌ {html.escape(str(exc))}")
        return
    except Exception:
        log.exception("Unexpected error inspecting %s", url)
        await _refresh_form(context, form, "❌ Unexpected error while inspecting the repository.")
        return

    if not insp.files:
        await _refresh_form(context, form, "📭 That repository has no files on this branch.")
        return

    session = {"inspection": insp, "cwd": insp.start_path, "page": 0, "selected": set(), "view": []}
    context.user_data[SESSION_KEY] = session
    context.user_
