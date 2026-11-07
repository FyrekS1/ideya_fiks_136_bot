import json
import uuid
import random
import string
import aiosqlite

DB_NAME = "school_sync.db"

def generate_invite_code(grade: str) -> str:
    chars = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
    safe_grade = grade.replace(" ", "").upper()
    return f"{safe_grade}-{chars}"

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                full_name TEXT,
                role TEXT DEFAULT 'student',
                school TEXT,
                grade TEXT
            )
        """)
        # Миграция: добавляем full_name если таблица существовала без него
        try:
            await db.execute("ALTER TABLE users ADD COLUMN full_name TEXT")
        except Exception:
            pass

        await db.execute("""
            CREATE TABLE IF NOT EXISTS classes (
                invite_code TEXT PRIMARY KEY,
                school TEXT,
                grade TEXT,
                teacher_id TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS quizzes (
                id TEXT PRIMARY KEY,
                topic TEXT,
                quiz_data TEXT,
                teacher_id TEXT,
                deadline TEXT,
                is_active BOOLEAN DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS personal_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                note_text TEXT,
                is_processed BOOLEAN DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS group_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                school TEXT,
                grade TEXT,
                raw_text TEXT,
                is_processed BOOLEAN DEFAULT 0
            )
        """)
        # Попытки прохождения квизов (аналитика для учителя)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS quiz_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                quiz_id TEXT,
                user_id TEXT,
                student_name TEXT,
                score INTEGER,
                total_questions INTEGER,
                answers_json TEXT,
                passed_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Модуль «Спокойный день» для ученика (задачи, расписание, дедлайны)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS student_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                title TEXT,
                priority TEXT DEFAULT 'normal', -- 'urgent', 'normal', 'low'
                category TEXT DEFAULT 'task',
                is_completed BOOLEAN DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()

async def add_or_update_user(user_id: str, role: str, school: str = None, grade: str = None, full_name: str = None):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            INSERT INTO users (user_id, full_name, role, school, grade) 
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET 
                full_name=COALESCE(excluded.full_name, users.full_name),
                role=COALESCE(excluded.role, users.role), 
                school=COALESCE(excluded.school, users.school), 
                grade=COALESCE(excluded.grade, users.grade)
        """, (str(user_id), full_name, role, school, grade))
        await db.commit()

async def clear_user_class(user_id: str):
    """Сбрасывает класс и школу пользователя при намеренном выходе из класса"""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET school = NULL, grade = NULL WHERE user_id = ?", (str(user_id),))
        await db.commit()

async def get_user(user_id: str) -> dict | None:
    """Проверяет, зарегистрирован ли пользователь"""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT role, school, grade, full_name FROM users WHERE user_id = ?", (str(user_id),)) as cursor:
            row = await cursor.fetchone()
            if row:
                return {"role": row[0], "school": row[1], "grade": row[2], "full_name": row[3]}
    return None

async def create_class(teacher_id: str, school: str, grade: str) -> str:
    async with aiosqlite.connect(DB_NAME) as db:
        for _ in range(5):
            invite_code = generate_invite_code(grade)
            try:
                await db.execute(
                    "INSERT INTO classes (invite_code, school, grade, teacher_id) VALUES (?, ?, ?, ?)",
                    (invite_code, school, grade, str(teacher_id))
                )
                await db.commit()
                return invite_code
            except aiosqlite.IntegrityError:
                continue
        raise RuntimeError("Не удалось сгенерировать уникальный код класса.")

async def get_class_by_invite(invite_code: str) -> dict | None:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT school, grade FROM classes WHERE invite_code = ?", (invite_code,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return {"school": row[0], "grade": row[1]}
    return None

async def get_teacher_classes(teacher_id: str) -> list[dict]:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT invite_code, school, grade FROM classes WHERE teacher_id = ?", (teacher_id,)) as cursor:
            rows = await cursor.fetchall()
            return [{"invite_code": r[0], "school": r[1], "grade": r[2]} for r in rows]

async def save_quiz(topic: str, quiz_dict: dict, teacher_id: str, deadline: str = None) -> str:
    quiz_id = str(uuid.uuid4())[:8]
    final_topic = (topic or "").strip() or quiz_dict.get("title") or quiz_dict.get("topic") or "Квиз"
    quiz_json = json.dumps(quiz_dict, ensure_ascii=False)
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO quizzes (id, topic, quiz_data, teacher_id, deadline) VALUES (?, ?, ?, ?, ?)",
            (quiz_id, final_topic, quiz_json, teacher_id, deadline)
        )
        await db.commit()
    return quiz_id

