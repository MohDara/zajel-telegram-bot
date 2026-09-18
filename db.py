import os
import sqlite3
import shutil
import glob
import logging
from contextlib import contextmanager
from datetime import datetime
from typing import Optional, Tuple, List, Dict, Any
from urllib.parse import urlparse, unquote
from cryptography.fernet import Fernet
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("ZajelDB")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# 1. PERSISTENT ENCRYPTION KEY RESOLUTION (Zero Data Loss Protection)
# ---------------------------------------------------------------------------
KEY_FILE_PATH = os.getenv("APP_KEY_FILE", os.path.join(BASE_DIR, ".app_secret.key"))


def _write_key_file(key_text: str):
    with open(KEY_FILE_PATH, "w", encoding="utf-8") as kf:
        kf.write(key_text)
    try:
        if os.name == "posix":
            os.chmod(KEY_FILE_PATH, 0o600)
    except Exception:
        pass


def _resolve_fernet_key() -> Tuple[bytes, str]:
    """
    Guarantees a permanent encryption key across reboots.
    Priority 1: Environment variable APP_SECRET_KEY (sanitized of quotes/whitespace).
    Priority 2: Persistent keyfile on disk (.app_secret.key).
    Priority 3: Generate key and persist to disk (.app_secret.key) so next reboot keeps it.
    Returns (key_bytes, source) where source is 'environment', 'keyfile' or 'generated'.
    """
    env_key = os.getenv("APP_SECRET_KEY", "").strip().strip("'\"")
    if env_key:
        try:
            # Validate Fernet key format
            key_bytes = env_key.encode("utf-8")
            Fernet(key_bytes)
            # Sync key to disk file for redundancy if file does not exist
            if not os.path.exists(KEY_FILE_PATH):
                try:
                    _write_key_file(env_key)
                except Exception:
                    pass
            return key_bytes, "environment"
        except Exception as e:
            logger.error(f"Invalid APP_SECRET_KEY in environment: {e}")

    # Fallback to key file
    if os.path.exists(KEY_FILE_PATH):
        try:
            with open(KEY_FILE_PATH, "r", encoding="utf-8") as kf:
                file_key = kf.read().strip().strip("'\"")
            key_bytes = file_key.encode("utf-8")
            Fernet(key_bytes)
            logger.info("Loaded persistent Fernet key from keyfile.")
            return key_bytes, "keyfile"
        except Exception as e:
            logger.error(f"Failed reading keyfile {KEY_FILE_PATH}: {e}")

    # Generate permanent key and write to disk
    generated = Fernet.generate_key().decode("utf-8")
    try:
        _write_key_file(generated)
        logger.warning(
            f"APP_SECRET_KEY not set in environment. Generated persistent key and saved to {KEY_FILE_PATH}. "
            "Please copy this key to your cloud environment variables to ensure persistence across platform re-creations!"
        )
    except Exception as e:
        logger.error(f"Could not persist key to file {KEY_FILE_PATH}: {e}")

    return generated.encode("utf-8"), "generated"


_FERNET_KEY, _KEY_SOURCE = _resolve_fernet_key()
fernet = Fernet(_FERNET_KEY)

# ---------------------------------------------------------------------------
# 2. DATABASE CONFIGURATION & RESILIENT CONNECTION
# ---------------------------------------------------------------------------
DB_PATH = os.getenv("DB_PATH", os.path.join(BASE_DIR, "zajel_users.db"))
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()


