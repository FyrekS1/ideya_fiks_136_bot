import os
import re
import csv
import asyncio
import logging
from datetime import datetime
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

from maxapi import Bot, Dispatcher, F
from maxapi.enums.format import Format
from maxapi.enums import UploadType
from maxapi.types import MessageCreated, Command, MessageCallback, BotStarted, InputMedia
from maxapi.context import MemoryContext, State, StatesGroup
from maxapi.types.attachments.buttons import CallbackButton, ClipboardButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from core.ai_work import create_quiz, generate_student_error_review, generate_teacher_lesson_plan
from core.file_parser import extract_materials_from_message
from core.database import (
    init_db, save_quiz, get_quiz, add_or_update_user, get_user,
    add_personal_note, add_group_note, create_class, get_class_by_invite,
    get_teacher_classes, get_teacher_quizzes,
    save_quiz_attempt, get_quiz_stats, get_quiz_attempt, get_quiz_attempts_for_export,
    add_student_task, get_student_tasks, toggle_student_task, get_student_day_summary,
    clear_user_class
)
from core.schedule_parser import (
    get_schedule, format_day_schedule, schedule_updater_loop,
    normalize_class_name, fetch_and_update_schedule, get_today_lessons_info
)
from core.morning_notifications import morning_notifications_loop, send_morning_notifications

logging.basicConfig(level=logging.INFO)
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Добавь BOT_TOKEN в файл .env")

ADMIN_ID = "43359625"
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
BOT_LINK = os.getenv("BOT_LINK", "https://max.ru/ideya_fiks_136_bot")
_PROCESSING_LOCKS = set()
_COMPLETED_REVIEWS = set()

# ================= МАШИНЫ СОСТОЯНИЙ =================
class RegFSM(StatesGroup):
    waiting_for_name = State()
    waiting_for_role = State()
    waiting_for_grade = State()

class TeacherQuizFSM(StatesGroup):
    waiting_for_topic = State()
    collecting_materials = State()
    waiting_for_count = State()

class TeacherClassFSM(StatesGroup):
    waiting_for_school = State()
    waiting_for_grade = State()

class StudentReminderFSM(StatesGroup):
    waiting_for_text = State()
    waiting_for_type = State()

class StudentQuizFSM(StatesGroup):
    waiting_for_id = State()
    passing_quiz = State()
    
class StudentClassFSM(StatesGroup):
    waiting_for_code = State()

class StudentTaskFSM(StatesGroup):
    waiting_for_title = State()
    waiting_for_priority = State()

# ================= ХЕЛПЕРЫ =================
def get_user_id(event):
    """Надежное получение ID пользователя из любого события"""
    if hasattr(event, "from_user") and event.from_user:
        return getattr(event.from_user, "user_id", None) or getattr(event.from_user, "id", None)
    if hasattr(event, "user") and event.user:
        return getattr(event.user, "user_id", None) or getattr(event.user, "id", None)
    if hasattr(event, "message") and event.message and hasattr(event.message, "sender"):
        return getattr(event.message.sender, "user_id", None) or getattr(event.message.sender, "id", None)
    return None

def extract_text(event: MessageCreated) -> str:
    """Безопасно извлекает текст сообщения (возвращает пустую строку, если текста нет)."""
    body = getattr(event.message, "body", None)
    if not body:
        return ""
    text = getattr(body, "text", None)
    return (text or "").strip()

async def send_event_message(event, text: str, attachments=None, format=Format.HTML):
    """Безопасная отправка сообщения для любых событий: MessageCreated, MessageCallback, BotStarted."""
    if hasattr(event, "message") and event.message and hasattr(event.message, "answer"):
        return await event.message.answer(text, attachments=attachments, format=format)
    chat_id = getattr(event, "chat_id", None)
    if not chat_id and hasattr(event, "chat") and event.chat:
        chat_id = getattr(event.chat, "id", None) or getattr(event.chat, "chat_id", None)
    if not chat_id and hasattr(event, "message") and event.message:
        chat_id = getattr(event.message, "chat_id", None)
    user_id = get_user_id(event)
    bot_inst = getattr(event, "bot", None) or bot
    if chat_id:
        return await bot_inst.send_message(chat_id=chat_id, text=text, attachments=attachments, format=format)
    elif user_id:
        return await bot_inst.send_message(user_id=int(user_id), text=text, attachments=attachments, format=format)

async def show_main_menu(event, user: dict):
    """Показывает чистое и компактное главное меню"""
    builder = InlineKeyboardBuilder()
    full_name = user.get("full_name") or ("Учитель" if user["role"] == "teacher" else "Ученик")
    user_id = str(get_user_id(event))
    
    if user["role"] == "teacher":
        builder.row(
            CallbackButton(text="+ Создать квиз", payload="menu_create_quiz"),
            CallbackButton(text="⌕ Мои квизы", payload="menu_my_quizzes")
        )
        builder.row(
            CallbackButton(text="웃 Классы", payload="menu_classes"),
            CallbackButton(text="◫ Расписание", payload="menu_schedule")
        )
        if user_id == ADMIN_ID:
            builder.row(
                CallbackButton(text="⚡ Утренняя рассылка", payload="admin_morning_broadcast")
            )
        builder.row(
            CallbackButton(text="⇆ Режим ученика", payload="menu_switch_role")
        )
        text = (
            f"<b>Школа.Синхрон</b> · Учитель\n"
            f"{full_name} · Лицей № 44\n\n"
            f"Управление квизами, аналитика и расписание:"
        )
    else:
        grade = user.get("grade")
        grade_badge = f" · {grade}" if grade else ""
        class_btn_text = f"웃 Класс {grade}" if grade else "웃 Вступить в класс"
        class_payload = "menu_my_class" if grade else "menu_join_class"
        
        builder.row(
            CallbackButton(text="? Пройти квиз", payload="menu_solve_quizzes"),
            CallbackButton(text="✓ План на день", payload="menu_day_plan")
        )
        builder.row(
            CallbackButton(text="◫ Расписание", payload="menu_schedule"),
            CallbackButton(text="⚑ Заметка", payload="menu_add_reminder")
        )
        builder.row(
            CallbackButton(text="◑ Итоги дня", payload="menu_day_summary"),
            CallbackButton(text=class_btn_text, payload=class_payload)
        )
        if user_id == ADMIN_ID:
            builder.row(
                CallbackButton(text="⚡ Утренняя рассылка", payload="admin_morning_broadcast")
            )
        builder.row(
            CallbackButton(text="⇆ Режим учителя", payload="menu_switch_role")
        )
        text = (
            f"<b>Школа.Синхрон</b> · Ученик\n"
            f"{full_name}{grade_badge} · Лицей № 44\n\n"
            f"Учебные материалы, расписание и задачи:"
        )
        
    await send_event_message(event, text, attachments=[builder.as_markup()], format=Format.HTML)

# ================= ПЕРЕХОД ПО ССЫЛКЕ (ONBOARDING) =================
@dp.bot_started()
async def on_bot_started(event: BotStarted, context: MemoryContext):
    """Срабатывает, когда юзер жмет 'Начать' после перехода по ссылке"""
    user_id = get_user_id(event)
    raw_payload = getattr(event, "payload", None)
    payload = str(raw_payload).strip() if raw_payload else None

    action = None
    if payload:
        action = "join" if "-" in payload else "pass"

    user = await get_user(user_id) if user_id else None

    # Если юзер новый — запускаем регистрацию с сохранением целевого действия
    if not user:
        await context.clear()
        await context.update_data(pending_action=action, pending_payload=payload)
        await context.set_state(RegFSM.waiting_for_name)
        await send_event_message(
            event,
            "<b>Школа.Синхрон</b> · Лицей № 44\n\nДобро пожаловать! Введите ваши фамилию и имя для регистрации:",
            format=Format.HTML
        )
        return

    # Если юзер уже зарегистрирован и есть payload — сразу выполняем целевое действие
    if action == "join" and payload:
        await execute_join(event, context, payload, user)
    elif action == "pass" and payload:
        await execute_pass(event, context, payload)
    else:
        await context.clear()
        await show_main_menu(event, user)

@dp.message_created(Command(["start", "join", "pass"]))
async def cmd_router(event: MessageCreated, context: MemoryContext):
    """Обрабатывает входы в бота, инвайт-коды и прохождение квизов"""
    text = extract_text(event)
    if not text:
        return
    parts = text.split()
    cmd = parts[0].lower()
    payload = parts[1] if len(parts) > 1 else None

    action = None
    if cmd == "/join": action = "join"
    elif cmd == "/pass": action = "pass"
    elif cmd == "/start" and payload:
        action = "join" if "-" in payload else "pass"

    user_id = get_user_id(event)
    user = await get_user(user_id)

    # Если юзер новый — запускаем регистрацию с выбором роли
    if not user:
        await context.clear()
        await context.update_data(pending_action=action, pending_payload=payload)
        await context.set_state(RegFSM.waiting_for_name)
        await send_event_message(
            event,
            "<b>Школа.Синхрон</b> · Лицей № 44\n\nВведите фамилию и имя для регистрации:",
            format=Format.HTML
        )
        return

    # Выполняем запрошенное действие
    if action == "join" and payload:
        await execute_join(event, context, payload, user)
    elif action == "pass" and payload:
        await execute_pass(event, context, payload)
    else:
        await context.clear()
        await show_main_menu(event, user)

async def execute_join(event, context, code: str, user: dict):
    cls = await get_class_by_invite(code)
    if cls:
        await add_or_update_user(get_user_id(event), user["role"], cls["school"], cls["grade"], full_name=user.get("full_name"))
        user["school"] = cls["school"]
        user["grade"] = cls["grade"]
        await send_event_message(event, f"Подключение выполнено: класс <b>{cls['grade']}</b> ({cls['school']})", format=Format.HTML)
    else:
        await send_event_message(event, "Класс по указанному коду не найден. Проверьте код и повторите попытку.")
    await context.clear()
    await show_main_menu(event, user)

