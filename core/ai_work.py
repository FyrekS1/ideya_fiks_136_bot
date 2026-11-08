import json
import re
import logging
from core.config import MODEL, FALLBACK_MODEL, build_client
from core.schemas import validate_query

logger = logging.getLogger(__name__)

QUIZ_PROMPT = (
    "Ты помогаешь учителям в проекте «Школа.Синхрон». "
    "Составь квиз по переданному материалу/теме.\n"
    "ВАЖНО: Рассуждай предельно кратко (1 предложение), сразу выводи валидный JSON.\n\n"
    "ОТВЕТ ДОЛЖЕН БЫТЬ СТРОГО ВАЛИДНЫМ JSON ОБЪЕКТОМ следующей структуры:\n"
    "{\n"
    '  "title": "Краткая тема квиза",\n'
    '  "questions": [\n'
    '    {\n'
    '      "question_text": "Текст вопроса. Если варианты ответа длинные, напиши их здесь: 1) ... 2) ...",\n'
    '      "options": [\n'
    '        {"text": "Вариант 1 (СТРОГО до 40 символов)", "is_correct": true, "explanation": "Краткое и строгое объяснение сути правила/закона без бытовых аналогий и мнемоник"},\n'
    '        {"text": "Вариант 2 (СТРОГО до 40 символов)", "is_correct": false, "explanation": "Краткое объяснение, почему этот вариант неверен и какое правило нарушено (без аналогий и мнемоник)"}\n'
    '      ]\n'
    '    }\n'
    '  ]\n'
    "}\n\n"
    "ВАЖНЫЕ ПРАВИЛА:\n"
    "1. Количество вопросов должно строго соответствовать запросу пользователя.\n"
    "2. В каждом вопросе минимум 2 варианта ответа и СТРОГО 1 правильный ('is_correct': true).\n"
    "3. Текст каждого варианта ответа ('text') СТРОГО до 40 символов. Если длинный — используй цифры '1', '2' и т.д.\n"
    "4. В поле 'explanation' кратко и понятно объясняй саму суть правила или закона. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНЫ жизненные аналогии и мнемоники.\n"
    "5. Верни ТОЛЬКО чистый JSON (можно внутри блока ```json ... ```)."
)


def _extract_and_validate(raw_content: str) -> dict:
    raw = raw_content.strip()
    # 1. Сначала пробуем блок кода ```json ... ``` или ``` ... ```
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw)
    if match:
        candidate = match.group(1).strip()
        try:
            return validate_query(json.loads(candidate))
        except Exception:
            pass

    # 2. Пробуем извлечь внешний JSON-объект { ... }
    first_brace = raw.find("{")
    last_brace = raw.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        candidate = raw[first_brace:last_brace + 1]
        try:
            return validate_query(json.loads(candidate))
        except Exception:
            pass

    # 3. Прямой парсинг
    payload = json.loads(raw)
    return validate_query(payload)


def get_model_candidates() -> list[tuple[str, str]]:
    """
    Возвращает список моделей для запроса с многоуровневым резервированием (fallback).
    Учитывает независимые провайдерские маршруты на случай 502/503/429 от конкретного шлюза.
    """
    candidates = []
    if MODEL:
        candidates.append((MODEL, f"Основная ({MODEL})"))
    if FALLBACK_MODEL and FALLBACK_MODEL != MODEL:
        candidates.append((FALLBACK_MODEL, f"Резервная ({FALLBACK_MODEL})"))
    for alt in ["glm-5.3", "qwen-plus", "zai-org/GLM-5.3-Flash", "zai-org/GLM-5.3"]:
        if alt not in [c[0] for c in candidates]:
            candidates.append((alt, f"Резервный маршрут ({alt})"))
    return candidates


async def create_quiz(topic: str) -> dict:
    """Генерирует квиз с использованием цепочки отказоустойчивых моделей с автоматическим фоллбэком."""
    models_to_try = get_model_candidates()
    client = build_client()
    last_exc = None

    for model_name, label in models_to_try:
        try:
            logger.info(f"Генерация квиза через {label} [модель: {model_name}]...")
            response = await client.chat.completions.create(
                model=model_name,
                max_tokens=4096,
                messages=[
                    {"role": "system", "content": QUIZ_PROMPT},
                    {"role": "user", "content": topic}
                ]
            )

            if not response.choices:
                raise ValueError("Модель вернула пустой список choices")

            message = response.choices[0].message
            raw_content = message.content or getattr(message, "reasoning_content", None) or ""
            if not raw_content.strip():
                raise ValueError("Пустое тело ответа")

            result = _extract_and_validate(raw_content)
            logger.info(f"Успешная генерация через {label}!")
            return result

        except Exception as exc:
            logger.warning(f"Ошибка при генерации через {label} ({model_name}): {exc}. Переходим к следующей модели...")
            last_exc = exc

    raise last_exc or RuntimeError("Не удалось сгенерировать квиз ни одной из доступных моделей")