def get_db_connection():
    if DATABASE_URL:
        import pg8000.dbapi
        import ssl
        from urllib.parse import parse_qs
        parsed = urlparse(DATABASE_URL)
        host = parsed.hostname or "localhost"
        user = unquote(parsed.username or "")
        password = unquote(parsed.password or "")
        # Strip query string that some providers append (e.g. ?sslmode=require)
        database = unquote((parsed.path or "").lstrip("/").split("?")[0])
        query = parse_qs(parsed.query)
        sslmode = (query.get("sslmode", [""])[0] or "").lower()
        no_verify = os.getenv("PGSSL_NO_VERIFY", "").strip().lower() in ("1", "true", "yes")

        if host in ["localhost", "127.0.0.1"] or sslmode == "disable":
            ssl_ctx = None
        else:
            ssl_ctx = ssl.create_default_context()
            # Only relax verification for explicit opt-in or dotless internal hostnames
            if no_verify or "." not in host or sslmode in ("no-verify",):
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE
                logger.warning(f"TLS certificate verification disabled for database host {host}")

        conn = pg8000.dbapi.connect(
            user=user,
            password=password,
            host=host,
            port=parsed.port or 5432,
            database=database,
            ssl_context=ssl_ctx
        )
        return "pg", conn

    # SQLite with WAL mode and busy timeout for high concurrency.
    # synchronous=FULL guarantees committed transactions survive power loss.
    conn = sqlite3.connect(DB_PATH, timeout=20.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=15000;")
    conn.execute("PRAGMA synchronous=FULL;")
    return "sqlite", conn


@contextmanager
def get_db_cursor():
    """
    Context manager for safe database interactions with automatic commit/rollback
    and guaranteed connection closure to prevent leaks.
    """
    db_type, conn = get_db_connection()
    cursor = conn.cursor()
    try:
        yield db_type, cursor
        conn.commit()
    except Exception:
        if hasattr(conn, "rollback"):
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 3. SCHEMA INITIALIZATION & AUTOMATIC BACKUP
# ---------------------------------------------------------------------------
BACKUP_DIR = os.getenv(
    "BACKUP_DIR",
    os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "backups")
)
BACKUP_KEEP = int(os.getenv("BACKUP_KEEP", "20") or "20")


def _prune_old_backups(keep: int = BACKUP_KEEP):
    try:
        files = sorted(glob.glob(os.path.join(BACKUP_DIR, "zajel_users_*.db")))
        for old in files[:-keep] if keep > 0 else []:
            try:
                os.remove(old)
            except Exception:
                pass
    except Exception:
        pass


def backup_sqlite_db() -> Optional[str]:
    """
    Creates an atomic, verified, timestamped online snapshot of the SQLite database.
    Never overwrites a known-good backup with a possibly corrupted file.
    Returns the path of the newest verified backup, or None on failure.
    """
    if DATABASE_URL:
        return None  # Managed by cloud postgres backups
    if not os.path.exists(DB_PATH):
        return None

    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
    except Exception as e:
        logger.warning(f"Could not create backup directory {BACKUP_DIR}: {e}")
        return None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_path = os.path.join(BACKUP_DIR, f"zajel_users_{timestamp}.db")

    src_conn = None
    dst_conn = None
    try:
        # Use SQLite online backup API to prevent copying locked/inconsistent states
        src_conn = sqlite3.connect(DB_PATH, timeout=20.0)
        dst_conn = sqlite3.connect(backup_path)
        with dst_conn:
            src_conn.backup(dst_conn, pages=100)
        integrity = dst_conn.execute("PRAGMA integrity_check;").fetchone()
        if not integrity or integrity[0] != "ok":
            raise RuntimeError(f"backup integrity check failed: {integrity}")
    except Exception as e:
        logger.warning(f"Online SQLite backup error: {e}")
        for conn in (src_conn, dst_conn):
            try:
                if conn:
                    conn.close()
            except Exception:
                pass
        try:
            if os.path.exists(backup_path):
                os.remove(backup_path)
        except Exception:
            pass
        return None

    for conn in (dst_conn, src_conn):
        try:
            conn.close()
        except Exception:
            pass

    # Refresh the conventional .bak copy only from a verified snapshot
    try:
        shutil.copy2(backup_path, f"{DB_PATH}.bak")
    except Exception as e:
        logger.warning(f"Could not refresh {DB_PATH}.bak: {e}")

    _prune_old_backups()
    logger.info(f"Verified database backup created: {backup_path}")
    return backup_path


