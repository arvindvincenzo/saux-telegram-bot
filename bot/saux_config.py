import re
import hmac
import hashlib
import urllib.parse
from datetime import datetime, date
from typing import Optional, Dict, Any

# ==========================================
# SAUX Centralized Configuration & Assets
# ==========================================

BOT_NAME = "SAUX Super Intelligence"
BOT_HANDLE = "@sauxsibot"
MINI_APP_NAME = "Crate"
MINI_APP_URL = "https://t.me/sauxsibot/crate"

OFFICIAL_SUPERGROUP_URL = "https://t.me/strangerspace"
OFFICIAL_CHANNEL_URL = OFFICIAL_SUPERGROUP_URL

# Telegram Stars Premium Plans (currency="XTR")
PREMIUM_PLANS = {
    "buy_stars_50": {
        "stars": 50,
        "days": 7,
        "name": "1 Week Starter Pass",
        "description": "7 days of unlimited video fetching, priority cloud queue & AI reasoning context"
    },
    "buy_stars_150": {
        "stars": 150,
        "days": 30,
        "name": "1 Month Pro Pass",
        "description": "30 days of unlimited video fetching, priority cloud queue & AI reasoning context"
    },
    "buy_stars_350": {
        "stars": 350,
        "days": 3650,  # Lifetime
        "name": "Lifetime VIP Access",
        "description": "Permanent lifetime access to all SAUX Super Intelligence features with zero limits"
    }
}

# ==========================================
# Promotional Templates
# ==========================================

VIDEO_CAPTION_PROMO = (
    "\n\n"
    "✨ <b>SAUX Super Intelligence</b> | <a href=\"https://t.me/strangerspace\">Strangerspace</a>\n"
    "⭐ Upgrade to <b>Premium</b> with /premium for unlimited downloads & VIP speed!"
)

AI_REPLY_PROMO = (
    "\n\n────────────\n"
    "🚀 <b>SAUX Super Intelligence</b> • <a href=\"https://t.me/strangerspace\">Strangerspace</a>\n"
    "⭐ <b>Premium:</b> Unlimited downloads & VIP speed with /premium"
)


def format_video_caption(title: str, is_premium: bool = False) -> str:
    """
    Formats the video caption for Telegram delivery.
    - Preserves the true video title.
    - If user is Premium: Delivers clean title with ZERO promotional text.
    - If user is Free: Appends consistent promotional section under the title.
    - Strictly respects Telegram's 1024 character caption limit.
    """
    clean_title = (title or "Video").strip()

    # Clean out internal brackets or filesystem artifacts if any leaked
    clean_title = re.sub(r'\[FILE:\s*[^\]]+\]', '', clean_title, flags=re.IGNORECASE)
    clean_title = re.sub(r'\[TITLE:\s*[^\]]+\]', '', clean_title, flags=re.IGNORECASE)
    clean_title = re.sub(r'[A-Za-z]:\\[^\s\n*`<>]+', '', clean_title).strip()

    if is_premium:
        return clean_title[:1024]

    promo = VIDEO_CAPTION_PROMO
    # Telegram max caption is 1024 characters
    max_title_len = 1024 - len(promo) - 5
    if len(clean_title) > max_title_len:
        clean_title = clean_title[:max_title_len].rstrip() + "..."

    return f"{clean_title}{promo}"


def format_ai_reply(text: str, is_premium: bool = False) -> str:
    """
    Appends the promotional footer underneath normal AI replies for free users.
    Premium users get a 100% clean, uninterrupted response.
    """
    clean_text = (text or "").strip()
    if is_premium or not clean_text:
        return clean_text

    # Prevent duplicate footers
    if "SAUX Super Intelligence" in clean_text and "/premium" in clean_text:
        return clean_text

    # Telegram message limit is 4096 characters
    promo = AI_REPLY_PROMO
    max_text_len = 4096 - len(promo) - 5
    if len(clean_text) > max_text_len:
        clean_text = clean_text[:max_text_len].rstrip() + "..."

    return f"{clean_text}{promo}"


# ==========================================
# SAUX Crate Mini App Validation (initData)
# ==========================================

def validate_telegram_init_data(init_data: str, bot_token: str) -> Optional[Dict[str, Any]]:
    """
    Validates Telegram WebApp initData query string using HMAC-SHA256 according to
    Telegram's official authentication protocol.
    Returns parsed data dictionary if valid, or None if validation fails.
    """
    try:
        if not init_data or not bot_token:
            return None

        parsed_data = dict(urllib.parse.parse_qsl(init_data))
        if "hash" not in parsed_data:
            return None

        received_hash = parsed_data.pop("hash")

        # Sort remaining key=value pairs alphabetically
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))

        # Secret key: HMAC-SHA256 of bot_token with key "WebAppData"
        secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()

        # Calculated hash
        calculated_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

        if hmac.compare_digest(calculated_hash, received_hash):
            return parsed_data
        return None
    except Exception:
        return None


# ==========================================
# Grounded SAUX Bot Knowledge Base
# ==========================================

SAUX_KNOWLEDGE_BASE = (
    "Identity & Purpose:\n"
    "You are SAUX Super Intelligence (SAUX Bot), an all-in-one autonomous AI powerhouse and high-speed media agent on Telegram.\n\n"
    "Core Capabilities & Services:\n"
    "1. Ultra-Fast Video Fetcher & Downloader:\n"
    "   - Instant downloading and delivery of videos, reels, and shorts in seconds from YouTube, Instagram, TikTok, X (Twitter), Reddit, and Pinterest in crystal-clear HD.\n"
    "2. Audio Extractor & Precision Video Trimmer:\n"
    "   - Extract pristine MP3 audio from any video link or uploaded media.\n"
    "   - Trim and clip videos between precise timestamps (/trim, /clip).\n"
    "3. Universal Media & File Converter:\n"
    "   - Effortlessly convert between MP4, WebM, GIF, MP3, WAV, PNG, JPG, and PDF.\n"
    "4. Live Real-Time Web Intelligence (SearXNG):\n"
    "   - Factual information retrieval, live news, sports, tech updates, and financial markets without search bias.\n"
    "5. Smart Image Optimization:\n"
    "   - Target file size compression (KB/MB) preserving sharp visual fidelity.\n"
    "6. Next-Gen Multi-Model Reasoning:\n"
    "   - Powered by top-tier LLM reasoning for coding, mathematics, business logic, and creative writing.\n"
    "7. Crate (Telegram Mini App):\n"
    "   - 'Crate' is the official built-in Telegram Mini App accessible via the bottom menu button. It gives users a visual dashboard to organize, preview, and manage their downloaded media crate, playlists, and conversion tools directly inside Telegram.\n"
    "8. SAUX Premium (Powered by Telegram Stars):\n"
    "   - Unlocks unlimited video downloads (zero throttling), VIP priority queue execution, unconstrained AI context, and a 100% ad-free experience.\n"
    "   - Plans: 50 ⭐ (1 Week Starter), 150 ⭐ (1 Month Pro), 350 ⭐ (Lifetime VIP Access). Users can type /premium to subscribe.\n"
    "9. Community & Discussion:\n"
    "   - Official Strangerspace: https://t.me/strangerspace\n"
)
