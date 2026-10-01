import os, re, ast, json, shutil, difflib, logging, subprocess
import asyncio
from pathlib import Path
from html import escape as html_es
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

# ---------------- CONFIG ----------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit("❌ BOT_TOKEN set koro:  export BOT_TOKEN='...'")

ROOT       = Path.home() / "copypaste"
WORKSPACE  = ROOT / "repos"
STATE_FILE = ROOT / "state.json"
WORKSPACE.mkdir(parents=True, exist_ok=True)

MAX_MSG     = 3800
IGNORE_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
               "dist", "build", ".idea", ".vscode", ".mypy_cache"}

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("copypaste")


# ---------------- STATE ----------------
def load_state():
    if STATE_FILE.exists():
        try: return json.loads(STATE_FILE.read_text())
        except Exception: pass
    return {"mode": "confirm", "current_repo": None, "pending": {}}

STATE = load_state()
def save_state(): STATE_FILE.write_text(json.dumps(STATE, indent=2, default=str))
def get_pending(uid): return STATE["pending"].get(str(uid), {})
def set_pending(uid, d): STATE["pending"][str(uid)] = d; save_state()
def clear_pending(uid): STATE["pending"].pop(str(uid), None); save_state()


# ---------------- PATHS ----------------
def current_repo():
    n = STATE.get("current_repo")
    if not n: raise RuntimeError("Repo select koro: /clone <url> ba /use <name>")
    p = WORKSPACE / n
    if not p.exists(): raise RuntimeError(f"Repo '{n}' nei.")
    return p

def safe_resolve(repo, rel_path):
    p = (repo / rel_path).resolve()
    if not str(p).startswith(str(repo.resolve())):
        raise ValueError("Path repo root er baire")
    return p

def rel(repo, path):
    try: return str(path.relative_to(repo))
    except ValueError: return str(path)


# ---------------- AST ----------------
def _iter_syms(tree):
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield n

def _find_sym(tree, name):
    for n in _iter_syms(tree):
        if n.name == name: return n
    return None

def _lines(node):
    start = node.lineno
    if getattr(node, "decorator_list", None):
        start = node.decorator_list[0].lineno
    return start, getattr(node, "end_lineno", node.lineno)

def list_symbols(path):
    try: tree = ast.parse(path.read_text(errors="ignore"))
    except Exception: return []
    out = []
    for n in _iter_syms(tree):
        s, e = _lines(n)
        kind = "class" if isinstance(n, ast.ClassDef) else "func"
        out.append((n.name, kind, s, e))
    return out

def insert_after(src, symbol, code):
    node = _find_sym(ast.parse(src), symbol)
    if not node: raise ValueError(f"Symbol '{symbol}' paoa gelo na")
    _, end = _lines(node)
    L = src.split("\n")
    return "\n".join(L[:end] + ["", code.rstrip()] + L[end:])

def insert_before(src, symbol, code):
    node = _find_sym(ast.parse(src), symbol)
    if not node: raise ValueError(f"Symbol '{symbol}' paoa gelo na")
    start, _ = _lines(node)
    L = src.split("\n")
    return "\n".join(L[:start-1] + [code.rstrip(), ""] + L[start-1:])

def replace_symbol(src, symbol, code):
    node = _find_sym(ast.parse(src), symbol)
    if not node: raise ValueError(f"Symbol '{symbol}' paoa gelo na")
    start, end = _lines(node)
    L = src.split("\n")
    return "\n".join(L[:start-1] + [code.rstrip()] + L[end:])


# ---------------- SEARCH ----------------
def find_in_repo(repo, name):
    hits, nl = [], name.lower()
    for f in repo.rglob("*.py"):
        if any(p in IGNORE_DIRS for p in f.parts): continue
        for sym, kind, s, e in list_symbols(f):
            if nl in sym.lower():
                hits.append((rel(repo, f), sym, kind, s, e))
    return hits

def grep_repo(repo, pattern, limit=60):
    try: rx = re.compile(pattern)
    except re.error as e: raise ValueError(f"Bad regex: {e}")
    hits = []
    for f in repo.rglob("*"):
        if not f.is_file() or any(p in IGNORE_DIRS for p in f.parts): continue
        try:
            if f.stat().st_size > 500_000: continue
            for i, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
                if rx.search(line):
                    hits.append((rel(repo, f), i, line.strip()[:180]))
                    if len(hits) >= limit: return hits
        except Exception: continue
    return hits


