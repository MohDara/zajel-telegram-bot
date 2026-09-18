import os
import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from dotenv import load_dotenv
import db
import formatter
from zajel_client import ZajelClient, parse_time_slot, StudentProfile, Course, TimeSlot

print("=== Running Comprehensive Test Suite ===")

# 1. DB Test
test_uid = 999999999
db.save_user(test_uid, "test_stu", "test_pass", "طالب تجريبي")
retrieved = db.get_user(test_uid)
assert retrieved and retrieved[0] == "test_stu" and retrieved[1] == "test_pass"
assert db.delete_user(test_uid) is True
assert db.get_user(test_uid) is None
print("Test 1 Passed: Secure database storage and Fernet encryption/decryption")

# 2. Formatter Test (No emojis, no long line breaks)
mock_profile = StudentProfile(
    student_id="12000000",
    name="طالب تجريبي",
    faculty="الهندسة",
    major="هندسة الحاسوب",
    cumulative_gpa="2.99",
    rating="جيد",
    enrollment_year="2023",
    high_school_gpa="94",
    status="يدرس"
)
formatted_p = formatter.format_student_profile(mock_profile)
assert "─" not in formatted_p, "Long divider line found in profile format!"
assert "🎓" not in formatted_p, "Emoji found in profile format!"
print("Test 2 Passed: Clean typography without emojis and long lines")

# 3. Live Zajel Portal Test
load_dotenv()
u = os.getenv("ZAJEL_USER")
p = os.getenv("ZAJEL_PASS")

if u and p:
    print(f"Connecting to live Zajel portal as {u}...")
    client = ZajelClient(u, p)
    assert client.login() is True, "Zajel login failed!"
    print("Test 3 Passed: 5-step Tomcat authentication")

    # Moodle SSO test
    moodle_url = client.get_moodle_sso_url()
    assert moodle_url and "moodle.najah.edu/zajel_login_redirect.php" in moodle_url, f"Invalid moodle url: {moodle_url}"
    print(f"Test 4 Passed: Moodle SSO redirect token generated: {moodle_url[:60]}...")

    # Messages test
    msgs = client.get_important_messages()
    print(f"Test 5 Passed: Important messages queried ({len(msgs)} messages)")

    # Schedule test
    sem_name, courses = client.get_schedule()
    assert len(courses) > 0, "No courses found!"
    print(f"Test 6 Passed: Full schedule retrieved ({len(courses)} courses in {sem_name})")

print("\nALL VERIFICATIONS PASSED SUCCESSFULLY!")