async def execute_pass(event, context, quiz_id: str):
    quiz = await get_quiz(quiz_id)
    if not quiz or "error" in quiz:
        await send_event_message(event, "Квиз не найден или закрыт учителем.")
        user = await get_user(get_user_id(event))
        if user:
            await show_main_menu(event, user)
        return
    await start_quiz_session(event, context, quiz_id, quiz)

# ================= РЕГИСТРАЦИЯ И СМЕНА РОЛИ =================
@dp.message_created(RegFSM.waiting_for_name)
async def process_name(event: MessageCreated, context: MemoryContext):
    full_name = extract_text(event)
    if not full_name:
        await event.message.answer("Пожалуйста, введите ваше имя и фамилию текстом:")
        return
    await context.update_data(full_name=full_name)
    await context.set_state(RegFSM.waiting_for_role)
    
    builder = InlineKeyboardBuilder()
    builder.row(
        CallbackButton(text="• Ученик", payload="reg_role_student"),
        CallbackButton(text="• Учитель", payload="reg_role_teacher")
    )
    await event.message.answer(
        f"<b>{full_name}</b>\nВыберите статус для настройки доступа:",
        attachments=[builder.as_markup()],
        format=Format.HTML
    )

@dp.message_callback(F.callback.payload.startswith("reg_role_"), RegFSM.waiting_for_role)
async def process_role_choice(event: MessageCallback, context: MemoryContext):
    role = event.callback.payload.replace("reg_role_", "")
    user_id = get_user_id(event)
    data = await context.get_data()
    full_name = data.get("full_name", "Пользователь")

    if role == "student":
        await add_or_update_user(user_id=user_id, role="student", full_name=full_name)
        await context.set_state(RegFSM.waiting_for_grade)
        await context.update_data(role="student")

        builder = InlineKeyboardBuilder()
        builder.row(
            CallbackButton(text="5 класс", payload="reg_par_5"),
            CallbackButton(text="6 класс", payload="reg_par_6"),
            CallbackButton(text="7 класс", payload="reg_par_7")
        )
        builder.row(
            CallbackButton(text="8 класс", payload="reg_par_8"),
            CallbackButton(text="9 класс", payload="reg_par_9")
        )
        builder.row(
            CallbackButton(text="10 класс", payload="reg_par_10"),
            CallbackButton(text="11 класс", payload="reg_par_11")
        )
        builder.row(CallbackButton(text="→ Пропустить", payload="reg_grade_skip"))

        await event.answer()
        await event.message.answer(
            f"<b>Выбор класса</b> · Лицей № 44\n\n"
            f"{full_name}, выберите вашу параллель:",
            attachments=[builder.as_markup()],
            format=Format.HTML
        )
        return

    # Если учитель
    await add_or_update_user(user_id=user_id, role="teacher", full_name=full_name, school="МБОУ Лицей № 44")
    user = await get_user(user_id)
    await event.answer()
    await event.message.answer(
        f"<b>Регистрация завершена</b>\n{full_name} · Учитель (Лицей № 44)",
        format=Format.HTML
    )

    action = data.get("pending_action")
    payload = data.get("pending_payload")
    if action == "join" and payload:
        await execute_join(event, context, payload, user)
    elif action == "pass" and payload:
        await execute_pass(event, context, payload)
    else:
        await context.clear()
        await show_main_menu(event, user)

@dp.message_callback(F.callback.payload.startswith("reg_par_"), RegFSM.waiting_for_grade)
async def process_reg_parallel(event: MessageCallback, context: MemoryContext):
    par = event.callback.payload.replace("reg_par_", "")
    schedule_data = await get_schedule()
    classes = schedule_data.get("metadata", {}).get("classes_by_grade", {}).get(par, [])
    if not classes:
        classes = [c for c in schedule_data.get("metadata", {}).get("classes", []) if c.startswith(par)]

    builder = InlineKeyboardBuilder()
    cls_btns = [CallbackButton(text=c, payload=f"reg_grade_{c}") for c in classes]
    chunk = 4 if len(classes) >= 8 else 3
    for i in range(0, len(cls_btns), chunk):
        builder.row(*cls_btns[i:i + chunk])
    builder.row(
        CallbackButton(text="← К параллелям", payload="reg_back_par"),
        CallbackButton(text="→ Пропустить", payload="reg_grade_skip")
    )

    await event.answer()
    await event.message.answer(
        f"<b>Параллель {par} классов</b> · Лицей № 44\n\nВыберите ваш класс:",
        attachments=[builder.as_markup()],
        format=Format.HTML
    )

@dp.message_callback(F.callback.payload == "reg_back_par", RegFSM.waiting_for_grade)
async def process_reg_back_parallel(event: MessageCallback, context: MemoryContext):
    builder = InlineKeyboardBuilder()
    builder.row(
        CallbackButton(text="5 класс", payload="reg_par_5"),
        CallbackButton(text="6 класс", payload="reg_par_6"),
        CallbackButton(text="7 класс", payload="reg_par_7")
    )
    builder.row(
        CallbackButton(text="8 класс", payload="reg_par_8"),
        CallbackButton(text="9 класс", payload="reg_par_9")
    )
    builder.row(
        CallbackButton(text="10 класс", payload="reg_par_10"),
        CallbackButton(text="11 класс", payload="reg_par_11")
    )
    builder.row(CallbackButton(text="→ Пропустить", payload="reg_grade_skip"))

    await event.answer()
    await event.message.answer(
        "<b>Выбор класса</b> · Лицей № 44\n\nВыберите вашу параллель:",
        attachments=[builder.as_markup()],
        format=Format.HTML
    )

@dp.message_callback(F.callback.payload.startswith("reg_grade_"), RegFSM.waiting_for_grade)
async def process_reg_grade(event: MessageCallback, context: MemoryContext):
    grade_choice = event.callback.payload.replace("reg_grade_", "")
    user_id = get_user_id(event)
    data = await context.get_data()
    full_name = data.get("full_name", "Ученик")

    if grade_choice == "skip":
        grade = None
        school = None
        status_msg = "Класс можно выбрать в любое время в главном меню."
    else:
        grade = grade_choice
        school = "МБОУ Лицей № 44"
        status_msg = f"Класс: <b>{grade}</b> ({school}).\nРасписание уроков подключено."

    await add_or_update_user(user_id=user_id, role="student", school=school, grade=grade, full_name=full_name)
    user = await get_user(user_id)

    await event.answer()
    await event.message.answer(
        f"<b>Регистрация завершена</b>\n{status_msg}",
        format=Format.HTML
    )

    action = data.get("pending_action")
    payload = data.get("pending_payload")
    if action == "join" and payload:
        await execute_join(event, context, payload, user)
    elif action == "pass" and payload:
        await execute_pass(event, context, payload)
    else:
        await context.clear()
        await show_main_menu(event, user)

@dp.message_created(RegFSM.waiting_for_grade)
async def process_reg_grade_text(event: MessageCreated, context: MemoryContext):
    raw_text = extract_text(event)
    if not raw_text:
        await event.message.answer("Пожалуйста, укажите ваш класс текстом (например: 9А):")
        return
    norm_grade = normalize_class_name(raw_text)
    user_id = get_user_id(event)
    data = await context.get_data()
    full_name = data.get("full_name", "Ученик")

    school = "МБОУ Лицей № 44"
    await add_or_update_user(user_id=user_id, role="student", school=school, grade=norm_grade, full_name=full_name)
    user = await get_user(user_id)

    await event.message.answer(
        f"✅ <b>Регистрация завершена!</b>\nТвой класс: <b>{norm_grade}</b> ({school}).\nРасписание уроков подключено автоматически!",
        format=Format.HTML
    )

    action = data.get("pending_action")
    payload = data.get("pending_payload")
    if action == "join" and payload:
        await execute_join(event, context, payload, user)
    elif action == "pass" and payload:
        await execute_pass(event, context, payload)
    else:
        await context.clear()
        await show_main_menu(event, user)

@dp.message_callback(F.callback.payload == "menu_switch_role")
async def btn_switch_role(event: MessageCallback, context: MemoryContext):
    user_id = get_user_id(event)
    user = await get_user(user_id)
    if not user:
        return
        
    new_role = "teacher" if user["role"] == "student" else "student"
    await add_or_update_user(user_id=user_id, role=new_role, school=user["school"], grade=user["grade"], full_name=user.get("full_name"))
    user["role"] = new_role
    
    await context.clear()
    new_title = "Учитель" if new_role == "teacher" else "Ученик"
    await event.answer(f"Роль изменена: {new_title}")
    await event.message.answer(f"Роль изменена: <b>{new_title}</b>", format=Format.HTML)
    await show_main_menu(event, user)

@dp.message_created(Command("role"))
async def cmd_switch_role(event: MessageCreated, context: MemoryContext):
    user_id = get_user_id(event)
    user = await get_user(user_id)
    if not user:
        return
        
    new_role = "teacher" if user["role"] == "student" else "student"
    await add_or_update_user(user_id=user_id, role=new_role, school=user["school"], grade=user["grade"], full_name=user.get("full_name"))
    user["role"] = new_role
    
    await context.clear()
    new_title = "Учитель" if new_role == "teacher" else "Ученик"
    await event.message.answer(f"Роль изменена: <b>{new_title}</b>", format=Format.HTML)
    await show_main_menu(event, user)

# ================= УЧИТЕЛЬ: КЛАССЫ =================
@dp.message_callback(F.callback.payload == "menu_classes")
async def show_teacher_classes(event: MessageCallback, context: MemoryContext):
    classes = await get_teacher_classes(get_user_id(event))
    builder = InlineKeyboardBuilder()
    
    text = "<b>Классы</b>\n\n"
    if not classes:
        text += "Созданных классов пока нет."
    else:
        for c in classes:
            text += f"Класс <b>{c['grade']}</b> · {c['school']}\n"
            text += f"Код: <code>{c['invite_code']}</code>\n"
            text += f"Ссылка: {BOT_LINK}?start={c['invite_code']}\n\n"
            
    builder.row(
        CallbackButton(text="+ Создать класс", payload="btn_create_class"),
        CallbackButton(text="← В главное меню", payload="menu_back")
    )
    
    await event.answer()
    await event.message.answer(text, attachments=[builder.as_markup()], format=Format.HTML)

