import pytest
from core.ai_work import clean_html_for_max
from core.schemas import validate_query
from core.file_parser import is_valid_document


def test_clean_html_for_max():
    raw_html = "<p>Первый пункт</p><br><li>Второй пункт</li><strong>Жирный</strong>"
    cleaned = clean_html_for_max(raw_html)
    assert "<p>" not in cleaned
    assert "</p>" not in cleaned
    assert "<li>" not in cleaned
    assert "• Второй пункт" in cleaned
    assert "<strong>Жирный</strong>" in cleaned


def test_quiz_schema_validation():
    valid_payload = {
        "title": "Тест на знание лицея",
        "questions": [
            {
                "question_text": "Сколько дней в учебной неделе Лицея № 44?",
                "options": [
                    {"text": "4 дня", "is_correct": False, "explanation": "Неверно"},
                    {"text": "5 дней", "is_correct": True, "explanation": "В лицее 5-дневная неделя"},
                    {"text": "6 дней", "is_correct": False, "explanation": "В лицее нет субботы"}
                ]
            }
        ]
    }
    validated = validate_query(valid_payload)
    assert validated["title"] == "Тест на знание лицея"
    assert len(validated["questions"]) == 1
    assert len(validated["questions"][0]["options"]) == 3
    assert validated["questions"][0]["options"][1]["is_correct"] is True


def test_quiz_schema_button_length_truncation():
    # Длина кнопки в интерфейсе MAX должна быть строго ограничена 40 символами
    payload = {
        "title": "Тест длины",
        "questions": [
            {
                "question_text": "Вопрос с длинным вариантом",
                "options": [
                    {"text": "А" * 50, "is_correct": True, "explanation": "Объяснение"},
                    {"text": "Б", "is_correct": False, "explanation": "Объяснение"}
                ]
            }
        ]
    }
    validated = validate_query(payload)
    assert len(validated["questions"][0]["options"][0]["text"]) <= 40
    assert validated["questions"][0]["options"][0]["text"].endswith("...")


def test_magic_bytes_detection():
    pdf_bytes = b"%PDF-1.4\n%test content\n%%EOF"
    docx_bytes = b"PK\x03\x04\x14\x00\x06\x00test"
    fake_bytes = b"NOT_A_VALID_DOCUMENT_HEADER"

    assert is_valid_document(pdf_bytes, "test.pdf") is True
    assert is_valid_document(docx_bytes, "test.docx") is True
    assert is_valid_document(fake_bytes, "test.pdf") is False
    assert is_valid_document(fake_bytes, "test.docx") is False
