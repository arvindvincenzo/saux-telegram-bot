import asyncio
import logging
import os
import re
import uuid
from typing import List, Optional
import httpx

from config import SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, SUPABASE_LOGGING_ENABLED

logger = logging.getLogger("supabase_logger")

def get_supabase_url() -> str:
    return (os.getenv("SUPABASE_URL") or SUPABASE_URL or "").strip().rstrip("/")

def get_supabase_key() -> str:
    return (
        os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        or os.getenv("SUPABASE_KEY")
        or os.getenv("SUPABASE_ANON_KEY")
        or SUPABASE_SERVICE_ROLE_KEY
        or ""
    ).strip()

def is_logging_enabled() -> bool:
    env_val = os.getenv("SUPABASE_LOGGING_ENABLED")
    if env_val is not None:
        return env_val.strip().lower() in ("true", "1", "yes")
    return SUPABASE_LOGGING_ENABLED

# URL extraction regex: extracts full URLs including parameters
URL_PATTERN = re.compile(r"https?://[^\s<>\"'{}|\\^`]+", re.IGNORECASE)

# Secret redaction patterns to avoid persisting sensitive credentials
REDACTION_PATTERNS = [
    # Telegram bot token: 123456789:ABCdefGhIjkLmNoPqRsTuVwXyZ...
    (re.compile(r"\b\d{8,12}:[a-zA-Z0-9_-]{30,50}\b"), "[REDACTED_TELEGRAM_TOKEN]"),
    # Google API Key: AIza...
    (re.compile(r"\bAIza[0-9A-Za-z-_]{30,45}\b"), "[REDACTED_API_KEY]"),
    # Generic API Keys (Groq, OpenAI, Anthropic, etc.)
    (re.compile(r"\b(?:gsk_|sk-ant-|sk-|key-)[a-zA-Z0-9_-]{20,}\b"), "[REDACTED_API_KEY]"),
    # Bearer tokens
    (re.compile(r"(?i)\bbearer\s+[a-zA-Z0-9_\-\.]{20,}\b"), "Bearer [REDACTED_TOKEN]"),
    # Explicit password/secret key-value pairs
    (re.compile(r"(?i)\b(password|passwd|secret|api_key|token)\s*[:=]\s*\S+"), r"\1=[REDACTED]"),
]

def redact_secrets(text: Optional[str]) -> Optional[str]:
    """Redacts API keys, credentials, and authentication tokens from text."""
    if not text:
        return text
    clean_text = text
    for pattern, replacement in REDACTION_PATTERNS:
        clean_text = pattern.sub(replacement, clean_text)
    return clean_text

def extract_user_urls(text: Optional[str]) -> List[str]:
    """
    Extracts and normalizes URLs exclusively from user text.
    Strips trailing punctuation commonly attached in conversational chat.
    Deduplicates URLs while preserving original submission order.
    """
    if not text:
        return []
    
    matches = URL_PATTERN.findall(text)
    cleaned_urls: List[str] = []
    seen = set()

    for m in matches:
        # Strip trailing sentence punctuation
        u = m.rstrip(".,;:!?'\")>]}")
        if u and u not in seen:
            seen.add(u)
            cleaned_urls.append(u)

    return cleaned_urls

async def _post_record(client: httpx.AsyncClient, endpoint: str, payload: dict | list) -> bool:
    """Helper to post JSON data to Supabase PostgREST with a single retry."""
    sb_url = get_supabase_url()
    sb_key = get_supabase_key()
    url = f"{sb_url}/rest/v1/{endpoint}"
    headers = {
        "apikey": sb_key,
        "Authorization": f"Bearer {sb_key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal"
    }

    for attempt in range(2):
        try:
            resp = await client.post(url, json=payload, headers=headers, timeout=8.0)
            if resp.status_code in (200, 201, 204):
                return True
            logger.warning(
                f"Supabase PostgREST returned HTTP {resp.status_code} for {endpoint}: {resp.text[:200]}"
            )
            if resp.status_code < 500:
                # Client error (4xx), do not retry
                break
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            if attempt == 0:
                await asyncio.sleep(0.5)
                continue
            logger.warning(f"Network error contacting Supabase for {endpoint}: {e}")
        except Exception as e:
            logger.warning(f"Unexpected error posting to Supabase {endpoint}: {e}")
            break
    return False

async def log_interaction_task(
    platform_user_id: int | str,
    username: Optional[str],
    display_name: Optional[str],
    user_message: str,
    ai_response: Optional[str] = None,
    message_type: str = "text",
    conversation_id: Optional[str] = None,
    platform: str = "telegram",
    explicit_user_urls: Optional[List[str]] = None
) -> Optional[str]:
    """
    Asynchronously persists an interaction and any user-submitted URLs to Supabase.
    Runs completely non-blocking without delaying user replies.
    """
    if not is_logging_enabled() or not get_supabase_url() or not get_supabase_key():
        return None

    interaction_id = str(uuid.uuid4())

    # 1. Clean and redact inputs
    safe_user_message = redact_secrets(user_message) or ""
    safe_ai_response = redact_secrets(ai_response)

    # 2. Extract URLs exclusively from the original incoming user message
    if explicit_user_urls is not None:
        user_urls = explicit_user_urls
    else:
        user_urls = extract_user_urls(user_message)

    interaction_payload = {
        "id": interaction_id,
        "platform": platform,
        "platform_user_id": str(platform_user_id),
        "username": username or None,
        "display_name": display_name or None,
        "conversation_id": str(conversation_id) if conversation_id else None,
        "user_message": safe_user_message,
        "ai_response": safe_ai_response,
        "message_type": message_type,
    }

    try:
        async with httpx.AsyncClient() as client:
            # Insert user_interaction
            success = await _post_record(client, "user_interactions", interaction_payload)
            if not success:
                logger.warning("Failed to record user_interaction in Supabase.")
                return None

            # Insert submitted_urls if any were present in the user's message
            if user_urls:
                url_payloads = [
                    {
                        "id": str(uuid.uuid4()),
                        "interaction_id": interaction_id,
                        "platform_user_id": str(platform_user_id),
                        "original_url": url,
                        "platform": platform,
                    }
                    for url in user_urls
                ]
                await _post_record(client, "submitted_urls", url_payloads)

        return interaction_id
    except Exception as e:
        logger.warning(f"Error in Supabase logging task: {e}")
        return None

def log_interaction(
    platform_user_id: int | str,
    username: Optional[str],
    display_name: Optional[str],
    user_message: str,
    ai_response: Optional[str] = None,
    message_type: str = "text",
    conversation_id: Optional[str] = None,
    platform: str = "telegram",
    explicit_user_urls: Optional[List[str]] = None
):
    """
    Fire-and-forget logging function. Launches log_interaction_task as a background
    asyncio task so that the bot message handler is never blocked.
    """
    try:
        asyncio.create_task(
            log_interaction_task(
                platform_user_id=platform_user_id,
                username=username,
                display_name=display_name,
                user_message=user_message,
                ai_response=ai_response,
                message_type=message_type,
                conversation_id=conversation_id,
                platform=platform,
                explicit_user_urls=explicit_user_urls
            )
        )
    except Exception as e:
        logger.warning(f"Could not spawn Supabase log task: {e}")