@dp.message_callback(F.callback.payload == "btn_create_class")
async def process_create_class_school(event: MessageCallback, context: MemoryContext):
    await context.set_state(TeacherClassFSM.waiting_for_school)
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="← В главное меню", payload="menu_back"))
    await event.answer()
    await event.message.answer("Укажите номер или название школы (например: Лицей № 44):", attachments=[builder.as_markup()])

@dp.message_created(TeacherClassFSM.waiting_for_school)
async def process_create_class_grade(event: MessageCreated, context: MemoryContext):
    school = extract_text(event)
    await context.update_data(school=school)
    await context.set_state(TeacherClassFSM.waiting_for_grade)
    await event.message.answer("Укажите параллель и литеру (например: 9А или 10Б):")

@dp.message_created(TeacherClassFSM.waiting_for_grade)
async def finish_create_class(event: MessageCreated, context: MemoryContext):
    data = await context.get_data()
    grade = extract_text(event)
    code = await create_class(get_user_id(event), data.get("school", "Лицей № 44"), grade)
    await context.clear()
    
    builder = InlineKeyboardBuilder()
    builder.row(ClipboardButton(text="📋 Скопировать код", payload=code))
    builder.row(CallbackButton(text="⌂ В главное меню", payload="menu_back"))
    await event.message.answer(
        f"Класс <b>{grade}</b> успешно создан.\n\n"
        f"Данные для подключения учеников:\n"
        f"──────────────\n"
        f"Код: <code>{code}</code>\n"
        f"Ссылка: {BOT_LINK}?start={code}", 
        attachments=[builder.as_markup()], format=Format.HTML
    )

# ================= УЧИТЕЛЬ: КВИЗЫ И АНАЛИТИКА =================
@dp.message_callback(F.callback.payload == "menu_my_quizzes")
async def show_teacher_quizzes(event: MessageCallback, context: MemoryContext):
    quizzes = await get_teacher_quizzes(get_user_id(event))
    builder = InlineKeyboardBuilder()
    
    text = "<b>Квизы и аналитика</b>\n\n"
    if not quizzes:
        text += "Созданных квизов пока нет. Создайте первый квиз из материалов урока."
    else:
        text += "Выберите квиз для просмотра аналитики и карты слабых тем:\n\n"
        for q in quizzes:
            status = "✓ Активен" if q['is_active'] else "▫️ Закрыт"
            text += f"• <b>{q['topic']}</b> ({status}) — код: <code>{q['id']}</code>\n"
            builder.row(CallbackButton(text=f"• {q['topic'][:22]}", payload=f"quiz_stats_{q['id']}"))
            
    builder.row(
        CallbackButton(text="+ Новый квиз", payload="menu_create_quiz"),
        CallbackButton(text="← В главное меню", payload="menu_back")
    )
    await event.answer()
    await event.message.answer(text, attachments=[builder.as_markup()], format=Format.HTML)

@dp.message_callback(F.callback.payload.startswith("quiz_stats_"))
async def show_quiz_statistics(event: MessageCallback, context: MemoryContext):
    quiz_id = event.callback.payload.replace("quiz_stats_", "")
    quiz = await get_quiz(quiz_id)
    topic = ((quiz.get("title") or quiz.get("topic") or "").strip() or "Квиз") if quiz else "Квиз"
    stats = await get_quiz_stats(quiz_id)
    
    builder = InlineKeyboardBuilder()
    
    await event.answer()
    if not stats:
        builder.row(
            CallbackButton(text="← К списку квизов", payload="menu_my_quizzes"),
            CallbackButton(text="⌂ В главное меню", payload="menu_back")
        )
        await event.message.answer(
            f"<b>Аналитика · {topic}</b> (код: <code>{quiz_id}</code>)\n\n"
            "Пока никто из учеников не прошел этот квиз.\n\n"
            f"Ссылка для учеников: {BOT_LINK}?start={quiz_id}",
            attachments=[builder.as_markup()],
            format=Format.HTML
        )
        return

    text = (
        f"<b>Аналитика · {topic}</b>\n\n"
        f"• Сдали: <b>{stats['total_attempts']}</b> учеников\n"
        f"• Средний балл: <b>{stats['avg_score']} из {stats['max_score']}</b>\n\n"
        f"<b>Результаты учеников:</b>\n"
    )
    for s in stats["students"][:15]:
        text += f"• {s['name']}: <b>{s['score']}/{s['total']}</b>\n"
    if len(stats["students"]) > 15:
        text += f"... и еще {len(stats['students']) - 15} учеников\n"

    text += "\n<b>Проблемные темы:</b>\n"
    if not stats["weak_topics"]:
        text += "Все вопросы решены без систематических ошибок.\n"
    else:
        has_critical = False
        for wt in stats["weak_topics"]:
            if wt["error_rate"] > 0:
                warning = "⚠️" if wt["error_rate"] >= 40 else "•"
                if wt["error_rate"] >= 40:
                    has_critical = True
                short_text = wt['text'][:55] + "..." if len(wt['text']) > 55 else wt['text']
                text += f"{warning} <b>Вопрос {wt['num']}</b> ({wt['error_rate']}% ошибок): <i>«{short_text}»</i>\n"
        if has_critical:
            text += "\n<i>Вопросы с отметкой ⚠️ вызвали наибольшие затруднения — рекомендуется разобрать их в начале урока.</i>\n"

    builder.row(
        CallbackButton(text="✦ План урока (ИИ)", payload=f"quiz_lplan_{quiz_id}"),
        CallbackButton(text="↓ Ведомость (CSV)", payload=f"quiz_export_{quiz_id}")
    )
    builder.row(
        CallbackButton(text="← К списку квизов", payload="menu_my_quizzes"),
        CallbackButton(text="⌂ В главное меню", payload="menu_back")
    )

    await event.message.answer(text, attachments=[builder.as_markup()], format=Format.HTML)

@dp.message_callback(F.callback.payload.startswith("quiz_lplan_"))
async def handle_quiz_lesson_plan(event: MessageCallback, context: MemoryContext):
    quiz_id = event.callback.payload.replace("quiz_lplan_", "")
    user_id = get_user_id(event)
    lock_key = f"lplan_{quiz_id}_{user_id}"

    if lock_key in _PROCESSING_LOCKS:
        await event.answer("План уже генерируется, подождите.")
        return
    _PROCESSING_LOCKS.add(lock_key)

    await event.answer()
    
    try:
        quiz = await get_quiz(quiz_id)
        topic = quiz.get("topic") if quiz else "Квиз"
        stats = await get_quiz_stats(quiz_id)
        
        builder = InlineKeyboardBuilder()
        builder.row(CallbackButton(text="← К аналитике", payload=f"quiz_stats_{quiz_id}"))
        builder.row(CallbackButton(text="⌂ В главное меню", payload="menu_back"))

        if not stats or not stats.get("weak_topics"):
            return await event.message.answer(
                f"<b>{topic}</b>: 100% результат. Все темы усвоены, повторение не требуется.",
                attachments=[builder.as_markup()],
                format=Format.HTML
            )
            
        await event.message.answer(
            "Формирование экспресс-сценария урока на основе ошибок класса...",
            format=Format.HTML
        )
        
        plan_text = await generate_teacher_lesson_plan(topic, stats["weak_topics"])
        await event.message.answer(
            f"<b>Экспресс-сценарий разбора · {topic}</b>\n\n{plan_text}",
            attachments=[builder.as_markup()],
            format=Format.HTML
        )
    finally:
        _PROCESSING_LOCKS.discard(lock_key)