# ---------------- GIT ----------------
def git(repo, *args, check=True):
    r = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    return r.stdout


# ---------------- VALIDATION ----------------
def validate_file(path):
    res = []
    if path.suffix == ".py":
        r = subprocess.run(["python", "-m", "py_compile", str(path)],
                           capture_output=True, text=True)
        res.append(("syntax", r.returncode == 0,
                    (r.stderr or r.stdout).strip()[:500]))
        if shutil.which("ruff"):
            r = subprocess.run(["ruff", "check", str(path)],
                               capture_output=True, text=True)
            res.append(("ruff", r.returncode == 0,
                        (r.stdout or r.stderr).strip()[:500]))
    else:
        res.append(("syntax", True, "skipped (non-Python)"))
    return res


# ---------------- DIFF ----------------
def unified_diff(o, n, p):
    return "".join(difflib.unified_diff(
        o.splitlines(keepends=True), n.splitlines(keepends=True),
        fromfile=f"a/{p}", tofile=f"b/{p}", n=2))


# ---------------- CODE EXTRACT ----------------
CODE_RE = re.compile(r"```(?:\w+)?\n?(.*?)```", re.DOTALL)
def extract_code(t):
    m = CODE_RE.search(t)
    return (m.group(1) if m else t).rstrip()


# ---------------- EDIT PIPELINE ----------------
def propose_edit(p, code):
    repo = current_repo()
    tgt  = safe_resolve(repo, p["path"])
    if not tgt.exists(): raise FileNotFoundError(f"{p['path']} nei")
    orig = tgt.read_text()
    act, anc = p["action"], p.get("anchor")
    if   act == "insert":  new = insert_after(orig, anc, code)
    elif act == "before":  new = insert_before(orig, anc, code)
    elif act == "replace": new = replace_symbol(orig, anc, code)
    elif act == "append":  new = orig.rstrip() + "\n\n" + code.rstrip() + "\n"
    elif act == "prepend": new = code.rstrip() + "\n\n" + orig
    else: raise ValueError(f"Unknown action: {act}")
    return tgt, orig, new

def do_edit_and_report(p):
    repo = current_repo()
    tgt, orig, new = propose_edit(p, p["code"])
    tgt.with_suffix(tgt.suffix + ".agentbak").write_text(orig)
    tgt.write_text(new)
    res  = validate_file(tgt)
    diff = unified_diff(orig, new, rel(repo, tgt))
    ok_all = all(ok for _, ok, _ in res)
    lines = [
        "📋 <b>copypaste — TASK REPORT</b>",
        f"<b>FILE:</b> <code>{html_esc(rel(repo, tgt))}</code>",
        f"<b>ACTION:</b> {html_esc(p['action'])}"
        + (f" — <code>{html_esc(p['anchor'])}</code>" if p.get("anchor") else ""),
        f"<b>DIFF:</b>\n<pre>{html_esc(diff[:1500]) or '(none)'}</pre>",
        "<b>VALIDATION:</b>",
    ]
    for n, ok, m in res:
        lines.append(f"  {'✅' if ok else '❌'} {n}: {html_esc(m or 'ok')}")
    lines.append(f"<b>RESULT:</b> {'SUCCESS' if ok_all else 'FAILED'}")
    rep = "\n".join(lines)
    STATE["last_report"] = rep; save_state()
    return rep


# ==================== HANDLERS ====================
async def cmd_start(u, c):
    await u.message.reply_text(
        "🤖 <b>copypaste v1.1</b> — Autonomous Coding Agent\n\n"
        "<b>Repo</b>\n"
        "/clone &lt;url&gt; · /use &lt;name&gt; · /repos · /repo\n\n"
        "<b>Inspect</b>\n"
        "/ls [path] · /cat &lt;path&gt; · /symbols &lt;path&gt;\n"
        "/find &lt;name&gt; · /grep &lt;pattern&gt;\n\n"
        "<b>Edit</b>\n"
        "/insert &lt;path&gt; &lt;after_symbol&gt;\n"
        "/before &lt;path&gt; &lt;before_symbol&gt;\n"
        "/replace &lt;path&gt; &lt;symbol&gt;\n"
        "/append &lt;path&gt; · /prepend &lt;path&gt;\n\n"
        "<b>Validate &amp; Git</b>\n"
        "/validate [path] · /status · /diff\n"
        "/commit &lt;msg&gt; · /undo &lt;path&gt;\n\n"
        "<b>Mode</b>: /mode  (confirm ⇄ auto)",
        parse_mode="HTML")

