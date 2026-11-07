"""Утренняя сводка в 07:00 МСК; остальные сценарии бота не затрагиваются."""

import os
import asyncio
import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from core.database import mark_notes_processed

MOSCOW = timezone(timedelta(hours=3), name="MSK")
DAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
logger = logging.getLogger(__name__)


def next_morning(now: datetime) -> datetime:
    now = now.astimezone(MOSCOW)
    target = now.replace(hour=7, minute=0, second=0, microsecond=0)
    return target if now < target else target + timedelta(days=1)


def _load_students(db_path: str):
    admin_id = os.getenv("ADMIN_ID", "43359625")
    with closing(sqlite3.connect(db_path)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute("""CREATE TABLE IF NOT EXISTS morning_deliveries (
            user_id TEXT NOT NULL, day TEXT NOT NULL, part INTEGER NOT NULL,
            PRIMARY KEY (user_id, day, part)
        )""")
        students = []
        for row in db.execute("""
            SELECT user_id, school, grade, full_name FROM users
            WHERE (role = 'student' OR grade IS NOT NULL) AND user_id != ?
        """, (admin_id,)):
            student = dict(row)
            user_id = row["user_id"]
            school = row["school"]
            grade = row["grade"]

            student["tasks"] = [r[0] for r in db.execute("""
                SELECT title FROM student_tasks WHERE user_id = ? AND is_completed = 0
                ORDER BY CASE priority WHEN 'urgent' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END, id DESC
            """, (user_id,))]

            p_notes = db.execute("""
                SELECT id, note_text FROM personal_notes WHERE user_id = ? AND is_processed = 0 ORDER BY id
            """, (user_id,)).fetchall()
            student["personal_note_ids"] = [r[0] for r in p_notes]
            student["notes"] = [r[1] for r in p_notes]

            g_notes = db.execute("""
                SELECT id, raw_text FROM group_notes
                WHERE school = ? AND (grade = ? OR grade IS NULL) AND is_processed = 0 ORDER BY id
            """, (school, grade)).fetchall() if grade else []
            student["group_note_ids"] = [r[0] for r in g_notes]
            student["group_notes"] = [r[1] for r in g_notes]

            students.append(student)
        return students


def _delivered(db_path: str, user_id: str, day: str, part: int, *, save: bool = False) -> bool:
    with closing(sqlite3.connect(db_path)) as db, db:
        key = (str(user_id), day, part)
        if save:
            db.execute("INSERT OR IGNORE INTO morning_deliveries VALUES (?, ?, ?)", key)
            return True
        return db.execute(
            "SELECT 1 FROM morning_deliveries WHERE user_id = ? AND day = ? AND part = ?", key
        ).fetchone() is not None


def build_message(student: dict, lessons: list[dict], now: datetime, is_preview: bool = False) -> str:
    now = now.astimezone(MOSCOW)
    preview_tag = " (тест рассылки на понедельник)" if is_preview else ""
    grade = student.get("grade")

    if grade:
        lines = [f"Доброе утро! {now:%d.%m.%Y} · {DAYS[now.weekday()]}{preview_tag}",
                 f"Класс {grade}", "", "Расписание на сегодня:"]
        if lessons:
            for lesson in lessons:
                cabinet = f" · каб. {lesson['cabinet']}" if lesson.get("cabinet") else ""
                lines.append(f"{lesson['lesson']}. {lesson['subject']}{cabinet}")
        else:
            lines.append("Уроков нет.")
    else:
        lines = [
            f"Доброе утро! {now:%d.%m.%Y} · {DAYS[now.weekday()]}{preview_tag}",
            "",
            "Класс пока не указан. Чтобы каждое утро получать расписание уроков, выберите ваш класс в главном меню бота."
        ]

    lines.extend(["", "Напоминания на день:"])
    has_reminders = False
    for key, title in (("tasks", "Невыполненные задачи:"), ("notes", "Личные заметки:"),
                       ("group_notes", "Заметки класса:")):
        if student.get(key):
            has_reminders = True
            lines.append(title)
            bullet = "✗" if key == "tasks" else "•"
            lines.extend(f"{bullet} {text}" for text in student[key])
    if not has_reminders:
        lines.append("Напоминаний нет.")
    return "\n".join(lines)


async def send_morning_notifications(bot, schedule: dict, now: datetime, db_path: str = "school_sync.db", force: bool = False) -> dict:
    from core.schedule_parser import normalize_class_name

    now = now.astimezone(MOSCOW)

    # В не-force режиме рассылка работает строго в 07:00 и только в учебные дни (пн-пт, 5 дней обучения)
    if not force:
        if (now.hour, now.minute) != (7, 0):
            return {"sent": 0, "recipients": [], "errors": []}
        if now.weekday() >= 5:  # Суббота (5) или Воскресенье (6) — выходные
            logger.info("Утренняя рассылка пропущена: выходной день (5-дневная учебная неделя)")
            return {"sent": 0, "recipients": [], "errors": []}

    day = now.date().isoformat()
    students = await asyncio.to_thread(_load_students, db_path)
    sent_recipients = []
    errors = []

    for student in students:
        try:
            raw_grade = student.get("grade")
            grade = normalize_class_name(raw_grade) if raw_grade else None
            day_name = DAYS[now.weekday()]
            lessons = []
            is_preview = False

            if grade:
                lessons = schedule.get("schedule", {}).get(grade, {}).get(day_name, [])
                # Если сегодня выходной, но вызван принудительный запуск (force=True)
                if not lessons and force:
                    lessons = schedule.get("schedule", {}).get(grade, {}).get("понедельник", [])
                    is_preview = True

                # В обычном режиме: если уроков нет — пропускаем
                if not force and not lessons:
                    continue
            else:
                # Нет класса: если нет задач и не принудительный запуск — пропускаем
                if not force and not student.get("tasks") and not student.get("notes"):
                    continue

            text = build_message(student, lessons, now, is_preview=is_preview)
            # Простое текстовое сообщение (до 3500 символов в части)
            for part, offset in enumerate(range(0, len(text), 3500)):
                if not force and await asyncio.to_thread(_delivered, db_path, student["user_id"], day, part):
                    continue
                sent = await asyncio.wait_for(bot.send_message(
                    user_id=int(student["user_id"]), text=text[offset:offset + 3500]
                ), timeout=30)
                if sent is None:
                    raise RuntimeError("API не подтвердил отправку сообщения")
                if not force:
                    await asyncio.to_thread(_delivered, db_path, student["user_id"], day, part, save=True)
                # Rate limit MAX API: не более 2 сообщ/сек (0.55с интервал)
                await asyncio.sleep(0.55)

            # Помечаем отправленные заметки как обработанные
            if not force:
                p_ids = student.get("personal_note_ids", [])
                g_ids = student.get("group_note_ids", [])
                if p_ids or g_ids:
                    await mark_notes_processed(p_ids, g_ids)

            name = student.get("full_name") or student["user_id"]
            grade_str = f" ({grade})" if grade else " (класс не выбран)"
            sent_recipients.append(f"{name}{grade_str} [ID: {student['user_id']}]")
        except Exception as exc:
            logger.exception("Не удалось отправить утреннюю сводку ученику %s", student["user_id"])
            errors.append(f"{student.get('full_name') or student['user_id']}: {exc}")

    return {
        "sent": len(sent_recipients),
        "recipients": sent_recipients,
        "errors": errors
    }


async def morning_notifications_loop(bot):
    from core.database import DB_NAME
    from core.schedule_parser import get_schedule

    logger.info("Утренняя рассылка включена: 07:00 МСК, понедельник — пятница (5 дней обучения)")
    while True:
        now = datetime.now(MOSCOW)
        if (now.hour, now.minute) == (7, 0):
            try:
                schedule = await get_schedule()
                await send_morning_notifications(bot, schedule, datetime.now(MOSCOW), DB_NAME)
            except Exception:
                logger.exception("Ошибка утренней рассылки")
            # Спим 65 секунд, чтобы гарантированно выйти из минуты 07:00
            await asyncio.sleep(65)
        else:
            await asyncio.sleep(max(0.1, (next_morning(now) - now).total_seconds()))
