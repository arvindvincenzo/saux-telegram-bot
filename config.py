import os
from pathlib import Path
from dotenv import load_dotenv
from key_pool import KeyPool

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# Telegram Bot Token
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

# AI Brain Provider: 'gemini', 'groq', or 'ollama'
AI_PROVIDER = os.getenv("AI_PROVIDER", "gemini").strip().lower()

# Daily quota per Telegram user (Default: 50 requests / user / day)
DAILY_USER_LIMIT = int(os.getenv("DAILY_USER_LIMIT", "50"))

# Free Key Pools (comma-separated for key rotation)
raw_gemini_keys = os.getenv("GEMINI_API_KEYS") or os.getenv("GEMINI_API_KEY", "")
gemini_key_pool = KeyPool(raw_gemini_keys)

raw_groq_keys = os.getenv("GROQ_API_KEYS") or os.getenv("GROQ_API_KEY", "")
groq_key_pool = KeyPool(raw_groq_keys)

# Model definitions
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "google:gemini-2.0-flash")
GROQ_MODEL = os.getenv("GROQ_MODEL", "groq:llama-3.3-70b-versatile")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "openai:qwen2.5:7b")

# Temp storage for downloads / processed media
TEMP_DIR = BASE_DIR / "temp_media"
TEMP_DIR.mkdir(parents=True, exist_ok=True)