@dp.message_callback(F.callback.payload.startswith("quiz_export_"))
async def handle_quiz_export(event: MessageCallback, context: MemoryContext):
    quiz_id = event.callback.payload.replace("quiz_export_", "")
    user_id = get_user_id(event)
    lock_key = f"export_{quiz_id}_{user_id}"

    if lock_key in _PROCESSING_LOCKS:
        await event.answer("Ведомость уже формируется, подождите.")
        return
    _PROCESSING_LOCKS.add(lock_key)

    await event.answer()
    
    try:
        quiz = await get_quiz(quiz_id)
        topic = quiz.get("topic") if quiz else "Квиз"
        attempts = await get_quiz_attempts_for_export(quiz_id)
        
        builder = InlineKeyboardBuilder()
        builder.row(CallbackButton(text="← К аналитике", payload=f"quiz_stats_{quiz_id}"))
        builder.row(CallbackButton(text="⌂ В главное меню", payload="menu_back"))

        if not attempts:
            return await event.message.answer(
                "Пока никто из учеников не прошел этот квиз. Нечего выгружать.",
                attachments=[builder.as_markup()]
            )
        
        grade_counts = {5: 0, 4: 0, 3: 0, 2: 0}
        rows_data = []
        
        for idx, att in enumerate(attempts, 1):
            score = att["score"]
            total = att["total_questions"] or 1
            pct = round((score / total) * 100)
            
            if pct >= 85:
                mark = 5
            elif pct >= 70:
                mark = 4
            elif pct >= 50:
                mark = 3
            else:
                mark = 2
                
            grade_counts[mark] += 1
            rows_data.append({
                "num": idx,
                "name": att["student_name"],
                "grade": att["grade"],
                "score": f"{score}/{total}",
                "pct": f"{pct}%",
                "mark": mark,
                "passed_at": att["passed_at"]
            })
            
        csv_filename = f"vedomost_{quiz_id}.csv"
        csv_path = os.path.join("/tmp", csv_filename)
        
        with open(csv_path, mode="w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f, delimiter=";")
            writer.writerow(["№", "ФИО Ученика", "Класс", "Баллы", "Процент", "Оценка", "Дата сдачи"])
            for r in rows_data:
                writer.writerow([r["num"], r["name"], r["grade"], r["score"], r["pct"], r["mark"], r["passed_at"]])
                
        summary_text = (
            f"<b>Ведомость результатов · {topic}</b>\n"
            f"Код квиза: <code>{quiz_id}</code>\n"
            f"Всего сдало: <b>{len(attempts)}</b> учеников\n\n"
            f"<b>Распределение оценок:</b>\n"
            f"• «5» (отлично): <b>{grade_counts[5]}</b>\n"
            f"• «4» (хорошо): <b>{grade_counts[4]}</b>\n"
            f"• «3» (удовлетворительно): <b>{grade_counts[3]}</b>\n"
            f"• «2» (неудовлетворительно): <b>{grade_counts[2]}</b>\n\n"
            f"<b>Список учеников:</b>\n"
        )
        for r in rows_data[:15]:
            summary_text += f"{r['num']}. {r['name']} ({r['grade']}): <b>{r['score']}</b> ({r['pct']}) — <b>Оценка {r['mark']}</b>\n"
        if len(rows_data) > 15:
            summary_text += f"... и еще {len(rows_data) - 15} учеников в прикрепленном файле.\n"
            
        summary_text += "\n<i>Файл CSV готов для импорта в электронный журнал или Excel.</i>"

        try:
            media_file = InputMedia(path=csv_path, type=UploadType.FILE)
            await event.message.answer(
                f"Файл ведомости: <code>{csv_filename}</code>",
                attachments=[media_file]
            )
        except Exception as e:
            logging.warning(f"Could not upload CSV attachment: {e}")

        await event.message.answer(summary_text, attachments=[builder.as_markup()], format=Format.HTML)
    finally:
        _PROCESSING_LOCKS.discard(lock_key)

@dp.message_created(Command("create"))
async def cmd_create(event: MessageCreated, context: MemoryContext):
    text = extract_text(event)
    parts = text.split(maxsplit=1)
    await context.update_data(materials=[])
    
    if len(parts) > 1:
        await context.update_data(topic=parts[1])
        await start_collecting_materials(event, context)
    else:
        await context.set_state(TeacherQuizFSM.waiting_for_topic)
        builder = InlineKeyboardBuilder()
        builder.row(CallbackButton(text="← В главное меню", payload="menu_back"))
        await event.message.answer("Укажите тему квиза (например: «Законы Ньютона» или «Квадратные уравнения»):", attachments=[builder.as_markup()])

@dp.message_callback(F.callback.payload == "menu_create_quiz")
async def start_quiz_creation(event: MessageCallback, context: MemoryContext):
    await context.update_data(materials=[])
    await context.set_state(TeacherQuizFSM.waiting_for_topic)
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="← В главное меню", payload="menu_back"))
    await event.answer()
    await event.message.answer("Укажите тему квиза (например: «Строение клетки» или «Квадратные уравнения»):", attachments=[builder.as_markup()])

@dp.message_created(TeacherQuizFSM.waiting_for_topic)
async def process_quiz_topic(event: MessageCreated, context: MemoryContext):
    topic = extract_text(event)
    if not topic:
        await event.message.answer("Пожалуйста, укажите тему квиза текстом:")
        return
    await context.update_data(topic=topic)
    await start_collecting_materials(event, context)

async def start_collecting_materials(event, context: MemoryContext):
    await context.set_state(TeacherQuizFSM.collecting_materials)
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="✓ Готово (без материалов)", payload="quiz_done_materials"))
    
    msg = await event.message.answer(
        "Отправьте материалы урока текстом или файлами <b>PDF / DOCX</b> (до 10 шт).\nКогда все готово — нажмите «Готово».",
        attachments=[builder.as_markup()],
        format=Format.HTML
    )
    
    last_id = getattr(msg.message, "message_id", getattr(msg, "message_id", None)) if hasattr(msg, "message") else getattr(msg, "message_id", None)
    await context.update_data(last_done_btn_id=last_id)

@dp.message_created(TeacherQuizFSM.collecting_materials)
async def collect_materials(event: MessageCreated, context: MemoryContext):
    data = await context.get_data()
    materials = data.get("materials", [])
    last_btn_msg = data.get("last_done_btn_id")
    
    if last_btn_msg:
        try:
            chat_id = event.message.chat.chat_id if hasattr(event, "message") else event.chat.chat_id
            await event.bot.delete_message(chat_id=chat_id, message_id=last_btn_msg)
        except Exception:
            pass

    added_items = []

    # 1. Проверяем вложенные файлы (PDF, DOCX, TXT)
    if event.message.body and event.message.body.attachments:
        try:
            extracted_files = await extract_materials_from_message(event.bot, event.message)
            for fname, text_content in extracted_files:
                materials.append(f"[Файл {fname}]:\n{text_content}")
                added_items.append(f"Файл «{fname}» ({len(text_content)} симв.)")
        except ValueError as ve:
            await event.message.answer(f"{ve}\nОтправьте файл в PDF/DOCX или скопируйте текст в чат.")
            return
        except Exception as e:
            logging.error(f"Ошибка загрузки файла: {e}")
            await event.message.answer(f"Не удалось прочитать вложение: {e}. Попробуйте отправить текст сообщения.")
            return

    # 2. Проверяем текст сообщения
    text = extract_text(event)
    if text:
        materials.append(text)
        short_preview = text[:30] + "..." if len(text) > 30 else text
        added_items.append(f"Текст: «{short_preview}»")

    if not added_items:
        await event.message.answer("Текст или файл не распознан. Отправьте PDF, DOCX или обычный текст.")
        return

    await context.update_data(materials=materials)
    
    status_summary = "\n".join(f"• {item}" for item in added_items)
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text=f"✓ Готово ({len(materials)}/10)", payload="quiz_done_materials"))
    
    msg = await event.message.answer(
        f"Материалы добавлены:\n{status_summary}\n\nВсего: <b>{len(materials)}/10</b>. Отправьте еще или нажмите «Готово».",
        attachments=[builder.as_markup()],
        format=Format.HTML
    )
    
    last_id = getattr(msg.message, "message_id", getattr(msg, "message_id", None)) if hasattr(msg, "message") else getattr(msg, "message_id", None)
    await context.update_data(last_done_btn_id=last_id)

@dp.message_callback(F.callback.payload == "quiz_done_materials", TeacherQuizFSM.collecting_materials)
async def ask_question_count(event: MessageCallback, context: MemoryContext):
    await context.set_state(TeacherQuizFSM.waiting_for_count)
    builder = InlineKeyboardBuilder()
    builder.row(
        CallbackButton(text="• 3 вопроса", payload="count_3"),
        CallbackButton(text="• 5 вопросов", payload="count_5"),
        CallbackButton(text="• 10 вопросов", payload="count_10")
    )
    await event.answer()
    await event.message.answer("Количество вопросов:", attachments=[builder.as_markup()])

@dp.message_callback(F.callback.payload.startswith("count_"), TeacherQuizFSM.waiting_for_count)
async def process_count_btn(event: MessageCallback, context: MemoryContext):
    count = event.callback.payload.split("_")[1]
    await context.update_data(q_count=count)
    await event.answer()
    await generate_final_quiz(event, context)

@dp.message_created(TeacherQuizFSM.waiting_for_count)
async def process_count_txt(event: MessageCreated, context: MemoryContext):
    count = extract_text(event)
    if not count.isdigit():
        return await event.message.answer("Пожалуйста, укажите число.")
    await context.update_data(q_count=count)
    await generate_final_quiz(event, context)

async def generate_final_quiz(event, context: MemoryContext):
    data = await context.get_data()
    topic = data.get("topic", "Без темы")
    mats = "\n".join(data.get("materials", []))
    count = data.get("q_count", 5)
    
    prompt = f"Тема: {topic}\nМатериалы: {mats}\nСделай ровно {count} вопросов."
    await event.message.answer(f"Формирование квиза ({count} вопр.)... Обычно это занимает 10–20 секунд.", format=Format.HTML)
    
    user_id = get_user_id(event)
    user = await get_user(user_id)
    logging.info(f"Generating quiz: user={user_id}, topic='{topic}', count={count}")
    
    await context.update_data(is_generating=True)
    try:
        quiz_data = await create_quiz(prompt)
        quiz_id = await save_quiz(topic, quiz_data, teacher_id=user_id)
        logging.info(f"Quiz successfully saved: id={quiz_id}")
        builder = InlineKeyboardBuilder()
        builder.row(ClipboardButton(text="📋 Скопировать код", payload=quiz_id))
        builder.row(CallbackButton(text="⌂ В главное меню", payload="menu_back"))
        await event.message.answer(
            f"Квиз <b>«{topic}»</b> готов.\n\n"
            f"Сообщение для учеников:\n"
            f"──────────────\n"
            f"Квиз: <b>{topic}</b>\n"
            f"Код: <code>{quiz_id}</code>\n"
            f"Ссылка: {BOT_LINK}?start={quiz_id}",
            attachments=[builder.as_markup()],
            format=Format.HTML
        )
        await context.clear()
        await show_main_menu(event, user)
    except Exception as e:
        logging.exception(f"Quiz generation error: {e}")
        await context.update_data(is_generating=False)
        builder = InlineKeyboardBuilder()
        builder.row(CallbackButton(text="🔄 Повторить генерацию", payload="quiz_retry"))
        builder.row(CallbackButton(text="⌂ В главное меню", payload="menu_back"))
        await event.message.answer(
            "⚠️ <b>Временная ошибка нейросети при формировании квиза.</b>\n\n"
            "Сервер ИИ был перегружен или не ответил вовремя.\n\n"
            "Вы можете нажать <b>«🔄 Повторить генерацию»</b>, чтобы попробовать снова с теми же материалами, или вернуться в главное меню.",
            attachments=[builder.as_markup()],
            format=Format.HTML
        )

@dp.message_callback(F.callback.payload == "quiz_retry")
async def retry_quiz_generation(event: MessageCallback, context: MemoryContext):
    await event.answer()
    data = await context.get_data()
    if not data or not data.get("topic"):
        user = await get_user(get_user_id(event))
        await context.clear()
        await event.message.answer("Данные предыдущего запроса устарели. Пожалуйста, создайте квиз заново через меню.")
        return await show_main_menu(event, user)

    if data.get("is_generating"):
        return await event.message.answer("⏳ Генерация уже выполняется, пожалуйста, подождите...")

    await generate_final_quiz(event, context)


