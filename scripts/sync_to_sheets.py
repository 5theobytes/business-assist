"""Зеркалит Firestore (`sessions`) в Google Sheet с тремя листами + спеки в Drive.

Запуск:
    python -m scripts.sync_to_sheets

Env:
    FIRESTORE_DATABASE     — имя БД (по умолчанию '(default)')
    FIRESTORE_COLLECTION   — имя коллекции (по умолчанию 'sessions')
    GOOGLE_SHEETS_ID       — ID целевой таблицы (часть URL после /d/)
    GOOGLE_DRIVE_FOLDER_ID — (опционально) ID папки Drive для выгрузки спек

Service-account, что и для Firestore, должен иметь Editor-доступ и к листу,
и (если задан) к папке Drive.

Листы:
  • «Сводка»   — одна строка на сессию: профиль, точка A/Б/Ресурсы, ссылки на спеки
  • «Диалоги» — одна строка на пару (бот→владелец): question, answer, comment +
                логирование внутренних мыслей бота (target_field, hint, rationale)
  • «Аналитика» — KPI (включая total_comments)

Спеки больших размеров уезжают в Drive как `<session_id>_business.md` и
`<session_id>_dev.md`; в «Сводке» — только webViewLink.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

# bootstrap materializes GCP credentials from GCP_* env into a temp JSON file
from app import bootstrap  # noqa: F401  (side-effect: must run before google imports)

SUMMARY_SHEET = "Сводка"
INCOMPLETE_SHEET = "Незавершённые опросы"
COMPLETED_SHEET = "Завершённые опросы"
ANALYTICS_SHEET = "Аналитика"
SPECS_SHEET = "Спеки"
PROFILES_SHEET = "Профили"
LEADS_SHEET = "Лиды для связи"
FUNNEL_SHEET = "Воронка"
COMMENTS_SHEET = "Жалобы и фидбек"
DAILY_SHEET = "Активность по дням"
LEGACY_DIALOGS_SHEET = "Диалоги"  # удалить если есть, сохраняется только для del_worksheet

LIST_SEP = "; "

SUMMARY_HEADERS = [
    # identification + timestamps
    "session_id", "telegram_chat_id", "workflow",
    "started_at", "ended_at", "duration", "last_update",
    # conversation state
    "phase", "turn_count", "num_classifications", "num_comments",
    # profile (screening)
    "возраст", "пол", "сектор", "что съедает время",
    # point A
    "бизнес", "стадия", "команда", "каналы", "инструменты", "боли", "объём в день",
    # point B
    "основная ценность", "желаемый результат", "метрика успеха", "вне рамок",
    "срок", "аудитория", "user_flows", "fr_owner",
    # resources
    "бюджет ₽/мес", "есть подписка ИИ", "провайдер ИИ", "интеграции есть",
    "интеграции нужны", "тех.опытность", "поддержка", "ограничения", "канал",
]

DIALOGS_HEADERS = [
    "session_id", "telegram_chat_id", "turn", "started_at",
    "phase", "target_section", "target_field", "owner_question_hint",
    "question", "answer", "comment",
    "classifier_rationale", "internal_rationale",
]

SPECS_HEADERS = [
    "session_id", "telegram_chat_id",
    "started_at", "ended_at", "duration",
    "business_spec", "dev_spec",
]

PROFILES_HEADERS = [
    "session_id", "telegram_chat_id", "@telegram",
    "имя", "email", "возраст", "пол", "сектор", "что съедает время",
    "started_at", "phase",
]

LEADS_HEADERS = [
    "session_id", "имя", "email",
    "telegram_chat_id", "@telegram",
    "сектор", "бюджет ₽/мес",
    "started_at", "ended_at", "duration", "оценка времени",
    "статус",   # МАНУАЛ — не перезаписывается при sync
]

FUNNEL_HEADERS = [
    "фаза", "всего сессий", "% от screening_complete", "% от total",
]

COMMENTS_HEADERS = [
    "session_id", "имя", "when", "turn",
    "текст комментария", "вопрос бота", "rationale",
]

DAILY_HEADERS = ["день", "новые сессии", "завершённые", "комментарии"]


# ---------------------------------------------------------------------------
# Pure formatters (testable without network)
# ---------------------------------------------------------------------------

def _join(xs: Any) -> str:
    if not xs:
        return ""
    if isinstance(xs, list):
        return LIST_SEP.join(str(x) for x in xs if x not in (None, ""))
    return str(xs)


def _str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "да" if v else "нет"
    return str(v)


def _ts(v: Any) -> str:
    """Render any datetime-like value as 'YYYY-MM-DD HH:MM' UTC."""
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
    if isinstance(v, str) and v:
        # ISO-string from pydantic dump → parse back if possible
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(
                timezone.utc,
            ).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return v
    return str(v) if v else ""


def _to_dt(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc)
    if isinstance(v, str) and v:
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def _format_duration(start: datetime | None, end: datetime | None) -> str:
    if start is None or end is None:
        return ""
    delta = end - start
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        return ""
    if total_seconds < 60:
        return f"{total_seconds}с"
    minutes, seconds = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes}м {seconds:02d}с"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}ч {minutes:02d}м"


def _session_dates(data: dict[str, Any]) -> tuple[datetime | None, datetime | None]:
    """Return (started_at, ended_at) for a session doc.

    Falls back to Firestore-level updated_at for sessions that pre-date the
    new conversation.started_at / ended_at fields:
      - if started_at missing → use updated_at as best-effort start
      - if ended_at missing AND phase=='done' → use updated_at as end
      - else ended_at stays None
    """
    state = data.get("state") or {}
    conv = state.get("conversation") or {}
    started = _to_dt(conv.get("started_at"))
    ended = _to_dt(conv.get("ended_at"))
    upd = _to_dt(data.get("updated_at"))

    if started is None and upd is not None:
        started = upd
    if ended is None and (conv.get("phase") == "done") and upd is not None:
        ended = upd
    return started, ended


def _count_comments(data: dict[str, Any]) -> int:
    return sum(1 for c in (data.get("classifications") or []) if c.get("type") == "comment")


def summary_row(doc_id: str, data: dict[str, Any]) -> list[str]:
    state = data.get("state") or {}
    profile = state.get("profile") or {}
    a = state.get("point_a") or {}
    b = state.get("point_b") or {}
    res = state.get("resources") or {}
    conv = state.get("conversation") or {}

    started, ended = _session_dates(data)

    return [
        doc_id,
        _str(data.get("telegram_chat_id")),
        _str(data.get("workflow_name")),
        _ts(started),
        _ts(ended),
        _format_duration(started, ended),
        _ts(data.get("updated_at")),
        _str(conv.get("phase")),
        _str(conv.get("turn_count")),
        _str(len(data.get("classifications") or [])),
        _str(_count_comments(data)),
        _str(profile.get("age_range")),
        _str(profile.get("gender")),
        _str(profile.get("sector")),
        _str(profile.get("time_eater")),
        _str(a.get("business_type")),
        _str(a.get("stage")),
        _str(a.get("team_size")),
        _join(a.get("channels")),
        _join(a.get("tools_in_use")),
        _join(a.get("pain_points")),
        _str(a.get("daily_volume")),
        _str(b.get("primary_value")),
        _str(b.get("desired_outcome")),
        _str(b.get("success_metric")),
        _join(b.get("out_of_scope")),
        _str(b.get("timeframe")),
        _str(b.get("audience")),
        _join(b.get("user_flows")),
        _join(b.get("functional_requirements_owner")),
        _str(res.get("monthly_budget_rub")),
        _str(res.get("has_llm_key")),
        _str(res.get("llm_provider")),
        _join(res.get("integrations_available")),
        _join(res.get("integrations_needed")),
        _str(res.get("tech_savviness")),
        _str(res.get("maintainer")),
        _join(res.get("deployment_constraints")),
        _str(res.get("preferred_channel")),
    ]


def format_summary_rows(
    docs: Iterable[tuple[str, dict[str, Any]]],
) -> list[list[str]]:
    rows = [SUMMARY_HEADERS]
    for doc_id, data in docs:
        rows.append(summary_row(doc_id, data))
    return rows


def _classifications_for_pair(
    classifications: list[dict[str, Any]],
    pair_index: int,
    screening_completed: bool,
) -> tuple[str, str]:
    """Return (classifier_rationale, comments_joined) for a transcript Q-A pair.

    Matching strategy:
      - if screening completed AND pair_index <= 4: match by turn=pair_index AND
        rationale.startswith("screening")
      - else: match by turn=(pair_index - 4 if screening completed else pair_index)
        AND rationale does NOT start with "screening"
    """
    if screening_completed and pair_index <= 4:
        wanted_turn = pair_index
        is_screening = True
    elif screening_completed:
        wanted_turn = pair_index - 4
        is_screening = False
    else:
        wanted_turn = pair_index
        is_screening = False

    classifier_rationale = ""
    comments: list[str] = []
    for c in classifications:
        if c.get("turn") != wanted_turn:
            continue
        rat = c.get("rationale") or ""
        rat_is_screening = rat.startswith("screening")
        if rat_is_screening != is_screening:
            continue
        if c.get("type") == "answer" and not classifier_rationale:
            classifier_rationale = rat
        elif c.get("type") == "comment":
            comments.append(c.get("text") or "")
    return classifier_rationale, "\n".join(c for c in comments if c)


# Текстовый разделитель между сессиями: одна строка с тире во всех колонках.
# Раньше использовали 3-строчный pattern (пустая/серая/пустая) с batch_format на
# среднюю строку — но `write_sheet` делает write-then-clear (clear только VALUES,
# не FORMATTING). Это значит, что при сжатии данных серые строки от прошлого
# sync'а оставались в новых местах поверх контента → визуальный мусор.
#
# Текстовый разделитель не зависит от состояния форматирования листа: всегда
# виден, всегда стирается вместе с values при clear.
_SEP_DASHES = "─" * 24


@dataclass
class QACSheet:
    """Готовый набор строк диалогов с текстовыми разделителями между сессиями.

    `gray_row_indices` сохранён для обратной совместимости (всегда пустой список).
    Раньше использовался для batch_format поверх серых строк; теперь разделитель
    встроен в текст ячеек и не требует форматирования.
    """
    rows: list[list[str]] = field(default_factory=list)
    gray_row_indices: list[int] = field(default_factory=list)


def _qac_rows_for_session(
    doc_id: str, data: dict[str, Any],
) -> list[list[str]]:
    """Все Q-A строки одной сессии (без header, без разделителей)."""
    transcript = data.get("transcript") or []
    classifications = data.get("classifications") or []
    chat_id = _str(data.get("telegram_chat_id"))
    started, _ = _session_dates(data)
    when = _ts(started)
    state = data.get("state") or {}
    screening_step = (state.get("conversation") or {}).get("screening_step") or 0
    screening_completed = screening_step >= 4

    rows: list[list[str]] = []
    pair_index = 0
    for i in range(len(transcript) - 1):
        if transcript[i].get("role") != "assistant":
            continue
        if transcript[i + 1].get("role") != "user":
            continue
        pair_index += 1
        assistant = transcript[i]
        user = transcript[i + 1]
        meta = assistant.get("meta") or {}
        classifier_rat, comment = _classifications_for_pair(
            classifications, pair_index, screening_completed,
        )
        rows.append([
            doc_id,
            chat_id,
            _str(pair_index),
            when,
            _str(meta.get("phase")),
            _str(meta.get("target_section")),
            _str(meta.get("target_field")),
            _str(meta.get("owner_question_hint")),
            _str(assistant.get("content")),
            _str(user.get("content")),
            comment,
            classifier_rat,
            _str(meta.get("internal_rationale")),
        ])
    return rows


def _is_done(data: dict[str, Any]) -> bool:
    state = data.get("state") or {}
    conv = state.get("conversation") or {}
    return conv.get("phase") == "done"


def format_qac_split(
    docs: Iterable[tuple[str, dict[str, Any]]],
) -> tuple[QACSheet, QACSheet]:
    """Разделяет ходы по фазе: incomplete (phase != done) и completed (== done).

    Между сессиями вставляет одну строку-разделитель с тире во всех колонках
    (`_SEP_DASHES`). Текстовый разделитель не зависит от состояния форматирования
    листа — он всегда виден и всегда стирается вместе с values при clear, в
    отличие от старого подхода с серым фоном (который оставался от прошлого
    sync'а поверх новых данных, см. write_sheet docstring).

    `gray_row_indices` всегда пустой — поле сохранено для обратной совместимости.
    """
    width = len(DIALOGS_HEADERS)
    separator_row = [_SEP_DASHES] * width
    docs_list = list(docs)  # фиксируем — будем итерировать дважды

    def build(filter_done: bool) -> QACSheet:
        sheet = QACSheet(rows=[list(DIALOGS_HEADERS)], gray_row_indices=[])
        first = True
        for doc_id, data in docs_list:
            if _is_done(data) != filter_done:
                continue
            session_rows = _qac_rows_for_session(doc_id, data)
            if not session_rows:
                continue
            if not first:
                sheet.rows.append(list(separator_row))  # текстовый разделитель
            sheet.rows.extend(session_rows)
            first = False
        return sheet

    return build(filter_done=False), build(filter_done=True)


def apply_gray_separators(
    worksheet, gray_row_indices: list[int], num_cols: int,
) -> None:
    """Раньше красила указанные строки серым фоном через batch_format.

    Сохранена как no-op для обратной совместимости с вызовами из run_full_sync —
    текущий `format_qac_split` всегда возвращает пустой `gray_row_indices`,
    разделитель встроен текстом в строку (`_SEP_DASHES`), формат-вызовы больше
    не нужны. Если когда-нибудь нужно будет вернуть серый фон, потребуется
    сначала чистить старое форматирование (см. комментарий в format_qac_split).
    """
    if not gray_row_indices:
        return
    # Колонки — A..Z для 13 столбцов это всегда A..Z (≤26)
    if num_cols > 26:
        # safety net: импортируем gspread.utils только при необходимости
        from gspread.utils import rowcol_to_a1
        end_col = rowcol_to_a1(1, num_cols).rstrip("1")
    else:
        end_col = chr(ord("A") + num_cols - 1)
    fmts = [
        {
            "range": f"A{r}:{end_col}{r}",
            "format": {"backgroundColor": {"red": 0.85, "green": 0.85, "blue": 0.85}},
        }
        for r in gray_row_indices
    ]
    worksheet.batch_format(fmts)


def format_specs_full_rows(
    docs: Iterable[tuple[str, dict[str, Any]]],
) -> list[list[str]]:
    """Лист «Спеки» — id + даты + duration + полный текст обеих спек в ячейках.

    Включаются только сессии, у которых хотя бы одна из спек уже сгенерирована.
    """
    rows: list[list[str]] = [SPECS_HEADERS]
    for doc_id, data in docs:
        biz = data.get("business_spec")
        dev = data.get("dev_spec")
        if not (biz or dev):
            continue
        started, ended = _session_dates(data)
        rows.append([
            doc_id,
            _str(data.get("telegram_chat_id")),
            _ts(started),
            _ts(ended),
            _format_duration(started, ended),
            _str(biz),
            _str(dev),
        ])
    return rows


# ---------------------------------------------------------------------------
# Profiles / Leads / Funnel / Comments / Daily
# ---------------------------------------------------------------------------

def format_profiles_rows(
    docs: Iterable[tuple[str, dict[str, Any]]],
) -> list[list[str]]:
    """Лист «Профили» — компактный контакт-лист, одна строка на сессию."""
    rows: list[list[str]] = [PROFILES_HEADERS]
    for doc_id, data in docs:
        state = data.get("state") or {}
        profile = state.get("profile") or {}
        conv = state.get("conversation") or {}
        started, _ = _session_dates(data)
        tg_username = profile.get("telegram_username")
        tg_handle = f"@{tg_username}" if tg_username else ""
        rows.append([
            doc_id,
            _str(data.get("telegram_chat_id")),
            tg_handle,
            _str(profile.get("name")),
            _str(profile.get("email")),
            _str(profile.get("age_range")),
            _str(profile.get("gender")),
            _str(profile.get("sector")),
            _str(profile.get("time_eater")),
            _ts(started),
            _str(conv.get("phase")),
        ])
    return rows


def format_leads_rows(
    docs: Iterable[tuple[str, dict[str, Any]]],
    existing_statuses: dict[str, str] | None = None,
) -> list[list[str]]:
    """Лист «Лиды для связи» — только завершённые сессии, для CRM-flow.

    Колонка «статус» (последняя) — мануальная: при наличии `existing_statuses[sid]`
    переносим её, иначе пустая. Так sync не затирает ручные правки.

    Полный текст спек смотрим в листе «Спеки» по тому же session_id (Ctrl+F).
    """
    existing_statuses = existing_statuses or {}
    rows: list[list[str]] = [LEADS_HEADERS]
    for doc_id, data in docs:
        if not _is_done(data):
            continue
        state = data.get("state") or {}
        profile = state.get("profile") or {}
        resources = state.get("resources") or {}
        conv = state.get("conversation") or {}
        started, ended = _session_dates(data)
        tg_username = profile.get("telegram_username")
        tg_handle = f"@{tg_username}" if tg_username else ""
        rows.append([
            doc_id,
            _str(profile.get("name")),
            _str(profile.get("email")),
            _str(data.get("telegram_chat_id")),
            tg_handle,
            _str(profile.get("sector")),
            _str(resources.get("monthly_budget_rub")),
            _ts(started),
            _ts(ended),
            _format_duration(started, ended),
            _str(conv.get("estimate_text")),
            existing_statuses.get(doc_id, ""),
        ])
    return rows


def _read_existing_leads_statuses(spreadsheet) -> dict[str, str]:
    """Прочесть текущий лист «Лиды для связи» и вернуть {session_id: status}.

    Колонка «статус» ищется по ИМЕНИ заголовка, не по индексу — это переживает
    изменения порядка колонок (например, добавление @telegram).

    Используется для preservation статуса перед write_sheet (write_sheet делает
    clear()). Если листа нет / он пустой / нет колонки — вернёт {}.
    """
    try:
        ws = spreadsheet.worksheet(LEADS_SHEET)
    except Exception:
        return {}
    try:
        all_rows = ws.get_all_values()
    except Exception:
        return {}
    if len(all_rows) < 2:
        return {}
    header = all_rows[0]
    try:
        status_idx = header.index("статус")
    except ValueError:
        return {}
    if 0 not in range(len(header)):  # session_id в позиции 0 ожидается
        return {}
    out: dict[str, str] = {}
    for row in all_rows[1:]:
        if not row:
            continue
        sid = row[0] if len(row) > 0 else ""
        status = row[status_idx] if len(row) > status_idx else ""
        if sid and status:
            out[sid] = status
    return out


# Канонический порядок фаз для воронки.
_PHASE_ORDER = [
    "greeting", "screening", "point_a", "point_b", "resources", "spec_review", "done",
]
_PHASE_LABELS = {
    "greeting":    "пришли (всего)",
    "screening":   "начали screening",
    "point_a":     "прошли screening, в discovery",
    "point_b":     "перешли в Точку B",
    "resources":   "обсуждаем ресурсы",
    "spec_review": "пересказ перед планом",
    "done":        "получили план",
}


def build_funnel_rows(
    docs: Iterable[tuple[str, dict[str, Any]]],
) -> list[list[str]]:
    """Лист «Воронка» — кумулятивная конверсия по фазам.

    «Достигли фазы X или дальше» = сумма сессий с phase index ≥ index(X).
    `screening_complete` = достигли `point_a` или дальше (т.е. прошли screening).
    """
    docs_list = list(docs)
    n_total = len(docs_list)
    if n_total == 0:
        return [FUNNEL_HEADERS]

    phase_index = {p: i for i, p in enumerate(_PHASE_ORDER)}
    by_current = {p: 0 for p in _PHASE_ORDER}
    for _, d in docs_list:
        state = d.get("state") or {}
        conv = state.get("conversation") or {}
        ph = conv.get("phase") or "greeting"
        if ph not in by_current:
            ph = "greeting"
        by_current[ph] += 1

    # Кумулятивно: достигли фазы X или дальше
    counts_at_or_after = []
    for i, p in enumerate(_PHASE_ORDER):
        cnt = sum(by_current[q] for q in _PHASE_ORDER[i:])
        counts_at_or_after.append(cnt)

    n_screening_complete = counts_at_or_after[phase_index["point_a"]]

    def _pct(num: int, denom: int) -> str:
        if denom <= 0:
            return "—"
        return f"{round(100 * num / denom, 1)}%"

    rows: list[list[str]] = [FUNNEL_HEADERS]
    for ph in _PHASE_ORDER:
        i = phase_index[ph]
        cnt = counts_at_or_after[i]
        rows.append([
            _PHASE_LABELS[ph],
            _str(cnt),
            _pct(cnt, n_screening_complete) if i >= phase_index["point_a"] else "—",
            _pct(cnt, n_total),
        ])
    return rows


def format_comments_rows(
    docs: Iterable[tuple[str, dict[str, Any]]],
) -> list[list[str]]:
    """Лист «Жалобы и фидбек» — все классификации kind=comment, отдельным потоком.

    Для каждого comment'а ищем вопрос бота: walk transcript pairs, берём N-ю пару.
    """
    rows: list[list[str]] = [COMMENTS_HEADERS]
    for doc_id, data in docs:
        state = data.get("state") or {}
        profile = state.get("profile") or {}
        transcript = data.get("transcript") or []
        classifications = data.get("classifications") or []

        started, _ = _session_dates(data)
        when = _ts(started)
        name = _str(profile.get("name"))

        # Pre-compute pairs (Q, _) by index for O(1) lookup
        pairs: list[str] = []  # 1-indexed via [turn-1]
        for i in range(len(transcript) - 1):
            if (
                transcript[i].get("role") == "assistant"
                and transcript[i + 1].get("role") == "user"
            ):
                pairs.append(transcript[i].get("content") or "")

        for c in classifications:
            if c.get("type") != "comment":
                continue
            turn = int(c.get("turn") or 0)
            text = c.get("text") or ""
            rationale = c.get("rationale") or ""
            bot_q = pairs[turn - 1] if 1 <= turn <= len(pairs) else ""
            rows.append([
                doc_id, name, when, _str(turn),
                text, bot_q, rationale,
            ])
    return rows


def build_daily_rows(
    docs: Iterable[tuple[str, dict[str, Any]]],
) -> list[list[str]]:
    """Лист «Активность по дням» — ежедневные счётчики.

    new = сессии, у которых started_at в этот день;
    done = сессии, у которых ended_at в этот день;
    comments = сумма kind=comment по сессиям с started_at в этот день.
    """
    from collections import defaultdict
    by_day_new: dict[str, int] = defaultdict(int)
    by_day_done: dict[str, int] = defaultdict(int)
    by_day_comments: dict[str, int] = defaultdict(int)

    for _, d in docs:
        started, ended = _session_dates(d)
        if started:
            day = started.date().isoformat()
            by_day_new[day] += 1
            by_day_comments[day] += _count_comments(d)
        if _is_done(d) and ended:
            day = ended.date().isoformat()
            by_day_done[day] += 1

    all_days = sorted(set(by_day_new) | set(by_day_done) | set(by_day_comments))
    rows: list[list[str]] = [DAILY_HEADERS]
    for day in all_days:
        rows.append([
            day,
            _str(by_day_new[day]),
            _str(by_day_done[day]),
            _str(by_day_comments[day]),
        ])
    return rows


def build_analytics_rows(
    docs: Iterable[tuple[str, dict[str, Any]]],
    *,
    now: datetime | None = None,
) -> list[list[str]]:
    docs_list = list(docs)
    now = now or datetime.now(timezone.utc)

    by_phase: dict[str, int] = {}
    with_biz = with_dev = 0
    total_turns = 0
    total_comments = 0
    last_24h = last_7d = 0
    completed_durations: list[float] = []  # seconds, only for done sessions

    for _, d in docs_list:
        state = d.get("state") or {}
        conv = state.get("conversation") or {}
        phase = conv.get("phase") or "unknown"
        by_phase[phase] = by_phase.get(phase, 0) + 1
        total_turns += int(conv.get("turn_count") or 0)
        total_comments += _count_comments(d)
        if d.get("business_spec"):
            with_biz += 1
        if d.get("dev_spec"):
            with_dev += 1
        upd = d.get("updated_at")
        if isinstance(upd, datetime):
            delta = now - upd.astimezone(timezone.utc)
            if delta <= timedelta(hours=24):
                last_24h += 1
            if delta <= timedelta(days=7):
                last_7d += 1
        started, ended = _session_dates(d)
        if started and ended and ended >= started:
            completed_durations.append((ended - started).total_seconds())

    n = len(docs_list)
    avg_turns = round(total_turns / n, 2) if n else 0
    avg_comments = round(total_comments / n, 2) if n else 0

    phases = ["greeting", "screening", "point_a", "point_b", "resources", "spec_review", "done"]
    rows: list[list[str]] = [["metric", "value"]]
    rows.append(["total_sessions", _str(n)])
    for ph in phases:
        rows.append([f"sessions_in_phase_{ph}", _str(by_phase.get(ph, 0))])
    for ph, count in sorted(by_phase.items()):
        if ph not in phases:
            rows.append([f"sessions_in_phase_{ph}", _str(count)])
    rows.append(["sessions_with_business_spec", _str(with_biz)])
    rows.append(["sessions_with_dev_spec", _str(with_dev)])
    rows.append(["avg_turn_count", _str(avg_turns)])
    rows.append(["total_comments", _str(total_comments)])
    rows.append(["avg_comments_per_session", _str(avg_comments)])
    avg_dur_min = (
        round(sum(completed_durations) / len(completed_durations) / 60, 1)
        if completed_durations else 0
    )
    rows.append(["completed_sessions", _str(len(completed_durations))])
    rows.append(["avg_duration_min", _str(avg_dur_min)])
    rows.append(["last_24h_new_sessions", _str(last_24h)])
    rows.append(["last_7d_new_sessions", _str(last_7d)])
    rows.append(["generated_at", _ts(now)])
    return rows


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def fetch_all_sessions() -> list[tuple[str, dict[str, Any]]]:
    from google.cloud import firestore

    db = os.environ.get("FIRESTORE_DATABASE") or "(default)"
    coll = os.environ.get("FIRESTORE_COLLECTION") or "sessions"
    client = firestore.Client(database=db)
    return [(snap.id, snap.to_dict() or {}) for snap in client.collection(coll).stream()]


def open_sheet(sheet_id: str):
    import gspread

    creds_path = os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
    with open(creds_path, "r", encoding="utf-8") as f:
        creds_dict = json.load(f)
    gc = gspread.service_account_from_dict(creds_dict)
    return gc.open_by_key(sheet_id)


def write_sheet(spreadsheet, title: str, rows: list[list[str]]):
    """Idempotent: ensure a worksheet with `title` exists, write rows starting
    at A1, then clear any trailing rows that were filled by previous (larger)
    data but are not part of the new data.

    Why write-then-clear (not clear-then-write): writing the new rows first
    preserves existing data if the update fails. Trailing rows are cleared
    only after the replacement data has been written.

    Empty-rows safety: if `rows` is empty we do NOT touch the sheet at all
    — protects against a formatter bug returning [] from accidentally
    nuking the sheet on the next sync.

    Returns the worksheet object so caller can apply formatting.
    """
    import logging
    log = logging.getLogger("sync_to_sheets")

    try:
        ws = spreadsheet.worksheet(title)
    except Exception:
        ws = spreadsheet.add_worksheet(
            title=title, rows=max(len(rows), 100), cols=max((len(rows[0]) if rows else 1), 10),
        )

    # Empty rows safety — never erase existing content on the basis of an empty payload.
    if not rows:
        return ws

    # 1) Write new rows — overwrites in-place from A1 down. If this fails, the
    #    exception propagates up (caller knows sync didn't succeed) and the
    #    previous content of the sheet is preserved (we haven't cleared yet).
    ws.update(values=rows, range_name="A1")

    width = max((len(r) for r in rows), default=1)
    width = min(width, 26)  # cap at column Z
    end_col = chr(ord("A") + width - 1)

    # 2) Reset cell formatting (background colour) on the entire data area.
    #    Row-based formatting can drift when sessions are inserted. Resetting
    #    the data area's background before applying current separators prevents
    #    old formatting from being attached to different rows.
    try:
        ws.format(
            f"A1:{end_col}5000",
            {"backgroundColor": {"red": 1, "green": 1, "blue": 1}},
        )
    except Exception as e:  # noqa: BLE001 — formatting reset is best-effort
        log.warning(
            "formatting reset failed for sheet %r: %s — "
            "may see stale gray bands until manual cleanup",
            title, e,
        )

    # 3) Clear any trailing rows from the previous (larger) write. If clear
    #    fails — sheet has the correct new content + maybe stale leftover rows;
    #    that's far better than an empty sheet, so just log and return.
    start_row = len(rows) + 1
    end_row = 5000
    if start_row <= end_row:
        trailing_range = f"A{start_row}:{end_col}{end_row}"
        try:
            ws.batch_clear([trailing_range])
        except Exception as e:  # noqa: BLE001 — graceful degradation
            log.warning(
                "trailing clear failed for sheet %r range %s: %s — "
                "sheet has correct new content but may have stale rows below",
                title, trailing_range, e,
            )

    return ws


def maybe_drop_legacy_dialogs_sheet(spreadsheet) -> None:
    """Старый лист «Диалоги» больше не используется — заменён на два."""
    try:
        legacy = spreadsheet.worksheet(LEGACY_DIALOGS_SHEET)
    except Exception:
        return
    try:
        spreadsheet.del_worksheet(legacy)
    except Exception:
        pass


def run_full_sync(*, raise_if_unconfigured: bool = False, verbose: bool = False) -> dict:
    """Прогоняет полный sync Firestore → Sheets.

    Возвращает {"sessions": N, "sheet_url": str|None}.
    Если `GOOGLE_SHEETS_ID` не задан и `raise_if_unconfigured=False` — тихо
    пропускает работу. Если True — кидает SystemExit.

    Drive не используется: полный текст спек кладётся в лист «Спеки» внутри
    самой таблицы (см. format_specs_full_rows).

    Используется и из CLI (`main()`), и из runtime-дебаунсера в проде.
    """
    import logging
    log = logging.getLogger("sync_to_sheets")

    sheet_id = os.environ.get("GOOGLE_SHEETS_ID")
    if not sheet_id:
        msg = "GOOGLE_SHEETS_ID не задан в окружении"
        if raise_if_unconfigured:
            raise SystemExit(msg)
        log.info("sync skipped: %s", msg)
        return {"sessions": 0, "sheet_url": None}

    docs = fetch_all_sessions()
    if verbose:
        print(f"Прочитано из Firestore: {len(docs)} сессий")

    sheet = open_sheet(sheet_id)
    write_sheet(sheet, SUMMARY_SHEET, format_summary_rows(docs))

    incomplete, completed = format_qac_split(docs)
    inc_ws = write_sheet(sheet, INCOMPLETE_SHEET, incomplete.rows)
    apply_gray_separators(inc_ws, incomplete.gray_row_indices, len(DIALOGS_HEADERS))
    comp_ws = write_sheet(sheet, COMPLETED_SHEET, completed.rows)
    apply_gray_separators(comp_ws, completed.gray_row_indices, len(DIALOGS_HEADERS))

    write_sheet(sheet, SPECS_SHEET, format_specs_full_rows(docs))
    write_sheet(sheet, PROFILES_SHEET, format_profiles_rows(docs))

    existing_statuses = _read_existing_leads_statuses(sheet)
    write_sheet(sheet, LEADS_SHEET, format_leads_rows(docs, existing_statuses))

    write_sheet(sheet, FUNNEL_SHEET, build_funnel_rows(docs))
    write_sheet(sheet, COMMENTS_SHEET, format_comments_rows(docs))
    write_sheet(sheet, DAILY_SHEET, build_daily_rows(docs))
    write_sheet(sheet, ANALYTICS_SHEET, build_analytics_rows(docs))
    maybe_drop_legacy_dialogs_sheet(sheet)

    sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
    if verbose:
        print(f"Готово. Лист: {sheet_url}")
    return {"sessions": len(docs), "sheet_url": sheet_url}


def main() -> None:
    """CLI entry-point. Тонкий wrapper над run_full_sync, чтобы краснеть при пустом env."""
    run_full_sync(raise_if_unconfigured=True, verbose=True)


if __name__ == "__main__":
    main()
