import os
import sqlite3
from typing import Optional, Tuple, List, Dict
from urllib.parse import urlparse
from cryptography.fernet import Fernet
from dotenv import load_dotenv

load_dotenv()

DEFAULT_FALLBACK_KEY = "<redacted_key>"
KEY_ENV = os.getenv("APP_SECRET_KEY", DEFAULT_FALLBACK_KEY).strip()
if not KEY_ENV:
    KEY_ENV = DEFAULT_FALLBACK_KEY

fernet = Fernet(KEY_ENV.encode() if isinstance(KEY_ENV, str) else KEY_ENV)
DB_PATH = "zajel_users.db"
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()


def get_db_connection():
    """Returns a database connection (PostgreSQL if DATABASE_URL is set, otherwise SQLite)."""
    if DATABASE_URL:
        import pg8000.dbapi
        # Render sometimes provides postgres:// instead of postgresql://
        parsed = urlparse(DATABASE_URL)
        return "pg", pg8000.dbapi.connect(
            user=parsed.username,
            password=parsed.password,
            host=parsed.hostname,
            port=parsed.port or 5432,
            database=parsed.path.lstrip("/"),
            ssl_context=True if parsed.hostname not in ["localhost", "127.0.0.1"] else None
        )
    return "sqlite", sqlite3.connect(DB_PATH)


def init_db():
    db_type, conn = get_db_connection()
    cursor = conn.cursor()
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
    conn.commit()
    conn.close()


def save_user(telegram_id: int, username: str, password: str, student_name: str = ""):
    init_db()
    encrypted_pw = fernet.encrypt(password.encode()).decode()
    db_type, conn = get_db_connection()
    cursor = conn.cursor()

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
    conn.commit()
    conn.close()


def get_user(telegram_id: int) -> Optional[Tuple[str, str, str]]:
    init_db()
    db_type, conn = get_db_connection()
    cursor = conn.cursor()
    ph = "%s" if db_type == "pg" else "?"
    cursor.execute(f"SELECT username, encrypted_password, student_name FROM users WHERE telegram_id = {ph}", (telegram_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    username, enc_pw, name = row
    try:
        decrypted_pw = fernet.decrypt(enc_pw.encode()).decode()
        return username, decrypted_pw, name or ""
    except Exception:
        return None


def delete_user(telegram_id: int) -> bool:
    init_db()
    db_type, conn = get_db_connection()
    cursor = conn.cursor()
    ph = "%s" if db_type == "pg" else "?"
    cursor.execute(f"DELETE FROM users WHERE telegram_id = {ph}", (telegram_id,))
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def update_student_name(telegram_id: int, name: str):
    init_db()
    db_type, conn = get_db_connection()
    cursor = conn.cursor()
    ph = "%s" if db_type == "pg" else "?"
    cursor.execute(f"UPDATE users SET student_name = {ph}, last_active = CURRENT_TIMESTAMP WHERE telegram_id = {ph}", (name.strip(), telegram_id))
    conn.commit()
    conn.close()


def get_all_users() -> list:
    init_db()
    db_type, conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT telegram_id, username, student_name, created_at, last_active FROM users ORDER BY created_at DESC")
    rows = cursor.fetchall()
    conn.close()
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
    init_db()
    db_type, conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT telegram_id FROM users")
    rows = cursor.fetchall()
    conn.close()
    return [r[0] for r in rows]


init_db()