# ================= УЧЕНИК: ПРОХОЖДЕНИЕ КВИЗА =================
@dp.message_callback(F.callback.payload == "menu_solve_quizzes")
async def ask_for_quiz_id(event: MessageCallback, context: MemoryContext):
    await context.set_state(StudentQuizFSM.waiting_for_id)
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="← В главное меню", payload="menu_back"))
    await event.answer()
    await event.message.answer("Введите 8-значный код квиза:", attachments=[builder.as_markup()])

@dp.message_created(StudentQuizFSM.waiting_for_id)
async def process_quiz_id_manual(event: MessageCreated, context: MemoryContext):
    code = extract_text(event)
    if code:
        await execute_pass(event, context, code)

async def start_quiz_session(event, context: MemoryContext, quiz_id: str, quiz: dict):
    await context.set_state(StudentQuizFSM.passing_quiz)
    await context.update_data(
        quiz_id=quiz_id,
        q_idx=0,
        score=0,
        answers=[],
        is_answering=False,
        answered_questions=[]
    )
    title = quiz.get("title") or quiz.get("topic") or "Квиз"
    await send_event_message(event, f"Квиз: <b>{title}</b>\nВопросов: <b>{len(quiz['questions'])}</b>", format=Format.HTML)
    await send_quiz_question(event, quiz, 0)

async def send_quiz_question(event, quiz: dict, q_idx: int):
    question = quiz["questions"][q_idx]
    builder = InlineKeyboardBuilder()
    
    for i, opt in enumerate(question["options"]):
        safe_btn = opt["text"][:37] + "..." if len(opt["text"]) > 40 else opt["text"]
        builder.row(CallbackButton(text=safe_btn, payload=f"qopt_{q_idx}_{i}"))
        
    text = f"<b>Вопрос {q_idx + 1} из {len(quiz['questions'])}</b>\n\n{question['question_text']}"
    await send_event_message(event, text, attachments=[builder.as_markup()], format=Format.HTML)

@dp.message_callback(F.callback.payload.startswith("qopt_"), StudentQuizFSM.passing_quiz)
async def handle_quiz_answer(event: MessageCallback, context: MemoryContext):
    data = await context.get_data()
    q_idx = data.get("q_idx", 0)
    
    parts = event.callback.payload.split("_")
    if len(parts) >= 3:
        try:
            target_q_idx = int(parts[1])
            selected_idx = int(parts[2])
        except ValueError:
            target_q_idx = q_idx
            selected_idx = 0
    else:
        target_q_idx = q_idx
        selected_idx = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        
    answered_questions = data.get("answered_questions", [])
    if data.get("is_answering") or target_q_idx != q_idx or target_q_idx in answered_questions:
        await event.answer("Ответ на этот вопрос уже принят.")
        return

    # Lock answer processing to avoid double clicks
    await context.update_data(is_answering=True)
    
    quiz = await get_quiz(data.get("quiz_id"))
    if not quiz or "questions" not in quiz or q_idx >= len(quiz["questions"]):
        await event.answer("Квиз не найден.")
        await context.clear()
        return

    opt = quiz["questions"][q_idx]["options"][selected_idx]
    is_correct = opt["is_correct"]

    score = data.get("score", 0)
    answers = data.get("answers", [])
    if is_correct:
        score += 1
    
    answers.append({
        "question_text": quiz["questions"][q_idx]["question_text"],
        "is_correct": is_correct,
        "selected": opt["text"]
    })

    await event.answer()

    # Удаляем сообщение с вопросом
    try:
        await event.message.delete()
    except Exception as e:
        logging.debug(f"Could not delete question message: {e}")

    explanation = opt.get("explanation", "").strip()
    clean_explanation = re.sub(
        r"^(?:✅|❌)?\s*(?:верно|правильно|неверно|ошибка|неправильно)[!.,:\s-]*",
        "",
        explanation,
        flags=re.IGNORECASE
    ).strip()

    is_last = (q_idx + 1 >= len(quiz["questions"]))
    builder = InlineKeyboardBuilder()
    btn_text = "Завершить квиз →" if is_last else "Следующий вопрос →"
    builder.row(CallbackButton(text=btn_text, payload=f"quiz_next_{q_idx + 1}"))

    if is_correct:
        feedback_text = "<b>Верно</b>"
        if clean_explanation:
            feedback_text += f"\n\n{clean_explanation}"
    else:
        q_text = quiz["questions"][q_idx]["question_text"]
        feedback_text = f"<b>Неверно</b>\n\n{q_text}\n\n"
        if clean_explanation:
            feedback_text += clean_explanation
        else:
            feedback_text += f"Выбранный вариант «{opt['text']}» неверен."

    new_answered = answered_questions + [target_q_idx]
    await context.update_data(
        q_idx=q_idx + 1,
        score=score,
        answers=answers,
        answered_questions=new_answered,
        is_answering=False
    )

    await event.message.answer(feedback_text, attachments=[builder.as_markup()], format=Format.HTML)

@dp.message_callback(F.callback.payload.startswith("quiz_next_"), StudentQuizFSM.passing_quiz)
async def handle_quiz_next_step(event: MessageCallback, context: MemoryContext):
    await event.answer()

    # При переходе к следующему вопросу удаляем сообщение с объяснением
    try:
        await event.message.delete()
    except Exception as e:
        logging.debug(f"Could not delete feedback message on next step: {e}")

    data = await context.get_data()
    q_idx = data.get("q_idx", 0)
    quiz = await get_quiz(data.get("quiz_id"))

    if not quiz or "questions" not in quiz:
        await event.message.answer("Квиз не найден.")
        await context.clear()
        return

    if q_idx < len(quiz["questions"]):
        await send_quiz_question(event, quiz, q_idx)
    else:
        user_id = get_user_id(event)
        user = await get_user(user_id)
        student_name = user.get("full_name") or "Ученик"
        score = data.get("score", 0)
        answers = data.get("answers", [])
        
        attempt_id = await save_quiz_attempt(
            quiz_id=data["quiz_id"],
            user_id=user_id,
            student_name=student_name,
            score=score,
            total_questions=len(quiz["questions"]),
            answers=answers
        )
        
        builder = InlineKeyboardBuilder()
        if score < len(quiz["questions"]):
            builder.row(CallbackButton(text="✦ Разбор ошибок (ИИ)", payload=f"quiz_review_{attempt_id}"))
        builder.row(CallbackButton(text="⌂ В главное меню", payload="menu_back"))

        pct = round((score / len(quiz["questions"])) * 100)
        await event.message.answer(
            f"<b>Квиз завершен</b>\n\n"
            f"Результат: <b>{score} из {len(quiz['questions'])}</b> ({pct}%)\n"
            f"Ответы сохранены и переданы учителю.",
            attachments=[builder.as_markup()],
            format=Format.HTML
        )
        await context.clear()

@dp.message_callback(F.callback.payload.startswith("quiz_review_"))
async def handle_student_quiz_review(event: MessageCallback, context: MemoryContext):
    attempt_id_str = event.callback.payload.replace("quiz_review_", "")
    user_id = get_user_id(event)
    lock_key = f"review_{attempt_id_str}_{user_id}"

    if lock_key in _COMPLETED_REVIEWS:
        await event.answer("Персональный разбор для этого квиза уже сформирован.")
        return

    if lock_key in _PROCESSING_LOCKS:
        await event.answer("Разбор уже генерируется, подождите.")
        return
    _PROCESSING_LOCKS.add(lock_key)

    await event.answer("Формируем разбор ошибок...")

    try:
        try:
            attempt_id = int(attempt_id_str)
        except ValueError:
            return await event.message.answer("Ошибка: неверный идентификатор попытки.")
            
        attempt = await get_quiz_attempt(attempt_id)
        if not attempt:
            return await event.message.answer("Результаты попытки не найдены.")
            
        quiz = await get_quiz(attempt["quiz_id"])
        topic = ((quiz.get("title") or quiz.get("topic") or "").strip() or "Квиз") if quiz else "Квиз"
        wrong_answers = [ans for ans in attempt.get("answers", []) if not ans.get("is_correct")]
        
        builder = InlineKeyboardBuilder()
        builder.row(CallbackButton(text="⌂ В главное меню", payload="menu_back"))

        if not wrong_answers:
            _COMPLETED_REVIEWS.add(lock_key)
            return await event.message.answer(
                "Ошибок в этом квизе нет. Все ответы верны.",
                attachments=[builder.as_markup()]
            )
            
        await event.message.answer(
            "Формирование персонального разбора ошибок...",
            format=Format.HTML
        )
        
        review_text = await generate_student_error_review(topic, wrong_answers)
        msg_payload = f"<b>Разбор ошибок · {topic}</b>\n\n{review_text}"

        try:
            await event.message.answer(
                msg_payload,
                attachments=[builder.as_markup()],
                format=Format.HTML
            )
            _COMPLETED_REVIEWS.add(lock_key)
        except Exception as send_err:
            logging.warning(f"Failed to send review as HTML: {send_err}. Falling back to plain text.")
            plain_text = re.sub(r"<[^>]+>", "", msg_payload)
            await event.message.answer(
                plain_text,
                attachments=[builder.as_markup()]
            )
            _COMPLETED_REVIEWS.add(lock_key)

        # После успешной отправки обновляем предыдущее сообщение
        try:
            builder_done = InlineKeyboardBuilder()
            builder_done.row(CallbackButton(text="⌂ В главное меню", payload="menu_back"))
            await event.message.edit(attachments=[builder_done.as_markup()])
        except Exception:
            pass

    except Exception as e:
        logging.error(f"Error in handle_student_quiz_review: {e}")
        builder_retry = InlineKeyboardBuilder()
        builder_retry.row(
            CallbackButton(text="↻ Повторить разбор", payload=f"quiz_review_{attempt_id_str}"),
            CallbackButton(text="⌂ В главное меню", payload="menu_back")
        )
        await event.message.answer(
            "Не удалось сформировать разбор от ИИ в данный момент. Нажмите кнопку ниже, чтобы попробовать снова.",
            attachments=[builder_retry.as_markup()]
        )
    finally:
        _PROCESSING_LOCKS.discard(lock_key)

