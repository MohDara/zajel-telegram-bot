import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Tuple, Dict
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


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
            soup3 = BeautifulSoup(resp3.content.decode('windows-1256', errors='replace'), 'html.parser')
            form3 = soup3.find('form')
            if not form3 or not form3.get('action'):
                return False
            url4 = urljoin(resp3.url, form3.get('action'))

            # Step 4: Login with timestamp
            ts = str(int(time.time() * 1000))
            resp4 = self.session.post(url4, data={'startDate': ts}, timeout=15)
            soup4 = BeautifulSoup(resp4.content.decode('windows-1256', errors='replace'), 'html.parser')
            form4 = soup4.find('form')
            if not form4 or not form4.get('action'):
                return False

            action4 = form4.get('action')
            if 'WhitePage' in action4:
                return False

            url5 = urljoin(resp4.url, action4)

            # Step 5: Complete login into mainN
            resp5 = self.session.post(url5, timeout=15)
            if resp5.status_code == 200:
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

    def get_schedule(self, cou: Optional[str] = None) -> Tuple[str, List[Course]]:
        semesters = self.get_semesters()
        if not semesters:
            return '', []

        if not cou:
            cou, semester_name = semesters[0]
        else:
            semester_name = next((name for val, name in semesters if val == cou), cou)

        soup = self._post_with_retry('program', data={'cou': cou})
        if not soup:
            return semester_name, []

        tables = soup.find_all('table')
        if len(tables) < 3:
            return semester_name, []

        table = tables[2]
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
        now = datetime.now()
        day_key = WEEKDAY_TO_DAY_KEY.get(now.weekday(), 'احد')
        today_name = DAY_MAP.get(day_key, 'اليوم')

        if courses is None:
            _, courses = self.get_schedule()

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