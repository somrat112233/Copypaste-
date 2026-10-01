"""Central configuration for CopyPaste. Values come from environment variables."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
BACKUP_DIR = BASE_DIR / "backups"

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# Optional but recommended: raises GitHub rate limit and allows private repos.
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_API = "https://api.github.com"
GITHUB_TIMEOUT = 20

# Optional comma-separated Telegram user IDs allowed to use the bot. Empty = everyone.
ALLOWED_USER_IDS = {
    int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip().isdigit()
}

# File browser page size (buttons per page)
PAGE_SIZE = 8

# Render injects PORT for web services; used by the health server in main.py
PORT = int(os.environ.get("PORT", "10000"))

DATA_DIR.mkdir(exist_ok=True)
BACKUP_DIR.mkdir(exist_ok=True)
