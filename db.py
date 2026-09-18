import os
import sqlite3
from typing import Optional, Tuple
from cryptography.fernet import Fernet
from dotenv import load_dotenv, set_key

load_dotenv()

# Key management: Read or generate Fernet key
KEY_ENV = os.getenv("APP_SECRET_KEY")
if not KEY_ENV:
    generated_key = Fernet.generate_key().decode()
    try:
        set_key(".env", "APP_SECRET_KEY", generated_key)
    except Exception:
        pass
    KEY_ENV = generated_key

fernet = Fernet(KEY_ENV.encode() if isinstance(KEY_ENV, str) else KEY_ENV)
DB_PATH = "zajel_users.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
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
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO users (telegram_id, username, encrypted_password, student_name, last_active)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(telegram_id) DO UPDATE SET
            username = excluded.username,
            encrypted_password = excluded.encrypted_password,
            student_name = CASE WHEN excluded.student_name != '' THEN excluded.student_name ELSE users.student_name END,
            last_active = CURRENT_TIMESTAMP
    """, (telegram_id, username.strip(), encrypted_pw, student_name.strip()))
    conn.commit()
    conn.close()


def get_user(telegram_id: int) -> Optional[Tuple[str, str, str]]:
    init_db()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT username, encrypted_password, student_name FROM users WHERE telegram_id = ?", (telegram_id,))
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
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM users WHERE telegram_id = ?", (telegram_id,))
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def update_student_name(telegram_id: int, name: str):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET student_name = ?, last_active = CURRENT_TIMESTAMP WHERE telegram_id = ?", (name.strip(), telegram_id))
    conn.commit()
    conn.close()


# Initialize on import
init_db()