def get_backup_summary() -> Tuple[int, str]:
    """Returns (number of retained verified backups, timestamp of the newest one)."""
    try:
        files = sorted(glob.glob(os.path.join(BACKUP_DIR, "zajel_users_*.db")))
        if not files:
            return 0, "none"
        latest = datetime.fromtimestamp(os.path.getmtime(files[-1])).strftime("%Y-%m-%d %H:%M:%S")
        return len(files), latest
    except Exception:
        return 0, "unknown"


def _verify_encryption_key() -> None:
    """
    Fail-fast guard against silent data loss: if the database already contains
    encrypted credentials but the active Fernet key cannot decrypt any of them,
    refuse to start with a fresh/wrong key unless ALLOW_KEY_RESET=1 is set.
    """
    try:
        with get_db_cursor() as (db_type, cursor):
            cursor.execute("SELECT COUNT(*) FROM users")
            row = cursor.fetchone()
            total = row[0] if row else 0
            if not total:
                return
            cursor.execute("SELECT encrypted_password FROM users LIMIT 20")
            samples = [r[0] for r in cursor.fetchall()]
    except Exception as e:
        logger.warning(f"Could not verify encryption key against database: {e}")
        return

    for enc in samples:
        try:
            fernet.decrypt(str(enc).encode("utf-8"))
            return  # key works
        except Exception:
            continue

    allow_reset = os.getenv("ALLOW_KEY_RESET", "").strip().lower() in ("1", "true", "yes")
    msg = (
        f"ENCRYPTION KEY MISMATCH: {total} stored credential(s) cannot be decrypted with the "
        f"current key (source: {_KEY_SOURCE}). Restore the original APP_SECRET_KEY/keyfile or the "
        f"credentials are permanently lost. Set ALLOW_KEY_RESET=1 only if you accept starting fresh."
    )
    if allow_reset:
        logger.critical(msg + " ALLOW_KEY_RESET=1 detected: continuing with an unusable key.")
    else:
        logger.critical(msg)
        raise RuntimeError(msg)


def init_db():
    """Initializes the database schema once on startup and performs an automated snapshot."""
    with get_db_cursor() as (db_type, cursor):
        if db_type == "pg":
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id BIGINT PRIMARY KEY,
                    username TEXT NOT NULL,
                    encrypted_password TEXT NOT NULL,
                    student_name TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
        else:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL,
                    encrypted_password TEXT NOT NULL,
                    student_name TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

    # Enforce one university number per Telegram account at the database level
    try:
        with get_db_cursor() as (db_type, cursor):
            cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username_unique ON users(username)"
            )
    except Exception as e:
        logger.warning(f"Could not create unique username index (existing duplicates?): {e}")

    # Create safety backup on startup, then verify key/data consistency
    if not DATABASE_URL and os.path.exists(DB_PATH):
        backup_sqlite_db()
    _verify_encryption_key()


# ---------------------------------------------------------------------------
# 4. DATA ACCESS FUNCTIONS
# ---------------------------------------------------------------------------
def is_username_registered(username: str, exclude_telegram_id: Optional[int] = None) -> bool:
    with get_db_cursor() as (db_type, cursor):
        ph = "%s" if db_type == "pg" else "?"
        if exclude_telegram_id is not None:
            cursor.execute(
                f"SELECT 1 FROM users WHERE username = {ph} AND telegram_id != {ph}",
                (username.strip(), exclude_telegram_id)
            )
        else:
            cursor.execute(
                f"SELECT 1 FROM users WHERE username = {ph}",
                (username.strip(),)
            )
        row = cursor.fetchone()
        return row is not None


def save_user(telegram_id: int, username: str, password: str, student_name: str = ""):
    encrypted_pw = fernet.encrypt(password.encode("utf-8")).decode("utf-8")
    with get_db_cursor() as (db_type, cursor):
        ph = "%s" if db_type == "pg" else "?"
        query = f"""
            INSERT INTO users (telegram_id, username, encrypted_password, student_name, last_active)
            VALUES ({ph}, {ph}, {ph}, {ph}, CURRENT_TIMESTAMP)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username = excluded.username,
                encrypted_password = excluded.encrypted_password,
                student_name = CASE WHEN excluded.student_name != '' THEN excluded.student_name ELSE users.student_name END,
                last_active = CURRENT_TIMESTAMP
        """
        cursor.execute(query, (telegram_id, username.strip(), encrypted_pw, student_name.strip()))


