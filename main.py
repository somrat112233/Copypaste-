"""CopyPaste bot entry point (polling + tiny health server so Render keeps the service alive)."""
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import BotCommand, Update
from telegram.error import Conflict
from telegram.ext import Application, ContextTypes

from bot.handlers import register_handlers
from config import BOT_TOKEN, PORT

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # keeps the bot token out of the logs


class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"CopyPaste bot is running")

    def log_message(self, *args):
        pass


def _start_health_server() -> None:
    server = HTTPServer(("0.0.0.0", PORT), _Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()


async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start", "Open the menu"),
        BotCommand("copy", "Open the copy form"),
    ])


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, Conflict):
        logging.warning("Conflict: another instance is polling with the same BOT_TOKEN.")
        return
    logging.error("Unhandled error", exc_info=context.error)


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN environment variable is not set.")
    _start_health_server()
    app = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()
    register_handlers(app)
    app.add_error_handler(_on_error)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
