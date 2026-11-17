from typing import Optional, Any, Tuple, List, Dict
import sqlite3
import json
import uuid
import logging
import asyncio
import time
from datetime import datetime, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import config

logger = logging.getLogger(__name__)


def _json_serial(obj):
    if isinstance(obj, datetime):
        return {"__type__": "datetime", "val": obj.isoformat()}
    raise TypeError(f"Type {type(obj)} not serializable")


def _json_deserial(d):
    if isinstance(d, dict) and d.get("__type__") == "datetime":
        return datetime.fromisoformat(d["val"])
    return d


def _dumps(obj):
    return json.dumps(obj, default=_json_serial)


def _loads(s):
    if s is None:
        return None
    return json.loads(s, object_hook=_json_deserial)


class Database:
    """
    Non-blocking, cancellation-safe database interface with dedicated thread executors.
    - Serialized writes via a dedicated single-threaded writer executor.
    - Concurrent reads via a multi-threaded reader executor.
    - Cancellation-shielded write execution preventing abandoned dirty transactions.
    - Strict PRAGMA foreign_keys = ON, journal_mode = WAL, and busy_timeout = 5000.
    - Atomic BEGIN IMMEDIATE transactions on all read-modify-write attribute updates.
    - Explicit connection closure on every path.
    - Full dual-compatibility: Awaitable inside running event loops, direct sync outside.
    """

    def __init__(self, db_path: Optional[str] = None):
        self.use_sqlite = True
        try:
            import pymongo
            self.mongo_client = pymongo.MongoClient(config.mongodb_uri, serverSelectionTimeoutMS=1000)
            self.mongo_client.server_info()  # quick ping
            self.db = self.mongo_client["chatgpt_telegram_bot"]
            self.user_collection = self.db["user"]
            self.dialog_collection = self.db["dialog"]
            self.use_sqlite = False
            logger.info("Connected to MongoDB successfully.")
        except Exception as e:
            self.use_sqlite = True
            logger.info(f"MongoDB not available ({e}), using embedded SQLite database.")
            if db_path:
                self.db_path = db_path
            else:
                db_dir = Path(__file__).parent.parent.resolve() / "data"
                db_dir.mkdir(parents=True, exist_ok=True)
                self.db_path = str(db_dir / "database.db")
            self._init_sqlite()

        # Dedicated Thread Pools
        self._writer_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="db_writer")
        self._reader_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="db_reader")
        self._write_lock = asyncio.Lock()

    def _init_sqlite(self):
        with self._get_connection() as conn:
            conn.execute("PRAGMA foreign_keys = ON;")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY,
                    data TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS dialogs (
                    id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    data TEXT NOT NULL
                )
            """)
            conn.commit()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    # -------------------------------------------------------------------------
    # Executor Bridges (Cancellation-Shielded)
    # -------------------------------------------------------------------------

    def _run_read_sync(self, fn: Any) -> Any:
        conn = self._get_connection()
        try:
            return fn(conn)
        finally:
            conn.close()

    def _run_write_sync(self, fn: Any) -> Any:
        for attempt in range(3):
            conn = self._get_connection()
            try:
                result = fn(conn)
                return result
            except sqlite3.OperationalError as e:
                if "locked" in str(e).lower() or "busy" in str(e).lower():
                    if attempt < 2:
                        time.sleep(0.05 * (2 ** attempt))
                        continue
                raise
            finally:
                conn.close()

    async def _execute_read_async(self, fn: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._reader_executor, self._run_read_sync, fn)

    async def _execute_write_async(self, fn: Any) -> Any:
        async with self._write_lock:
            loop = asyncio.get_running_loop()
            write_fut = loop.run_in_executor(self._writer_executor, self._run_write_sync, fn)
            # Shield execution: caller cancellation will not abort an in-flight DB commit
            return await asyncio.shield(write_fut)

    def _is_in_loop(self) -> bool:
        try:
            loop = asyncio.get_running_loop()
            return loop is not None and loop.is_running()
        except RuntimeError:
            return False

    # -------------------------------------------------------------------------
    # 1. check_if_user_exists
    # -------------------------------------------------------------------------

    def _check_if_user_exists_impl(self, conn: sqlite3.Connection, user_id: int, raise_exception: bool = False) -> bool:
        row = conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is not None:
            return True
        if raise_exception:
            raise ValueError(f"User {user_id} does not exist")
        return False

    async def check_if_user_exists_async(self, user_id: int, raise_exception: bool = False) -> bool:
        if not self.use_sqlite:
            res = self.user_collection.count_documents({"_id": user_id}) > 0
            if not res and raise_exception:
                raise ValueError(f"User {user_id} does not exist")
            return res
        return await self._execute_read_async(lambda conn: self._check_if_user_exists_impl(conn, user_id, raise_exception))

    def check_if_user_exists(self, user_id: int, raise_exception: bool = False) -> Any:
        if self._is_in_loop():
            return self.check_if_user_exists_async(user_id, raise_exception)
        if not self.use_sqlite:
            res = self.user_collection.count_documents({"_id": user_id}) > 0
            if not res and raise_exception:
                raise ValueError(f"User {user_id} does not exist")
            return res
        return self._run_read_sync(lambda conn: self._check_if_user_exists_impl(conn, user_id, raise_exception))

    # -------------------------------------------------------------------------
    # 2. add_new_user
    # -------------------------------------------------------------------------

    def _build_user_dict(self, user_id: int, chat_id: int, username: str, first_name: str, last_name: str) -> dict:
        return {
            "_id": user_id,
            "chat_id": chat_id,
            "username": username,
            "first_name": first_name,
            "last_name": last_name,
            "last_interaction": datetime.now(),
            "first_seen": datetime.now(),
            "current_dialog_id": None,
            "current_chat_mode": "assistant",
            "current_model": config.models["available_text_models"][0],
            "n_used_tokens": {},
            "n_generated_images": 0,
            "n_transcribed_seconds": 0.0
        }

    def _add_new_user_impl(self, conn: sqlite3.Connection, user_dict: dict):
        user_id = user_dict["_id"]
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE;")
        try:
            row = cursor.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone()
            if row is None:
                cursor.execute("INSERT INTO users (id, data) VALUES (?, ?)", (user_id, _dumps(user_dict)))
            cursor.execute("COMMIT;")
        except Exception:
            cursor.execute("ROLLBACK;")
            raise

    async def add_new_user_async(self, user_id: int, chat_id: int, username: str = "", first_name: str = "", last_name: str = ""):
        user_dict = self._build_user_dict(user_id, chat_id, username, first_name, last_name)
        if not self.use_sqlite:
            if not self.user_collection.count_documents({"_id": user_id}) > 0:
                self.user_collection.insert_one(user_dict)
            return
        await self._execute_write_async(lambda conn: self._add_new_user_impl(conn, user_dict))

    def add_new_user(self, user_id: int, chat_id: int, username: str = "", first_name: str = "", last_name: str = "") -> Any:
        if self._is_in_loop():
            return self.add_new_user_async(user_id, chat_id, username, first_name, last_name)
        user_dict = self._build_user_dict(user_id, chat_id, username, first_name, last_name)
        if not self.use_sqlite:
            if not self.user_collection.count_documents({"_id": user_id}) > 0:
                self.user_collection.insert_one(user_dict)
            return
        return self._run_write_sync(lambda conn: self._add_new_user_impl(conn, user_dict))

    # -------------------------------------------------------------------------
    # 3. start_new_dialog
    # -------------------------------------------------------------------------

    def _start_new_dialog_impl(self, conn: sqlite3.Connection, user_id: int) -> str:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE;")
        try:
            row = cursor.execute("SELECT data FROM users WHERE id = ?", (user_id,)).fetchone()
            if not row:
                cursor.execute("ROLLBACK;")
                raise ValueError(f"User {user_id} does not exist")

            user_dict = _loads(row["data"])
            dialog_id = str(uuid.uuid4())
            dialog_dict = {
                "_id": dialog_id,
                "user_id": user_id,
                "chat_mode": user_dict.get("current_chat_mode", "assistant"),
                "start_time": datetime.now(),
                "model": user_dict.get("current_model", config.models["available_text_models"][0]),
                "messages": []
            }
            cursor.execute("INSERT INTO dialogs (id, user_id, data) VALUES (?, ?, ?)", (dialog_id, user_id, _dumps(dialog_dict)))
            user_dict["current_dialog_id"] = dialog_id
            cursor.execute("UPDATE users SET data = ? WHERE id = ?", (_dumps(user_dict), user_id))
            cursor.execute("COMMIT;")
            return dialog_id
        except Exception:
            cursor.execute("ROLLBACK;")
            raise

    async def start_new_dialog_async(self, user_id: int) -> str:
        if not self.use_sqlite:
            self.check_if_user_exists(user_id, raise_exception=True)
            dialog_id = str(uuid.uuid4())
            dialog_dict = {
                "_id": dialog_id,
                "user_id": user_id,
                "chat_mode": self.get_user_attribute(user_id, "current_chat_mode"),
                "start_time": datetime.now(),
                "model": self.get_user_attribute(user_id, "current_model"),
                "messages": []
            }
            self.dialog_collection.insert_one(dialog_dict)
            self.user_collection.update_one({"_id": user_id}, {"$set": {"current_dialog_id": dialog_id}})
            return dialog_id
        return await self._execute_write_async(lambda conn: self._start_new_dialog_impl(conn, user_id))

    def start_new_dialog(self, user_id: int) -> Any:
        if self._is_in_loop():
            return self.start_new_dialog_async(user_id)
        if not self.use_sqlite:
            self.check_if_user_exists(user_id, raise_exception=True)
            dialog_id = str(uuid.uuid4())
            dialog_dict = {
                "_id": dialog_id,
                "user_id": user_id,
                "chat_mode": self.get_user_attribute(user_id, "current_chat_mode"),
                "start_time": datetime.now(),
                "model": self.get_user_attribute(user_id, "current_model"),
                "messages": []
            }
            self.dialog_collection.insert_one(dialog_dict)
            self.user_collection.update_one({"_id": user_id}, {"$set": {"current_dialog_id": dialog_id}})
            return dialog_id
        return self._run_write_sync(lambda conn: self._start_new_dialog_impl(conn, user_id))

    # -------------------------------------------------------------------------
    # 4. get_user_attribute
    # -------------------------------------------------------------------------

    def _get_user_attribute_impl(self, conn: sqlite3.Connection, user_id: int, key: str) -> Any:
        row = conn.execute("SELECT data FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            return None
        user_dict = _loads(row["data"])
        return user_dict.get(key)

    async def get_user_attribute_async(self, user_id: int, key: str) -> Any:
        if not self.use_sqlite:
            user_dict = self.user_collection.find_one({"_id": user_id})
            return user_dict.get(key) if user_dict else None
        return await self._execute_read_async(lambda conn: self._get_user_attribute_impl(conn, user_id, key))

    def get_user_attribute(self, user_id: int, key: str) -> Any:
        if self._is_in_loop():
            return self.get_user_attribute_async(user_id, key)
        if not self.use_sqlite:
            user_dict = self.user_collection.find_one({"_id": user_id})
            return user_dict.get(key) if user_dict else None
        return self._run_read_sync(lambda conn: self._get_user_attribute_impl(conn, user_id, key))

    # -------------------------------------------------------------------------
    # 5. set_user_attribute (Atomic RMW)
    # -------------------------------------------------------------------------

    def _set_user_attribute_impl(self, conn: sqlite3.Connection, user_id: int, key: str, value: Any):
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE;")
        try:
            row = cursor.execute("SELECT data FROM users WHERE id = ?", (user_id,)).fetchone()
            if not row:
                cursor.execute("ROLLBACK;")
                raise ValueError(f"User {user_id} does not exist")
            user_dict = _loads(row["data"])
            user_dict[key] = value
            cursor.execute("UPDATE users SET data = ? WHERE id = ?", (_dumps(user_dict), user_id))
            cursor.execute("COMMIT;")
        except Exception:
            cursor.execute("ROLLBACK;")
            raise

    async def set_user_attribute_async(self, user_id: int, key: str, value: Any):
        if not self.use_sqlite:
            self.user_collection.update_one({"_id": user_id}, {"$set": {key: value}})
            return
        await self._execute_write_async(lambda conn: self._set_user_attribute_impl(conn, user_id, key, value))

    def set_user_attribute(self, user_id: int, key: str, value: Any) -> Any:
        if self._is_in_loop():
            return self.set_user_attribute_async(user_id, key, value)
        if not self.use_sqlite:
            self.user_collection.update_one({"_id": user_id}, {"$set": {key: value}})
            return
        return self._run_write_sync(lambda conn: self._set_user_attribute_impl(conn, user_id, key, value))

    # -------------------------------------------------------------------------
    # 6. is_user_premium
    # -------------------------------------------------------------------------

    def _is_user_premium_impl(self, conn: sqlite3.Connection, user_id: int) -> bool:
        row = conn.execute("SELECT data FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            return False
        user_dict = _loads(row["data"])
        is_prem = bool(user_dict.get("is_premium"))
        if not is_prem:
            return False

        expiry_str = user_dict.get("premium_expiry")
        if not expiry_str:
            return True

        try:
            expiry_dt = datetime.strptime(str(expiry_str).strip(), "%Y-%m-%d").date()
            if datetime.now().date() > expiry_dt:
                # Expired: Auto-downgrade to Free inside immediate transaction
                cursor = conn.cursor()
                cursor.execute("BEGIN IMMEDIATE;")
                try:
                    r2 = cursor.execute("SELECT data FROM users WHERE id = ?", (user_id,)).fetchone()
                    if r2:
                        u2 = _loads(r2["data"])
                        u2["is_premium"] = False
                        cursor.execute("UPDATE users SET data = ? WHERE id = ?", (_dumps(u2), user_id))
                    cursor.execute("COMMIT;")
                except Exception:
                    cursor.execute("ROLLBACK;")
                logger.info(f"User {user_id} premium subscription expired on {expiry_str}. Downgraded to free.")
                return False
            return True
        except Exception:
            return True

    async def is_user_premium_async(self, user_id: int) -> bool:
        if not self.use_sqlite:
            return self._is_user_premium_mongo(user_id)
        return await self._execute_write_async(lambda conn: self._is_user_premium_impl(conn, user_id))

    def _is_user_premium_mongo(self, user_id: int) -> bool:
        try:
            user_dict = self.user_collection.find_one({"_id": user_id})
            if not user_dict or not user_dict.get("is_premium"):
                return False
            expiry_str = user_dict.get("premium_expiry")
            if not expiry_str:
                return True
            expiry_dt = datetime.strptime(str(expiry_str).strip(), "%Y-%m-%d").date()
            if datetime.now().date() > expiry_dt:
                self.user_collection.update_one({"_id": user_id}, {"$set": {"is_premium": False}})
                return False
            return True
        except Exception:
            return False

    def is_user_premium(self, user_id: int) -> Any:
        if self._is_in_loop():
            return self.is_user_premium_async(user_id)
        if not self.use_sqlite:
            return self._is_user_premium_mongo(user_id)
        return self._run_write_sync(lambda conn: self._is_user_premium_impl(conn, user_id))

    # -------------------------------------------------------------------------
    # 7. get_premium_info
    # -------------------------------------------------------------------------

    def _get_premium_info_impl(self, conn: sqlite3.Connection, user_id: int) -> dict:
        is_prem = self._is_user_premium_impl(conn, user_id)
        row = conn.execute("SELECT data FROM users WHERE id = ?", (user_id,)).fetchone()
        user_dict = _loads(row["data"]) if row else {}
        expiry_str = user_dict.get("premium_expiry")
        stars_paid = user_dict.get("premium_stars_paid") or 0
        tier_name = user_dict.get("premium_tier") or ("VIP Lifetime" if stars_paid >= 350 else "Pro")

        days_remaining = None
        if expiry_str:
            try:
                expiry_dt = datetime.strptime(str(expiry_str).strip(), "%Y-%m-%d").date()
                delta = (expiry_dt - datetime.now().date()).days
                days_remaining = max(0, delta)
            except Exception:
                pass

        return {
            "is_premium": is_prem,
            "expiry_date": expiry_str,
            "days_remaining": days_remaining,
            "stars_paid": stars_paid,
            "tier_name": tier_name if is_prem else "Free"
        }

    async def get_premium_info_async(self, user_id: int) -> dict:
        if not self.use_sqlite:
            return self.get_premium_info_sync(user_id)
        return await self._execute_read_async(lambda conn: self._get_premium_info_impl(conn, user_id))

    def get_premium_info(self, user_id: int) -> Any:
        if self._is_in_loop():
            return self.get_premium_info_async(user_id)
        return self.get_premium_info_sync(user_id)

    def get_premium_info_sync(self, user_id: int) -> dict:
        if not self.use_sqlite:
            is_prem = self._is_user_premium_mongo(user_id)
            user_dict = self.user_collection.find_one({"_id": user_id}) or {}
            expiry_str = user_dict.get("premium_expiry")
            stars_paid = user_dict.get("premium_stars_paid") or 0
            tier_name = user_dict.get("premium_tier") or ("VIP Lifetime" if stars_paid >= 350 else "Pro")
            days_remaining = None
            if expiry_str:
                try:
                    expiry_dt = datetime.strptime(str(expiry_str).strip(), "%Y-%m-%d").date()
                    days_remaining = max(0, (expiry_dt - datetime.now().date()).days)
                except Exception:
                    pass
            return {"is_premium": is_prem, "expiry_date": expiry_str, "days_remaining": days_remaining, "stars_paid": stars_paid, "tier_name": tier_name if is_prem else "Free"}
        return self._run_read_sync(lambda conn: self._get_premium_info_impl(conn, user_id))

    # -------------------------------------------------------------------------
    # 8. set_user_premium (Atomic RMW)
    # -------------------------------------------------------------------------

    def _set_user_premium_impl(self, conn: sqlite3.Connection, user_id: int, days: int, stars_paid: int, tier_name: str):
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE;")
        try:
            row = cursor.execute("SELECT data FROM users WHERE id = ?", (user_id,)).fetchone()
            if not row:
                cursor.execute("ROLLBACK;")
                raise ValueError(f"User {user_id} does not exist")

            user_dict = _loads(row["data"])
            now = datetime.now()
            existing_expiry = user_dict.get("premium_expiry")
            base_date = now
            if existing_expiry:
                try:
                    e_dt = datetime.strptime(str(existing_expiry).strip(), "%Y-%m-%d")
                    if e_dt > now:
                        base_date = e_dt
                except Exception:
                    pass

            new_expiry_str = (base_date + timedelta(days=days)).strftime("%Y-%m-%d")
            user_dict["is_premium"] = True
            user_dict["premium_expiry"] = new_expiry_str
            user_dict["premium_stars_paid"] = (user_dict.get("premium_stars_paid") or 0) + stars_paid
            if tier_name:
                user_dict["premium_tier"] = tier_name

            cursor.execute("UPDATE users SET data = ? WHERE id = ?", (_dumps(user_dict), user_id))
            cursor.execute("COMMIT;")
            logger.info(f"User {user_id} granted premium until {new_expiry_str} ({days} days, {stars_paid} stars).")
        except Exception:
            cursor.execute("ROLLBACK;")
            raise

    async def set_user_premium_async(self, user_id: int, days: int, stars_paid: int, tier_name: str = ""):
        if not self.use_sqlite:
            self.set_user_premium_sync(user_id, days, stars_paid, tier_name)
            return
        await self._execute_write_async(lambda conn: self._set_user_premium_impl(conn, user_id, days, stars_paid, tier_name))

    def set_user_premium(self, user_id: int, days: int, stars_paid: int, tier_name: str = "") -> Any:
        if self._is_in_loop():
            return self.set_user_premium_async(user_id, days, stars_paid, tier_name)
        return self.set_user_premium_sync(user_id, days, stars_paid, tier_name)

    def set_user_premium_sync(self, user_id: int, days: int, stars_paid: int, tier_name: str = ""):
        if not self.use_sqlite:
            now = datetime.now()
            existing_expiry = self.get_user_attribute(user_id, "premium_expiry")
            base_date = now
            if existing_expiry:
                try:
                    e_dt = datetime.strptime(str(existing_expiry).strip(), "%Y-%m-%d")
                    if e_dt > now:
                        base_date = e_dt
                except Exception:
                    pass
            new_expiry_str = (base_date + timedelta(days=days)).strftime("%Y-%m-%d")
            self.set_user_attribute(user_id, "is_premium", True)
            self.set_user_attribute(user_id, "premium_expiry", new_expiry_str)
            self.set_user_attribute(user_id, "premium_stars_paid", (self.get_user_attribute(user_id, "premium_stars_paid") or 0) + stars_paid)
            if tier_name:
                self.set_user_attribute(user_id, "premium_tier", tier_name)
            return
        return self._run_write_sync(lambda conn: self._set_user_premium_impl(conn, user_id, days, stars_paid, tier_name))

    # -------------------------------------------------------------------------
    # 9. record_referral (Atomic RMW)
    # -------------------------------------------------------------------------

    def _record_referral_impl(self, conn: sqlite3.Connection, new_user_id: int, referrer_id: int) -> bool:
        if new_user_id == referrer_id:
            return False

        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE;")
        try:
            # Check referrer exists
            r_ref = cursor.execute("SELECT data FROM users WHERE id = ?", (referrer_id,)).fetchone()
            if not r_ref:
                cursor.execute("ROLLBACK;")
                return False

            # Check new user
            r_new = cursor.execute("SELECT data FROM users WHERE id = ?", (new_user_id,)).fetchone()
            if not r_new:
                cursor.execute("ROLLBACK;")
                return False

            new_dict = _loads(r_new["data"])
            if new_dict.get("referrer_id"):
                cursor.execute("ROLLBACK;")
                return False  # Already referred

            # Set referrer_id on new user
            new_dict["referrer_id"] = referrer_id
            cursor.execute("UPDATE users SET data = ? WHERE id = ?", (_dumps(new_dict), new_user_id))

            # Update referrer's list
            ref_dict = _loads(r_ref["data"])
            referrals = ref_dict.get("referrals") or []
            if new_user_id not in referrals:
                referrals.append(new_user_id)
                ref_dict["referrals"] = referrals
                ref_dict["referral_count"] = len(referrals)
                cursor.execute("UPDATE users SET data = ? WHERE id = ?", (_dumps(ref_dict), referrer_id))

            cursor.execute("COMMIT;")
            logger.info(f"Recorded referral: user {new_user_id} referred by {referrer_id}.")
            return True
        except Exception:
            cursor.execute("ROLLBACK;")
            raise

    async def record_referral_async(self, new_user_id: int, referrer_id: int) -> bool:
        if not self.use_sqlite:
            return self.record_referral_sync(new_user_id, referrer_id)
        return await self._execute_write_async(lambda conn: self._record_referral_impl(conn, new_user_id, referrer_id))

    def record_referral(self, new_user_id: int, referrer_id: int) -> Any:
        if self._is_in_loop():
            return self.record_referral_async(new_user_id, referrer_id)
        return self.record_referral_sync(new_user_id, referrer_id)

    def record_referral_sync(self, new_user_id: int, referrer_id: int) -> bool:
        if not self.use_sqlite:
            if new_user_id == referrer_id or self.get_user_attribute(new_user_id, "referrer_id"):
                return False
            if not self.check_if_user_exists(referrer_id):
                return False
            self.set_user_attribute(new_user_id, "referrer_id", referrer_id)
            referrals = self.get_user_attribute(referrer_id, "referrals") or []
            if new_user_id not in referrals:
                referrals.append(new_user_id)
                self.set_user_attribute(referrer_id, "referrals", referrals)
                self.set_user_attribute(referrer_id, "referral_count", len(referrals))
                return True
            return False
        return self._run_write_sync(lambda conn: self._record_referral_impl(conn, new_user_id, referrer_id))

    # -------------------------------------------------------------------------
    # 10. get_affiliate_info
    # -------------------------------------------------------------------------

    def _get_affiliate_info_impl(self, conn: sqlite3.Connection, user_id: int) -> dict:
        row = conn.execute("SELECT data FROM users WHERE id = ?", (user_id,)).fetchone()
        user_dict = _loads(row["data"]) if row else {}
        referrals = user_dict.get("referrals") or []
        stars_earned = user_dict.get("affiliate_stars_earned") or 0

        return {
            "referral_count": len(referrals),
            "is_partner": True,  # Open to all users
            "stars_earned": stars_earned,
            "commission_rate": 50,  # 50% target commission
            "commission_permille": 500,  # Telegram official permille parameter
            "referral_link": f"https://t.me/sauxsibot?start=aff_{user_id}",
            "mini_app_referral_link": f"https://t.me/sauxsibot/crate?startapp=aff_{user_id}"
        }

    async def get_affiliate_info_async(self, user_id: int) -> dict:
        if not self.use_sqlite:
            return self.get_affiliate_info_sync(user_id)
        return await self._execute_read_async(lambda conn: self._get_affiliate_info_impl(conn, user_id))

    def get_affiliate_info(self, user_id: int) -> Any:
        if self._is_in_loop():
            return self.get_affiliate_info_async(user_id)
        return self.get_affiliate_info_sync(user_id)

    def get_affiliate_info_sync(self, user_id: int) -> dict:
        if not self.use_sqlite:
            referrals = self.get_user_attribute(user_id, "referrals") or []
            stars_earned = self.get_user_attribute(user_id, "affiliate_stars_earned") or 0
            return {
                "referral_count": len(referrals),
                "is_partner": True,
                "stars_earned": stars_earned,
                "commission_rate": 50,
                "commission_permille": 500,
                "referral_link": f"https://t.me/sauxsibot?start=aff_{user_id}",
                "mini_app_referral_link": f"https://t.me/sauxsibot/crate?startapp=aff_{user_id}"
            }
        return self._run_read_sync(lambda conn: self._get_affiliate_info_impl(conn, user_id))

    # -------------------------------------------------------------------------
    # 11. credit_referral_commission (Atomic RMW)
    # -------------------------------------------------------------------------

    def _credit_referral_commission_impl(self, conn: sqlite3.Connection, paying_user_id: int, stars_paid: int) -> Tuple[Optional[int], int]:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE;")
        try:
            r_pay = cursor.execute("SELECT data FROM users WHERE id = ?", (paying_user_id,)).fetchone()
            if not r_pay:
                cursor.execute("ROLLBACK;")
                return None, 0

            payer_dict = _loads(r_pay["data"])
            referrer_id = payer_dict.get("referrer_id")
            if not referrer_id:
                cursor.execute("ROLLBACK;")
                return None, 0

            r_ref = cursor.execute("SELECT data FROM users WHERE id = ?", (referrer_id,)).fetchone()
            if not r_ref:
                cursor.execute("ROLLBACK;")
                return None, 0

            commission = int(stars_paid * 0.5)
            if commission <= 0:
                cursor.execute("ROLLBACK;")
                return None, 0

            ref_dict = _loads(r_ref["data"])
            cur_earned = ref_dict.get("affiliate_stars_earned") or 0
            ref_dict["affiliate_stars_earned"] = cur_earned + commission
            history = ref_dict.get("affiliate_history") or []
            history.append({
                "from_user": paying_user_id,
                "stars_paid": stars_paid,
                "commission": commission,
                "date": datetime.now().isoformat()
            })
            ref_dict["affiliate_history"] = history
            cursor.execute("UPDATE users SET data = ? WHERE id = ?", (_dumps(ref_dict), referrer_id))
            cursor.execute("COMMIT;")
            logger.info(f"Credited {commission} stars to referrer {referrer_id} for user {paying_user_id} payment of {stars_paid} stars.")
            return referrer_id, commission
        except Exception:
            cursor.execute("ROLLBACK;")
            raise

    async def credit_referral_commission_async(self, paying_user_id: int, stars_paid: int) -> Tuple[Optional[int], int]:
        if not self.use_sqlite:
            return self.credit_referral_commission_sync(paying_user_id, stars_paid)
        return await self._execute_write_async(lambda conn: self._credit_referral_commission_impl(conn, paying_user_id, stars_paid))

    def credit_referral_commission(self, paying_user_id: int, stars_paid: int) -> Any:
        if self._is_in_loop():
            return self.credit_referral_commission_async(paying_user_id, stars_paid)
        return self.credit_referral_commission_sync(paying_user_id, stars_paid)

    def credit_referral_commission_sync(self, paying_user_id: int, stars_paid: int) -> Tuple[Optional[int], int]:
        if not self.use_sqlite:
            referrer_id = self.get_user_attribute(paying_user_id, "referrer_id")
            if not referrer_id or not self.check_if_user_exists(referrer_id):
                return None, 0
            commission = int(stars_paid * 0.5)
            if commission <= 0:
                return None, 0
            current_earned = self.get_user_attribute(referrer_id, "affiliate_stars_earned") or 0
            self.set_user_attribute(referrer_id, "affiliate_stars_earned", current_earned + commission)
            history = self.get_user_attribute(referrer_id, "affiliate_history") or []
            history.append({"from_user": paying_user_id, "stars_paid": stars_paid, "commission": commission, "date": datetime.now().isoformat()})
            self.set_user_attribute(referrer_id, "affiliate_history", history)
            return referrer_id, commission
        return self._run_write_sync(lambda conn: self._credit_referral_commission_impl(conn, paying_user_id, stars_paid))

    # -------------------------------------------------------------------------
    # 12. get_dialog_messages
    # -------------------------------------------------------------------------

    def _get_dialog_messages_impl(self, conn: sqlite3.Connection, user_id: int, dialog_id: Optional[str]) -> list:
        if dialog_id is None:
            r_user = conn.execute("SELECT data FROM users WHERE id = ?", (user_id,)).fetchone()
            if not r_user:
                return []
            dialog_id = _loads(r_user["data"]).get("current_dialog_id")

        if not dialog_id:
            return []

        row = conn.execute("SELECT data FROM dialogs WHERE id = ? AND user_id = ?", (dialog_id, user_id)).fetchone()
        if not row:
            return []
        return _loads(row["data"]).get("messages", [])

    async def get_dialog_messages_async(self, user_id: int, dialog_id: Optional[str] = None) -> list:
        if not self.use_sqlite:
            return self.get_dialog_messages_sync(user_id, dialog_id)
        return await self._execute_read_async(lambda conn: self._get_dialog_messages_impl(conn, user_id, dialog_id))

    def get_dialog_messages(self, user_id: int, dialog_id: Optional[str] = None) -> Any:
        if self._is_in_loop():
            return self.get_dialog_messages_async(user_id, dialog_id)
        return self.get_dialog_messages_sync(user_id, dialog_id)

    def get_dialog_messages_sync(self, user_id: int, dialog_id: Optional[str] = None) -> list:
        if not self.use_sqlite:
            self.check_if_user_exists(user_id, raise_exception=True)
            if dialog_id is None:
                dialog_id = self.get_user_attribute(user_id, "current_dialog_id")
            dialog_dict = self.dialog_collection.find_one({"_id": dialog_id, "user_id": user_id})
            return dialog_dict["messages"] if dialog_dict else []
        return self._run_read_sync(lambda conn: self._get_dialog_messages_impl(conn, user_id, dialog_id))

    # -------------------------------------------------------------------------
    # 13. set_dialog_messages (Atomic RMW)
    # -------------------------------------------------------------------------

    def _set_dialog_messages_impl(self, conn: sqlite3.Connection, user_id: int, dialog_messages: list, dialog_id: Optional[str]):
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE;")
        try:
            if dialog_id is None:
                r_user = cursor.execute("SELECT data FROM users WHERE id = ?", (user_id,)).fetchone()
                if not r_user:
                    cursor.execute("ROLLBACK;")
                    raise ValueError(f"User {user_id} does not exist")
                dialog_id = _loads(r_user["data"]).get("current_dialog_id")

            row = cursor.execute("SELECT data FROM dialogs WHERE id = ? AND user_id = ?", (dialog_id, user_id)).fetchone()
            if row:
                d_dict = _loads(row["data"])
                d_dict["messages"] = dialog_messages
                cursor.execute("UPDATE dialogs SET data = ? WHERE id = ?", (_dumps(d_dict), dialog_id))
            cursor.execute("COMMIT;")
        except Exception:
            cursor.execute("ROLLBACK;")
            raise

    async def set_dialog_messages_async(self, user_id: int, dialog_messages: list, dialog_id: Optional[str] = None):
        if not self.use_sqlite:
            self.set_dialog_messages_sync(user_id, dialog_messages, dialog_id)
            return
        await self._execute_write_async(lambda conn: self._set_dialog_messages_impl(conn, user_id, dialog_messages, dialog_id))

    def set_dialog_messages(self, user_id: int, dialog_messages: list, dialog_id: Optional[str] = None) -> Any:
        if self._is_in_loop():
            return self.set_dialog_messages_async(user_id, dialog_messages, dialog_id)
        return self.set_dialog_messages_sync(user_id, dialog_messages, dialog_id)

    def set_dialog_messages_sync(self, user_id: int, dialog_messages: list, dialog_id: Optional[str] = None):
        if not self.use_sqlite:
            self.check_if_user_exists(user_id, raise_exception=True)
            if dialog_id is None:
                dialog_id = self.get_user_attribute(user_id, "current_dialog_id")
            self.dialog_collection.update_one({"_id": dialog_id, "user_id": user_id}, {"$set": {"messages": dialog_messages}})
            return
        return self._run_write_sync(lambda conn: self._set_dialog_messages_impl(conn, user_id, dialog_messages, dialog_id))

    # -------------------------------------------------------------------------
    # 14. update_n_used_tokens (Atomic RMW)
    # -------------------------------------------------------------------------

    def _update_n_used_tokens_impl(self, conn: sqlite3.Connection, user_id: int, model: str, n_input_tokens: int, n_output_tokens: int):
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE;")
        try:
            row = cursor.execute("SELECT data FROM users WHERE id = ?", (user_id,)).fetchone()
            if not row:
                cursor.execute("ROLLBACK;")
                return

            user_dict = _loads(row["data"])
            tokens = user_dict.get("n_used_tokens") or {}
            if model in tokens:
                tokens[model]["n_input_tokens"] += n_input_tokens
                tokens[model]["n_output_tokens"] += n_output_tokens
            else:
                tokens[model] = {
                    "n_input_tokens": n_input_tokens,
                    "n_output_tokens": n_output_tokens
                }
            user_dict["n_used_tokens"] = tokens
            cursor.execute("UPDATE users SET data = ? WHERE id = ?", (_dumps(user_dict), user_id))
            cursor.execute("COMMIT;")
        except Exception:
            cursor.execute("ROLLBACK;")
            raise

    async def update_n_used_tokens_async(self, user_id: int, model: str, n_input_tokens: int, n_output_tokens: int):
        if not self.use_sqlite:
            self.update_n_used_tokens_sync(user_id, model, n_input_tokens, n_output_tokens)
            return
        await self._execute_write_async(lambda conn: self._update_n_used_tokens_impl(conn, user_id, model, n_input_tokens, n_output_tokens))

    def update_n_used_tokens(self, user_id: int, model: str, n_input_tokens: int, n_output_tokens: int) -> Any:
        if self._is_in_loop():
            return self.update_n_used_tokens_async(user_id, model, n_input_tokens, n_output_tokens)
        return self.update_n_used_tokens_sync(user_id, model, n_input_tokens, n_output_tokens)

    def update_n_used_tokens_sync(self, user_id: int, model: str, n_input_tokens: int, n_output_tokens: int):
        if not self.use_sqlite:
            n_used_tokens_dict = self.get_user_attribute(user_id, "n_used_tokens") or {}
            if model in n_used_tokens_dict:
                n_used_tokens_dict[model]["n_input_tokens"] += n_input_tokens
                n_used_tokens_dict[model]["n_output_tokens"] += n_output_tokens
            else:
                n_used_tokens_dict[model] = {"n_input_tokens": n_input_tokens, "n_output_tokens": n_output_tokens}
            self.set_user_attribute(user_id, "n_used_tokens", n_used_tokens_dict)
            return
        return self._run_write_sync(lambda conn: self._update_n_used_tokens_impl(conn, user_id, model, n_input_tokens, n_output_tokens))

    def _get_all_users_impl(self, conn: sqlite3.Connection) -> List[dict]:
        if self.use_sqlite:
            cursor = conn.cursor()
            cursor.execute("SELECT id, data FROM users ORDER BY rowid DESC")
            rows = cursor.fetchall()
            users = []
            for r in rows:
                try:
                    u = _loads(r[1])
                    if isinstance(u, dict):
                        u["_id"] = r[0]
                        users.append(u)
                except Exception:
                    pass
            return users
        else:
            return list(self.user_collection.find({}).sort("_id", -1))

    async def get_all_users_async(self) -> List[dict]:
        if self.use_sqlite:
            return await self._execute_read_async(self._get_all_users_impl)
        else:
            return await asyncio.to_thread(self._get_all_users_impl, None)

    def get_all_users(self) -> Any:
        if self._is_in_loop():
            return self.get_all_users_async()
        if self.use_sqlite:
            return self._run_read_sync(self._get_all_users_impl)
        else:
            return self._get_all_users_impl(None)

    def _get_user_dialog_count_impl(self, conn: sqlite3.Connection, user_id: int) -> int:
        if self.use_sqlite:
            cursor = conn.cursor()
            cursor.execute("SELECT count(*) FROM dialogs WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            return row[0] if row else 0
        else:
            return self.dialog_collection.count_documents({"user_id": user_id})

    async def get_user_dialog_count_async(self, user_id: int) -> int:
        if self.use_sqlite:
            return await self._execute_read_async(lambda conn: self._get_user_dialog_count_impl(conn, user_id))
        else:
            return await asyncio.to_thread(self._get_user_dialog_count_impl, None, user_id)

    def get_user_dialog_count(self, user_id: int) -> Any:
        if self._is_in_loop():
            return self.get_user_dialog_count_async(user_id)
        if self.use_sqlite:
            return self._run_read_sync(lambda conn: self._get_user_dialog_count_impl(conn, user_id))
        else:
            return self._get_user_dialog_count_impl(None, user_id)

    def close(self):
        """Cleanly drains and shuts down background database thread executors."""
        self._writer_executor.shutdown(wait=True)
        self._reader_executor.shutdown(wait=True)
