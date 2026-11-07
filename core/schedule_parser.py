import csv
import io
import re
import logging
import asyncio
from datetime import datetime, timezone, timedelta
import httpx
import aiosqlite

logger = logging.getLogger(__name__)

MOSCOW = timezone(timedelta(hours=3), name="MSK")

SPREADSHEET_ID = "1GOc7NuHbwa94QwPLWM0kGJOFQU3iszGK"
GRADE_SHEET_GIDS = {
    "5": "925792215",
    "6": "1333352466",
    "7": "1367582746",
    "8": "822267835",
    "9": "1145447977",
    "10": "1534986807",
    "11": "551024178",
}

DEFAULT_SHEET_URL = (
    f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit"
)
DB_NAME = "school_sync.db"

# Дни недели по порядку (0=понедельник ... 6=воскресенье)
RUSSIAN_DAYS = [
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье"
]

TRANS_CYRILLIC = str.maketrans({
    "A": "А", "B": "В", "E": "Е", "I": "И", "K": "К", "M": "М",
    "H": "Н", "O": "О", "P": "Р", "C": "С", "T": "Т", "X": "Х"
})

_CACHED_SCHEDULE = None
_LAST_UPDATE_TIME = None


def extract_csv_export_url(sheet_url: str, default_gid: str = "0") -> str:
    """
    Преобразует стандартную ссылку Google Таблицы в прямую ссылку на экспорт в CSV.
    """
    match_id = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", sheet_url)
    if not match_id:
        raise ValueError(f"Не удалось извлечь ID таблицы из ссылки: {sheet_url}")
    sheet_id = match_id.group(1)

    match_gid = re.search(r"[#&?]gid=([0-9]+)", sheet_url)
    gid = match_gid.group(1) if match_gid else default_gid

    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"


def normalize_class_name(grade: str) -> str:
    """Нормализует обозначение класса (например: '9а', '9-а', '9 А', '7а (акад)' -> '7А')."""
    if not grade:
        return ""
    m = re.search(r"(\d+)\s*[-_]?\s*([А-Яа-яA-Za-z])", str(grade))
    if m:
        num = m.group(1)
        letter = m.group(2).upper().translate(TRANS_CYRILLIC)
        return f"{num}{letter}"
    clean = re.sub(r"[\s\-_]", "", str(grade)).upper()
    return clean.translate(TRANS_CYRILLIC)


def parse_single_grade_sheet(csv_text: str, grade_str: str) -> tuple[str, dict]:
    """
    Парсит один лист параллели (5, 6, 7, 8, 9, 10 или 11) МБОУ Лицей №44.
    Возвращает (school_name, classes_schedule).
    """
    reader = list(csv.reader(io.StringIO(csv_text)))
    if len(reader) < 7:
        return "МБОУ Лицей № 44 г. Чебоксары", {}

    school_name = "МБОУ Лицей № 44 г. Чебоксары"
    for r in reader[:4]:
        for cell in r:
            if "Лицей" in cell:
                school_name = " ".join(cell.split())
                break

    header_row_idx = None
    classes_map = {}  # col_idx -> class_name (например 2 -> '5А')

    for idx, row in enumerate(reader[:10]):
        found = {}
        for c_idx, cell in enumerate(row):
            val = cell.strip()
            m = re.match(rf"^{grade_str}\s*([А-Яа-яA-Za-z])", val)
            if m:
                letter = m.group(1).upper().translate(TRANS_CYRILLIC)
                found[c_idx] = f"{grade_str}{letter}"
        if len(found) >= 2:
            header_row_idx = idx
            classes_map = found
            break

    if not classes_map:
        return school_name, {}

    valid_days = set(RUSSIAN_DAYS[:5])
    classes_schedule = {
        cls_name: {day: [] for day in RUSSIAN_DAYS[:5]}
        for cls_name in classes_map.values()
    }

    current_day = None

    for row_idx in range(header_row_idx + 1, len(reader)):
        row = reader[row_idx]
        if not row or not any(x.strip() for x in row):
            continue

        # Проверяем день недели
        first_col = row[0].strip().lower() if len(row) > 0 else ""
        if first_col in valid_days:
            current_day = first_col
        else:
            for cell in row[:2]:
                c_val = cell.strip().lower()
                if c_val in valid_days:
                    current_day = c_val
                    break

        if not current_day:
            continue

        # Номер урока из столбца 1 (или столбца 11, если столбец 1 пуст)
        lesson_str = row[1].strip() if len(row) > 1 else ""
        if not lesson_str.isdigit():
            # Запасной вариант: проверяем первый найденный столбец с цифрой 1..8
            for c_val in (row[1:2] + (row[11:12] if len(row) > 11 else [])):
                if c_val.strip().isdigit():
                    lesson_str = c_val.strip()
                    break

        if not lesson_str.isdigit():
            continue
        lesson_num = int(lesson_str)

        for col_idx, cls_name in classes_map.items():
            subject = row[col_idx].strip() if len(row) > col_idx else ""
            cabinet = row[col_idx + 1].strip() if len(row) > col_idx + 1 else ""

            if subject:
                clean_subj = " ".join(subject.split())
                clean_cab = " ".join(cabinet.split())
                classes_schedule[cls_name][current_day].append({
                    "lesson": lesson_num,
                    "subject": clean_subj,
                    "cabinet": clean_cab
                })

    for cls_name, days in classes_schedule.items():
        for d, lessons in days.items():
            lessons.sort(key=lambda x: x["lesson"])

    return school_name, classes_schedule


