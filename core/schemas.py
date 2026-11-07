def json_schema_format(name: str, schema: dict) -> dict:
    return {"type": "json_schema", "json_schema": {"name": name, "schema": schema}}

QUIZ_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": "Краткая тема квиза",
        },
        "questions": {
            "type": "array",
            "description": "Список вопросов.",
            "items": {
                "type": "object",
                "properties": {
                    "question_text": {
                        "type": "string",
                        "description": "Текст вопроса. Если варианты ответов длинные, напиши их прямо здесь.",
                    },
                    "options": {
                        "type": "array",
                        "description": "Варианты ответов. Минимум 2. Строго 1 правильный.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {
                                    "type": "string",
                                    "description": "Текст на кнопке. СТРОГО до 40 символов!",
                                    "maxLength": 40
                                },
                                "is_correct": {
                                    "type": "boolean",
                                },
                                "explanation": {
                                    "type": "string",
                                },
                            },
                            "required": ["text", "is_correct", "explanation"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["question_text", "options"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["title", "questions"],
    "additionalProperties": False,
}

def validate_query(query):
    if not isinstance(query, dict):
        raise ValueError("Ожидался JSON объект квиза")
        
    title = query.get("title") or query.get("topic") or "Квиз"
    questions = query.get("questions")
    
    if not questions or not isinstance(questions, list):
        raise ValueError("В ответе модели отсутствует список вопросов")
        
    cleaned_questions = []
    seen_texts = set()

    for i, q in enumerate(questions):
        q_text = (q.get("question_text") or f"Вопрос {i+1}").strip()
        if q_text in seen_texts:
            continue
        seen_texts.add(q_text)

        raw_options = q.get("options") or []
        if len(raw_options) < 2:
            continue

        cleaned_options = []
        has_correct = False

        for opt in raw_options:
            opt_text = str(opt.get("text", "")).strip()
            # Автоматическая безопасная обрезка под лимит интерфейса Max (40 символов)
            if len(opt_text) > 40:
                opt_text = opt_text[:37] + "..."
            if not opt_text:
                opt_text = "Вариант"

            is_correct = bool(opt.get("is_correct", False))
            if is_correct and not has_correct:
                has_correct = True
            elif is_correct and has_correct:
                is_correct = False

            explanation = str(opt.get("explanation") or "").strip()
            if not explanation:
                explanation = "Верный ответ" if is_correct else "Неверный ответ"

            cleaned_options.append({
                "text": opt_text,
                "is_correct": is_correct,
                "explanation": explanation
            })

        # Если модель не пометила ни один как правильный, делаем первый правильным
        if not has_correct and cleaned_options:
            cleaned_options[0]["is_correct"] = True

        cleaned_questions.append({
            "question_text": q_text,
            "options": cleaned_options
        })

    if not cleaned_questions:
        raise ValueError("Не удалось сформировать валидные вопросы")

    return {
        "title": title,
        "questions": cleaned_questions
    }