@dp.message_callback(F.callback.payload.startswith("qopt_") | F.callback.payload.startswith("quiz_next_"))
async def handle_stale_quiz_answer(event: MessageCallback, context: MemoryContext):
    await event.answer("Этот вопрос уже не активен или квиз завершен.")

# ================= УЧЕНИК: МОДУЛЬ «СПОКОЙНЫЙ ДЕНЬ» =================
@dp.message_callback(F.callback.payload == "menu_day_plan")
async def cb_student_day_plan(event: MessageCallback, context: MemoryContext):
    await event.answer()
    await show_student_day_plan(event, context)

@dp.message_created(Command(["plan", "today"]))
async def cmd_student_day_plan(event: MessageCreated, context: MemoryContext):
    await show_student_day_plan(event, context)

async def show_student_day_plan(event, context: MemoryContext):
    user_id = get_user_id(event)
    user = await get_user(user_id) if user_id else None
    grade = user.get("grade") if user else None
    grade_str = f" · {grade}" if grade else ""
    
    builder = InlineKeyboardBuilder()
    text = f"<b>План на день</b>{grade_str}\n\n"

    # 1. Расписание уроков на сегодня / ближайший учебный день
    text += "<b>Уроки:</b>\n"
    if grade:
        try:
            sched_data = await get_schedule()
            info = get_today_lessons_info(sched_data, grade)
            if info.get("found"):
                if info["is_weekend"]:
                    today_name = info["today_day_name"].capitalize()
                    text += f"Сегодня {today_name} — уроков нет (выходной).\n"
                    next_lessons = info.get("monday_lessons", [])
                    if next_lessons:
                        text += "<b>Ближайшие уроки в понедельник:</b>\n"
                        for l in next_lessons:
                            cab = f" · каб. {l['cabinet']}" if l.get("cabinet") else ""
                            text += f"• <b>{l['lesson']}</b>. {l['subject']}{cab}\n"
                else:
                    today_name = info["today_day_name"].capitalize()
                    text += f"<b>{today_name}:</b>\n"
                    for l in info["today_lessons"]:
                        cab = f" · каб. {l['cabinet']}" if l.get("cabinet") else ""
                        text += f"• <b>{l['lesson']}</b>. {l['subject']}{cab}\n"
            else:
                text += f"Расписание для {grade} временно недоступно.\n"
        except Exception as e:
            logging.error(f"Error fetching schedule for day plan: {e}")
            text += "Не удалось загрузить расписание уроков.\n"
    else:
        text += "Класс не выбран. Укажите свой класс в меню, чтобы здесь автоматически отображались уроки.\n"
    
    text += "\n"

    # 2. Задачи и дедлайны
    text += "<b>Задачи и дедлайны:</b>\n"
    tasks = await get_student_tasks(user_id) if user_id else []
    if not tasks:
        text += "<i>Задач пока нет. Добавьте задание или контрольную через «+ Новая задача».</i>\n"
    else:
        active_tasks = [t for t in tasks if not t["is_completed"]]
        completed_tasks = [t for t in tasks if t["is_completed"]]
        
        if active_tasks:
            urgent_tasks = [t for t in active_tasks if t["priority"] == "urgent"]
            normal_tasks = [t for t in active_tasks if t["priority"] == "normal"]
            low_tasks = [t for t in active_tasks if t["priority"] == "low"]
            
            if urgent_tasks:
                text += "<b>Срочно и важно:</b>\n"
                for t in urgent_tasks:
                    text += f"✗ {t['title']}\n"
                text += "\n"
                
            if normal_tasks:
                text += "<b>Текущие задания:</b>\n"
                for t in normal_tasks:
                    text += f"✗ {t['title']}\n"
                text += "\n"
                
            if low_tasks:
                text += "<b>Мелкие дела:</b>\n"
                for t in low_tasks:
                    text += f"✗ {t['title']}\n"
                text += "\n"
        else:
            text += "<i>Все текущие задачи выполнены!</i>\n\n"

        if completed_tasks:
            text += "<b>Выполнено:</b>\n"
            for t in completed_tasks[-5:]:
                text += f"✓ <s>{t['title']}</s>\n"
            text += "\n"
            
        for t in active_tasks[:3]:
            btn_title = t["title"][:20] + "..." if len(t["title"]) > 20 else t["title"]
            builder.row(CallbackButton(text=f"✓ Выполнено: {btn_title}", payload=f"task_toggle_{t['id']}"))

        if completed_tasks:
            last_done = completed_tasks[-1]
            btn_title = last_done["title"][:18] + "..." if len(last_done["title"]) > 18 else last_done["title"]
            builder.row(CallbackButton(text=f"↺ Вернуть: {btn_title}", payload=f"task_toggle_{last_done['id']}"))

    builder.row(
        CallbackButton(text="+ Новая задача", payload="task_add"),
        CallbackButton(text="◑ Итоги дня", payload="menu_day_summary")
    )
    builder.row(
        CallbackButton(text="◫ Все расписание", payload="menu_schedule"),
        CallbackButton(text="← В главное меню", payload="menu_back")
    )
    
    markup = builder.as_markup()
    if isinstance(event, MessageCallback):
        try:
            await event.message.edit(text=text, attachments=[markup], format=Format.HTML)
            return
        except Exception as e:
            logging.debug(f"Could not edit message, falling back to answer: {e}")
            
    await event.message.answer(text, attachments=[markup], format=Format.HTML)

@dp.message_callback(F.callback.payload.startswith("task_toggle_"))
async def toggle_task_btn(event: MessageCallback, context: MemoryContext):
    task_id = int(event.callback.payload.replace("task_toggle_", ""))
    user_id = str(get_user_id(event))
    is_done = await toggle_student_task(task_id, user_id=user_id)
    msg = "Задача выполнена" if is_done else "Задача возвращена в работу"
    await event.answer(msg)
    await show_student_day_plan(event, context)

@dp.message_callback(F.callback.payload == "menu_day_summary")
async def show_day_summary(event: MessageCallback, context: MemoryContext):
    user_id = get_user_id(event)
    user = await get_user(user_id)
    summary = await get_student_day_summary(user_id)
    
    builder = InlineKeyboardBuilder()
    builder.row(
        CallbackButton(text="← К плану дня", payload="menu_day_plan"),
        CallbackButton(text="⌂ В главное меню", payload="menu_back")
    )
    
    name = f", {user.get('full_name')}" if user and user.get("full_name") else ""
    text = (
        f"<b>Итоги дня{name}</b>\n\n"
        f"• Выполнено сегодня: <b>{summary['completed']}</b> из <b>{summary['total']}</b> задач\n"
        f"• Осталось: <b>{summary['pending']}</b> (срочных: <b>{summary['pending_urgent']}</b>)\n\n"
        "<i>План на завтра сформирован. Задачи сохранены.</i>"
    )
    await event.answer()
    await event.message.answer(text, attachments=[builder.as_markup()], format=Format.HTML)

@dp.message_callback(F.callback.payload == "task_add")
async def start_add_task(event: MessageCallback, context: MemoryContext):
    await context.set_state(StudentTaskFSM.waiting_for_title)
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="← Отмена", payload="menu_day_plan"))
    await event.answer()
    await event.message.answer("Укажите название задачи или дедлайна (например: «Подготовиться к контрольной по алгебре»):", attachments=[builder.as_markup()])

@dp.message_created(StudentTaskFSM.waiting_for_title)
async def process_task_title(event: MessageCreated, context: MemoryContext):
    title = extract_text(event)
    if not title:
        await event.message.answer("Пожалуйста, введите название задачи текстом:")
        return
    await context.update_data(task_title=title)
    await context.set_state(StudentTaskFSM.waiting_for_priority)
    
    builder = InlineKeyboardBuilder()
    builder.row(
        CallbackButton(text="! Срочно", payload="tpri_urgent"),
        CallbackButton(text="• Обычная", payload="tpri_normal"),
        CallbackButton(text="· В фоне", payload="tpri_low")
    )
    
    await event.message.answer(f"Задача: <b>{title}</b>\nВыберите приоритет:", attachments=[builder.as_markup()], format=Format.HTML)

@dp.message_callback(F.callback.payload.startswith("tpri_"), StudentTaskFSM.waiting_for_priority)
async def process_task_priority(event: MessageCallback, context: MemoryContext):
    priority = event.callback.payload.replace("tpri_", "")
    data = await context.get_data()
    title = data.get("task_title", "Задача")
    user_id = get_user_id(event)
    
    await add_student_task(user_id, title, priority=priority)
    await context.clear()
    await event.answer("Задача добавлена в план дня.")
    await show_student_day_plan(event, context)

# ================= УЧЕНИК: КЛАСС =================
@dp.message_callback(F.callback.payload == "menu_join_class")
async def join_class_btn(event: MessageCallback, context: MemoryContext):
    await context.set_state(StudentClassFSM.waiting_for_code)
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="Выбрать класс Лицея № 44", payload="change_class_par"))
    builder.row(CallbackButton(text="← В главное меню", payload="menu_back"))
    await event.answer()
    await event.message.answer(
        "Введите код приглашения в класс (например: 9А-XXXX)\nили выберите ваш класс из списка школы:",
        attachments=[builder.as_markup()]
    )

@dp.message_created(StudentClassFSM.waiting_for_code)
async def process_join_class(event: MessageCreated, context: MemoryContext):
    user_id = get_user_id(event)
    user = await get_user(user_id)
    code = extract_text(event)
    if code:
        await execute_join(event, context, code, user)

