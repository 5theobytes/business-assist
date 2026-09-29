"""Unit-tests for `agent.reply` comment plumbing.

Avoid Firestore by injecting a fake store into the agent module, so these run
without GCP credentials.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeStore:
    saves: list[dict] = field(default_factory=list)
    classifications: list[dict] = field(default_factory=list)
    sessions: dict = field(default_factory=dict)

    def save(self, session, *, telegram_chat_id=None):
        self.sessions[session.id] = session
        self.saves.append({"session_id": session.id, "telegram_chat_id": telegram_chat_id})

    def load(self, sid):
        return self.sessions.get(sid)

    def find_by_telegram_chat(self, chat_id):
        return None

    def append_classification(self, session_id, *, turn, kind, text, rationale):
        self.classifications.append(
            {"session_id": session_id, "turn": turn, "kind": kind, "text": text, "rationale": rationale}
        )


def _setup_agent(monkeypatch):
    """Wire app.agent with MockLLM + FakeStore. Returns (agent, store, mock)."""
    from app import agent
    from app.llm import MockLLM
    from app.workflows import build

    def responder(system, messages, tools):
        if tools and tools[0]["name"] == "split_user_message":
            user_msg = messages[-1]["content"].split("СООБЩЕНИЕ ПОЛЬЗОВАТЕЛЯ:\n")[-1]
            return {"tool_use": {"name": "split_user_message", "input": {
                "answer_text": user_msg, "comment_text": None, "rationale": "test",
            }}}
        if tools and tools[0]["name"] == "update_state":
            return {"tool_use": {"name": "update_state", "input": {}}}
        return {"text": "Ок, что дальше?"}

    mock = MockLLM(responder=responder)
    store = FakeStore()
    monkeypatch.setattr(agent, "_llm", mock)
    monkeypatch.setattr(agent, "_workflow", build("v2", llm=mock))
    monkeypatch.setattr(agent, "_store", store)
    monkeypatch.setattr(agent, "_warm_cache", {})
    return agent, store, mock


def _ff_through_screening(agent, session):
    """Fast-forward through 6 screening turns: name (text), 4 choices, email."""
    agent.reply(session, "Тест-Юзер")        # Q1 name (free text)
    agent.reply(session, "1")                  # Q2 gender = female
    agent.reply(session, "1")                  # Q3 age = "<25"
    agent.reply(session, "test@example.com")  # Q4 email
    agent.reply(session, "1")                  # Q5 sector = services
    agent.reply(session, "1")                  # Q6 time_eater = client_comms


def test_inline_kommentariy_extracted_and_saved(monkeypatch):
    agent, store, _mock = _setup_agent(monkeypatch)
    session = agent.create_session()
    _ff_through_screening(agent, session)
    store.classifications.clear()

    agent.reply(session, "у меня магазин в инстаграме\nкомментарий: вопрос путаный")

    comment_rows = [c for c in store.classifications if c["kind"] == "comment"]
    assert any("вопрос путаный" in c["text"] for c in comment_rows)


def test_external_comment_saved_alongside_answer(monkeypatch):
    agent, store, _mock = _setup_agent(monkeypatch)
    session = agent.create_session()
    _ff_through_screening(agent, session)
    store.classifications.clear()

    agent.reply(
        session,
        "у меня репетиторство",
        external_comment="бот забыл спросить про сезонность",
    )

    comment_rows = [c for c in store.classifications if c["kind"] == "comment"]
    assert any("сезонность" in c["text"] for c in comment_rows)


def test_only_comment_does_not_advance_state(monkeypatch):
    agent, store, _mock = _setup_agent(monkeypatch)
    session = agent.create_session()
    _ff_through_screening(agent, session)
    turns_before = session.state.conversation.turn_count
    store.classifications.clear()

    reply = agent.reply(session, "комментарий: я уже на это отвечала")

    # State не должен сдвинуться при чистом комментарии.
    assert session.state.conversation.turn_count == turns_before
    # Комментарий должен быть в классификациях.
    comment_rows = [c for c in store.classifications if c["kind"] == "comment"]
    assert any("я уже на это отвечала" in c["text"] for c in comment_rows)
    # Бот должен подтвердить и повторить последний свой вопрос.
    assert "комментарий" in reply.lower() or "продолжим" in reply.lower()


def test_comment_during_screening_still_persisted(monkeypatch):
    """Если в фазе screening владелец дописал «комментарий: …» сверх цифры —
    тоже сохраняется как kind='comment'."""
    agent, store, _mock = _setup_agent(monkeypatch)
    session = agent.create_session()
    store.classifications.clear()

    agent.reply(session, "2\nкомментарий: непонятно зачем спрашиваете возраст")

    comment_rows = [c for c in store.classifications if c["kind"] == "comment"]
    assert any("непонятно зачем" in c["text"] for c in comment_rows)
    # Цифра 2 должна нормально провалиться в screening и продвинуть step.
    assert session.state.conversation.screening_step == 1


def test_no_comment_no_extra_classification(monkeypatch):
    agent, store, _mock = _setup_agent(monkeypatch)
    session = agent.create_session()
    _ff_through_screening(agent, session)
    store.classifications.clear()

    agent.reply(session, "у меня магазин в инстаграме")

    # comment-классификаций НЕ должно быть от чистого ответа без комментария.
    bare_comments = [c for c in store.classifications if c["kind"] == "comment"]
    assert bare_comments == []


def test_create_session_sets_started_at(monkeypatch):
    """create_session должен фиксировать started_at в state.conversation."""
    from datetime import datetime, timezone
    agent, _store, _mock = _setup_agent(monkeypatch)
    before = datetime.now(timezone.utc)
    session = agent.create_session()
    after = datetime.now(timezone.utc)

    started = session.state.conversation.started_at
    assert started is not None
    # таймстамп лежит в окне между before и after
    assert before <= started <= after
    # ended_at пока пуст — сессия только началась
    assert session.state.conversation.ended_at is None


def test_create_session_attaches_meta_to_opener(monkeypatch):
    """Скрипинг-опенер должен иметь meta с target_field=name (Q1 теперь — имя)."""
    agent, _store, _mock = _setup_agent(monkeypatch)
    session = agent.create_session()
    opener = session.transcript[0]
    assert opener["role"] == "assistant"
    assert "meta" in opener
    meta = opener["meta"]
    assert meta["target_field"] == "name"
    assert meta["target_section"] == "Profile"
    assert meta["phase"] == "screening"
    assert meta["kind"] == "screening_question"


def test_screening_advance_attaches_meta_to_next_question(monkeypatch):
    """После ответа на Q1 (name) следующий бот-вопрос — Q2 (gender)."""
    agent, _store, _mock = _setup_agent(monkeypatch)
    session = agent.create_session()
    agent.reply(session, "Иван")  # answer Q1 (name = free text)
    last_assistant = next(e for e in reversed(session.transcript) if e["role"] == "assistant")
    meta = last_assistant.get("meta") or {}
    assert meta.get("target_field") == "gender"
    assert meta.get("phase") == "screening"


def test_inline_comment_voice_mode_without_colon(monkeypatch):
    """from_voice=True: «комментарий ...» без двоеточия должно сработать."""
    agent, store, _mock = _setup_agent(monkeypatch)
    session = agent.create_session()
    _ff_through_screening(agent, session)
    store.classifications.clear()

    agent.reply(
        session,
        "у меня магазин\nкомментарий бот забыл что-то спросить",
        from_voice=True,
    )

    comment_rows = [c for c in store.classifications if c["kind"] == "comment"]
    assert any("забыл что-то" in c["text"] for c in comment_rows)


def test_two_sessions_do_not_cross_contaminate(monkeypatch):
    """Две сессии с разными session_id (разные web-вкладки или TG-чаты), прогоняем
    их интерливенгно — транскрипт, классификации и state каждой сессии остаются
    изолированными. Это формализует ключевую гарантию архитектуры: session_id
    разделяет всё.

    Screening-порядок (новый): Q1 name → Q2 gender → Q3 age_range → ...
    """
    agent, store, _mock = _setup_agent(monkeypatch)
    s_a = agent.create_session(telegram_chat_id=11)
    s_b = agent.create_session(telegram_chat_id=22)
    assert s_a.id != s_b.id

    # Интерливенг A↔B по 3 хода: name → gender → age_range.
    agent.reply(s_a, "Анна", telegram_chat_id=11)   # A.Q1 name
    agent.reply(s_b, "Борис", telegram_chat_id=22)  # B.Q1 name
    agent.reply(s_a, "1", telegram_chat_id=11)       # A.Q2 gender = female
    agent.reply(s_b, "2", telegram_chat_id=22)       # B.Q2 gender = male
    agent.reply(s_a, "3", telegram_chat_id=11)       # A.Q3 age = "35-45"
    agent.reply(s_b, "4", telegram_chat_id=22)       # B.Q3 age = "45-55"

    # Профили РАЗНЫЕ — никакого смешения.
    assert s_a.state.profile.name == "Анна"
    assert s_b.state.profile.name == "Борис"
    assert s_a.state.profile.gender == "female"
    assert s_b.state.profile.gender == "male"
    assert s_a.state.profile.age_range == "35-45"
    assert s_b.state.profile.age_range == "45-55"

    # Шаги screening продвинуты независимо.
    assert s_a.state.conversation.screening_step == 3
    assert s_b.state.conversation.screening_step == 3

    # Транскрипты не пересекаются.
    assert s_a.transcript is not s_b.transcript
    a_user_msgs = {m["content"] for m in s_a.transcript if m["role"] == "user"}
    b_user_msgs = {m["content"] for m in s_b.transcript if m["role"] == "user"}
    assert "Борис" not in a_user_msgs
    assert "Анна" not in b_user_msgs

    # Классификации разделены по session_id.
    a_class = [c for c in store.classifications if c["session_id"] == s_a.id]
    b_class = [c for c in store.classifications if c["session_id"] == s_b.id]
    assert a_class and b_class
    assert all(c["session_id"] == s_a.id for c in a_class)
    assert all(c["session_id"] == s_b.id for c in b_class)
    # Никакая запись из A не попала в B и наоборот.
    a_texts = {c["text"] for c in a_class}
    b_texts = {c["text"] for c in b_class}
    assert "Борис" not in a_texts
    assert "Анна" not in b_texts


def test_inline_comment_voice_mode_no_match_in_text_mode(monkeypatch):
    """Без from_voice: триггер без двоеточия НЕ должен сработать."""
    agent, store, _mock = _setup_agent(monkeypatch)
    session = agent.create_session()
    _ff_through_screening(agent, session)
    store.classifications.clear()

    agent.reply(session, "у меня магазин\nкомментарий бот забыл что-то спросить")

    comment_rows = [c for c in store.classifications if c["kind"] == "comment"]
    # триггер без двоеточия в текстовом режиме не должен сработать
    assert not any("забыл что-то" in c["text"] for c in comment_rows)