def parse_schedule_csv_content(csv_text: str) -> dict:
    """
    Обратная совместимость для парсинга отдельного CSV листа (по умолчанию 9 класс).
    """
    school_name, classes_sched = parse_single_grade_sheet(csv_text, "9")
    if not classes_sched:
        # Пробуем определить параллель автоматически
        for g in GRADE_SHEET_GIDS:
            s_name, s_sched = parse_single_grade_sheet(csv_text, g)
            if s_sched:
                school_name, classes_sched = s_name, s_sched
                break

    now_iso = datetime.now(MOSCOW).isoformat()
    return {
        "metadata": {
            "school": school_name,
            "classes": sorted(list(classes_sched.keys())),
            "updated_at": now_iso
        },
        "schedule": classes_sched
    }


async def init_schedule_table():
    """Создает таблицу для сохранения расписания в SQLite при необходимости."""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS school_schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_url TEXT,
                updated_at TEXT,
                schedule_json TEXT
            )
        """)
        await db.commit()


async def save_schedule_to_db(data: dict, source_url: str):
    """Сохраняет спарсенное расписание в базу данных."""
    import json
    if not data or not data.get("schedule"):
        logger.warning("Попытка сохранить пустое расписание в БД отменена.")
        return
    await init_schedule_table()
    json_str = json.dumps(data, ensure_ascii=False)
    now_str = datetime.now(MOSCOW).strftime("%Y-%m-%d %H:%M:%S")

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM school_schedules")
        await db.execute(
            "INSERT INTO school_schedules (source_url, updated_at, schedule_json) VALUES (?, ?, ?)",
            (source_url, now_str, json_str)
        )
        await db.commit()


async def load_schedule_from_db() -> dict | None:
    """Загружает последнее расписание из базы данных."""
    import json
    await init_schedule_table()
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT schedule_json FROM school_schedules ORDER BY id DESC LIMIT 1") as cursor:
            row = await cursor.fetchone()
            if row:
                return json.loads(row[0])
    return None


async def fetch_and_update_schedule(sheet_url: str = DEFAULT_SHEET_URL) -> dict:
    """
    Скачивает свежие таблицы всех параллелей 5-11 из Google Sheets, парсит и сохраняет в память и БД.
    """
    global _CACHED_SCHEDULE, _LAST_UPDATE_TIME
    logger.info(f"Загрузка расписания всех параллелей 5-11 из Google Sheets: {sheet_url}")

    match_id = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", sheet_url)
    sheet_id = match_id.group(1) if match_id else SPREADSHEET_ID

    all_schedules = {}
    classes_by_grade = {}
    detected_school = "МБОУ Лицей № 44 г. Чебоксары"

    async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
        async def _fetch_grade(grade_num: str, gid: str):
            export_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
            try:
                resp = await client.get(export_url, headers={"User-Agent": "Mozilla/5.0 SchoolBot/1.0"})
                if resp.status_code == 200:
                    return grade_num, resp.text
                else:
                    logger.warning(f"Параллель {grade_num}: статус {resp.status_code}")
                    return grade_num, ""
            except Exception as e:
                logger.error(f"Ошибка загрузки параллели {grade_num}: {e}")
                return grade_num, ""

        tasks = [_fetch_grade(g, gid) for g, gid in GRADE_SHEET_GIDS.items()]
        results = await asyncio.gather(*tasks)

        for grade_num, csv_text in results:
            if not csv_text:
                continue
            school_name, sched = parse_single_grade_sheet(csv_text, grade_num)
            if school_name:
                detected_school = school_name
            all_schedules.update(sched)
            classes_by_grade[grade_num] = sorted(list(sched.keys()), key=lambda c: (int(re.match(r"^\d+", c).group(0)), c))

    if not all_schedules:
        logger.warning("Не удалось спарсить ни один класс. Расписание в БД не перезаписывается.")
        cached = await load_schedule_from_db()
        if cached:
            _CACHED_SCHEDULE = cached
            return cached

    # Сортируем все классы
    all_classes = sorted(list(all_schedules.keys()), key=lambda c: (int(re.match(r"^\d+", c).group(0)), c))
    grades_list = [g for g in ["5", "6", "7", "8", "9", "10", "11"] if g in classes_by_grade]

    now_iso = datetime.now(MOSCOW).isoformat()
    parsed_data = {
        "metadata": {
            "school": detected_school,
            "grades": grades_list,
            "classes_by_grade": classes_by_grade,
            "classes": all_classes,
            "updated_at": now_iso
        },
        "schedule": all_schedules
    }

    _CACHED_SCHEDULE = parsed_data
    _LAST_UPDATE_TIME = datetime.now(MOSCOW)

    await save_schedule_to_db(parsed_data, sheet_url)
    logger.info(f"Расписание 5-11 успешно обновлено. Всего классов: {len(all_classes)}: {all_classes}")
    return parsed_data


async def get_schedule(force_refresh: bool = False, sheet_url: str = DEFAULT_SHEET_URL) -> dict:
    """Возвращает кэшированное расписание или загружает свежее."""
    global _CACHED_SCHEDULE
    if force_refresh or _CACHED_SCHEDULE is None:
        db_data = await load_schedule_from_db()
        if db_data and not force_refresh:
            _CACHED_SCHEDULE = db_data
            return _CACHED_SCHEDULE
        return await fetch_and_update_schedule(sheet_url)
    return _CACHED_SCHEDULE


def format_day_schedule(schedule_data: dict, class_name: str, day_name: str = None) -> str:
    """
    Форматирует расписание уроков на конкретный день для красивого вывода в боте.
    """
    norm_class = normalize_class_name(class_name)
    classes = schedule_data.get("schedule", {})
    class_sched = classes.get(norm_class)

    if not class_sched:
        available = ", ".join(schedule_data.get("metadata", {}).get("classes", []))
        return f"❌ Расписание для класса <b>{norm_class}</b> не найдено.\nДоступные классы: {available}"

    if not day_name:
        # Автоматически определяем сегодняшний день по МСК
        now_msk = datetime.now(MOSCOW)
        weekday_idx = now_msk.weekday()  # 0=понедельник ... 4=пятница, 5=суббота, 6=воскресенье
        if weekday_idx >= 5:  # Суббота или воскресенье -> показываем понедельник
            day_name = "понедельник"
            day_title = "понедельник (следующая неделя, сегодня выходной)"
        else:
            day_name = RUSSIAN_DAYS[weekday_idx]
            day_title = f"{day_name} (сегодня)"
    else:
        day_name = day_name.lower().strip()
        day_title = day_name

    lessons = class_sched.get(day_name, [])
    if not lessons:
        return f"<b>{norm_class}</b> · {day_title.capitalize()}\n\nУроков нет."

    lines = [f"<b>{norm_class}</b> · {day_title.capitalize()}\n"]
    for item in lessons:
        cab_str = f" · каб. {item['cabinet']}" if item["cabinet"] else ""
        lines.append(f"<b>{item['lesson']}</b>. {item['subject']}{cab_str}")

    return "\n".join(lines)


def get_today_lessons_info(schedule_data: dict, class_name: str) -> dict:
    """
    Возвращает детальную информацию об уроках для класса на сегодня.
    В лицее 5-дневная учебная неделя (суббота и воскресенье — выходные).
    На выходных предоставляет статус выходного и список уроков на понедельник.
    """
    norm_class = normalize_class_name(class_name)
    classes = schedule_data.get("schedule", {})
    class_sched = classes.get(norm_class)

    if not class_sched:
        return {
            "found": False,
            "class_name": norm_class,
            "message": f"Класс {norm_class} не найден в расписании."
        }

    now_msk = datetime.now(MOSCOW)
    weekday_idx = now_msk.weekday()
    if weekday_idx >= 5:
        is_weekend = True
        day_name = RUSSIAN_DAYS[weekday_idx]
        today_lessons = []
    else:
        day_name = RUSSIAN_DAYS[weekday_idx]
        today_lessons = class_sched.get(day_name, [])
        is_weekend = (len(today_lessons) == 0)

    monday_lessons = class_sched.get("понедельник", [])

    return {
        "found": True,
        "class_name": norm_class,
        "weekday_idx": weekday_idx,
        "today_day_name": day_name,
        "today_lessons": today_lessons,
        "is_weekend": is_weekend,
        "monday_lessons": monday_lessons
    }


async def schedule_updater_loop(interval_hours: float = 12.0, sheet_url: str = DEFAULT_SHEET_URL):
    """
    Фоновый воркер: обновляет расписание каждые interval_hours (по умолчанию раз в 12 часов).
    """
    logger.info(f"Запущен периодический планировщик расписания (каждые {interval_hours} ч.)")
    while True:
        try:
            await fetch_and_update_schedule(sheet_url)
        except Exception as e:
            logger.error(f"Ошибка при периодическом обновлении расписания: {e}")

        # Спим 12 часов
        await asyncio.sleep(interval_hours * 3600)


if __name__ == "__main__":
    async def _test_main():
        print("=== ТЕСТ ПАРСЕРА РАСПИСАНИЯ 5-11 КЛАССОВ ===")
        data = await fetch_and_update_schedule(DEFAULT_SHEET_URL)
        print(f"Школа: {data['metadata']['school']}")
        print(f"Параллели: {data['metadata']['grades']}")
        print(f"Классы по параллелям: {data['metadata']['classes_by_grade']}")
        print(f"Всего классов: {len(data['metadata']['classes'])}")
        print(f"Обновлено: {data['metadata']['updated_at']}\n")

        for test_cls in ["5А", "8С", "10А", "11И"]:
            print(format_day_schedule(data, test_cls, "понедельник"))
            print("-" * 40)

    asyncio.run(_test_main())