def clean_html_for_max(text: str) -> str:
    """
    Санитизирует HTML-разметку под требования MAX Messenger API:
    - Разрешенные теги: <b>, <strong>, <i>, <em>, <del>, <s>, <ins>, <u>, <pre>, <code>, <a>, <mark>, <blockquote>
    - Неподдерживаемые теги (<p>, <br>, <ul>, <ol>, <li>, <div>, <span>, <hr>) заменяются на переносы строк и маркеры
    - Блоки кода ``` вырезаются
    """
    text = re.sub(r"^```(?:html)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = re.sub(r"<hr\s*/?>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</?p\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<li>\s*", "• ", text, flags=re.IGNORECASE)
    text = re.sub(r"</?li\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</?(?:ul|ol|div|span)\s*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


async def generate_student_error_review(topic: str, wrong_answers: list[dict]) -> str:
    """Генерирует персональный разбор ошибок для ученика: четкий, без аналогий и мнемоник, за 2-3 секунды."""
    models_to_try = get_model_candidates()


    wrong_summary = []
    for i, item in enumerate(wrong_answers, 1):
        wrong_summary.append(
            f"Вопрос {i}: {item.get('question_text')}\n"
            f"Ответ ученика (ошибочный): {item.get('selected')}"
        )
    formatted_wrongs = "\n\n".join(wrong_summary)

    system_prompt = (
        "Ты — опытный и тактичный школьный преподаватель в проекте «Школа.Синхрон».\n"
        "Твоя задача — провести ясный, логичный и краткий разбор ошибок школьника.\n\n"
        "СТРОГИЕ ПРАВИЛА:\n"
        "1. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНЫ любые бытовые и жизненные аналогии (никаких «представь автобус», «это как пицца», «ведро с водой» и т.п.).\n"
        "2. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНЫ мнемоники, стишки, рифмы, выдуманные ассоциации и сказочные примеры.\n"
        "3. Объясняй саму научную суть: законы, формулы, правила, определения, причинно-следственные связи.\n"
        "4. Излагай материал доступным языком для школьника (8-9 класс): просто, последовательно, без заумного усложнения, но академически строго.\n"
        "5. Для каждой ошибки кратко укажи:\n"
        "   - <b>В чём ошибка:</b> почему выбранный ответ неверен.\n"
        "   - <b>Как правильно:</b> точное правило или логический вывод.\n"
        "6. Форматирование: разрешены ТОЛЬКО теги <b>, <i>, <code>. Запрещены теги <hr>, <p>, <br>, <ul>, <li> и блоки ```."
    )

    prompt = (
        f"Тема: «{topic}»\n\n"
        f"Ошибки ученика:\n{formatted_wrongs}\n\n"
        "Сделай последовательный, понятный и краткий разбор каждой ошибки по правилам: только предметная суть, формулы и правила, без бытовых аналогий и без мнемоник."
    )

    client = build_client()

    for model_name, label in models_to_try:
        try:
            logger.info(f"Генерация ИИ-разбора ошибок через {label} [модель: {model_name}]...")
            response = await client.chat.completions.create(
                model=model_name,
                max_tokens=2048,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt}
                ]
            )
            if response.choices and response.choices[0].message.content:
                text = response.choices[0].message.content
                return clean_html_for_max(text)
        except Exception as exc:
            logger.warning(f"Ошибка ИИ-разбора через {label}: {exc}")

    return "Не удалось получить разбор от ИИ в данный момент. Попробуйте позже."


async def generate_teacher_lesson_plan(topic: str, weak_topics: list[dict]) -> str:
    """Генерирует 5-минутный экспресс-сценарий урока для учителя на основе слабых мест класса."""
    models_to_try = get_model_candidates()


    weak_summary = []
    for item in weak_topics[:5]:
        weak_summary.append(
            f"• Вопрос {item.get('num')}: «{item.get('text')}» (Ошиблись: {item.get('error_rate')}% учеников)"
        )
    formatted_weaks = "\n".join(weak_summary)

    prompt = (
        f"Тема проверочного квиза: «{topic}»\n\n"
        f"Вопросы с наибольшим числом ошибок у класса:\n{formatted_weaks}\n\n"
        "Составь компактный 5-минутный сценарий разбора для начала следующего урока:\n"
        "1. Фокус внимания: ключевое понятие или правило, вызвавшее путаницу.\n"
        "2. Краткое объяснение (2 мин): наглядная схема или метафора.\n"
        "3. 2 блиц-вопроса для фронтального опроса класса (с верными ответами в скобках).\n"
        "Пиши чётко, профессионально, без лишних вводных слов и эмодзи. Формат HTML (теги <b>, <i>, списки, без блоков ```)."
    )

    system_prompt = (
        "Ты — методист и ассистент учителя в проекте «Школа.Синхрон».\n"
        "Рассуждай предельно кратко (1 предложение). Отвечай на русском языке сразу готовым сценарием разбора в HTML-разметке."
    )

    client = build_client()

    for model_name, label in models_to_try:
        try:
            logger.info(f"Генерация сценария урока через {label} [модель: {model_name}]...")
            response = await client.chat.completions.create(
                model=model_name,
                max_tokens=4096,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt}
                ]
            )
            if response.choices and response.choices[0].message.content:
                text = response.choices[0].message.content
                return clean_html_for_max(text)
        except Exception as exc:
            logger.warning(f"Ошибка генерации сценария через {label}: {exc}")

    return "❌ Не удалось сгенерировать сценарий урока от ИИ в данный момент."