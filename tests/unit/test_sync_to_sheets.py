"""Pure-formatter tests for scripts/sync_to_sheets — no network, no gspread/Drive."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from unittest.mock import MagicMock

from scripts.sync_to_sheets import (
    COMMENTS_HEADERS,
    DAILY_HEADERS,
    DIALOGS_HEADERS,
    FUNNEL_HEADERS,
    LEADS_HEADERS,
    PROFILES_HEADERS,
    SPECS_HEADERS,
    SUMMARY_HEADERS,
    apply_gray_separators,
    build_analytics_rows,
    build_daily_rows,
    build_funnel_rows,
    format_comments_rows,
    format_leads_rows,
    format_profiles_rows,
    format_qac_split,
    format_specs_full_rows,
    format_summary_rows,
    summary_row,
    write_sheet,
)


# Внутри подмодуля тестов помощник для тестов диалогов — собрать transcript-pair с meta.
def _qac_pair(q="Q?", a="A", meta=None):
    return [
        {"role": "assistant", "content": q, "meta": meta or {}},
        {"role": "user", "content": a},
    ]


def _doc(**overrides):
    """Make a synthetic Firestore session doc with sensible defaults."""
    base = {
        "telegram_chat_id": None,
        "workflow_name": "v2",
        "updated_at": datetime(2026, 5, 7, 17, 0, tzinfo=timezone.utc),
        "business_spec": None,
        "dev_spec": None,
        "rule_violations": [],
        "classifications": [],
        "transcript": [],
        "state": {
            "profile": {},
            "point_a": {},
            "point_b": {},
            "resources": {},
            "conversation": {"phase": "screening", "turn_count": 0, "screening_step": 0},
        },
    }
    base.update(overrides)
    return base


# ---- summary --------------------------------------------------------------

def _col(name: str) -> int:
    return SUMMARY_HEADERS.index(name)


def test_summary_row_with_empty_state():
    row = summary_row("sid-1", _doc())

    assert len(row) == len(SUMMARY_HEADERS)
    assert row[_col("session_id")] == "sid-1"
    assert row[_col("workflow")] == "v2"
    # started_at — fallback на updated_at для старых сессий
    assert row[_col("started_at")] == "2026-05-07 17:00"
    # ended_at — пусто, фаза не done
    assert row[_col("ended_at")] == ""
    assert row[_col("duration")] == ""
    assert row[_col("phase")] == "screening"
    assert row[_col("num_comments")] == "0"


def test_summary_row_counts_comments():
    doc = _doc(classifications=[
        {"turn": 1, "type": "answer", "text": "x", "rationale": "screening Q1"},
        {"turn": 2, "type": "comment", "text": "questions feel weird", "rationale": "..."},
        {"turn": 3, "type": "comment", "text": "another", "rationale": "..."},
    ])
    row = summary_row("sid-3", doc)
    assert row[_col("num_classifications")] == "3"
    assert row[_col("num_comments")] == "2"


def test_summary_row_includes_new_owner_fields():
    doc = _doc(state={
        "profile": {},
        "point_a": {},
        "point_b": {
            "user_flows": ["клиент пишет — бот отвечает"],
            "functional_requirements_owner": ["должен сам отвечать ночью"],
        },
        "resources": {
            "deployment_constraints": ["сайт уже на Tilda"],
            "preferred_channel": "telegram",
        },
        "conversation": {"phase": "done", "turn_count": 12, "screening_step": 4},
    })
    row = summary_row("sid-4", doc)
    assert "клиент пишет — бот отвечает" in row[_col("user_flows")]
    assert "должен сам отвечать ночью" in row[_col("fr_owner")]
    assert "сайт уже на Tilda" in row[_col("ограничения")]
    assert "telegram" in row[_col("канал")]


def test_summary_row_uses_new_started_ended_with_duration():
    """Если в state выставлены started_at/ended_at — используем их и считаем duration."""
    started = datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc)
    ended = datetime(2026, 5, 7, 14, 23, 30, tzinfo=timezone.utc)
    doc = _doc(state={
        "profile": {}, "point_a": {}, "point_b": {}, "resources": {},
        "conversation": {
            "phase": "done", "turn_count": 12, "screening_step": 4,
            "started_at": started.isoformat(),
            "ended_at": ended.isoformat(),
        },
    })
    row = summary_row("sid-5", doc)
    assert row[_col("started_at")] == "2026-05-07 14:00"
    assert row[_col("ended_at")] == "2026-05-07 14:23"
    assert row[_col("duration")] == "23м 30с"


def test_summary_row_done_session_falls_back_to_updated_at_for_ended():
    """Старые завершённые сессии без ended_at — используем updated_at."""
    doc = _doc(
        updated_at=datetime(2026, 5, 7, 14, 30, tzinfo=timezone.utc),
        state={
            "profile": {}, "point_a": {}, "point_b": {}, "resources": {},
            "conversation": {"phase": "done", "turn_count": 12, "screening_step": 4},
        },
    )
    row = summary_row("sid-6", doc)
    # started_at fallback = updated_at
    assert row[_col("started_at")] == "2026-05-07 14:30"
    # ended_at fallback = updated_at (фаза done)
    assert row[_col("ended_at")] == "2026-05-07 14:30"
    # duration 0с (start == end)
    assert row[_col("duration")] == "0с"


def test_format_summary_rows_includes_header():
    docs = [("sid-1", _doc()), ("sid-2", _doc(business_spec="x", dev_spec="y"))]
    rows = format_summary_rows(docs)
    assert rows[0] == SUMMARY_HEADERS
    assert len(rows) == 3  # header + 2 sessions
    assert rows[1][_col("session_id")] == "sid-1"
    assert rows[2][_col("session_id")] == "sid-2"


# ---- dialogs split (Q-A-Comment + meta + grey separators) -----------------

def _doc_with_pairs(phase: str, *pairs, **kw):
    """Build a doc whose transcript = concatenation of given Q-A pairs."""
    transcript = []
    for p in pairs:
        transcript.extend(p)
    state = kw.pop("state", None) or {
        "profile": {}, "point_a": {}, "point_b": {}, "resources": {},
        "conversation": {"phase": phase, "turn_count": len(pairs), "screening_step": 4},
    }
    return _doc(transcript=transcript, state=state, **kw)


def test_format_qac_split_filters_by_phase():
    """3 сессии: 1 в point_a, 2 в done. В каждом срезе — только свои."""
    in_progress = _doc_with_pairs("point_a", _qac_pair("Q1?", "A1"))
    done1 = _doc_with_pairs("done", _qac_pair("Q-d1?", "Ad1"))
    done2 = _doc_with_pairs("done", _qac_pair("Q-d2?", "Ad2"))

    incomplete, completed = format_qac_split([
        ("inp", in_progress), ("d1", done1), ("d2", done2),
    ])
    q_col = DIALOGS_HEADERS.index("question")
    inc_qs = [r[q_col] for r in incomplete.rows[1:] if r[0]]
    comp_qs = [r[q_col] for r in completed.rows[1:] if r[0]]
    assert "Q1?" in inc_qs
    assert "Q-d1?" not in inc_qs and "Q-d2?" not in inc_qs
    assert "Q-d1?" in comp_qs and "Q-d2?" in comp_qs
    assert "Q1?" not in comp_qs


def test_format_qac_split_inserts_1_row_separator_between_sessions():
    """Между двумя done-сессиями — одна строка-разделитель с тире во всех колонках.

    Раньше использовался 3-строчный pattern (пустая/серая/пустая) с batch_format,
    но clear/batch_clear в gspread не трогает форматирование — серые строки от
    предыдущих, более длинных sync'ов оставались поверх свежих данных. Текстовый
    разделитель не зависит от состояния форматирования листа.
    """
    done1 = _doc_with_pairs("done", _qac_pair("Qa", "Aa"))
    done2 = _doc_with_pairs("done", _qac_pair("Qb", "Ab"))
    _, completed = format_qac_split([("a", done1), ("b", done2)])

    width = len(DIALOGS_HEADERS)
    q_col = DIALOGS_HEADERS.index("question")

    assert completed.rows[0] == list(DIALOGS_HEADERS)
    assert completed.rows[1][q_col] == "Qa"
    # Одна строка-разделитель: каждая ячейка содержит тире, ширина = len(DIALOGS_HEADERS).
    sep = completed.rows[2]
    assert len(sep) == width
    assert all("─" in cell for cell in sep)
    assert completed.rows[3][q_col] == "Qb"
    # gray_row_indices сохраняется как поле, но всегда пуст (форматирование больше не используется).
    assert completed.gray_row_indices == []


def test_format_qac_split_separator_row_contains_dashes():
    """Разделитель между сессиями: колонка A содержит тире (визуально различимы)."""
    done1 = _doc_with_pairs("done", _qac_pair("Qa", "Aa"))
    done2 = _doc_with_pairs("done", _qac_pair("Qb", "Ab"))
    _, completed = format_qac_split([("a", done1), ("b", done2)])

    # Разделитель — единственная строка между двумя session-ами.
    sep_row = completed.rows[2]
    assert "─" in sep_row[0]
    # Минимум 10 тире подряд в колонке A — гарантия видимости.
    assert "─" * 10 in sep_row[0]


def test_format_qac_split_no_separator_before_first_session():
    done1 = _doc_with_pairs("done", _qac_pair("Q?", "A"))
    _, completed = format_qac_split([("a", done1)])
    assert completed.rows[0] == list(DIALOGS_HEADERS)
    assert completed.rows[1][DIALOGS_HEADERS.index("question")] == "Q?"
    assert completed.gray_row_indices == []


def test_format_qac_split_skips_sessions_with_no_pairs():
    """Сессия без Q-A пар (только приветствие) — не вставляет разделитель."""
    empty_done = _doc_with_pairs("done")  # no pairs
    real_done = _doc_with_pairs("done", _qac_pair("Q?", "A"))
    _, completed = format_qac_split([("e", empty_done), ("r", real_done)])
    assert completed.gray_row_indices == []
    assert completed.rows[1][DIALOGS_HEADERS.index("question")] == "Q?"


def test_format_qac_split_preserves_meta():
    pair = _qac_pair("Расскажите?", "у меня магазин", meta={
        "phase": "point_a",
        "target_field": "business_type",
        "target_section": "Context",
        "owner_question_hint": "В двух словах…",
        "internal_rationale": "asker LLM с подсказкой",
    })
    doc = _doc_with_pairs("point_a", pair)
    incomplete, _ = format_qac_split([("sid", doc)])
    r = incomplete.rows[1]
    assert r[DIALOGS_HEADERS.index("target_field")] == "business_type"
    assert r[DIALOGS_HEADERS.index("target_section")] == "Context"
    assert r[DIALOGS_HEADERS.index("owner_question_hint")] == "В двух словах…"
    assert r[DIALOGS_HEADERS.index("internal_rationale")] == "asker LLM с подсказкой"


def test_format_qac_split_no_meta_back_compat():
    """Старая сессия без meta — meta-колонки пустые, без падения."""
    transcript = [
        {"role": "assistant", "content": "Q?"},  # no meta
        {"role": "user", "content": "A"},
    ]
    doc = _doc_with_pairs("point_a")
    doc["transcript"] = transcript
    incomplete, _ = format_qac_split([("sid", doc)])
    r = incomplete.rows[1]
    assert r[DIALOGS_HEADERS.index("phase")] == ""
    assert r[DIALOGS_HEADERS.index("target_field")] == ""
    assert r[DIALOGS_HEADERS.index("internal_rationale")] == ""
    assert r[DIALOGS_HEADERS.index("question")] == "Q?"
    assert r[DIALOGS_HEADERS.index("answer")] == "A"


def test_format_qac_split_skips_non_paired_messages():
    """orphan user/assistant без пары — пропускается."""
    transcript = [
        {"role": "user", "content": "orphan"},
        {"role": "assistant", "content": "Q?"},
        {"role": "user", "content": "A"},
        {"role": "assistant", "content": "trailing"},  # no following user
    ]
    doc = _doc_with_pairs("point_a")
    doc["transcript"] = transcript
    incomplete, _ = format_qac_split([("sid", doc)])
    # header + 1 valid pair (Q?, A)
    valid_rows = [r for r in incomplete.rows[1:] if r[0]]
    assert len(valid_rows) == 1
    assert valid_rows[0][DIALOGS_HEADERS.index("question")] == "Q?"


def test_apply_gray_separators_calls_batch_format_with_correct_ranges():
    ws = MagicMock()
    apply_gray_separators(ws, [4, 12, 20], num_cols=13)
    ws.batch_format.assert_called_once()
    fmts = ws.batch_format.call_args.args[0]
    assert len(fmts) == 3
    # 13 колонок → end_col = chr('A' + 12) = 'M'
    ranges = [f["range"] for f in fmts]
    assert ranges == ["A4:M4", "A12:M12", "A20:M20"]
    for f in fmts:
        bg = f["format"]["backgroundColor"]
        assert 0 < bg["red"] < 1 and 0 < bg["green"] < 1 and 0 < bg["blue"] < 1


def test_apply_gray_separators_noop_when_empty():
    ws = MagicMock()
    apply_gray_separators(ws, [], num_cols=13)
    ws.batch_format.assert_not_called()


def test_format_qac_split_uses_started_at_for_when_column():
    """Колонка started_at в строке хода = started_at сессии (не updated_at)."""
    started = datetime(2026, 5, 8, 14, 0, tzinfo=timezone.utc)
    doc = _doc_with_pairs(
        "done",
        _qac_pair("Q?", "A"),
        state={
            "profile": {}, "point_a": {}, "point_b": {}, "resources": {},
            "conversation": {
                "phase": "done", "turn_count": 1, "screening_step": 4,
                "started_at": started.isoformat(),
            },
        },
    )
    _, completed = format_qac_split([("sid", doc)])
    r = completed.rows[1]
    assert r[DIALOGS_HEADERS.index("started_at")] == "2026-05-08 14:00"


def test_format_qac_split_classifier_rationale_and_comment_attached():
    """Screening Q1 — классификации `screening Q1 ...` цепляются к первой паре."""
    pair = _qac_pair("Q1?", "1", meta={
        "phase": "screening", "target_field": "age_range",
        "target_section": "Profile", "kind": "screening_question",
        "internal_rationale": "screening Q1",
    })
    classifications = [
        {"turn": 1, "type": "answer", "text": "<25", "rationale": "screening Q1 (age_range) choice"},
        {"turn": 1, "type": "comment", "text": "странный вопрос", "rationale": "screening Q1 free-text"},
    ]
    doc = _doc(
        transcript=pair,
        classifications=classifications,
        state={
            "profile": {}, "point_a": {}, "point_b": {}, "resources": {},
            "conversation": {"phase": "point_a", "turn_count": 0, "screening_step": 4},
        },
    )
    incomplete, _ = format_qac_split([("sid", doc)])
    r = incomplete.rows[1]
    assert r[DIALOGS_HEADERS.index("classifier_rationale")] == "screening Q1 (age_range) choice"
    assert r[DIALOGS_HEADERS.index("comment")] == "странный вопрос"


# ---- specs sheet ----------------------------------------------------------

def test_format_specs_full_rows_includes_full_text_and_dates():
    started = datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc)
    ended = datetime(2026, 5, 7, 14, 23, tzinfo=timezone.utc)
    doc_with = _doc(
        business_spec="# Простой план\n\nДолгий текст…",
        dev_spec="# Implementation Spec\n\n## Goal\n…",
        telegram_chat_id=4242,
        state={
            "profile": {}, "point_a": {}, "point_b": {}, "resources": {},
            "conversation": {
                "phase": "done", "turn_count": 12, "screening_step": 4,
                "started_at": started.isoformat(),
                "ended_at": ended.isoformat(),
            },
        },
    )
    doc_without = _doc()  # no specs — must be filtered out
    rows = format_specs_full_rows([("sid-with", doc_with), ("sid-without", doc_without)])

    assert rows[0] == SPECS_HEADERS
    assert len(rows) == 2  # header + 1 doc with specs
    r = rows[1]
    assert r[0] == "sid-with"
    assert r[1] == "4242"
    assert r[2] == "2026-05-07 14:00"
    assert r[3] == "2026-05-07 14:23"
    assert r[4] == "23м 00с"
    assert "Простой план" in r[5]   # full biz spec
    assert "Implementation Spec" in r[6]  # full dev spec


def test_format_specs_full_rows_skips_when_no_specs():
    rows = format_specs_full_rows([("sid", _doc())])
    assert rows == [SPECS_HEADERS]


# ---- analytics ------------------------------------------------------------

def test_build_analytics_counts_phases_specs_and_comments():
    now = datetime(2026, 5, 7, 18, 0, tzinfo=timezone.utc)
    docs = [
        ("a", _doc(
            state={"conversation": {"phase": "screening", "turn_count": 0}},
            updated_at=now - timedelta(days=10),
            classifications=[{"turn": 1, "type": "comment", "text": "x", "rationale": "..."}],
        )),
        ("b", _doc(
            state={"conversation": {"phase": "point_a", "turn_count": 3}},
            updated_at=now - timedelta(days=2),
        )),
        ("c", _doc(
            business_spec="x", dev_spec="y",
            state={"conversation": {"phase": "done", "turn_count": 15}},
            updated_at=now - timedelta(hours=1),
            classifications=[
                {"turn": 1, "type": "comment", "text": "a", "rationale": "..."},
                {"turn": 2, "type": "comment", "text": "b", "rationale": "..."},
            ],
        )),
        ("d", _doc(
            business_spec="z",
            state={"conversation": {"phase": "done", "turn_count": 10}},
            updated_at=now - timedelta(days=3),
        )),
    ]
    rows = build_analytics_rows(docs, now=now)
    metrics = {row[0]: row[1] for row in rows[1:]}

    assert metrics["total_sessions"] == "4"
    assert metrics["sessions_in_phase_done"] == "2"
    assert metrics["sessions_with_business_spec"] == "2"
    assert metrics["sessions_with_dev_spec"] == "1"
    assert metrics["avg_turn_count"] == "7.0"
    assert metrics["total_comments"] == "3"
    assert metrics["avg_comments_per_session"] == "0.75"
    assert metrics["last_24h_new_sessions"] == "1"
    assert metrics["last_7d_new_sessions"] == "3"
    # completed_sessions/avg_duration_min — есть, посчитаны на done-сессиях с обоими таймстампами
    assert "completed_sessions" in metrics
    assert "avg_duration_min" in metrics


def test_build_analytics_avg_duration_min_only_done_sessions():
    """avg_duration_min считается только по сессиям с обоими started_at и ended_at."""
    now = datetime(2026, 5, 7, 18, 0, tzinfo=timezone.utc)
    started = datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc)
    ended_15min = datetime(2026, 5, 7, 14, 15, tzinfo=timezone.utc)
    ended_45min = datetime(2026, 5, 7, 14, 45, tzinfo=timezone.utc)

    docs = [
        ("a", _doc(state={
            "profile": {}, "point_a": {}, "point_b": {}, "resources": {},
            "conversation": {"phase": "done", "turn_count": 10, "screening_step": 4,
                             "started_at": started.isoformat(),
                             "ended_at": ended_15min.isoformat()},
        })),
        ("b", _doc(state={
            "profile": {}, "point_a": {}, "point_b": {}, "resources": {},
            "conversation": {"phase": "done", "turn_count": 12, "screening_step": 4,
                             "started_at": started.isoformat(),
                             "ended_at": ended_45min.isoformat()},
        })),
        # in-progress — не учитывается в avg_duration
        ("c", _doc(state={
            "profile": {}, "point_a": {}, "point_b": {}, "resources": {},
            "conversation": {"phase": "point_a", "turn_count": 3, "screening_step": 4,
                             "started_at": started.isoformat()},
        })),
    ]
    rows = build_analytics_rows(docs, now=now)
    metrics = {row[0]: row[1] for row in rows[1:]}
    assert metrics["completed_sessions"] == "2"
    assert metrics["avg_duration_min"] == "30.0"  # (15 + 45) / 2


# ---- profiles / leads / funnel / comments / daily ----------------------


def _profile_doc(**profile_overrides):
    """Doc с заполненным profile из intake."""
    p = {
        "name": "Анна", "email": "anna@example.com",
        "age_range": "25-35", "gender": "female",
        "sector": "services", "time_eater": "client_comms",
    }
    p.update(profile_overrides)
    return _doc(state={
        "profile": p,
        "point_a": {}, "point_b": {}, "resources": {},
        "conversation": {"phase": "point_a", "turn_count": 0, "screening_step": 4},
    })


# ---- Профили ---------------------------------------------------------------

def test_format_profiles_rows_one_per_session():
    docs = [
        ("sid-a", _profile_doc(name="Анна")),
        ("sid-b", _profile_doc(name="Борис", gender="male")),
    ]
    rows = format_profiles_rows(docs)
    assert rows[0] == PROFILES_HEADERS
    assert len(rows) == 3
    assert rows[1][PROFILES_HEADERS.index("session_id")] == "sid-a"
    assert rows[1][PROFILES_HEADERS.index("имя")] == "Анна"
    assert rows[2][PROFILES_HEADERS.index("имя")] == "Борис"


def test_format_profiles_rows_includes_screening_fields():
    rows = format_profiles_rows([("sid", _profile_doc(
        name="Иван", email="ivan@x.com",
        age_range="35-45", sector="goods", time_eater="sales",
    ))])
    r = rows[1]
    assert r[PROFILES_HEADERS.index("имя")] == "Иван"
    assert r[PROFILES_HEADERS.index("email")] == "ivan@x.com"
    assert r[PROFILES_HEADERS.index("возраст")] == "35-45"
    assert r[PROFILES_HEADERS.index("сектор")] == "goods"
    assert r[PROFILES_HEADERS.index("что съедает время")] == "sales"
    assert r[PROFILES_HEADERS.index("phase")] == "point_a"


def test_format_profiles_rows_includes_telegram_username():
    """Если в profile есть telegram_username — выводим как @username."""
    doc = _profile_doc(telegram_username="anna_doe")
    rows = format_profiles_rows([("sid", doc)])
    assert rows[1][PROFILES_HEADERS.index("@telegram")] == "@anna_doe"


def test_format_profiles_rows_empty_telegram_when_missing():
    """Если telegram_username не задан — колонка пуста."""
    doc = _profile_doc()
    rows = format_profiles_rows([("sid", doc)])
    assert rows[1][PROFILES_HEADERS.index("@telegram")] == ""


# ---- Лиды для связи --------------------------------------------------------

def _done_doc(**overrides):
    d = _profile_doc(**overrides.pop("profile", {}))
    d["state"]["conversation"]["phase"] = "done"
    d["business_spec"] = overrides.get("business_spec", "biz")
    d["dev_spec"] = overrides.get("dev_spec", "dev")
    d["state"]["conversation"]["estimate_text"] = overrides.get("estimate", "2-3 недели")
    d["state"]["resources"] = overrides.get("resources", {"monthly_budget_rub": 5000})
    d["telegram_chat_id"] = overrides.get("telegram_chat_id", 4242)
    return d


def test_format_leads_rows_filters_done_only():
    docs = [
        ("d1", _done_doc()),
        ("p1", _profile_doc()),  # phase=point_a, не done
        ("d2", _done_doc()),
    ]
    rows = format_leads_rows(docs)
    assert rows[0] == LEADS_HEADERS
    assert len(rows) == 3  # header + 2 done
    sids = [r[0] for r in rows[1:]]
    assert sids == ["d1", "d2"]


def test_format_leads_rows_preserves_status():
    rows = format_leads_rows(
        [("d1", _done_doc())],
        existing_statuses={"d1": "оплачено"},
    )
    assert rows[1][LEADS_HEADERS.index("статус")] == "оплачено"


def test_format_leads_rows_empty_status_when_not_in_existing():
    rows = format_leads_rows([("d1", _done_doc())])
    assert rows[1][LEADS_HEADERS.index("статус")] == ""


def test_format_leads_rows_includes_estimate_text():
    """Колонка «оценка времени» — берётся из state.conversation.estimate_text."""
    rows = format_leads_rows([("d1", _done_doc(estimate="2-3 недели"))])
    r = rows[1]
    assert r[LEADS_HEADERS.index("оценка времени")] == "2-3 недели"


def test_format_leads_rows_includes_telegram_username_with_at():
    """В Лидах @username выводится с @-знаком для удобной кликаемости."""
    doc = _done_doc(profile={"telegram_username": "anna_doe"})
    rows = format_leads_rows([("d1", doc)])
    assert rows[1][LEADS_HEADERS.index("@telegram")] == "@anna_doe"


# ---- Воронка ---------------------------------------------------------------

def test_build_funnel_rows_cumulative_counts():
    docs = []
    # 5 в screening, 3 в point_a, 2 в done — итого 10
    for _ in range(5):
        d = _doc()
        d["state"]["conversation"]["phase"] = "screening"
        docs.append(("s", d))
    for _ in range(3):
        d = _doc()
        d["state"]["conversation"]["phase"] = "point_a"
        docs.append(("a", d))
    for _ in range(2):
        d = _doc()
        d["state"]["conversation"]["phase"] = "done"
        docs.append(("d", d))

    rows = build_funnel_rows(docs)
    by_label = {row[0]: row[1] for row in rows[1:]}
    # Cumulative «достигли фазы X или дальше»:
    assert by_label["пришли (всего)"] == "10"
    assert by_label["начали screening"] == "10"        # 5+3+2
    assert by_label["прошли screening, в discovery"] == "5"   # 3+2
    assert by_label["получили план"] == "2"


def test_build_funnel_rows_percentages():
    docs = []
    # 10 в screening, 4 в done. screening_complete = 4 (все done прошли screening)
    for _ in range(10):
        d = _doc()
        d["state"]["conversation"]["phase"] = "screening"
        docs.append(("s", d))
    for _ in range(4):
        d = _doc()
        d["state"]["conversation"]["phase"] = "done"
        docs.append(("d", d))

    rows = build_funnel_rows(docs)
    pct_total_idx = FUNNEL_HEADERS.index("% от total")
    pct_screen_idx = FUNNEL_HEADERS.index("% от screening_complete")
    by_label = {row[0]: row for row in rows[1:]}

    # «получили план» = 4 из 14 → 28.6% total; 4 из 4 (screening_complete) → 100%
    done_row = by_label["получили план"]
    assert done_row[pct_total_idx] == "28.6%"
    assert done_row[pct_screen_idx] == "100.0%"


# ---- Жалобы и фидбек -------------------------------------------------------

def test_format_comments_rows_matches_bot_question_by_turn():
    transcript = [
        {"role": "assistant", "content": "Q1?"},
        {"role": "user", "content": "A1"},
        {"role": "assistant", "content": "Q2?"},
        {"role": "user", "content": "A2"},
    ]
    classifications = [
        {"turn": 2, "type": "comment", "text": "вопрос путаный", "rationale": "..."},
    ]
    doc = _profile_doc()
    doc["transcript"] = transcript
    doc["classifications"] = classifications

    rows = format_comments_rows([("sid", doc)])
    assert rows[0] == COMMENTS_HEADERS
    assert len(rows) == 2
    r = rows[1]
    assert r[COMMENTS_HEADERS.index("turn")] == "2"
    assert r[COMMENTS_HEADERS.index("текст комментария")] == "вопрос путаный"
    assert r[COMMENTS_HEADERS.index("вопрос бота")] == "Q2?"


def test_format_comments_rows_includes_owner_name():
    doc = _profile_doc(name="Иван")
    doc["transcript"] = [
        {"role": "assistant", "content": "Q?"},
        {"role": "user", "content": "A"},
    ]
    doc["classifications"] = [
        {"turn": 1, "type": "comment", "text": "что-то", "rationale": "..."},
    ]
    rows = format_comments_rows([("sid", doc)])
    assert rows[1][COMMENTS_HEADERS.index("имя")] == "Иван"


def test_format_comments_rows_skips_non_comment_classifications():
    doc = _profile_doc()
    doc["transcript"] = [
        {"role": "assistant", "content": "Q?"},
        {"role": "user", "content": "A"},
    ]
    doc["classifications"] = [
        {"turn": 1, "type": "answer", "text": "ответ", "rationale": "..."},
        {"turn": 1, "type": "comment", "text": "коммент", "rationale": "..."},
    ]
    rows = format_comments_rows([("sid", doc)])
    # header + 1 comment (answer skipped)
    assert len(rows) == 2
    assert rows[1][COMMENTS_HEADERS.index("текст комментария")] == "коммент"


# ---- Активность по дням ----------------------------------------------------

def test_build_daily_rows_groups_by_started_date():
    docs = [
        ("a", _doc(updated_at=datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc),
                   state={"profile": {}, "point_a": {}, "point_b": {}, "resources": {},
                          "conversation": {"phase": "screening", "turn_count": 0,
                                            "screening_step": 0,
                                            "started_at": datetime(2026, 5, 8, 10, 0,
                                                                   tzinfo=timezone.utc).isoformat()}})),
        ("b", _doc(updated_at=datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc),
                   state={"profile": {}, "point_a": {}, "point_b": {}, "resources": {},
                          "conversation": {"phase": "screening", "turn_count": 0,
                                            "screening_step": 0,
                                            "started_at": datetime(2026, 5, 8, 12, 0,
                                                                   tzinfo=timezone.utc).isoformat()}})),
        ("c", _doc(updated_at=datetime(2026, 5, 9, 10, 0, tzinfo=timezone.utc),
                   state={"profile": {}, "point_a": {}, "point_b": {}, "resources": {},
                          "conversation": {"phase": "screening", "turn_count": 0,
                                            "screening_step": 0,
                                            "started_at": datetime(2026, 5, 9, 10, 0,
                                                                   tzinfo=timezone.utc).isoformat()}})),
    ]
    rows = build_daily_rows(docs)
    assert rows[0] == DAILY_HEADERS
    by_day = {r[0]: r for r in rows[1:]}
    assert by_day["2026-05-08"][1] == "2"  # 2 new on 8th
    assert by_day["2026-05-09"][1] == "1"


def test_build_daily_rows_done_uses_ended_at():
    started = datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc)
    ended = datetime(2026, 5, 9, 11, 0, tzinfo=timezone.utc)
    doc = _doc(state={
        "profile": {}, "point_a": {}, "point_b": {}, "resources": {},
        "conversation": {
            "phase": "done", "turn_count": 5, "screening_step": 4,
            "started_at": started.isoformat(),
            "ended_at": ended.isoformat(),
        },
    })
    rows = build_daily_rows([("sid", doc)])
    by_day = {r[0]: r for r in rows[1:]}
    # new on 5/8, done on 5/9
    assert by_day["2026-05-08"][1] == "1"  # new
    assert by_day["2026-05-08"][2] == "0"  # done — нет (ended_at в другой день)
    assert by_day["2026-05-09"][1] == "0"
    assert by_day["2026-05-09"][2] == "1"


# ---- write_sheet (atomic-ish write-then-clear) ----------------------------


def _make_spreadsheet_with_existing_ws():
    """Helper: spreadsheet whose `worksheet(title)` returns a fresh MagicMock."""
    spreadsheet = MagicMock()
    ws = MagicMock()
    spreadsheet.worksheet.return_value = ws
    return spreadsheet, ws


def test_write_sheet_writes_then_clears_trailing_range():
    """write_sheet must call ws.update first, then ws.batch_clear with a
    trailing range starting at row len(rows)+1."""
    spreadsheet, ws = _make_spreadsheet_with_existing_ws()
    rows = [
        ["h1", "h2", "h3"],
        ["a", "b", "c"],
        ["d", "e", "f"],
    ]
    result = write_sheet(spreadsheet, "Сводка", rows)

    assert result is ws
    ws.update.assert_called_once_with(values=rows, range_name="A1")
    ws.batch_clear.assert_called_once()
    cleared_ranges = ws.batch_clear.call_args.args[0]
    # 3 rows of data → trailing clear starts at row 4. width=3 → end col = "C".
    assert cleared_ranges == ["A4:C5000"]
    # Update must happen before batch_clear (write-then-clear ordering).
    method_order = [c[0] for c in ws.method_calls]
    assert method_order.index("update") < method_order.index("batch_clear")


def test_write_sheet_caps_width_at_column_z():
    """If row width > 26, trailing-clear end column is capped at Z."""
    spreadsheet, ws = _make_spreadsheet_with_existing_ws()
    wide_row = [f"c{i}" for i in range(30)]  # 30 columns
    write_sheet(spreadsheet, "Wide", [wide_row])

    cleared_ranges = ws.batch_clear.call_args.args[0]
    assert cleared_ranges == ["A2:Z5000"]


def test_write_sheet_empty_rows_is_noop():
    """If rows == [], do not call update or batch_clear or clear — protects
    against formatter bugs from erasing the sheet."""
    spreadsheet, ws = _make_spreadsheet_with_existing_ws()
    result = write_sheet(spreadsheet, "Сводка", [])

    assert result is ws
    ws.update.assert_not_called()
    ws.batch_clear.assert_not_called()
    ws.clear.assert_not_called()


def test_write_sheet_swallows_batch_clear_failure():
    """If batch_clear raises (network/quota/etc.), function returns ws
    without raising — sheet has correct new content + maybe stale trailing
    rows is far better than an empty sheet."""
    spreadsheet, ws = _make_spreadsheet_with_existing_ws()
    ws.batch_clear.side_effect = RuntimeError("API quota exceeded")
    rows = [["h1"], ["a"]]

    # Should not raise.
    result = write_sheet(spreadsheet, "Сводка", rows)

    assert result is ws
    ws.update.assert_called_once_with(values=rows, range_name="A1")
    ws.batch_clear.assert_called_once()


def test_write_sheet_creates_worksheet_when_missing():
    """If spreadsheet.worksheet(title) raises (sheet doesn't exist), call
    add_worksheet and proceed with the new ws."""
    spreadsheet = MagicMock()
    spreadsheet.worksheet.side_effect = Exception("not found")
    new_ws = MagicMock()
    spreadsheet.add_worksheet.return_value = new_ws
    rows = [["h"], ["x"]]

    result = write_sheet(spreadsheet, "NewSheet", rows)

    assert result is new_ws
    spreadsheet.add_worksheet.assert_called_once()
    new_ws.update.assert_called_once_with(values=rows, range_name="A1")


def test_write_sheet_roundtrip_with_header_and_data_rows():
    """Existing roundtrip behavior: header row + data rows are passed through
    to ws.update verbatim, in their original order."""
    spreadsheet, ws = _make_spreadsheet_with_existing_ws()
    rows = [
        list(SUMMARY_HEADERS),
        ["sid-1", "v2", "2026-05-08 10:00", "", "", "screening", "0", "0", "", "", "", "", "", "", "", "", "", ""],
    ]
    write_sheet(spreadsheet, "Сводка", rows)
    ws.update.assert_called_once_with(values=rows, range_name="A1")


def test_build_daily_rows_counts_comments():
    started = datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc)
    doc = _doc(
        classifications=[
            {"turn": 1, "type": "comment", "text": "x", "rationale": "..."},
            {"turn": 2, "type": "comment", "text": "y", "rationale": "..."},
            {"turn": 3, "type": "answer", "text": "z", "rationale": "..."},
        ],
        state={
            "profile": {}, "point_a": {}, "point_b": {}, "resources": {},
            "conversation": {
                "phase": "point_a", "turn_count": 3, "screening_step": 4,
                "started_at": started.isoformat(),
            },
        },
    )
    rows = build_daily_rows([("sid", doc)])
    by_day = {r[0]: r for r in rows[1:]}
    assert by_day["2026-05-08"][3] == "2"  # 2 comments на эту дату