async def get_quiz(quiz_id: str) -> dict | None:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT quiz_data, is_active, deadline, topic FROM quizzes WHERE id = ?", (quiz_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                if not row[1]: 
                    return {"error": "Этот квиз закрыт учителем."}
                data = json.loads(row[0])
                topic_str = (row[3] or "").strip() or data.get("title") or data.get("topic") or "Квиз"
                data["topic"] = topic_str
                data["title"] = topic_str
                return data
    return None

async def get_teacher_quizzes(teacher_id: str) -> list[dict]:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT id, topic, is_active, quiz_data FROM quizzes WHERE teacher_id = ?", (teacher_id,)) as cursor:
            rows = await cursor.fetchall()
            result = []
            for r in rows:
                topic = (r[1] or "").strip()
                if not topic and r[3]:
                    try:
                        qdata = json.loads(r[3])
                        topic = qdata.get("title") or qdata.get("topic") or "Квиз"
                    except Exception:
                        topic = "Квиз"
                result.append({"id": r[0], "topic": topic or "Квиз", "is_active": r[2]})
            return result

async def save_quiz_attempt(quiz_id: str, user_id: str, student_name: str, score: int, total_questions: int, answers: list[dict]) -> int:
    """Сохраняет результат прохождения квиза учеником и возвращает id попытки"""
    answers_json = json.dumps(answers, ensure_ascii=False)
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            """INSERT INTO quiz_attempts (quiz_id, user_id, student_name, score, total_questions, answers_json) 
               VALUES (?, ?, ?, ?, ?, ?)""",
            (quiz_id, user_id, student_name, score, total_questions, answers_json)
        )
        await db.commit()
        return cursor.lastrowid

async def get_quiz_attempt(attempt_id: int) -> dict | None:
    """Возвращает информацию о попытке прохождения квиза"""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            """SELECT id, quiz_id, user_id, student_name, score, total_questions, answers_json, passed_at 
               FROM quiz_attempts WHERE id = ?""",
            (attempt_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return {
                "id": row[0],
                "quiz_id": row[1],
                "user_id": row[2],
                "student_name": row[3],
                "score": row[4],
                "total_questions": row[5],
                "answers": json.loads(row[6]) if row[6] else [],
                "passed_at": row[7]
            }

async def get_quiz_attempts_for_export(quiz_id: str) -> list[dict]:
    """Возвращает список всех попыток по квизу для выгрузки в ведомость"""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            """SELECT a.id, a.user_id, a.student_name, u.grade, a.score, a.total_questions, a.passed_at 
               FROM quiz_attempts a 
               LEFT JOIN users u ON a.user_id = u.user_id 
               WHERE a.quiz_id = ? 
               ORDER BY a.student_name ASC, a.passed_at DESC""",
            (quiz_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                {
                    "id": r[0],
                    "user_id": r[1],
                    "student_name": r[2] or "Ученик",
                    "grade": r[3] or "—",
                    "score": r[4],
                    "total_questions": r[5],
                    "passed_at": r[6]
                }
                for r in rows
            ]


async def get_quiz_stats(quiz_id: str) -> dict | None:
    """Собирает аналитику по квизу для учителя: средний балл, список сдавших и карту слабых тем"""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT student_name, score, total_questions, answers_json FROM quiz_attempts WHERE quiz_id = ?",
            (quiz_id,)
        ) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        return None

    total_attempts = len(rows)
    total_score = sum(r[1] for r in rows)
    total_questions = rows[0][2] if rows else 0
    highest_score = max((r[1] for r in rows), default=0)
    avg_score = round(total_score / total_attempts, 1)

    # Карта слабых тем (подсчет ошибок по каждому вопросу)
    question_stats = {}  # q_idx -> {"text": ..., "errors": int, "total": int}
    students = []

    for name, score, total_q, ans_json in rows:
        students.append({"name": name or "Ученик", "score": score, "total": total_q})
        try:
            answers = json.loads(ans_json)
            for idx, ans in enumerate(answers):
                q_text = ans.get("question_text", f"Вопрос {idx + 1}")
                is_correct = ans.get("is_correct", False)
                if idx not in question_stats:
                    question_stats[idx] = {"text": q_text, "errors": 0, "total": 0}
                question_stats[idx]["total"] += 1
                if not is_correct:
                    question_stats[idx]["errors"] += 1
        except Exception:
            pass

    # Формируем слабые темы (процент ошибок > 30%)
    weak_topics = []
    for idx, stat in sorted(question_stats.items(), key=lambda x: x[1]["errors"], reverse=True):
        if stat["total"] > 0:
            err_percent = round((stat["errors"] / stat["total"]) * 100)
            weak_topics.append({
                "num": idx + 1,
                "text": stat["text"],
                "error_rate": err_percent,
                "errors_count": stat["errors"],
                "total_attempts": stat["total"]
            })

    return {
        "total_attempts": total_attempts,
        "avg_score": avg_score,
        "total_questions": total_questions,
        "max_score": total_questions,
        "highest_score": highest_score,
        "students": students,
        "weak_topics": weak_topics
    }

