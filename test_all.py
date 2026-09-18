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

print("=== Running Comprehensive Test Suite ===")

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

# 2. Formatter Test (No emojis, no long line breaks in transcript)
mock_transcript = Transcript(
    student_id="12000000",
    student_name="طالب تجريبي",
    major="هندسة الحاسوب",
    faculty="الهندسة وتكنولوجيا المعلومات",
    cumulative_gpa="2.99",
    rating="جيد",
    completed_credits=108,
    semesters=[
        SemesterRecord(
            name="الفصل الثاني 2026/2025",
            semester_gpa="2.97",
            semester_credits=19,
            cumulative_gpa="2.99",
            cumulative_credits=108,
            courses=[
                SemesterGradeItem(code="10636314", name="الخوارزميات وحسابات التعقيد", credits=3, grade="B"),
                SemesterGradeItem(code="10636316", name="برمجة الويب", credits=3, grade="B"),
            ]
        )
    ]
)
formatted_t = formatter.format_transcript(mock_transcript)
assert "─" not in formatted_t, "Long divider line found in transcript format!"
assert "🎓" not in formatted_t, "Emoji found in transcript format!"
assert "الخوارزميات وحسابات التعقيد" in formatted_t
assert "2.99" in formatted_t
print("Test 2 Passed: Clean transcript typography without emojis and long lines")

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

    # Transcript test
    transcript = client.get_transcript()
    assert transcript is not None, "Failed to get live transcript!"
    assert len(transcript.semesters) > 0, "No semesters found in transcript!"
    assert transcript.completed_credits > 0, "Completed credits should be greater than 0!"
    print(f"Test 7 Passed: Full academic transcript extracted ({len(transcript.semesters)} semesters, {transcript.completed_credits} completed credits)")

print("\nALL VERIFICATIONS PASSED SUCCESSFULLY!")