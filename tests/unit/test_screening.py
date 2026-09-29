"""Tests for app/screening.py — deterministic profile screening (6 questions)."""
from __future__ import annotations


def test_questions_count_is_six():
    from app.screening import SCREENING_QUESTIONS
    assert len(SCREENING_QUESTIONS) == 6


def test_question_order_and_fields():
    """Telegram-порядок: name → gender → age_range → email → sector → time_eater."""
    from app.screening import SCREENING_QUESTIONS
    fields = [q["field"] for q in SCREENING_QUESTIONS]
    assert fields == ["name", "gender", "age_range", "email", "sector", "time_eater"]


def test_each_question_has_required_keys():
    from app.screening import SCREENING_QUESTIONS
    for q in SCREENING_QUESTIONS:
        assert "field" in q
        assert "question" in q
        assert "type" in q
        assert q["type"] in ("free_text", "email", "choice")
        if q["type"] == "choice":
            assert "options" in q and len(q["options"]) >= 3


def test_parse_name_step_takes_whole_text():
    from app.screening import parse_input
    value, comment = parse_input("Анна", step=0)
    assert value == "Анна"
    assert comment is None


def test_parse_name_step_strips_whitespace():
    from app.screening import parse_input
    value, _ = parse_input("  Иван  ", step=0)
    assert value == "Иван"


def test_parse_email_valid():
    from app.screening import parse_input
    value, comment = parse_input("anna@example.com", step=3)
    assert value == "anna@example.com"
    assert comment is None


def test_parse_email_invalid_returns_none_with_raw_as_comment():
    from app.screening import parse_input
    value, comment = parse_input("not-an-email", step=3)
    assert value is None
    assert comment == "not-an-email"


def test_parse_email_various_invalid_formats():
    from app.screening import parse_input
    for bad in ["foo", "foo@", "@bar.com", "foo bar@x.com", "foo@bar"]:
        value, _ = parse_input(bad, step=3)
        assert value is None, f"{bad} should be invalid"


def test_parse_choice_step_pure_digit():
    """gender step (1) — option 1 = female."""
    from app.screening import parse_input
    value, comment = parse_input("1", step=1)
    assert value == "female"
    assert comment is None


def test_parse_choice_step_digit_with_comment():
    """age step (2), option 2 = 25-35; comment after."""
    from app.screening import parse_input
    value, comment = parse_input("2 а зачем спрашиваете", step=2)
    assert value == "25-35"
    assert comment == "а зачем спрашиваете"


def test_parse_choice_out_of_range_treated_as_comment():
    """age step has 5 options; '99' out of range."""
    from app.screening import parse_input
    value, comment = parse_input("99 какой-то текст", step=2)
    assert value is None
    assert comment == "99 какой-то текст"


def test_parse_choice_free_text_no_digit():
    """sector step (4), no digit — entire input as comment."""
    from app.screening import parse_input
    value, comment = parse_input("у меня репетиторство", step=4)
    assert value is None
    assert comment == "у меня репетиторство"


def test_parse_empty_input():
    from app.screening import parse_input
    assert parse_input("", step=0) == (None, None)
    assert parse_input("   ", step=0) == (None, None)


def test_format_question_choice_includes_numbered_options():
    """age step (2) — show '1. до 25' etc."""
    from app.screening import format_question
    text = format_question(2)
    assert "Сколько вам лет?" in text
    assert "1. до 25" in text
    assert "5. 55 и старше" in text


def test_format_question_free_text_no_options():
    """name step (0) — just the question, no options listed."""
    from app.screening import format_question
    text = format_question(0)
    assert "Как могу к вам обращаться?" in text
    assert "1." not in text  # no numbered list


def test_format_question_email_no_options():
    from app.screening import format_question
    text = format_question(3)
    assert "email" in text.lower()
    assert "1." not in text


def test_is_done_at_six():
    from app.screening import is_done
    assert is_done(6) is True
    assert is_done(5) is False
    assert is_done(0) is False


def test_field_at_returns_field_name():
    from app.screening import field_at
    assert field_at(0) == "name"
    assert field_at(3) == "email"
    assert field_at(5) == "time_eater"


def test_phase_has_screening_value():
    from app.state import Phase
    assert Phase.SCREENING.value == "screening"


def test_conversation_has_screening_step():
    from app.state import Conversation
    c = Conversation()
    assert c.screening_step == 0


def test_email_regex_helper():
    from app.screening import is_valid_email
    assert is_valid_email("a@b.co") is True
    assert is_valid_email("anna+tag@sub.example.com") is True
    assert is_valid_email("a@b") is False
    assert is_valid_email("@b.co") is False
    assert is_valid_email("a@.co") is False