# ================= МОДУЛЬ: СПОКОЙНЫЙ ДЕНЬ =================
async def add_student_task(user_id: str, title: str, priority: str = "normal", category: str = "task") -> int:
    """Добавляет задачу ученика с приоритетом ('urgent', 'normal', 'low')"""
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "INSERT INTO student_tasks (user_id, title, priority, category) VALUES (?, ?, ?, ?)",
            (user_id, title, priority, category)
        )
        await db.commit()
        return cursor.lastrowid

async def get_student_tasks(user_id: str, only_active: bool = False) -> list[dict]:
    """Возвращает задачи ученика, отсортированные по приоритету (urgent -> normal -> low)"""
    query = """
        SELECT id, title, priority, category, is_completed, created_at 
        FROM student_tasks 
        WHERE user_id = ?
    """
    if only_active:
        query += " AND is_completed = 0"
    query += " ORDER BY CASE priority WHEN 'urgent' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END, id DESC"

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(query, (user_id,)) as cursor:
            rows = await cursor.fetchall()
            return [
                {
                    "id": r[0],
                    "title": r[1],
                    "priority": r[2],
                    "category": r[3],
                    "is_completed": bool(r[4]),
                    "created_at": r[5]
                }
                for r in rows
            ]

async def toggle_student_task(task_id: int, user_id: str = None) -> bool:
    """Переключает статус выполнения задачи с проверкой владельца"""
    async with aiosqlite.connect(DB_NAME) as db:
        if user_id is not None:
            query = "SELECT is_completed FROM student_tasks WHERE id = ? AND user_id = ?"
            params = (task_id, str(user_id))
        else:
            query = "SELECT is_completed FROM student_tasks WHERE id = ?"
            params = (task_id,)

        async with db.execute(query, params) as cursor:
            row = await cursor.fetchone()
            if not row:
                return False
            new_val = 0 if row[0] else 1
            await db.execute("UPDATE student_tasks SET is_completed = ? WHERE id = ?", (new_val, task_id))
            await db.commit()
            return bool(new_val)

async def get_student_day_summary(user_id: str) -> dict:
    """Возвращает сводку дня: выполнено, осталось, срочных задач"""
    tasks = await get_student_tasks(user_id)
    total = len(tasks)
    completed = sum(1 for t in tasks if t["is_completed"])
    pending_urgent = sum(1 for t in tasks if not t["is_completed"] and t["priority"] == "urgent")
    pending_normal = sum(1 for t in tasks if not t["is_completed"] and t["priority"] != "urgent")
    return {
        "total": total,
        "completed": completed,
        "pending": total - completed,
        "pending_urgent": pending_urgent,
        "pending_normal": pending_normal
    }

# ================= ЗАМЕТКИ =================
async def add_personal_note(user_id: str, note_text: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO personal_notes (user_id, note_text) VALUES (?, ?)", (user_id, note_text))
        await db.commit()

async def add_group_note(school: str, grade: str | None, raw_text: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO group_notes (school, grade, raw_text) VALUES (?, ?, ?)", (school, grade, raw_text))
        await db.commit()

async def mark_notes_processed(personal_ids: list[int] = None, group_ids: list[int] = None):
    """Помечает отправленные заметки как обработанные (is_processed = 1)"""
    async with aiosqlite.connect(DB_NAME) as db:
        if personal_ids:
            placeholders = ",".join("?" for _ in personal_ids)
            await db.execute(f"UPDATE personal_notes SET is_processed = 1 WHERE id IN ({placeholders})", personal_ids)
        if group_ids:
            placeholders = ",".join("?" for _ in group_ids)
            await db.execute(f"UPDATE group_notes SET is_processed = 1 WHERE id IN ({placeholders})", group_ids)
        await db.commit()
