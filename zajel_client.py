import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Tuple, Dict
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


def get_local_now() -> datetime:
    """
    Returns current datetime in Palestine timezone (Asia/Jerusalem / UTC+2 or UTC+3 DST).
    Falls back to UTC+3 (Palestine summer time) if zoneinfo data is unavailable.
    """
    tz_name = os.getenv("TIMEZONE", "Asia/Jerusalem")
    try:
        return datetime.now(ZoneInfo(tz_name))
    except Exception:
        return datetime.now(timezone(timedelta(hours=3)))


@dataclass
class TimeSlot:
    day: str                 # e.g. 'احد', 'ثلاث'
    day_name: str            # e.g. 'الأحد'
    start_time: str          # e.g. '11:00'
    end_time: str            # e.g. '12:00'
    raw_time: str            # e.g. '12:00 - 11:00'
    room: str                # e.g. '111170'
    campus: str              # e.g. 'الجديد'
    is_online: bool          # True if online or hall 509999


@dataclass
class Course:
    course_id: str
    section: str
    name: str
    credits: int
    instructor: str
    total_absences: int
    excused_absences: int
    deprived: str
    slots: List[TimeSlot] = field(default_factory=list)


@dataclass
class StudentProfile:
    student_id: str
    name: str
    faculty: str
    major: str
    cumulative_gpa: str
    rating: str
    enrollment_year: str
    high_school_gpa: str
    status: str


@dataclass
class SemesterGradeItem:
    code: str
    name: str
    credits: int
    grade: str


@dataclass
class SemesterRecord:
    name: str
    semester_gpa: str
    semester_credits: int
    cumulative_gpa: str
    cumulative_credits: int
    courses: List[SemesterGradeItem] = field(default_factory=list)


@dataclass
class Transcript:
    student_id: str
    student_name: str
    major: str
    faculty: str
    cumulative_gpa: str
    rating: str
    completed_credits: int
    semesters: List[SemesterRecord] = field(default_factory=list)


@dataclass
class UpcomingActivity:
    activity_id: int
    name: str
    activity_name: str
    course_name: str
    component: str
    modulename: str
    event_type: str
    due_timestamp: int
    formatted_due_date: str
    time_remaining_str: str
    url: str
    action_name: str
    action_url: str
    is_actionable: bool


ARABIC_WEEKDAYS = {
    0: "الإثنين",
    1: "الثلاثاء",
    2: "الأربعاء",
    3: "الخميس",
    4: "الجمعة",
    5: "السبت",
    6: "الأحد"
}


