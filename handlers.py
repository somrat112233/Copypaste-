"""Telegram handlers: /copy <github_url> -> inspection -> interactive file browser."""
import asyncio
import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from config import ALLOWED_USER_IDS, PAGE_SIZE
from github.client import GitHubError, inspect_repository

log = logging.getLogger(__name__)
SESSION_KEY = "copy_session"


def _allowed(update: Update) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    user = update.effective_user
    return bool(user and user.id in ALLOWED_USER_IDS)


def _fmt_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def _short(name: str, limit: int = 34) -> str:
    return name if len(name) <= limit else name[: limit - 1] + "…"


def _listing(session: dict):
    """Directories and files directly inside the current folder."""
    cwd = session["cwd"]
    prefix = cwd + "/" if cwd else ""
    dirs, files = set(), []
    for i, entry in enumerate(session["inspection"].files):
        if not entry.path.startswith(prefix):
            continue
        rest = entry.path[len(prefix):]
        if "/" in rest:
            dirs.add(rest.split("/", 1)[0])
        else:
            files.append(i)
    files.sort(key=lambda i: session["inspection"].files[i].path.lower())
    return sorted(dirs, key=str.lower), files


def _render(session: dict):
    insp = session["inspection"]
    dirs, files = _listing(session)
    view = [("dir", d) for d in dirs] + [("file", i) for i in files]
    session["view"] = view

    pages = max(1, -(-len(view) // PAGE_SIZE))
    session["page"] = max(0, min(session["page"], pages - 1))
    page = session["page"]
    chunk = list(enumerate(view))[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    location = "/" + session["cwd"] if session["cwd"] else "/"
    text = (
        f"📦 <b>{html.escape(insp.full_name)}</b>"
        f"{' 🔒' if insp.private else ''}\n"
        f"🌿 Branch: <code>{html.escape(insp.branch)}</code>"
        f"{'' if insp.branch != insp.default_branch else ' (default)'}\n"
        f"📄 {len(insp.files)} files · {_fmt_size(insp.total_size)}\n"
        f"📍 <code>{html.escape(location)}</code> · page {page + 1}/{pages}\n"
        f"✅ Selected: {len(session['selected'])}"
    )
    if insp.truncated:
        text += "\n⚠️ GitHub truncated the tree (very large repo); some files may be missing."

    rows = []
    for view_idx, (kind, value) in chunk:
        if kind == "dir":
            rows.append([InlineKeyboardButton(f"📁 {_short(value)}", callback_data=f"cp:nav:{view_idx}")])
        else:
            entry = insp.files[value]
            name = entry.path.rsplit("/", 1)[-1]
            mark = "✅" if value in session["selected"] else "⬜"
            rows.append([InlineKeyboardButton(
                f"{mark} {_short(name, 28)} ({_fmt_size(entry.size)})",
                callback_data=f"cp:sel:{value}",
            )])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"cp:pg:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"cp:pg:{page + 1}"))
    if nav:
        rows.append(nav)

    tools = []
    if session["cwd"]:
        tools.append(InlineKeyboardButton("⬆️ Up", callback_data="cp:up:0"))
    if files:
        tools.append(InlineKeyboardButton("☑️ All here", callback_data="cp:all:0"))
        tools.append(InlineKeyboardButton("🧹 Clear here", callback_data="cp:none:0"))
    if tools:
        rows.append(tools)

    rows.append([
        InlineKeyboardButton(f"✅ Done ({len(session['selected'])})", callback_data="cp:done:0"),
        InlineKeyboardButton("❌ Cancel", callback_data="cp:cancel:0"),
    ])
    return text, InlineKeyboardMarkup(rows)


async def copy_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        await update.message.reply_text("⛔ You are not allowed to use this bot.")
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: <code>/copy &lt;github_url&gt;</code>\n"
            "Example: <code>/copy https://github.com/owner/repo</code>\n"
            "Branch/folder URLs work too: <code>.../tree/dev/src</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    url = context.args[0]
    status = await update.message.reply_text("🔎 Inspecting repository…")
    try:
        insp = await asyncio.to_thread(inspect_repository, url)
    except GitHubError as exc:
        await status.edit_text(f"❌ {exc}")
        return
    except Exception:
        log.exception("Unexpected error inspecting %s", url)
        await status.edit_text("❌ Unexpected error while inspecting the repository.")
        return

    if not insp.files:
        await status.edit_text("📭 That repository has no files on this branch.")
        return

    session = {
        "inspection": insp,
        "cwd": insp.start_path,
        "page": 0,
        "selected": set(),
        "view": [],
    }
    context.user_data[SESSION_KEY] = session
    text, markup = _render(session)
    await status.edit_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)


async def copy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _allowed(update):
        await query.answer("Not allowed.", show_alert=True)
        return

    session = context.user_data.get(SESSION_KEY)
    if not session:
        await query.answer("Session expired. Run /copy again.", show_alert=True)
        return

    try:
        _, action, arg = query.data.split(":", 2)
        arg_i = int(arg)
    except ValueError:
        await query.answer()
        return

    insp = session["inspection"]

    if action == "nav":
        kind, value = session["view"][arg_i]
        if kind == "dir":
            session["cwd"] = f"{session['cwd']}/{value}" if session["cwd"] else value
            session["page"] = 0
    elif action == "up":
        session["cwd"] = session["cwd"].rsplit("/", 1)[0] if "/" in session["cwd"] else ""
        session["page"] = 0
    elif action == "pg":
        session["page"] = arg_i
    elif action == "sel":
        session["selected"] ^= {arg_i}
    elif action in ("all", "none"):
        _, files = _listing(session)
        if action == "all":
            session["selected"] |= set(files)
        else:
            session["selected"] -= set(files)
    elif action == "cancel":
        context.user_data.pop(SESSION_KEY, None)
        await query.answer()
        await query.edit_message_text("🚫 Copy cancelled.")
        return
    elif action == "done":
        if not session["selected"]:
            await query.answer("Select at least one file first.", show_alert=True)
            return
        chosen = [insp.files[i] for i in sorted(session["selected"], key=lambda i: insp.files[i].path)]
        session["chosen"] = chosen
        listing = "\n".join(f"• <code>{html.escape(f.path)}</code> ({_fmt_size(f.size)})" for f in chosen[:40])
        if len(chosen) > 40:
            listing += f"\n… and {len(chosen) - 40} more"
        await query.answer()
        await query.edit_message_text(
            f"✅ <b>{len(chosen)} file(s) selected</b> from "
            f"<code>{html.escape(insp.full_name)}@{html.escape(insp.branch)}</code>\n\n{listing}\n\n"
            "➡️ Next step: Destination Select (coming in the next feature).",
            parse_mode=ParseMode.HTML,
        )
        return

    await query.answer()
    text, markup = _render(session)
    try:
        await query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


def register_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("copy", copy_command))
    app.add_handler(CallbackQueryHandler(copy_callback, pattern=r"^cp:"))
