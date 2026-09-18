from datetime import datetime
from typing import List, Tuple, Dict, Optional
from zajel_client import Course, TimeSlot, StudentProfile, Transcript


def split_message(text: str, max_len: int = 3500) -> List[str]:
    """Splits text into chunks if it exceeds Telegram message length limit."""
    if len(text) <= max_len:
        return [text]

    chunks = []
    lines = text.split("\n")
    current_chunk = []
    current_len = 0

    for line in lines:
        if current_len + len(line) + 1 > max_len:
            chunks.append("\n".join(current_chunk))
            current_chunk = [line]
            current_len = len(line)
        else:
            current_chunk.append(line)
            current_len += len(line) + 1

    if current_chunk:
        chunks.append("\n".join(current_chunk))

    return chunks


def format_today_classes(student_name: str, day_name: str, items: List[Tuple[Course, TimeSlot]]) -> str:
    today_str = datetime.now().strftime("%Y/%m/%d")

    if not items:
        return (
            f"محاضرات اليوم ({day_name} - {today_str})\n"
            f"الطالب: {student_name}\n\n"
            f"لا توجد لديك أي محاضرات مسجلة اليوم."
        )

    lines = [
        f"محاضرات اليوم ({day_name} - {today_str})",
        f"الطالب: {student_name}",
        f"عدد المحاضرات اليوم: {len(items)}\n"
    ]

    for idx, (course, slot) in enumerate(items, 1):
        if slot.is_online:
            loc = "إلكتروني (Online)"
        else:
            loc = f"قاعة {slot.room} - {slot.campus}"

        instructor = course.instructor if course.instructor and course.instructor != "لم يحدد" else "غير محدد"

        lines.append(f"{idx}. {course.name}")
        lines.append(f"   الوقت: {slot.start_time} - {slot.end_time}")
        lines.append(f"   المكان: {loc}")
        lines.append(f"   المدرس: {instructor}\n")

    return "\n".join(lines).strip()


def format_full_schedule(student_name: str, semester_name: str, courses: List[Course]) -> str:
    if not courses:
        return f"لم يتم العثور على مساقات مسجلة لفصل ({semester_name})."

    total_credits = sum(c.credits for c in courses)

    lines = [
        f"البرنامج الدراسي الأسبوعي ({semester_name})",
        f"الطالب: {student_name}",
        f"إجمالي المساقات: {len(courses)} | الساعات المعتمدة: {total_credits}\n"
    ]

    for idx, c in enumerate(courses, 1):
        inst_str = f" | المدرس: {c.instructor}" if c.instructor and c.instructor != "لم يحدد" else ""
        lines.append(f"{idx}. {c.name} ({c.section}) - {c.credits} س.م{inst_str}")

        for slot in c.slots:
            if slot.is_online:
                loc = "إلكتروني"
            else:
                loc = f"قاعة {slot.room} ({slot.campus})"
            lines.append(f"   • {slot.day_name}: {slot.start_time} - {slot.end_time} | {loc}")
        lines.append("")

    return "\n".join(lines).strip()


def format_student_profile(p: StudentProfile) -> str:
    lines = [
        "بطاقة الطالب الأكاديمية\n",
        f"الاسم: {p.name}",
        f"الرقم الجامعي: {p.student_id}",
        f"الكلية: {p.faculty}",
        f"التخصص: {p.major}",
        f"سنة الإلتحاق: {p.enrollment_year}",
        f"معدل التوجيهي: {p.high_school_gpa}%",
        f"الوضع الدراسي: {p.status}",
        f"المعدل التراكمي: {p.cumulative_gpa} ({p.rating})"
    ]
    return "\n".join(lines)


def format_transcript(t: Transcript) -> str:
    lines = [
        "كشف العلامات وسجل الدرجات الأكاديمي\n",
        f"الطالب: {t.student_name} ({t.student_id})",
        f"الكلية: {t.faculty}",
        f"التخصص: {t.major}",
        f"المعدل التراكمي: {t.cumulative_gpa} ({t.rating})",
        f"الساعات المنجزة بنجاح: {t.completed_credits} س.م\n"
    ]

    if not t.semesters:
        lines.append("لا توجد فصول دراسية مسجلة في كشف العلامات.")
        return "\n".join(lines).strip()

    # Show latest semester first
    for s in reversed(t.semesters):
        lines.append(f"[{s.name}]")
        sgpa = s.semester_gpa if s.semester_gpa else "--"
        scr = f"{s.semester_credits}" if s.semester_credits else "--"
        cgpa = s.cumulative_gpa if s.cumulative_gpa else "--"
        lines.append(f"معدل الفصل: {sgpa} | س. الفصل: {scr} | التراكمي بعده: {cgpa}")

        for c in s.courses:
            g = c.grade if c.grade else "--"
            lines.append(f"• {c.name} ({c.credits} س.م): {g}")
        lines.append("")

    return "\n".join(lines).strip()


def format_absences(courses: List[Course]) -> str:
    if not courses:
        return "لا توجد مساقات مسجلة حالياً."

    lines = [
        "سجل غيابات الفصل الحالي\n"
    ]

    has_any = False
    for c in courses:
        lines.append(f"• {c.name}")
        lines.append(f"  إجمالي الغياب: {c.total_absences} ساعة (بعذر: {c.excused_absences})")
        lines.append(f"  حرمان: {c.deprived}\n")
        if c.total_absences > 0:
            has_any = True

    if not has_any:
        lines.append("ليس لديك أي ساعات غياب مسجلة حتى الآن.")

    return "\n".join(lines).strip()


def format_messages(messages: List[dict]) -> str:
    if not messages:
        return "رسائل زاجل الهامة\n\nلا توجد رسائل أو إعلانات هامة في صندوق البريد حالياً."

    lines = [
        f"رسائل وإعلانات زاجل الهامة ({len(messages)})\n"
    ]

    for idx, m in enumerate(messages, 1):
        lines.append(f"{idx}. {m['text']}")
        if m.get("url"):
            lines.append(f"   رابط الإعلان: {m['url']}")
        lines.append("")

    return "\n".join(lines).strip()


def format_help() -> str:
    return (
        "بوت خدمات زاجل - جامعة النجاح الوطنية\n\n"
        "يمكنك استخدام الأزرار أدناه أو الأوامر التالية:\n\n"
        "/today - جدول ومحاضرات اليوم\n"
        "/schedule - البرنامج الأسبوعي كاملاً\n"
        "/grades - كشف العلامات وسجل الدرجات\n"
        "/absences - ساعات الغياب والحرمان\n"
        "/messages - الرسائل والإعلانات الهامة\n"
        "/moodle - رابط تسجيل الدخول المباشر إلى مودل\n"
        "/refresh - تحديث البيانات فوراً من خادم زاجل\n"
        "/logout - تسجيل الخروج وحذف البيانات من البوت"
    )