def format_activity_due_date(ts: int) -> Tuple[str, str]:
    """
    Given a unix timestamp, returns:
    1. formatted_due_date (e.g. 'الإثنين 2026/10/05 - 11:59 م')
    2. time_remaining_str (e.g. 'متبقي 11 يوماً' or 'متبقي 4 ساعات')
    Uses Palestine timezone (Asia/Jerusalem).
    """
    if not ts:
        return "", ""
    tz_name = os.getenv("TIMEZONE", "Asia/Jerusalem")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone(timedelta(hours=3))

    due_dt = datetime.fromtimestamp(ts, tz)
    now_dt = get_local_now()

    day_name = ARABIC_WEEKDAYS.get(due_dt.weekday(), "")
    hour_12 = due_dt.hour % 12
    if hour_12 == 0:
        hour_12 = 12
    period = "م" if due_dt.hour >= 12 else "ص"
    formatted_date = f"{day_name} {due_dt.strftime('%Y/%m/%d')} - {hour_12:02d}:{due_dt.minute:02d} {period}"

    diff = due_dt - now_dt
    total_seconds = diff.total_seconds()

    if total_seconds < 0:
        remaining_str = "انتهى موعد التسليم"
    elif diff.days > 1:
        remaining_str = f"متبقي {diff.days} يوماً"
    elif diff.days == 1:
        remaining_str = "متبقي يوم واحد"
    else:
        hours = int(total_seconds // 3600)
        minutes = int((total_seconds % 3600) // 60)
        if hours > 0:
            remaining_str = f"متبقي {hours} ساعة و {minutes} دقيقة"
        elif minutes > 0:
            remaining_str = f"متبقي {minutes} دقيقة"
        else:
            remaining_str = "أقل من دقيقة متبقية"

    return formatted_date, remaining_str


DAY_MAP = {
    'احد': 'الأحد',
    'اثنين': 'الإثنين',
    'ثلاث': 'الثلاثاء',
    'اربعاء': 'الأربعاء',
    'خميس': 'الخميس',
    'جمعة': 'الجمعة',
    'سبت': 'السبت',
}

# Python datetime weekday() -> 0: Mon, 1: Tue, 2: Wed, 3: Thu, 4: Fri, 5: Sat, 6: Sun
WEEKDAY_TO_DAY_KEY = {
    6: 'احد',
    0: 'اثنين',
    1: 'ثلاث',
    2: 'اربعاء',
    3: 'خميس',
    4: 'جمعة',
    5: 'سبت',
}


def parse_time_slot(time_str: str) -> Tuple[str, str]:
    parts = [p.strip() for p in time_str.split('-')]
    if len(parts) == 2:
        return parts[1], parts[0]
    return time_str, time_str


class ZajelClient:
    GATE_URL = 'https://zajeles.najah.edu/servlet/ZajelGate'
    BASE_SERVLET_URL = 'https://zajeles.najah.edu/servlet/'

    def __init__(self, username: str, password: str):
        self.username = str(username).strip()
        self.password = str(password).strip()
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Origin': 'https://zajeles.najah.edu',
            'Referer': 'https://zajeles.najah.edu/',
        })
        self.is_logged_in = False
        self._last_login_time = 0

    def login(self) -> bool:
        try:
            # Step 1: Initial gate request
            resp1 = self.session.post(self.GATE_URL, data={'liun': self.username, 'startDate': ''}, timeout=15)
            if resp1.status_code != 200:
                return False

            soup1 = BeautifulSoup(resp1.content.decode('windows-1256', errors='replace'), 'html.parser')
            form1 = soup1.find('form')
            if not form1 or not form1.get('action'):
                return False
            url2 = urljoin(resp1.url, form1.get('action'))

            # Step 2: Forward to password form
            resp2 = self.session.post(url2, timeout=15)
            soup2 = BeautifulSoup(resp2.content.decode('windows-1256', errors='replace'), 'html.parser')
            form2 = soup2.find('form')
            if not form2 or not form2.get('action'):
                return False
            url3 = urljoin(resp2.url, form2.get('action'))

            # Step 3: Post password
            resp3 = self.session.post(url3, data={'gsw': self.password}, timeout=15)
            html3 = resp3.content.decode('windows-1256', errors='replace')
            if 'غير صحيحة' in html3:
                return False

            soup3 = BeautifulSoup(html3, 'html.parser')
            form3 = soup3.find('form')
            if not form3 or not form3.get('action'):
                return False

            action3 = form3.get('action', '')
            if 'login' not in action3.lower():
                return False

            url4 = urljoin(resp3.url, action3)

            # Step 4: Login with timestamp
            ts = str(int(time.time() * 1000))
            resp4 = self.session.post(url4, data={'startDate': ts}, timeout=15)
            html4 = resp4.content.decode('windows-1256', errors='replace')
            soup4 = BeautifulSoup(html4, 'html.parser')
            form4 = soup4.find('form')

            # Step 5: Follow forward form if present (mainN or interstitial page like StuParDataStart)
            if form4 and form4.get('action'):
                action4 = form4.get('action', '')
                url5 = urljoin(resp4.url, action4)
                resp5 = self.session.post(url5, timeout=15)
                html5 = resp5.content.decode('windows-1256', errors='replace')
            else:
                resp5 = self.session.post(urljoin(self.BASE_SERVLET_URL, 'mainN'), timeout=15)
                html5 = resp5.content.decode('windows-1256', errors='replace')

            if resp5.status_code == 200 and not ('WhitePage' in html5 or 'غير صحيحة' in html5):
                if any(marker in html5 for marker in ('start', 'ZajSSChk', 'mainfont', 'headerfont', 'headfont', 'datafont')):
                    self.is_logged_in = True
                    self._last_login_time = time.time()
                    return True

            # Fallback verification: test if start/main page is accessible
            resp_check = self.session.get(urljoin(self.BASE_SERVLET_URL, 'start'), timeout=15)
            html_check = resp_check.content.decode('windows-1256', errors='replace')
            if resp_check.status_code == 200 and 'WhitePage' not in html_check and 'يرجى تسجيل الدخول' not in html_check:
                self.is_logged_in = True
                self._last_login_time = time.time()
                return True

            return False
        except Exception as e:
            print(f'[ZajelClient] Login error: {e}')
            return False

    def ensure_logged_in(self) -> bool:
        if not self.is_logged_in or (time.time() - self._last_login_time > 600):
            return self.login()
        return True

    def _post_with_retry(self, endpoint: str, data: Optional[dict] = None) -> Optional[BeautifulSoup]:
        if not self.ensure_logged_in():
            return None

        url = urljoin(self.BASE_SERVLET_URL, endpoint)
        try:
            resp = self.session.post(url, data=data, timeout=15)
            html = resp.content.decode('windows-1256', errors='replace')
            if 'WhitePage' in html or 'يرجى تسجيل الدخول' in html:
                if self.login():
                    resp = self.session.post(url, data=data, timeout=15)
                    html = resp.content.decode('windows-1256', errors='replace')
                else:
                    return None
            return BeautifulSoup(html, 'html.parser')
        except Exception as e:
            print(f'[ZajelClient] Request error ({endpoint}): {e}')
            return None

    def _get_with_retry(self, endpoint: str) -> Optional[BeautifulSoup]:
        if not self.ensure_logged_in():
            return None

        url = urljoin(self.BASE_SERVLET_URL, endpoint)
        try:
            resp = self.session.get(url, timeout=15)
            html = resp.content.decode('windows-1256', errors='replace')
            if 'WhitePage' in html or 'يرجى تسجيل الدخول' in html:
                if self.login():
                    resp = self.session.get(url, timeout=15)
                    html = resp.content.decode('windows-1256', errors='replace')
                else:
                    return None
            return BeautifulSoup(html, 'html.parser')
        except Exception as e:
            print(f'[ZajelClient] GET Request error ({endpoint}): {e}')
            return None

    def get_student_profile(self) -> Optional[StudentProfile]:
        soup = self._post_with_retry('marksPost')
        if not soup:
            return None

        tables = soup.find_all('table')
        if not tables:
            return None

        data_map = {}
        for row in tables[0].find_all('tr'):
            cells = [c.get_text(strip=True).lstrip(':') for c in row.find_all(['td', 'th']) if c.get_text(strip=True)]
            for i in range(0, len(cells) - 1, 2):
                key = cells[i].replace(':', '').strip()
                val = cells[i + 1].strip()
                data_map[key] = val

        gpa = ''
        rating = ''
        m = re.search(r'المعدل\s*التراكمي\s*[:\s]*(\d+\.\d+)\s*([\u0600-\u06FF]+)?', soup.get_text())
        if m:
            gpa = m.group(1)
            rating = m.group(2) or ''

        return StudentProfile(
            student_id=data_map.get('رقم الطالب', self.username),
            name=data_map.get('اسم الطالب', ''),
            faculty=data_map.get('الكلية', ''),
            major=data_map.get('التخصص', ''),
            cumulative_gpa=gpa or data_map.get('المعدل التراكمي', ''),
            rating=rating,
            enrollment_year=data_map.get('سنة الإلتحاق', ''),
            high_school_gpa=data_map.get('معدل التوجيهي', ''),
            status=data_map.get('الوضع الدراسي', 'منتظم')
        )

    def get_semesters(self) -> List[Tuple[str, str]]:
        soup = self._post_with_retry('program')
        if not soup:
            return []
        sel = soup.find('select', {'name': 'cou'})
        if not sel:
            return []
        options = []
        for opt in sel.find_all('option'):
            val = opt.get('value', '').strip()
            text = opt.get_text(strip=True)
            if val:
                options.append((val, text))
        return options

    def get_schedule(self, cou: Optional[str] = None) -> Tuple[str, Optional[List[Course]]]:
        semesters = self.get_semesters()
        if not semesters:
            return '', None

        if not cou:
            cou, semester_name = semesters[0]
        else:
            semester_name = next((name for val, name in semesters if val == cou), cou)

        soup = self._post_with_retry('program', data={'cou': cou})
        if not soup:
            return semester_name, None

        tables = soup.find_all('table')
        target_table = None
        for t in tables:
            txt = t.get_text()
            if 'رقم المساق' in txt and ('اسم المساق' in txt or 'الأيام' in txt):
                target_table = t
                break

        if not target_table:
            return semester_name, []

        table = target_table
        rows = table.find_all('tr')
        courses: List[Course] = []
        current_course: Optional[Course] = None

        for row in rows[1:]:
            cells = [c.get_text(strip=True) for c in row.find_all(['td', 'th'])]
            if not any(cells):
                continue

            if cells[0] and re.match(r'^\d+$', cells[0]):
                try:
                    cid = cells[0]
                    section = cells[1] if len(cells) > 1 else ''
                    name = cells[3] if len(cells) > 3 else ''
                    credits_val = int(cells[4]) if len(cells) > 4 and cells[4].isdigit() else 0
                    instructor = cells[11] if len(cells) > 11 else 'لم يحدد'
                    tot_abs = int(cells[12]) if len(cells) > 12 and cells[12].isdigit() else 0
                    exc_abs = int(cells[13]) if len(cells) > 13 and cells[13].isdigit() else 0
                    deprived = cells[14] if len(cells) > 14 else 'لا'

                    current_course = Course(
                        course_id=cid,
                        section=section,
                        name=name,
                        credits=credits_val,
                        instructor=instructor,
                        total_absences=tot_abs,
                        excused_absences=exc_abs,
                        deprived=deprived,
                        slots=[]
                    )
                    courses.append(current_course)

                    for day_key in DAY_MAP.keys():
                        if day_key in cells:
                            d_idx = cells.index(day_key)
                            time_val = cells[d_idx + 1] if len(cells) > d_idx + 1 else ''
                            room_val = cells[d_idx + 2] if len(cells) > d_idx + 2 else ''
                            campus_val = cells[d_idx + 3] if len(cells) > d_idx + 3 else ''
                            is_online = '509999' in room_val or 'إلكتروني' in cells or 'الكتروني' in cells
                            start_t, end_t = parse_time_slot(time_val)

                            current_course.slots.append(TimeSlot(
                                day=day_key,
                                day_name=DAY_MAP[day_key],
                                start_time=start_t,
                                end_time=end_t,
                                raw_time=time_val,
                                room=room_val,
                                campus=campus_val,
                                is_online=is_online
                            ))
                            break
                except Exception as e:
                    print(f'[ZajelClient] Error parsing course row: {e}')
            elif current_course:
                for day_key in DAY_MAP.keys():
                    if day_key in cells:
                        d_idx = cells.index(day_key)
                        time_val = cells[d_idx + 1] if len(cells) > d_idx + 1 else ''
                        room_val = cells[d_idx + 2] if len(cells) > d_idx + 2 else ''
                        campus_val = cells[d_idx + 3] if len(cells) > d_idx + 3 else ''
                        is_online = '509999' in room_val or any('لكترون' in c for c in cells)
                        start_t, end_t = parse_time_slot(time_val)

                        current_course.slots.append(TimeSlot(
                            day=day_key,
                            day_name=DAY_MAP[day_key],
                            start_time=start_t,
                            end_time=end_t,
                            raw_time=time_val,
                            room=room_val,
                            campus=campus_val,
                            is_online=is_online
                        ))
                        break

        return semester_name, courses

    def get_today_classes(self, courses: Optional[List[Course]] = None) -> Tuple[str, List[Tuple[Course, TimeSlot]]]:
        now = get_local_now()
        day_key = WEEKDAY_TO_DAY_KEY.get(now.weekday(), 'احد')
        today_name = DAY_MAP.get(day_key, 'اليوم')

        if courses is None:
            _, fetched_courses = self.get_schedule()
            courses = fetched_courses or []

        today_items: List[Tuple[Course, TimeSlot]] = []
        for course in courses:
            for slot in course.slots:
                if slot.day == day_key:
                    today_items.append((course, slot))

        def sort_key(item):
            time_parts = item[1].start_time.split(':')
            try:
                h = int(time_parts[0])
                m = int(time_parts[1]) if len(time_parts) > 1 else 0
                return h, m
            except Exception:
                return 99, 99

        today_items.sort(key=sort_key)
        return today_name, today_items

    def get_moodle_sso_url(self) -> Optional[str]:
        soup = self._post_with_retry('MoodleRedirect')
        if not soup:
            return None
        html = str(soup)
        pattern = r'parent\.location\s*=\s*[\x22\x27](https?://[^\x22\x27]+)[\x22\x27]'
        m = re.search(pattern, html)
        if m:
            return m.group(1).strip()
        return None

    def get_important_messages(self) -> List[Dict[str, Optional[str]]]:
        """
        Scrapes official university announcements from the main dashboard (start servlet via GET).
        Returns a list of dicts: [{'text': ..., 'url': ...}, ...]
        """
        soup = self._get_with_retry('start')
        if not soup:
            return []

        target_table = None
        for t in soup.find_all('table'):
            for r in t.find_all('tr'):
                if 'رسائل هامة' in r.get_text():
                    subtables = [st for st in t.find_all('table') if 'رسائل هامة' in st.get_text()]
                    if not subtables:
                        target_table = t
                        break
            if target_table:
                break

        messages = []
        if target_table:
            for tr in target_table.find_all('tr'):
                txt = tr.get_text(strip=True)
                txt = txt.replace('جديد', '').replace('✉', '').strip()
                if not txt or 'رسائل هامة' in txt:
                    continue
                link = tr.find('a')
                url = link.get('href') if link else None
                messages.append({'text': txt, 'url': url})

        return messages

    def get_transcript(self) -> Optional[Transcript]:
        """
        Scrapes student academic transcript (كشف العلامات) including all completed semesters,
        semester GPAs, cumulative GPAs, cumulative credits, and individual course letter grades.
        """
        soup = self._post_with_retry('marksPost')
        if not soup:
            return None

        tables = soup.find_all('table')
        if not tables:
            return None

        # Parse overview metadata
        profile = self.get_student_profile()
        student_id = profile.student_id if profile else self.username
        student_name = profile.name if profile else ''
        faculty = profile.faculty if profile else ''
        major = profile.major if profile else ''
        cumulative_gpa = profile.cumulative_gpa if profile else ''
        rating = profile.rating if profile else ''

        completed_credits = 0
        m_credits = re.search(r'ساعات أتمها بنجاح\s*[:\s]*(\d+)', soup.get_text())
        if m_credits:
            try:
                completed_credits = int(m_credits.group(1))
            except Exception:
                pass

        # Locate transcript table
        target_table = None
        for t in tables:
            txt = t.get_text()
            if 'رقم المساق' in txt and 'س.م' in txt and 'العلامة' in txt:
                target_table = t
                break

        if not target_table:
            return Transcript(
                student_id=student_id,
                student_name=student_name,
                major=major,
                faculty=faculty,
                cumulative_gpa=cumulative_gpa,
                rating=rating,
                completed_credits=completed_credits,
                semesters=[]
            )

        semesters: List[SemesterRecord] = []
        current_sem: Optional[SemesterRecord] = None

        # Iterate over non-recursive direct rows
        for r in target_table.find_all('tr', recursive=False):
            cells = [c.get_text(strip=True) for c in r.find_all(['td', 'th'], recursive=False)]
            clean_cells = [c for c in cells if c and c != '|']
            row_text = ' '.join(clean_cells)

            # Semester header row
            m_sem = re.search(r'(الفصل\s+(?:الأول|الثاني|الصيفي))\s*(\d{4}/\d{4})', row_text)
            if m_sem:
                sem_title = f"{m_sem.group(1)} {m_sem.group(2)}"
                if not current_sem or current_sem.name != sem_title:
                    current_sem = SemesterRecord(
                        name=sem_title,
                        semester_gpa='',
                        semester_credits=0,
                        cumulative_gpa='',
                        cumulative_credits=0,
                        courses=[]
                    )
                    semesters.append(current_sem)
                continue

            if not current_sem:
                continue

            # Course row: digits, name, credits, grade
            if len(clean_cells) >= 4 and re.match(r'^\d{6,8}$', clean_cells[0]):
                cr = int(clean_cells[2]) if clean_cells[2].isdigit() else 0
                grade_val = clean_cells[3] if len(clean_cells) > 3 else ''
                current_sem.courses.append(SemesterGradeItem(
                    code=clean_cells[0],
                    name=clean_cells[1],
                    credits=cr,
                    grade=grade_val
                ))
            elif 'معدل الفصل' in row_text:
                m_sgpa = re.search(r'معدل\s*الفصل\s*[:\s]*([\d\.]+)', row_text)
                if m_sgpa:
                    current_sem.semester_gpa = m_sgpa.group(1)
                if len(clean_cells) >= 2 and clean_cells[-1].isdigit():
                    current_sem.semester_credits = int(clean_cells[-1])
            elif 'المعدل التراكمي' in row_text:
                m_cgpa = re.search(r'المعدل\s*التراكمي\s*[:\s]*([\d\.]+)', row_text)
                if m_cgpa:
                    current_sem.cumulative_gpa = m_cgpa.group(1)
                if len(clean_cells) >= 2 and clean_cells[-1].isdigit():
                    current_sem.cumulative_credits = int(clean_cells[-1])

        if not completed_credits and semesters and semesters[-1].cumulative_credits:
            completed_credits = semesters[-1].cumulative_credits

        return Transcript(
            student_id=student_id,
            student_name=student_name,
            major=major,
            faculty=faculty,
            cumulative_gpa=cumulative_gpa,
            rating=rating,
            completed_credits=completed_credits,
            semesters=semesters
        )

    def get_upcoming_activities(self) -> List[UpcomingActivity]:
        """
        Scrapes upcoming due activities (assignments, quizzes, project milestones) from Moodle.
        Strictly ignores all lectures, meetings, and sessions (e.g. mod_zoom, BigBlueButton, attendance).
        """
        sso_url = self.get_moodle_sso_url()
        if not sso_url:
            return []

        s = requests.Session()
        s.headers.update({
            'User-Agent': self.session.headers.get('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'),
            'Origin': 'https://zajeles.najah.edu',
            'Referer': 'https://zajeles.najah.edu/'
        })

        try:
            # Step 1: Request SSO redirect URL
            resp1 = s.get(sso_url, timeout=15)
            soup1 = BeautifulSoup(resp1.text, 'html.parser')
            form1 = soup1.find('form')
            if not form1:
                return []

            action = form1.get('action', '')
            post_url = urljoin('https://moodle.najah.edu', action)
            form_data = {inp.get('name'): inp.get('value', '') for inp in form1.find_all('input') if inp.get('name')}

            # Step 2: Auto-submit SSO form into Moodle
            resp2 = s.post(post_url, data=form_data, allow_redirects=True, timeout=15)

            # Step 3: Extract Moodle sesskey
            m = re.search(r'"sesskey":"([^"]+)"', resp2.text)
            sesskey = m.group(1) if m else None

            activities: List[UpcomingActivity] = []
            seen_ids = set()

            def is_lecture(comp: str, mod: str, etype: str, title: str, url: str, purpose: str) -> bool:
                c_low = (comp or '').lower()
                m_low = (mod or '').lower()
                t_low = (title or '').lower()
                u_low = (url or '').lower()
                p_low = (purpose or '').lower()

                # 1. Non-deliverable components/modules to drop
                lecture_comps = {
                    'mod_zoom', 'mod_bigbluebuttonbn', 'mod_attendance', 'mod_teams',
                    'mod_collaborate', 'mod_meeting', 'mod_forum', 'mod_chat', 'mod_url',
                    'mod_resource', 'mod_page', 'mod_folder', 'mod_feedback', 'mod_choice',
                    'mod_survey', 'mod_lti', 'mod_h5pactivity'
                }
                if c_low in lecture_comps or m_low in {
                    'zoom', 'attendance', 'forum', 'chat', 'url', 'resource', 'page',
                    'bigbluebuttonbn', 'teams', 'folder', 'feedback', 'choice'
                }:
                    return True

                # 2. Meeting domains in URLs
                meeting_domains = ['zoom.us', 'teams.microsoft.com', 'meet.google.com', 'webex.com']
                if any(dom in u_low for dom in meeting_domains):
                    return True

                # 3. Meeting / lecture regex in title, url, or component
                lecture_patterns = [
                    r'zoom', r'teams', r'meet', r'webex', r'lecture', r'class', r'session',
                    r'office\s*hour', r'محاضر[ةه]?', r'جلس[ةه]', r'لقاء', r'شعب[ةه]',
                    r'حضور', r'غياب', r'ساع[ةه]\s*مكتبي[ةه]', r'تفاعلي', r'تعويضي[ةه]', r'رابط'
                ]
                for pat in lecture_patterns:
                    if re.search(pat, t_low) or re.search(pat, u_low):
                        return True

                # 4. Purpose check: if explicit purpose given and not assessment, drop
                if p_low and p_low not in {'assessment'}:
                    return True

                return False

            def is_deliverable(comp: str, mod: str, title: str, purpose: str) -> bool:
                c_low = (comp or '').lower()
                m_low = (mod or '').lower()
                t_low = (title or '').lower()
                p_low = (purpose or '').lower()

                # Whitelisted deliverable components
                deliverable_comps = {
                    'mod_assign', 'mod_quiz', 'mod_workshop', 'mod_vpl',
                    'mod_turnitintooltwo', 'mod_turnitintool'
                }
                if c_low in deliverable_comps or m_low in {
                    'assign', 'quiz', 'workshop', 'vpl', 'turnitintooltwo', 'turnitintool'
                }:
                    return True
                if p_low == 'assessment':
                    return True

                # Explicit deliverable keywords in title
                deliverable_patterns = [
                    r'واجب', r'مشروع', r'تسليم', r'تقرير', r'هومورك', r'كويز', r'امتحان', r'اختبار',
                    r'homework', r'project', r'assignment', r'submission', r'quiz', r'exam', r'report', r'phase'
                ]
                for pat in deliverable_patterns:
                    if re.search(pat, t_low):
                        return True

                return False

            now_ts = int(time.time())

            # Method A: Query core_calendar_get_action_events_by_timesort (AJAX API)
            if sesskey:
                service_url = f"https://moodle.najah.edu/lib/ajax/service.php?sesskey={sesskey}"
                last_event_id = None
                max_pages = 10  # Up to 500 events to ensure assignments are never drowned out

                for _ in range(max_pages):
                    args = {
                        "timesortfrom": now_ts - 3600,  # Include events due in the past hour as well
                        "limitnum": 50
                    }
                    if last_event_id:
                        args["aftereventid"] = last_event_id

                    payload = [{
                        "index": 0,
                        "methodname": "core_calendar_get_action_events_by_timesort",
                        "args": args
                    }]

                    try:
                        r_svc = s.post(service_url, json=payload, timeout=15)
                        data_svc = r_svc.json()
                    except Exception:
                        break

                    if not data_svc or not isinstance(data_svc, list) or 'data' not in data_svc[0]:
                        break

                    events_data = data_svc[0]['data']
                    events = events_data.get('events', [])
                    if not events:
                        break

                    for ev in events:
                        eid = ev.get('id')
                        if eid in seen_ids:
                            continue
                        seen_ids.add(eid)

                        comp = ev.get('component', '')
                        mod = ev.get('modulename', '')
                        etype = ev.get('eventtype', '')
                        name = ev.get('name', '')
                        act_name = ev.get('activityname') or name
                        ev_url = ev.get('url', '')
                        purpose = ev.get('purpose', '')

                        if is_lecture(comp, mod, etype, act_name, ev_url, purpose):
                            continue
                        if not is_deliverable(comp, mod, act_name, purpose):
                            continue

                        clean_name = re.sub(r'\s+is due$', '', act_name, flags=re.IGNORECASE).strip()
                        course_fullname = ev.get('course', {}).get('fullname', '')
                        due_ts = ev.get('timesort') or ev.get('timestart', 0)

                        f_date, rem_str = format_activity_due_date(due_ts)
                        action_dict = ev.get('action') or {}

                        activities.append(UpcomingActivity(
                            activity_id=eid,
                            name=clean_name,
                            activity_name=act_name,
                            course_name=course_fullname,
                            component=comp,
                            modulename=mod,
                            event_type=etype,
                            due_timestamp=due_ts,
                            formatted_due_date=f_date,
                            time_remaining_str=rem_str,
                            url=ev_url,
                            action_name=action_dict.get('name', ''),
                            action_url=action_dict.get('url', ''),
                            is_actionable=action_dict.get('actionable', True)
                        ))

                    last_event_id = events_data.get('lastid')
                    if not last_event_id or len(events) < 50:
                        break

            # Method B: Scrape upcoming events page if Method A returned 0 activities
            if not activities:
                cal_resp = s.get("https://moodle.najah.edu/calendar/view.php?view=upcoming", timeout=15)
                cal_soup = BeautifulSoup(cal_resp.text, 'html.parser')
                event_nodes = cal_soup.select('.event[data-event-id], .eventlist .event')
                for ev_node in event_nodes:
                    eid_str = ev_node.get('data-event-id', '0')
                    eid = int(eid_str) if eid_str.isdigit() else 0
                    if eid and eid in seen_ids:
                        continue
                    seen_ids.add(eid)

                    comp = ev_node.get('data-event-component', '')
                    etype = ev_node.get('data-event-eventtype', '')
                    title = ev_node.get('data-event-title') or (ev_node.select_one('.name').get_text(strip=True) if ev_node.select_one('.name') else '')

                    card_link = ev_node.select_one('a.card-link') or ev_node.find('a', href=lambda h: h and 'mod/' in h)
                    link = card_link.get('href') if card_link else ''

                    if is_lecture(comp, '', etype, title, link, ''):
                        continue
                    if not is_deliverable(comp, '', title, ''):
                        continue

                    course_elem = ev_node.find(lambda tag: tag.name == 'div' and tag.find('i', class_=lambda c: c and 'fa-graduation-cap' in c))
                    course_name = ""
                    if course_elem:
                        col11 = course_elem.find_parent('div', class_='row')
                        if col11:
                            course_name = col11.get_text(strip=True).replace('المساق', '').replace('Course', '').strip()

                    date_spans = ev_node.select('span.date')
                    date_str = " ".join([sp.get_text(strip=True) for sp in date_spans]) if date_spans else ""

                    clean_name = re.sub(r'\s+is due$', '', title, flags=re.IGNORECASE).strip()

                    activities.append(UpcomingActivity(
                        activity_id=eid,
                        name=clean_name,
                        activity_name=title,
                        course_name=course_name,
                        component=comp,
                        modulename='',
                        event_type=etype,
                        due_timestamp=0,
                        formatted_due_date=date_str,
                        time_remaining_str='',
                        url=link,
                        action_name='تسليم / استعراض',
                        action_url=link,
                        is_actionable=True
                    ))

            # Sort by due_timestamp ascending
            activities.sort(key=lambda a: a.due_timestamp if a.due_timestamp > 0 else 9999999999)
            return activities
        except Exception as e:
            print(f"[ZajelClient] Error querying Moodle activities: {e}")
            return []