@dp.message_callback(F.callback.payload == "menu_my_class")
async def menu_my_class(event: MessageCallback, context: MemoryContext):
    user = await get_user(get_user_id(event))
    grade = user.get("grade", "")
    school = user.get("school", "Лицей № 44")
    builder = InlineKeyboardBuilder()
    builder.row(
        CallbackButton(text="◫ Расписание", payload=f"sched_class_{grade}" if grade else "menu_schedule"),
        CallbackButton(text="⇄ Сменить класс", payload="change_class_par")
    )
    builder.row(
        CallbackButton(text="⎋ Выйти из класса", payload="leave_class"),
        CallbackButton(text="← В главное меню", payload="menu_back")
    )
    await event.answer()
    await event.message.answer(f"Класс: <b>{grade}</b> ({school})", attachments=[builder.as_markup()], format=Format.HTML)

@dp.message_callback(F.callback.payload == "change_class_par")
async def change_class_pick_par(event: MessageCallback, context: MemoryContext):
    builder = InlineKeyboardBuilder()
    builder.row(
        CallbackButton(text="5 класс", payload="change_class_p_5"),
        CallbackButton(text="6 класс", payload="change_class_p_6"),
        CallbackButton(text="7 класс", payload="change_class_p_7")
    )
    builder.row(
        CallbackButton(text="8 класс", payload="change_class_p_8"),
        CallbackButton(text="9 класс", payload="change_class_p_9")
    )
    builder.row(
        CallbackButton(text="10 класс", payload="change_class_p_10"),
        CallbackButton(text="11 класс", payload="change_class_p_11")
    )
    builder.row(CallbackButton(text="← В главное меню", payload="menu_back"))
    await event.answer()
    await event.message.answer(
        "<b>Выбор класса</b> · Лицей № 44\n\nВыберите вашу параллель:",
        attachments=[builder.as_markup()],
        format=Format.HTML
    )

@dp.message_callback(F.callback.payload.startswith("change_class_p_"))
async def change_class_pick_letter(event: MessageCallback, context: MemoryContext):
    par = event.callback.payload.replace("change_class_p_", "")
    schedule_data = await get_schedule()
    classes = schedule_data.get("metadata", {}).get("classes_by_grade", {}).get(par, [])
    if not classes:
        classes = [c for c in schedule_data.get("metadata", {}).get("classes", []) if c.startswith(par)]

    builder = InlineKeyboardBuilder()
    cls_btns = [CallbackButton(text=c, payload=f"set_user_class_{c}") for c in classes]
    chunk = 4 if len(classes) >= 8 else 3
    for i in range(0, len(cls_btns), chunk):
        builder.row(*cls_btns[i:i + chunk])
    builder.row(
        CallbackButton(text="← К параллелям", payload="change_class_par"),
        CallbackButton(text="⌂ В меню", payload="menu_back")
    )
    await event.answer()
    await event.message.answer(
        f"<b>Параллель {par} классов</b> · Лицей № 44\n\nВыберите ваш класс:",
        attachments=[builder.as_markup()],
        format=Format.HTML
    )

@dp.message_callback(F.callback.payload.startswith("set_user_class_"))
async def set_user_class(event: MessageCallback, context: MemoryContext):
    cls_name = event.callback.payload.replace("set_user_class_", "")
    user_id = get_user_id(event)
    user = await get_user(user_id)
    school = "МБОУ Лицей № 44"
    await add_or_update_user(user_id, "student", school=school, grade=cls_name, full_name=user.get("full_name"))
    
    builder = InlineKeyboardBuilder()
    builder.row(
        CallbackButton(text="◫ Посмотреть расписание", payload=f"sched_class_{cls_name}"),
        CallbackButton(text="⌂ В главное меню", payload="menu_back")
    )
    await event.answer("Класс сохранен.")
    await event.message.answer(
        f"✅ Твой класс успешно обновлен: <b>{cls_name}</b> ({school}).\nРасписание подключено!",
        attachments=[builder.as_markup()],
        format=Format.HTML
    )

@dp.message_callback(F.callback.payload == "leave_class")
async def leave_class(event: MessageCallback, context: MemoryContext):
    user_id = get_user_id(event)
    user = await get_user(user_id)
    await clear_user_class(user_id)
    if user:
        user["school"] = None
        user["grade"] = None
    await event.answer("Вы вышли из класса.")
    await show_main_menu(event, user)

# ================= НАПОМИНАНИЯ И ЗАМЕТКИ =================
@dp.message_callback(F.callback.payload == "menu_add_reminder")
async def btn_add_reminder(event: MessageCallback, context: MemoryContext):
    await context.set_state(StudentReminderFSM.waiting_for_text)
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="← В главное меню", payload="menu_back"))
    await event.answer()
    await event.message.answer("Введите текст заметки или напоминания:", attachments=[builder.as_markup()])

@dp.message_created(StudentReminderFSM.waiting_for_text)
async def process_reminder_text(event: MessageCreated, context: MemoryContext):
    rem_text = extract_text(event)
    if not rem_text:
        await event.message.answer("Пожалуйста, введите текст заметки:")
        return
    await context.update_data(rem_text=rem_text)
    await context.set_state(StudentReminderFSM.waiting_for_type)
    
    builder = InlineKeyboardBuilder()
    builder.row(
        CallbackButton(text="• Только мне", payload="rem_personal"),
        CallbackButton(text="≡ Всему классу", payload="rem_group")
    )
    await event.message.answer("Кому адресовано напоминание?", attachments=[builder.as_markup()])

@dp.message_callback(F.callback.payload.startswith("rem_"), StudentReminderFSM.waiting_for_type)
async def process_reminder_type(event: MessageCallback, context: MemoryContext):
    rem_type = event.callback.payload.split("_")[1]
    data = await context.get_data()
    user_id = get_user_id(event)
    user = await get_user(user_id)
    
    if rem_type == "personal":
        await add_personal_note(user_id, data["rem_text"])
        await event.message.answer("Личная заметка сохранена.")
    else:
        if not user.get("grade"):
            await event.message.answer("Вы не привязаны к классу. Сначала подключитесь к классу.")
        else:
            await add_group_note(user["school"], user["grade"], data["rem_text"])
            await event.message.answer("Заметка отправлена классу.")
            
    await event.answer()
    await context.clear()
    await show_main_menu(event, user)

# ================= УНИВЕРСАЛЬНАЯ КНОПКА "НАЗАД" =================
@dp.message_callback(F.callback.payload == "menu_back")
async def back_to_main(event: MessageCallback, context: MemoryContext):
    user = await get_user(get_user_id(event))
    await context.clear()
    await event.answer()
    if user:
        await show_main_menu(event, user)

# ================= РАСПИСАНИЕ УРОКОВ (ЛИЦЕЙ № 44) =================
@dp.message_callback(F.callback.payload == "menu_schedule")
async def show_schedule_hub(event: MessageCallback, context: MemoryContext):
    user_id = get_user_id(event)
    user = await get_user(user_id)
    grade = user.get("grade") if user else None
    
    schedule_data = await get_schedule()
    norm_grade = normalize_class_name(grade) if grade else ""

    builder = InlineKeyboardBuilder()

    if norm_grade and norm_grade in schedule_data.get("schedule", {}):
        text = format_day_schedule(schedule_data, norm_grade)
        day_row = [
            CallbackButton(text="Пн", payload=f"schedday_{norm_grade}_понедельник"),
            CallbackButton(text="Вт", payload=f"schedday_{norm_grade}_вторник"),
            CallbackButton(text="Ср", payload=f"schedday_{norm_grade}_среда"),
            CallbackButton(text="Чт", payload=f"schedday_{norm_grade}_четверг"),
            CallbackButton(text="Пт", payload=f"schedday_{norm_grade}_пятница"),
        ]
        builder.row(*day_row)
        builder.row(
            CallbackButton(text="⇄ Выбрать класс", payload="sched_pick_class"),
            CallbackButton(text="↺ Обновить", payload="sched_force_refresh")
        )
        builder.row(CallbackButton(text="← В главное меню", payload="menu_back"))
    else:
        text = "<b>Расписание занятий · Лицей № 44</b>\n\nВыберите параллель (5–11 класс):"
        builder.row(
            CallbackButton(text="5 класс", payload="sched_par_5"),
            CallbackButton(text="6 класс", payload="sched_par_6"),
            CallbackButton(text="7 класс", payload="sched_par_7")
        )
        builder.row(
            CallbackButton(text="8 класс", payload="sched_par_8"),
            CallbackButton(text="9 класс", payload="sched_par_9")
        )
        builder.row(
            CallbackButton(text="10 класс", payload="sched_par_10"),
            CallbackButton(text="11 класс", payload="sched_par_11")
        )
        builder.row(
            CallbackButton(text="↺ Обновить", payload="sched_force_refresh"),
            CallbackButton(text="← В главное меню", payload="menu_back")
        )

    if hasattr(event, "answer"):
        await event.answer()
    await event.message.answer(text, attachments=[builder.as_markup()], format=Format.HTML)

@dp.message_created(Command(["schedule", "lessons"]))
async def cmd_schedule(event: MessageCreated, context: MemoryContext):
    await show_schedule_hub(event, context)

@dp.message_callback(F.callback.payload == "sched_pick_class")
async def pick_class_schedule(event: MessageCallback, context: MemoryContext):
    user_id = get_user_id(event)
    user = await get_user(user_id)
    has_grade = bool(user and user.get("grade"))

    builder = InlineKeyboardBuilder()
    builder.row(
        CallbackButton(text="5 класс", payload="sched_par_5"),
        CallbackButton(text="6 класс", payload="sched_par_6"),
        CallbackButton(text="7 класс", payload="sched_par_7")
    )
    builder.row(
        CallbackButton(text="8 класс", payload="sched_par_8"),
        CallbackButton(text="9 класс", payload="sched_par_9")
    )
    builder.row(
        CallbackButton(text="10 класс", payload="sched_par_10"),
        CallbackButton(text="11 класс", payload="sched_par_11")
    )
    ctrl_row = [CallbackButton(text="↺ Обновить", payload="sched_force_refresh")]
    if has_grade:
        ctrl_row.append(CallbackButton(text="← Мой класс", payload="menu_schedule"))
    else:
        ctrl_row.append(CallbackButton(text="← В главное меню", payload="menu_back"))
    builder.row(*ctrl_row)

    await event.answer()
    await event.message.answer("<b>Расписание занятий · Лицей № 44</b>\n\nВыберите параллель:", attachments=[builder.as_markup()], format=Format.HTML)