def get_user(telegram_id: int) -> Optional[Tuple[str, str, str]]:
    with get_db_cursor() as (db_type, cursor):
        ph = "%s" if db_type == "pg" else "?"
        cursor.execute(
            f"SELECT username, encrypted_password, student_name FROM users WHERE telegram_id = {ph}",
            (telegram_id,)
        )
        row = cursor.fetchone()

    if not row:
        return None

    username, enc_pw, name = row
    try:
        decrypted_pw = fernet.decrypt(enc_pw.encode("utf-8")).decode("utf-8")
        return username, decrypted_pw, name or ""
    except Exception as e:
        logger.error(f"Decryption failed for user {telegram_id}: {e}")
        return None


def delete_user(telegram_id: int) -> bool:
    with get_db_cursor() as (db_type, cursor):
        ph = "%s" if db_type == "pg" else "?"
        cursor.execute(f"DELETE FROM users WHERE telegram_id = {ph}", (telegram_id,))
        return cursor.rowcount > 0


def delete_user_by_identifier(identifier: str) -> bool:
    clean_id = identifier.strip()
    with get_db_cursor() as (db_type, cursor):
        ph = "%s" if db_type == "pg" else "?"
        if clean_id.isdigit():
            num_id = int(clean_id)
            # Check if matching by telegram_id first
            cursor.execute(f"DELETE FROM users WHERE telegram_id = {ph}", (num_id,))
            if cursor.rowcount > 0:
                return True
            # Otherwise attempt by student username
            cursor.execute(f"DELETE FROM users WHERE username = {ph}", (clean_id,))
            return cursor.rowcount > 0
        else:
            cursor.execute(f"DELETE FROM users WHERE username = {ph}", (clean_id,))
            return cursor.rowcount > 0


def update_student_name(telegram_id: int, name: str):
    with get_db_cursor() as (db_type, cursor):
        ph = "%s" if db_type == "pg" else "?"
        cursor.execute(
            f"UPDATE users SET student_name = {ph}, last_active = CURRENT_TIMESTAMP WHERE telegram_id = {ph}",
            (name.strip(), telegram_id)
        )


def get_user_count() -> int:
    """Returns the total number of registered students in whichever database is active."""
    with get_db_cursor() as (db_type, cursor):
        cursor.execute("SELECT COUNT(*) FROM users")
        row = cursor.fetchone()
        return row[0] if row else 0


def get_all_users() -> list:
    with get_db_cursor() as (db_type, cursor):
        cursor.execute(
            "SELECT telegram_id, username, student_name, created_at, last_active FROM users ORDER BY created_at DESC"
        )
        rows = cursor.fetchall()

    result = []
    for r in rows:
        result.append({
            "telegram_id": r[0],
            "username": r[1],
            "student_name": r[2] or "غير محدد",
            "created_at": str(r[3]),
            "last_active": str(r[4])
        })
    return result


def get_all_telegram_ids() -> list:
    with get_db_cursor() as (db_type, cursor):
        cursor.execute("SELECT telegram_id FROM users")
        rows = cursor.fetchall()
        return [r[0] for r in rows]


def export_backup_data() -> Dict[str, Any]:
    """Exports all encrypted user credentials and metadata for disaster recovery."""
    with get_db_cursor() as (db_type, cursor):
        cursor.execute("SELECT telegram_id, username, encrypted_password, student_name, created_at, last_active FROM users")
        rows = cursor.fetchall()

    users_dump = []
    for r in rows:
        users_dump.append({
            "telegram_id": r[0],
            "username": r[1],
            "encrypted_password": r[2],
            "student_name": r[3] or "",
            "created_at": str(r[4]),
            "last_active": str(r[5])
        })

    return {
        "db_type": db_type,
        "total_users": len(users_dump),
        "users": users_dump
    }


# Initialize database schema and snapshot on import
init_db()