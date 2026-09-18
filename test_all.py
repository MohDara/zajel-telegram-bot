import os
import sys
import sqlite3
import tempfile
import shutil
from concurrent.futures import ThreadPoolExecutor

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Isolate tests from the production database and encryption key
_TEST_DIR = tempfile.mkdtemp(prefix="zajel_test_")
os.environ["DB_PATH"] = os.path.join(_TEST_DIR, "test_users.db")
os.environ["APP_KEY_FILE"] = os.path.join(_TEST_DIR, ".app_secret.key")
os.environ["BACKUP_DIR"] = os.path.join(_TEST_DIR, "backups")
os.environ["DATABASE_URL"] = ""

from dotenv import load_dotenv
import db
import formatter
from zajel_client import (
    ZajelClient,
    parse_time_slot,
    StudentProfile,
    Course,
    TimeSlot,
    SemesterGradeItem,
    SemesterRecord,
    Transcript,
)

print("=== Running Comprehensive Security, Persistence & Quality Test Suite ===")

# 1. DB Test (CRUD + duplicate checking + deletion by identifier)
test_uid = 999999999
db.save_user(test_uid, "test_stu_unique", "test_pass", "طالب تجريبي")
retrieved = db.get_user(test_uid)
assert retrieved and retrieved[0] == "test_stu_unique" and retrieved[1] == "test_pass"
assert db.is_username_registered("test_stu_unique") is True
assert db.is_username_registered("test_stu_unique", exclude_telegram_id=test_uid) is False
assert db.is_username_registered("non_existent_stu") is False

# Test delete by identifier
assert db.delete_user_by_identifier("test_stu_unique") is True
assert db.get_user(test_uid) is None
print("Test 1 Passed: Secure database storage, Fernet encryption, and duplicate checking")

# 2. Data Loss Prevention & Resilience Tests
# 2a. Encryption Key Persistence
assert len(db._FERNET_KEY) == 44, "Fernet key must be 44 base64 bytes!"
assert db.fernet is not None

# 2b. SQLite WAL mode & concurrency (if using local SQLite)
if not db.DATABASE_URL:
    conn = sqlite3.connect(db.DB_PATH)
    mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
    conn.close()
    assert mode.lower() == "wal", f"Expected WAL journal mode, got {mode}!"
    print("Test 2a Passed: SQLite Write-Ahead Logging (WAL) mode active")

    # 2c. Atomic backup generation
    bak_path = db.backup_sqlite_db()
    assert bak_path and os.path.exists(bak_path), "Failed to generate SQLite online backup!"
    print(f"Test 2b Passed: Online atomic backup created ({bak_path})")

# 2d. Disaster recovery data export
backup_data = db.export_backup_data()
assert "db_type" in backup_data and "users" in backup_data
assert backup_data["total_users"] >= 0
print(f"Test 2c Passed: Disaster recovery export valid ({backup_data['total_users']} registered users)")

# 2e. Concurrency Stress Test: 10 parallel threads reading/writing
def concurrent_worker(worker_id):
    temp_id = 8888000 + worker_id
    db.save_user(temp_id, f"test_worker_{worker_id}", "temp_pw", f"Worker {worker_id}")
    u = db.get_user(temp_id)
    assert u is not None and u[0] == f"test_worker_{worker_id}"
    db.delete_user(temp_id)
    return True

with ThreadPoolExecutor(max_workers=5) as executor:
    results = list(executor.map(concurrent_worker, range(10)))
assert all(results)
print("Test 2d Passed: Multi-threaded concurrency stress test (zero database lockups)")

# 3. Formatter Test (No emojis, no long line breaks in transcript)
mock_transcript = Transcript(
    student_id="12000000",
    student_name="طالب تجريبي",
    major="علم الحاسوب",
    faculty="الهندسة وتكنولوجيا المعلومات",
    cumulative_gpa="3.50",
    rating="جيد جداً",
    completed_credits=90,
    semesters=[
        SemesterRecord(
            name="الفصل الأول 2026/2025",
            semester_gpa="3.60",
            semester_credits=15,
            cumulative_gpa="3.50",
            cumulative_credits=90,
            courses=[
                SemesterGradeItem(code="10636111", name="تراكيب البيانات", credits=3, grade="A"),
                SemesterGradeItem(code="10636112", name="برمجة الويب", credits=3, grade="B+"),
            ]
        )
    ]
)
formatted_t = formatter.format_transcript(mock_transcript)
assert "─" not in formatted_t, "Long divider line found in transcript format!"
assert "🎓" not in formatted_t, "Emoji found in transcript format!"
assert "تراكيب البيانات" in formatted_t
assert "3.50" in formatted_t
print("Test 3 Passed: Clean transcript typography without emojis and long lines")

# 4. Live Zajel Portal Test
load_dotenv()
u = os.getenv("ZAJEL_USER")
p = os.getenv("ZAJEL_PASS")

if u and p:
    print(f"Connecting to live Zajel portal as {u}...")
    client = ZajelClient(u, p)
    assert client.login() is True, "Zajel login failed!"
    print("Test 4 Passed: 5-step Tomcat authentication")

    # Moodle SSO test
    moodle_url = client.get_moodle_sso_url()
    assert moodle_url and "moodle.najah.edu/zajel_login_redirect.php" in moodle_url, f"Invalid moodle url: {moodle_url}"
    print(f"Test 5 Passed: Moodle SSO redirect token generated: {moodle_url[:60]}...")

    # Messages test
    msgs = client.get_important_messages()
    print(f"Test 6 Passed: Important messages queried ({len(msgs)} messages)")

    # Schedule test
    sem_name, courses = client.get_schedule()
    assert len(courses) > 0, "No courses found!"
    print(f"Test 7 Passed: Full schedule retrieved ({len(courses)} courses in {sem_name})")

    # Transcript test
    transcript = client.get_transcript()
    assert transcript is not None, "Failed to get live transcript!"
    assert len(transcript.semesters) > 0, "No semesters found in transcript!"
    assert transcript.completed_credits > 0, "Completed credits should be greater than 0!"
    print(f"Test 8 Passed: Full academic transcript extracted ({len(transcript.semesters)} semesters, {transcript.completed_credits} completed credits)")

# 9. Telegram message splitting safety (oversized single lines)
long_line = "س" * 9000
chunks = formatter.split_message(long_line)
assert all(len(c) <= 3500 for c in chunks), "split_message produced oversized chunks!"
assert "".join(chunks) == long_line, "split_message lost characters!"
mixed = formatter.split_message(("short\n" * 50) + long_line + ("\nshort" * 50))
assert all(len(c) <= 3500 for c in mixed), "split_message mixed content oversized!"
print("Test 9 Passed: Message splitting never exceeds Telegram limits and loses no characters")

# 10. Backup retention & verification
backups = [f for f in os.listdir(db.BACKUP_DIR) if f.endswith(".db")]
assert len(backups) >= 1, "No verified backups retained!"
summary_count, summary_latest = db.get_backup_summary()
assert summary_count >= 1, "Backup summary reported no backups!"
print(f"Test 10 Passed: Verified backup retention active ({summary_count} snapshots, latest {summary_latest})")

shutil.rmtree(_TEST_DIR, ignore_errors=True)
print("\nALL VERIFICATIONS PASSED SUCCESSFULLY!")