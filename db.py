import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "bot_users.db"

def init_db():
    """Initializes the SQLite database."""
    with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=30000;")
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_quota (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                requests_today INTEGER DEFAULT 0,
                last_request_date TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_sessions (
                user_id INTEGER PRIMARY KEY,
                session_id TEXT,
                last_active TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS media_cache (
                url TEXT PRIMARY KEY,
                file_id TEXT,
                media_type TEXT,
                title TEXT,
                size_mb REAL,
                thumb_url TEXT,
                duration INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()

def _get_current_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def check_and_consume_quota(user_id: int, username: str, daily_limit: int) -> tuple[bool, int, int]:
    """
    Checks if a user is within their daily limit.
    If so, increments their request count and returns (True, remaining, daily_limit).
    If exceeded, returns (False, 0, daily_limit).
    """
    today = _get_current_date()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT requests_today, last_request_date FROM user_quota WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()

        if row is None:
            # First time user
            cursor.execute(
                "INSERT INTO user_quota (user_id, username, requests_today, last_request_date) VALUES (?, ?, 1, ?)",
                (user_id, username, today)
            )
            conn.commit()
            return True, daily_limit - 1, daily_limit

        requests_today, last_date = row
        if last_date != today:
            # New day reset
            requests_today = 1
            cursor.execute(
                "UPDATE user_quota SET requests_today = 1, last_request_date = ?, username = ? WHERE user_id = ?",
                (today, username, user_id)
            )
            conn.commit()
            return True, daily_limit - 1, daily_limit

        if requests_today >= daily_limit:
            return False, 0, daily_limit

        # Consume 1 task
        new_count = requests_today + 1
        cursor.execute(
            "UPDATE user_quota SET requests_today = ?, username = ? WHERE user_id = ?",
            (new_count, username, user_id)
        )
        conn.commit()
        return True, daily_limit - new_count, daily_limit

def get_user_stats(user_id: int, daily_limit: int) -> dict:
    """Returns today's usage statistics for a user."""
    today = _get_current_date()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT requests_today, last_request_date FROM user_quota WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()

        if row is None or row[1] != today:
            return {"used_today": 0, "remaining": daily_limit, "limit": daily_limit}

        used = row[0]
        remaining = max(0, daily_limit - used)
        return {"used_today": used, "remaining": remaining, "limit": daily_limit}

def get_user_session(user_id: int) -> str | None:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT session_id FROM user_sessions WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return row[0] if row else None

def set_user_session(user_id: int, session_id: str) -> None:
    today = _get_current_date()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO user_sessions (user_id, session_id, last_active)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET session_id = excluded.session_id, last_active = excluded.last_active
        """, (user_id, session_id, today))
        conn.commit()

def clear_user_session(user_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM user_sessions WHERE user_id = ?", (user_id,))
        conn.commit()

def get_cached_media(url: str) -> dict | None:
    """Retrieves instantly cached Telegram file_id (like LLM KV cache for zero-latency playback)."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT file_id, media_type, title, size_mb, thumb_url, duration FROM media_cache WHERE url = ?",
            (url.strip(),)
        )
        row = cursor.fetchone()
        if row:
            return {
                "file_id": row[0],
                "media_type": row[1],
                "title": row[2],
                "size_mb": row[3],
                "thumb_url": row[4],
                "duration": row[5]
            }
        return None

def set_cached_media(url: str, file_id: str, media_type: str, title: str, size_mb: float, thumb_url: str = None, duration: int = 0):
    """Caches Telegram file_id for sub-millisecond instant re-delivery."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO media_cache (url, file_id, media_type, title, size_mb, thumb_url, duration)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (url.strip(), file_id, media_type, title, size_mb, thumb_url, duration))
        conn.commit()

init_db()