@dp.message_callback(F.callback.payload.startswith("sched_par_"))
async def show_classes_for_parallel(event: MessageCallback, context: MemoryContext):
    par = event.callback.payload.replace("sched_par_", "")
    schedule_data = await get_schedule()
    classes_by_grade = schedule_data.get("metadata", {}).get("classes_by_grade", {})
    classes = classes_by_grade.get(par, [])
    if not classes:
        classes = [c for c in schedule_data.get("metadata", {}).get("classes", []) if c.startswith(par)]

    builder = InlineKeyboardBuilder()
    cls_btns = [CallbackButton(text=c, payload=f"sched_class_{c}") for c in classes]
    chunk = 4 if len(classes) >= 8 else 3
    for i in range(0, len(cls_btns), chunk):
        builder.row(*cls_btns[i:i + chunk])
    
    builder.row(
        CallbackButton(text="← К параллелям", payload="sched_pick_class"),
        CallbackButton(text="⌂ В меню", payload="menu_back")
    )

    await event.answer()
    await event.message.answer(
        f"<b>Параллель {par} классов · Лицей № 44</b>\n\nВыберите класс:",
        attachments=[builder.as_markup()],
        format=Format.HTML
    )

@dp.message_callback(F.callback.payload.startswith("sched_class_"))
async def show_class_default_schedule(event: MessageCallback, context: MemoryContext):
    cls_name = event.callback.payload.replace("sched_class_", "")
    schedule_data = await get_schedule()
    text = format_day_schedule(schedule_data, cls_name)
    m = re.match(r"^\d+", cls_name)
    par = m.group(0) if m else ""
    
    builder = InlineKeyboardBuilder()
    day_row = [
        CallbackButton(text="Пн", payload=f"schedday_{cls_name}_понедельник"),
        CallbackButton(text="Вт", payload=f"schedday_{cls_name}_вторник"),
        CallbackButton(text="Ср", payload=f"schedday_{cls_name}_среда"),
        CallbackButton(text="Чт", payload=f"schedday_{cls_name}_четверг"),
        CallbackButton(text="Пт", payload=f"schedday_{cls_name}_пятница"),
    ]
    builder.row(*day_row)
    nav_row = []
    if par:
        nav_row.append(CallbackButton(text=f"← {par} классы", payload=f"sched_par_{par}"))
    nav_row.append(CallbackButton(text="⇄ Параллели", payload="sched_pick_class"))
    builder.row(*nav_row)
    builder.row(CallbackButton(text="⌂ В главное меню", payload="menu_back"))

    await event.answer()
    await event.message.answer(text, attachments=[builder.as_markup()], format=Format.HTML)

@dp.message_callback(F.callback.payload.startswith("schedday_"))
async def show_class_specific_day(event: MessageCallback, context: MemoryContext):
    payload = event.callback.payload.replace("schedday_", "")
    parts = payload.split("_", 1)
    cls_name = parts[0]
    day_name = parts[1] if len(parts) > 1 else "понедельник"
    schedule_data = await get_schedule()
    text = format_day_schedule(schedule_data, cls_name, day_name)
    m = re.match(r"^\d+", cls_name)
    par = m.group(0) if m else ""
    
    builder = InlineKeyboardBuilder()
    day_row = [
        CallbackButton(text="Пн", payload=f"schedday_{cls_name}_понедельник"),
        CallbackButton(text="Вт", payload=f"schedday_{cls_name}_вторник"),
        CallbackButton(text="Ср", payload=f"schedday_{cls_name}_среда"),
        CallbackButton(text="Чт", payload=f"schedday_{cls_name}_четверг"),
        CallbackButton(text="Пт", payload=f"schedday_{cls_name}_пятница"),
    ]
    builder.row(*day_row)
    nav_row = []
    if par:
        nav_row.append(CallbackButton(text=f"← {par} классы", payload=f"sched_par_{par}"))
    nav_row.append(CallbackButton(text="⇄ Параллели", payload="sched_pick_class"))
    builder.row(*nav_row)
    builder.row(CallbackButton(text="⌂ В главное меню", payload="menu_back"))

    await event.answer()
    await event.message.answer(text, attachments=[builder.as_markup()], format=Format.HTML)

@dp.message_callback(F.callback.payload == "sched_force_refresh")
async def force_refresh_schedule(event: MessageCallback, context: MemoryContext):
    await event.answer("Обновление расписания...")
    try:
        data = await fetch_and_update_schedule()
        updated = data.get("metadata", {}).get("updated_at", "")
        await event.message.answer(f"Расписание всех параллелей 5-11 обновлено.\nСинхронизировано: {updated[:19]}")
    except Exception as e:
        await event.message.answer(f"Не удалось обновить расписание: {e}")
    await show_schedule_hub(event, context)

# ================= АДМИН: ПРЕЖДЕВРЕМЕННАЯ УТРЕННЯЯ РАССЫЛКА =================
async def _execute_morning_broadcast_for_admin(target_event, is_callback: bool = False):
    schedule_data = await get_schedule()
    from core.morning_notifications import MOSCOW
    now = datetime.now(MOSCOW)
    report = await send_morning_notifications(bot, schedule_data, now, force=True)
    
    sent_count = report.get("sent", 0)
    recipients = report.get("recipients", [])
    errors = report.get("errors", [])
    
    recipients_text = "\n".join(f"• {r}" for r in recipients) if recipients else "Ученики с расписанием не найдены."
    err_text = f"\n\n<b>Ошибки ({len(errors)}):</b>\n" + "\n".join(f"• {e}" for e in errors) if errors else ""
    
    msg_text = (
        f"⚡ <b>Утренняя рассылка выполнена принудительно (админ 43359625)</b>\n\n"
        f"Успешно доставлено: <b>{sent_count}</b>\n\n"
        f"<b>Получатели:</b>\n{recipients_text}{err_text}"
    )
    await send_event_message(target_event, msg_text, format=Format.HTML)
    
    # Отправляем сводку админу прямо в чат, чтобы он видел образец сообщения
    try:
        from core.morning_notifications import build_message
        sample_student = {
            "user_id": ADMIN_ID,
            "full_name": "Администратор",
            "grade": "9А",
            "tasks": ["Тестовая задача (образец рассылки)"],
            "notes": ["Личная заметка (образец)"],
            "group_notes": ["Объявление класса (образец)"]
        }
        lessons = schedule_data.get("schedule", {}).get("9А", {}).get("понедельник", [])
        admin_msg = build_message(sample_student, lessons, now, is_preview=True)
        await bot.send_message(user_id=int(ADMIN_ID), text=admin_msg)
    except Exception as e:
        logger.warning(f"Не удалось отправить образец админу: {e}")

@dp.message_created(Command(["morning_broadcast", "send_morning", "morning", "force_morning"]))
async def cmd_admin_morning_broadcast(event: MessageCreated, context: MemoryContext):
    user_id = str(get_user_id(event))
    if user_id != ADMIN_ID:
        await event.message.answer("❌ Команда доступна только администратору (ID 43359625).")
        return

    await event.message.answer("⏳ Запуск принудительной утренней рассылки для всех учеников...")
    try:
        await _execute_morning_broadcast_for_admin(event, is_callback=False)
    except Exception as e:
        logger.exception("Ошибка при принудительной рассылке админом")
        await event.message.answer(f"❌ Ошибка рассылки: {e}")

@dp.message_callback(F.callback.payload == "admin_morning_broadcast")
async def callback_admin_morning_broadcast(event: MessageCallback, context: MemoryContext):
    user_id = str(get_user_id(event))
    if user_id != ADMIN_ID:
        await event.answer("Доступ запрещен.")
        return

    await event.answer("Запуск рассылки...")
    await event.message.answer("⏳ Запуск принудительной утренней рассылки для всех учеников...")
    try:
        await _execute_morning_broadcast_for_admin(event, is_callback=True)
    except Exception as e:
        logger.exception("Ошибка при принудительной рассылке админом")
        await event.message.answer(f"❌ Ошибка рассылки: {e}")

# ================= МАГИЧЕСКИЙ ПЕРЕХВАТЧИК КОДОВ И СООБЩЕНИЙ =================
@dp.message_created(None, F.message.body.text)
async def magic_code_catcher(event: MessageCreated, context: MemoryContext):
    """Ловит любые неперехваченные сообщения и проверяет на коды или текстовые команды"""
    text = extract_text(event)
    if not text:
        return
    user_id = get_user_id(event)
    user = await get_user(user_id)
    
    if not user:
        return

    # Если это ровно 8 символов -> Квиз
    if len(text) == 8 and text.isalnum():
        await execute_pass(event, context, text)
        return
        
    # Если в тексте есть дефис и он короткий -> Класс (например, 9А-X7K2)
    if "-" in text and len(text) < 15:
        await execute_join(event, context, text, user)
        return

    # Текстовые команды рассылки для админа
    if str(user_id) == ADMIN_ID and text.lower() in (
        "утренняя рассылка", "рассылка", "рассылка утро",
        "/morning", "/morning_broadcast", "/send_morning", "/force_morning"
    ):
        await event.message.answer("⏳ Запуск принудительной утренней рассылки для всех учеников...")
        try:
            await _execute_morning_broadcast_for_admin(event, is_callback=False)
        except Exception as e:
            await event.message.answer(f"❌ Ошибка рассылки: {e}")
        return

    await event.message.answer("Команда не распознана. Выберите нужное действие в меню.")
    await show_main_menu(event, user)

# ================= ЗАПУСК =================
async def main():
    await init_db()
    # Фоновое обновление расписания каждые 12 часов
    asyncio.create_task(schedule_updater_loop(interval_hours=12.0))
    # Утренняя рассылка в 07:00 МСК
    asyncio.create_task(morning_notifications_loop(bot))
    logging.info("Бот запущен. Архитектура готова.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())