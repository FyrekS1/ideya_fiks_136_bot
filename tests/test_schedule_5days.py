from datetime import datetime
from core.schedule_parser import RUSSIAN_DAYS, MOSCOW, normalize_class_name, format_day_schedule
from core.morning_notifications import build_message


def test_russian_days_structure():
    # Проверка, что в лицее 5 учебных дней
    school_days = RUSSIAN_DAYS[:5]
    assert len(school_days) == 5
    assert school_days == ["понедельник", "вторник", "среда", "четверг", "пятница"]


def test_normalize_class_name():
    assert normalize_class_name("9а") == "9А"
    assert normalize_class_name("9-а") == "9А"
    assert normalize_class_name("9 А") == "9А"
    assert normalize_class_name("11и") == "11И"


def test_format_day_schedule_output():
    mock_schedule = {
        "metadata": {"classes": ["9А"]},
        "schedule": {
            "9А": {
                "понедельник": [
                    {"lesson": 1, "subject": "Алгебра", "cabinet": "204"},
                    {"lesson": 2, "subject": "Физика", "cabinet": "301"}
                ]
            }
        }
    }
    res = format_day_schedule(mock_schedule, "9А", "понедельник")
    assert "9А" in res
    assert "Алгебра" in res
    assert "каб. 204" in res
    assert "Физика" in res


def test_build_morning_message_task_formatting():
    student = {
        "user_id": "12345",
        "full_name": "Тестовый Ученик",
        "grade": "9А",
        "tasks": ["Сделать домашнее задание по физике"],
        "notes": []
    }
    lessons = [{"lesson": 1, "subject": "Информатика", "cabinet": "105"}]
    test_dt = datetime(2026, 11, 2, 7, 0, tzinfo=MOSCOW)

    msg = build_message(student, lessons, test_dt)
    assert "Доброе утро!" in msg
    assert "Класс 9А" in msg
    assert "1. Информатика · каб. 105" in msg
    # Невыполненные задачи должны помечаться символом крестика ✗
    assert "✗ Сделать домашнее задание по физике" in msg