async def cmd_clone(u, c):
    if not c.args: return await u.message.reply_text("Usage: /clone <git_url>")
    url  = c.args[0]
    name = url.rstrip("/").split("/")[-1].removesuffix(".git")
    dst  = WORKSPACE / name
    if dst.exists():
        STATE["current_repo"] = name; save_state()
        return await u.message.reply_text(f"Already cloned. Selected: {name}")
    
    msg = await u.message.reply_text(f"⏳ Cloning {url} ...\n(Time limit: 60s)")
    try:
        r = subprocess.run(["git", "clone", "--depth", "50", url, str(dst)],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            shutil.rmtree(dst, ignore_errors=True)
            err_msg = r.stderr.strip()[:800] or "Unknown git error"
            return await msg.edit_text(f"❌ Clone failed:\n{err_msg}")
    except subprocess.TimeoutExpired:
        shutil.rmtree(dst, ignore_errors=True)
        return await msg.edit_text(
            "❌ Clone timed out (60s).\n"
            "Check:\n"
            "• URL is correct?\n"
            "• Repo is public?\n"
            "• Internet connection is on?"
        )
    
    STATE["current_repo"] = name; save_state()
    await msg.edit_text(f"✅ Cloned & selected: <b>{name}</b>", parse_mode="HTML")

async def cmd_use(u, c):
    if not c.args: return await u.message.reply_text("Usage: /use <name>")
    if not (WORKSPACE / c.args[0]).exists():
        return await u.message.reply_text("Not found.")
    STATE["current_repo"] = c.args[0]; save_state()
    await u.message.reply_text(f"✅ Selected: {c.args[0]}")

async def cmd_repos(u, c):
    rp = [p.name for p in WORKSPACE.iterdir() if p.is_dir()]
    await u.message.reply_text(
        "\n".join(f"• <code>{html_esc(x)}</code>" for x in rp) or "(none)",
        parse_mode="HTML")

async def cmd_repo(u, c):
    try:
        r = current_repo()
        await u.message.reply_text(
            f"📦 <b>{r.name}</b>\n<code>{html_esc(str(r))}</code>",
            parse_mode="HTML")
    except Exception as e: await u.message.reply_text(f"❌ {e}")

async def cmd_ls(u, c):
    try:
        repo = current_repo()
        sub  = c.args[0] if c.args else ""
        tgt  = safe_resolve(repo, sub)
        if not tgt.exists(): return await u.message.reply_text("Path not found.")
        if tgt.is_file():
            return await u.message.reply_text(
                f"📄 {rel(repo, tgt)} ({tgt.stat().st_size} bytes)")
        entries = sorted(tgt.iterdir(),
                         key=lambda p: (not p.is_dir(), p.name.lower()))
        lines = []
        for p in entries[:200]:
            if p.name in IGNORE_DIRS: continue
            lines.append(("📁 " if p.is_dir() else "📄 ") + p.name)
        await u.message.reply_text(
            f"<b>{html_esc(rel(repo, tgt) or '.')}</b>\n"
            f"<pre>{html_esc(chr(10).join(lines) or '(empty)')}</pre>",
            parse_mode="HTML")
    except Exception as e: await u.message.reply_text(f"❌ {e}")

async def cmd_cat(u, c):
    try:
        if not c.args: return await u.message.reply_text("Usage: /cat <path>")
        repo = current_repo()
        tgt  = safe_resolve(repo, c.args[0])
        if not tgt.is_file(): return await u.message.reply_text("Not a file.")
        txt = tgt.read_text(errors="ignore")
        if len(txt) > 3500: txt = txt[:3500] + "\n... [truncated]"
        await u.message.reply_text(
            f"<b>{html_esc(c.args[0])}</b>\n<pre>{html_esc(txt)}</pre>",
            parse_mode="HTML")
    except Exception as e: await u.message.reply_text(f"❌ {e}")

async def cmd_symbols(u, c):
    try:
        if not c.args: return await u.message.reply_text("Usage: /symbols <path>")
        repo = current_repo()
        tgt  = safe_resolve(repo, c.args[0])
        syms = list_symbols(tgt)
        if not syms: return await u.message.reply_text("No symbols found.")
        body = "\n".join(f"<code>{html_esc(n)}</code> [{k}] L{s}–L{e}"
                         for n, k, s, e in syms)
        await u.message.reply_text(body[:MAX_MSG], parse_mode="HTML")
    except Exception as e: await u.message.reply_text(f"❌ {e}")

async def cmd_find(u, c):
    try:
        if not c.args: return await u.message.reply_text("Usage: /find <name>")
        repo = current_repo()
        hits = find_in_repo(repo, c.args[0])
        if not hits: return await u.message.reply_text("No matches.")
        body = "\n".join(f"<code>{html_esc(p)}</code> :: "
                         f"<b>{html_esc(n)}</b> [{k}] L{s}–L{e}"
                         for p, n, k, s, e in hits[:80])
        await u.message.reply_text(body[:MAX_MSG], parse_mode="HTML")
    except Exception as e: await u.message.reply_text(f"❌ {e}")

async def cmd_grep(u, c):
    try:
        if not c.args: return await u.message.reply_text("Usage: /grep <pattern>")
        repo = current_repo()
        hits = grep_repo(repo, " ".join(c.args))
        if not hits: return await u.message.reply_text("No matches.")
        body = "\n".join(f"<code>{html_esc(p)}:{i}</code> {html_esc(t)}"
                         for p, i, t in hits)
        await u.message.reply_text(body[:MAX_MSG], parse_mode="HTML")
    except Exception as e: await u.message.reply_text(f"❌ {e}")

async def _start_edit(u, c, action, need_anchor=True):
    if (need_anchor and len(c.args) < 2) or (not need_anchor and len(c.args) < 1):
        usage = (f"Usage: /{action} <path> <symbol>" if need_anchor
                 else f"Usage: /{action} <path>")
        return await u.message.reply_text(usage)
    path   = c.args[0]
    anchor = c.args[1] if need_anchor else None
    uid = u.effective_user.id
    set_pending(uid, {"stage": "awaiting_code", "action": action,
                      "path": path, "anchor": anchor})
    label = (f"after <b>{html_esc(anchor)}</b>" if action == "insert"
             else f"before <b>{html_esc(anchor)}</b>" if action == "before"
             else f"replacing <b>{html_esc(anchor)}</b>" if action == "replace"
             else "")
    await u.message.reply_text(
        f"Code block pathao (``` ``` er vitore) — {label}\n"
        f"File: <code>{html_esc(path)}</code>",
        parse_mode="HTML")

async def cmd_insert(u, c):  await _start_edit(u, c, "insert",  True)
async def cmd_before(u, c):  await _start_edit(u, c, "before",  True)
async def cmd_replace(u, c): await _start_edit(u, c, "replace", True)
async def cmd_append(u, c):  await _start_edit(u, c, "append",  False)
async def cmd_prepend(u, c): await _start_edit(u, c, "prepend", False)

async def on_message(u, c):
    uid = u.effective_user.id
    p = get_pending(uid)
    if not p or p.get("stage") != "awaiting_code": return
    code = extract_code(u.message.text or "")
    if not code.strip():
        return await u.message.reply_text("Empty. ``` ``` er vitore pathao.")
    p["code"] = code; p["stage"] = "awaiting_confirm"; set_pending(uid, p)

    if STATE.get("mode") == "auto":
        try:
            rep = do_edit_and_report(p); clear_pending(uid)
            await u.message.reply_text(rep[:MAX_MSG], parse_mode="HTML")
        except Exception as e:
            clear_pending(uid); await u.message.reply_text(f"❌ {e}")
        return

    try:
        repo = current_repo()
        tgt, orig, new = propose_edit(p, code)
        diff = unified_diff(orig, new, rel(repo, tgt))
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Apply",  callback_data="apply"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel"),
        ]])
        await u.message.reply_text(
            f"<b>Proposed</b> — <code>{html_esc(rel(repo, tgt))}</code>\n"
            f"<pre>{html_esc(diff[:2500]) or '(no change)'}</pre>",
            parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        clear_pending(uid); await u.message.reply_text(f"❌ {e}")

async def on_callback(u, c):
    q = u.callback_query; await q.answer()
    uid = q.from_user.id
    p = get_pending(uid)
    if not p or p.get("stage") != "awaiting_confirm":
        return await q.edit_message_text("No pending edit.")
    if q.data == "cancel":
        clear_pending(uid); return await q.edit_message_text("❌ Cancelled.")
    if q.data == "apply":
        try:
            rep = do_edit_and_report(p); clear_pending(uid)
            await q.edit_message_text(rep[:MAX_MSG], parse_mode="HTML")
        except Exception as e:
            clear_pending(uid); await q.edit_message_text(f"❌ {e}")

async def cmd_validate(u, c):
    try:
        repo = current_repo()
        if c.args: paths = [safe_resolve(repo, c.args[0])]
        else: paths = [f for f in repo.rglob("*.py")
                       if not any(p in IGNORE_DIRS for p in f.parts)][:50]
        lines = []
        for p in paths:
            for name, ok, msg in validate_file(p):
                lines.append(f"{'✅' if ok else '❌'} "
                             f"<code>{html_esc(rel(repo, p))}</code> "
                             f"[{name}] {html_esc(msg or 'ok')}")
                if not ok: break
        await u.message.reply_text(
            "\n".join(lines)[:MAX_MSG] or "(nothing to validate)",
            parse_mode="HTML")
    except Exception as e: await u.message.reply_text(f"❌ {e}")

async def cmd_status(u, c):
    try:
        out = git(current_repo(), "status", "--short")
        await u.message.reply_text(
            f"<pre>{html_esc(out or '(clean)')}</pre>", parse_mode="HTML")
    except Exception as e: await u.message.reply_text(f"❌ {e}")

async def cmd_diff(u, c):
    try:
        out = git(current_repo(), "diff")
        await u.message.reply_text(
            f"<pre>{html_esc(out[:3500] or '(no diff)')}</pre>",
            parse_mode="HTML")
    except Exception as e: await u.message.reply_text(f"❌ {e}")

async def cmd_commit(u, c):
    try:
        if not c.args: return await u.message.reply_text("Usage: /commit <message>")
        repo = current_repo(); msg = " ".join(c.args)
        git(repo, "add", "-A"); git(repo, "commit", "-m", msg)
        out = git(repo, "log", "-1", "--oneline")
        await u.message.reply_text(f"✅ Committed:\n<pre>{html_esc(out)}</pre>",
                                   parse_mode="HTML")
    except Exception as e: await u.message.reply_text(f"❌ {e}")

async def cmd_undo(u, c):
    try:
        if not c.args: return await u.message.reply_text("Usage: /undo <path>")
        repo = current_repo()
        tgt  = safe_resolve(repo, c.args[0])
        bak  = tgt.with_suffix(tgt.suffix + ".agentbak")
        if not bak.exists(): return await u.message.reply_text("No backup.")
        tgt.write_text(bak.read_text())
        await u.message.reply_text(f"✅ Restored {c.args[0]}")
    except Exception as e: await u.message.reply_text(f"❌ {e}")

async def cmd_mode(u, c):
    STATE["mode"] = "auto" if STATE.get("mode") == "confirm" else "confirm"
    save_state()
    await u.message.reply_text(f"Mode: <b>{STATE['mode']}</b>", parse_mode="HTML")


# ---------------- MAIN ----------------
def main():
    # Python 3.14-এ ইভেন্ট লুপ না থাকায় এটি জোরপূর্বক তৈরি করা হচ্ছে
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    app = Application.builder().token(BOT_TOKEN).build()
    cmds = {
        "start": cmd_start, "help": cmd_start,
        "clone": cmd_clone, "use": cmd_use,
        "repos": cmd_repos, "repo": cmd_repo,
        "ls": cmd_ls, "cat": cmd_cat, "symbols": cmd_symbols,
        "find": cmd_find, "grep": cmd_grep,
        "insert": cmd_insert, "before": cmd_before, "replace": cmd_replace,
        "append": cmd_append, "prepend": cmd_prepend,
        "validate": cmd_validate, "status": cmd_status,
        "diff": cmd_diff, "commit": cmd_commit,
        "undo": cmd_undo, "mode": cmd_mode,
    }
    for n, fn in cmds.items():
        app.add_handler(CommandHandler(n, fn))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    log.info("🚀 copypaste v1.1 bot starting ...")
    app.run_polling()


if __name__ == "__main__":
    main()

