"""CopyPaste bot entry point (polling + tiny health server so Render keeps the service alive)."""
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from bot.handlers import register_handlers
from config import BOT_TOKEN, PORT

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)


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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("👋 CopyPaste ready. Use /copy <github_url> to begin.")


def main() -> None:
    if not BOT_TOKEN:
