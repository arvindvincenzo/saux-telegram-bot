import os
import yaml
import dotenv
from pathlib import Path

config_dir = Path(__file__).parent.parent.resolve() / "config"

# load yaml config
config_yaml = {}
config_yml_path = config_dir / "config.yml"
config_example_path = config_dir / "config.example.yml"

if config_yml_path.exists():
    with open(config_yml_path, 'r', encoding="utf-8") as f:
        config_yaml = yaml.safe_load(f) or {}
elif config_example_path.exists():
    with open(config_example_path, 'r', encoding="utf-8") as f:
        config_yaml = yaml.safe_load(f) or {}

# load .env config if present
env_path = config_dir / "config.env"
config_env = dotenv.dotenv_values(env_path) if env_path.exists() else {}

# config parameters with env variable fallback
telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN") or config_yaml.get("telegram_token", "")
openai_api_base = os.environ.get("OPENAI_API_BASE") or config_yaml.get("openai_api_base", "https://api.groq.com/openai/v1")

# Build keys list
raw_keys = config_yaml.get("openai_api_keys") or []
if isinstance(raw_keys, list) and len(raw_keys) > 0:
    openai_api_keys = [str(k).strip() for k in raw_keys if k and str(k).strip()]
elif config_yaml.get("openai_api_key"):
    openai_api_keys = [str(config_yaml["openai_api_key"]).strip()]
else:
    openai_api_keys = []

# Fallback / merge with GROQ_API_KEYS or OPENAI_API_KEYS from env if available
env_groq = os.environ.get("GROQ_API_KEYS") or os.environ.get("GROQ_API_KEY") or os.environ.get("OPENAI_API_KEYS") or os.environ.get("OPENAI_API_KEY")
if env_groq:
    env_keys = [k.strip() for k in env_groq.replace("\n", ",").split(",") if k.strip()]
    for k in env_keys:
        if k not in openai_api_keys:
            openai_api_keys.append(k)

openai_api_key = openai_api_keys[0] if openai_api_keys else None

openrouter_api_key = os.environ.get("OPENROUTER_API_KEY") or config_yaml.get("openrouter_api_key", None)
openrouter_api_base = config_yaml.get("openrouter_api_base", "https://openrouter.ai/api/v1")
allowed_telegram_usernames = config_yaml.get("allowed_telegram_usernames", [])
new_dialog_timeout = config_yaml.get("new_dialog_timeout", 600)
enable_message_streaming = config_yaml.get("enable_message_streaming", True)
return_n_generated_images = config_yaml.get("return_n_generated_images", 1)
image_size = config_yaml.get("image_size", "1024x1024")
n_chat_modes_per_page = config_yaml.get("n_chat_modes_per_page", 5)

# MongoDB or local storage fallback
mongodb_uri = os.environ.get("MONGODB_URI") or f"mongodb://mongo:{config_env.get('MONGODB_PORT', 27017)}"

# chat_modes
chat_modes_path = config_dir / "chat_modes.yml"
if chat_modes_path.exists():
    with open(chat_modes_path, 'r', encoding="utf-8") as f:
        chat_modes = yaml.safe_load(f) or {}
else:
    chat_modes = {}

# models
models_path = config_dir / "models.yml"
if models_path.exists():
    with open(models_path, 'r', encoding="utf-8") as f:
        models = yaml.safe_load(f) or {}
else:
    models = {}

# files and canonical storage directories
help_group_chat_video_path = Path(__file__).parent.parent.resolve() / "static" / "help_group_chat.mp4"
downloads_dir = Path(__file__).parent.resolve() / "downloads"
converted_dir = Path(__file__).parent.resolve() / "converted_files"
downloads_dir.mkdir(parents=True, exist_ok=True)
converted_dir.mkdir(parents=True, exist_ok=True)
