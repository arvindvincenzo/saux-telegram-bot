import os
from pathlib import Path
BASE_DIR = Path(__file__).resolve().parent
from key_pool import KeyPool
try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except ImportError:
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip("'\"")
                    if k not in os.environ:
                        os.environ[k] = v


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

# Supabase Analytics & Conversation Logging
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://agdruziwupgyoyafbjcs.supabase.co").strip().rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = (
    os.getenv("SUPABASE_SERVICE_ROLE_KEY") 
    or os.getenv("SUPABASE_KEY") 
    or os.getenv("SUPABASE_ANON_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImFnZHJ1eml3dXBneW95YWZiamNzIiwicm9sZSI6ImFub24iLCJpYXQiOjE3OTEyNTA5MzMsImV4cCI6MjEwNjgyNjkzM30.LCofOmrFsuLYWdzzFwGeDCDopz57vkKpZ_B4fsvmXyY")
).strip()
SUPABASE_LOGGING_ENABLED = os.getenv("SUPABASE_LOGGING_ENABLED", "true").strip().lower() in ("true", "1", "yes")

