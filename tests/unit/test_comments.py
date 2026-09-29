"""Tests for app.comments — deterministic 'комментарий:' splitter."""
from __future__ import annotations

from app.comments import parse_comment


def test_no_comment_returns_text_unchanged():
    answer, comment = parse_comment("у меня магазин в инстаграме")
    assert answer == "у меня магазин в инстаграме"
    assert comment is None


def test_comment_at_message_start():
    answer, comment = parse_comment("комментарий: вопрос непонятный")
    assert answer == ""
    assert comment == "вопрос непонятный"


def test_comment_after_answer_on_new_line():
    answer, comment = parse_comment("у меня магазин\nкомментарий: вопрос путаный")
    assert answer == "у меня магазин"
    assert comment == "вопрос путаный"


def test_case_insensitive():
    for variant in ("Комментарий:", "КОММЕНТАРИЙ:", "комментарий :"):
        _, comment = parse_comment(f"ответ\n{variant} текст")
        assert comment == "текст", variant


def test_alternative_spelling_kommentariya():
    """«комментария:» — некоторым удобнее в родительном падеже."""
    answer, comment = parse_comment("ответ\nкомментария: пометка")
    assert answer == "ответ"
    assert comment == "пометка"


def test_multiline_comment_kept_whole():
    text = "ответ бизнес-вопроса\nкомментарий: первая строка\nвторая строка\nтретья"
    answer, comment = parse_comment(text)
    assert answer == "ответ бизнес-вопроса"
    assert comment == "первая строка\nвторая строка\nтретья"


def test_word_kommentariy_in_text_not_triggered():
    """Слово «комментарий» в середине предложения не считается триггером."""
    answer, comment = parse_comment("раньше там был мой комментарий: продукт сезонный")
    # триггер требует начала строки — здесь его нет, всё в answer
    assert "комментарий" in answer
    assert comment is None


def test_empty_input():
    answer, comment = parse_comment("")
    assert answer == ""
    assert comment is None


def test_only_trigger_no_text():
    answer, comment = parse_comment("комментарий:")
    assert answer == ""
    assert comment is None


def test_whitespace_around_trigger_handled():
    answer, comment = parse_comment("ответ  \n  комментарий:   мой комментарий  ")
    assert answer == "ответ"
    assert comment == "мой комментарий"
