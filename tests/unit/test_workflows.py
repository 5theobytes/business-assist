"""End-to-end workflow tests with MockLLM."""
import pytest

from app.eval.mock_llm import mock_responder
from app.eval.personas import PERSONAS
from app.eval.simulator import simulate
from app.llm import MockLLM
from app.workflows import build


@pytest.fixture
def mock_llm():
    return MockLLM(responder=mock_responder)


@pytest.mark.parametrize("workflow_name", ["v1", "v2", "v3"])
def test_workflow_starts_with_greeting(workflow_name, mock_llm):
    wf = build(workflow_name, llm=mock_llm)
    s = wf.new_session()
    greeting = wf.start(s)
    assert greeting
    assert s.transcript[0]["role"] == "assistant"


@pytest.mark.parametrize("workflow_name", ["v1", "v2", "v3"])
def test_workflow_advances_on_user_message(workflow_name, mock_llm):
    wf = build(workflow_name, llm=mock_llm)
    s = wf.new_session()
    wf.start(s)
    reply = wf.respond(s, "у меня магазин в инстаграме, веду в excel, теряю заявки")
    assert reply
    assert s.state.conversation.turn_count == 1


def test_v2_extractor_populates_state_from_user_message():
    llm = MockLLM(responder=mock_responder)
    wf = build("v2", llm=llm)
    s = wf.new_session()
    wf.start(s)
    wf.respond(s, "у меня студия маникюра, пишут в whatsapp и директ инстаграма, веду в excel, теряю заявки")
    assert s.state.point_a.business_type
    # _merge_patch now canonicalizes ru/en variants in channels/tools lists,
    # so we assert canonical forms (WhatsApp, Excel) rather than raw casings.
    channel_lower = {c.lower() for c in s.state.point_a.channels}
    tool_lower = {t.lower() for t in s.state.point_a.tools_in_use}
    assert "whatsapp" in channel_lower
    assert "excel" in tool_lower
    assert s.state.point_a.pain_points


def test_v3_does_mom_test_for_first_3_turns():
    """V3's first 3 user turns should get Mom-Test follow-ups, no extraction yet."""
    llm = MockLLM(responder=mock_responder)
    wf = build("v3", llm=llm)
    s = wf.new_session()
    wf.start(s)
    wf.respond(s, "у меня магазин")
    # After turn 1: still in Mom-Test phase, state should be empty.
    assert s.state.point_a.business_type is None  # extractor not called yet


@pytest.mark.parametrize("workflow_name", ["v1", "v2", "v3"])
def test_full_simulation_with_anna_persona(workflow_name):
    anna = next(p for p in PERSONAS if "Анна" in p.name)
    result = simulate(workflow_name, anna, max_turns=20)
    assert result.transcript
    # All workflows should produce *some* response of substance
    bot_msgs = [m for m in result.transcript if m["role"] == "assistant"]
    assert len(bot_msgs) >= 3
    # V2 and V3 should produce structured state
    if workflow_name in ("v2", "v3"):
        assert result.final_state["point_a"]["business_type"]


@pytest.mark.parametrize("workflow_name", ["v2", "v3"])
def test_v2_v3_produce_dual_specs_when_complete(workflow_name):
    anna = next(p for p in PERSONAS if "Анна" in p.name)
    result = simulate(workflow_name, anna, max_turns=20)
    assert result.completed, f"{workflow_name} did not complete in 20 turns"
    assert result.business_spec, f"{workflow_name} produced no business spec"
    assert result.dev_spec, f"{workflow_name} produced no dev spec"


def test_v2_emit_specs_passes_skill_to_dev_call_without_pending_decisions():
    """Тех-план получает на вход state + transcript_tail. pending_decisions
    больше НЕ передаётся — все архитектурные развилки закрываются на фазе
    DEV_COVERAGE и сохраняются в state.resources.dev_coverage_qa."""
    from app.state import Phase
    from app.workflows.v2_adaptive import AdaptiveStateMachineWorkflow

    captured = []

    def responder(system, messages, tools):
        captured.append({"system": system, "messages": messages})
        return "STUB_OUTPUT"

    llm = MockLLM(responder=responder)
    wf = AdaptiveStateMachineWorkflow(llm)
    s = wf.new_session()
    s.append("user", "что-то про мой бизнес")
    s.append("assistant", "ок понял")
    s.state.conversation.phase = Phase.SPEC_REVIEW
    s.state.conversation.confirmed_summary = True

    wf._emit_specs(s)

    # Три вызова: биз-версия, тех-версия (со скилом), оценка срока.
    assert len(captured) == 3
    biz_call, dev_call, estimate_call = captured

    # Биз-вызов — стандартный промпт, без скил-блока.
    assert "dual-spec-writer" not in biz_call["system"].lower()
    assert '"point_a"' in biz_call["messages"][0]["content"]

    # Тех-вызов — системный промпт содержит скил.
    assert "dual-spec-writer" in dev_call["system"].lower() or "Dual Spec Writer" in dev_call["system"]
    dev_payload = dev_call["messages"][0]["content"]
    assert "pending_decisions" not in dev_payload  # explicitly removed
    assert "transcript_tail" in dev_payload
    assert "state" in dev_payload

    # Estimate-вызов — короткий, ждёт фразу-вилку.
    assert "вилку срока" in estimate_call["system"]

    assert s.business_spec
    assert s.dev_spec
    assert s.state.conversation.phase == Phase.DONE
    assert s.state.conversation.estimate_text  # выставлен


def test_collect_pending_decisions_is_now_empty_after_dev_coverage_refactor():
    """После DEV_COVERAGE-рефактора список pending_decisions пуст всегда —
    все архитектурные развилки закрываются на новой фазе сканером."""
    from app.spec_manifest import collect_pending_decisions
    from app.state import WorkflowState

    assert collect_pending_decisions(WorkflowState()) == []


def test_v2_ask_next_attaches_meta_with_target_field():
    """После _ask_next последний transcript-entry должен иметь meta с target_field."""
    from app.state import Phase
    from app.workflows.v2_adaptive import AdaptiveStateMachineWorkflow

    def responder(system, messages, tools):
        if tools and tools[0]["name"] == "update_state":
            return {"tool_use": {"name": "update_state", "input": {}}}
        return "Расскажите, чем занимается ваш бизнес?"

    llm = MockLLM(responder=responder)
    wf = AdaptiveStateMachineWorkflow(llm)
    s = wf.new_session()
    # Положим в state.phase = POINT_A, чтобы _ask_next целился в POINT_A-поля
    s.state.conversation.phase = Phase.POINT_A
    s.append("user", "/start")  # something to seed transcript

    wf.respond(s, "у меня магазин")

    last_assistant = next(
        e for e in reversed(s.transcript) if e["role"] == "assistant"
    )
    assert "meta" in last_assistant
    meta = last_assistant["meta"]
    assert meta["kind"] == "asker"
    # next_owner_field в POINT_A с пустым state → business_type (priority 1)
    assert meta["target_field"] == "business_type"
    assert meta["target_section"] == "Context"
    assert meta["owner_question_hint"]
    assert meta["internal_rationale"]


def test_v2_emit_specs_sets_ended_at():
    """После _emit_specs в state.conversation.ended_at должно быть выставлено."""
    from datetime import datetime, timezone
    from app.state import Phase
    from app.workflows.v2_adaptive import AdaptiveStateMachineWorkflow

    llm = MockLLM(responder=lambda system, messages, tools: "STUB")
    wf = AdaptiveStateMachineWorkflow(llm)
    s = wf.new_session()
    s.append("user", "...")
    s.append("assistant", "...")
    s.state.conversation.phase = Phase.SPEC_REVIEW
    s.state.conversation.confirmed_summary = True

    before = datetime.now(timezone.utc)
    wf._emit_specs(s)
    after = datetime.now(timezone.utc)

    assert s.state.conversation.ended_at is not None
    assert before <= s.state.conversation.ended_at <= after


def test_v2_emit_specs_chat_reply_is_ack_not_full_specs():
    """Финальное сообщение владельцу — короткий ack с оценкой времени и CTA,
    БЕЗ полного текста спек (они уезжают в Firestore/Sheets/Drive внутрь команды)."""
    from app.state import Phase
    from app.workflows.v2_adaptive import AdaptiveStateMachineWorkflow

    def responder(system, messages, tools):
        # Каждый из 3 вызовов вернёт длинный «спек-текст» с маркерами секций.
        if "вилку срока" in system:
            return "2-3 недели"
        return "## Goal\n\nДолгий текст спеки с # секциями и подробностями."

    llm = MockLLM(responder=responder)
    wf = AdaptiveStateMachineWorkflow(llm)
    s = wf.new_session()
    s.append("user", "...")
    s.append("assistant", "...")
    s.state.conversation.phase = Phase.SPEC_REVIEW
    s.state.conversation.confirmed_summary = True

    chat_reply = wf._emit_specs(s)

    # Не должно быть полного содержимого спек.
    assert "## Goal" not in chat_reply
    assert "Долгий текст спеки" not in chat_reply
    # Не должно быть слова «разработчик» (даже после sanitize).
    assert "разработчик" not in chat_reply.lower()
    # Не должно быть слова «исполнитель» — никакой третьей стороны.
    assert "исполнител" not in chat_reply.lower()
    # Не должно быть «передайте» / «отдайте» — мы реализуем сами.
    assert "передайте" not in chat_reply.lower()
    assert "отдайте" not in chat_reply.lower()

    # ДОЛЖНЫ быть: ack, оценка, CTA.
    assert "Спасибо" in chat_reply
    assert "сохранён" in chat_reply
    assert "2-3 недели" in chat_reply or "2-3" in chat_reply
    # The call to action invites the user to agree on how implementation proceeds.
    assert "согласовани" in chat_reply.lower() or "оплат" in chat_reply.lower()

    # Спеки сами при этом сохранены в session — для нашей внутренней работы.
    assert s.business_spec
    assert s.dev_spec
    assert s.state.conversation.estimate_text == "2-3 недели"


# ---------------------------------------------------------------------------
# llm_required detector — skip ChatGPT-subscription question for sync-only cases
# ---------------------------------------------------------------------------


def test_v2_detect_llm_requirement_marks_false_for_sync_case():
    """Когда point_b.desired_outcome — это «передать заказы из A в B»,
    detector должен пометить llm_required=False и подчистить has_llm_key."""
    from app.state import Phase
    from app.workflows.v2_adaptive import AdaptiveStateMachineWorkflow

    def responder(system, messages, tools):
        # Extractor: пустой patch, чтобы не вмешиваться.
        if tools and tools[0].get("name") == "update_state":
            return {"tool_use": {"name": "update_state", "input": {}}}
        # LLM detector — возвращает строгий JSON.
        if "llm_required" in system:
            return '{"llm_required": false, "reasoning": "pure sync"}'
        return "ok"

    llm = MockLLM(responder=responder)
    wf = AdaptiveStateMachineWorkflow(llm)
    s = wf.new_session()
    s.state.conversation.phase = Phase.POINT_B
    s.state.point_b.desired_outcome = "передавать заказы из Instagram в Google Sheets"
    s.state.resources.has_llm_key = True  # legacy stale value

    wf._maybe_detect_llm_requirement(s)

    assert s.state.resources.llm_required is False
    # has_llm_key должен быть очищен, потому что AI не нужен.
    assert s.state.resources.has_llm_key is None


def test_v2_detect_llm_requirement_skips_when_outcome_empty():
    from app.workflows.v2_adaptive import AdaptiveStateMachineWorkflow

    calls = []

    def responder(system, messages, tools):
        calls.append(system)
        return "{}"

    llm = MockLLM(responder=responder)
    wf = AdaptiveStateMachineWorkflow(llm)
    s = wf.new_session()
    # desired_outcome пустой → детектор не должен вызываться.
    wf._maybe_detect_llm_requirement(s)
    assert calls == []
    assert s.state.resources.llm_required is None


def test_v2_detect_llm_requirement_idempotent():
    from app.workflows.v2_adaptive import AdaptiveStateMachineWorkflow

    calls = []

    def responder(system, messages, tools):
        calls.append(system)
        return '{"llm_required": true, "reasoning": "x"}'

    llm = MockLLM(responder=responder)
    wf = AdaptiveStateMachineWorkflow(llm)
    s = wf.new_session()
    s.state.point_b.desired_outcome = "auto-replies via chatbot"
    wf._maybe_detect_llm_requirement(s)
    assert s.state.resources.llm_required is True
    assert len(calls) == 1
    # Повторный вызов — детектор не дёргается.
    wf._maybe_detect_llm_requirement(s)
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Question-first responder
# ---------------------------------------------------------------------------


def test_v2_handle_user_question_returns_answer_when_question_detected():
    from app.workflows.v2_adaptive import AdaptiveStateMachineWorkflow

    def responder(system, messages, tools):
        if "встречный ВОПРОС" in system or "has_question" in system:
            return '{"has_question": true, "answer": "Срок зависит от деталей."}'
        return "{}"

    llm = MockLLM(responder=responder)
    wf = AdaptiveStateMachineWorkflow(llm)
    s = wf.new_session()
    answer = wf._handle_user_question(s, "а сколько по времени?")
    assert answer == "Срок зависит от деталей."


def test_v2_handle_user_question_skips_when_no_question_signal():
    """Сообщение без знака вопроса и без «вопросных» стартеров → LLM не вызывается."""
    from app.workflows.v2_adaptive import AdaptiveStateMachineWorkflow

    calls = []

    def responder(system, messages, tools):
        calls.append(system)
        return '{"has_question": true, "answer": "wrong"}'

    llm = MockLLM(responder=responder)
    wf = AdaptiveStateMachineWorkflow(llm)
    s = wf.new_session()
    answer = wf._handle_user_question(s, "у меня кофейня в центре")
    assert answer is None
    assert calls == []


def test_v2_handle_user_question_returns_none_on_classifier_false():
    from app.workflows.v2_adaptive import AdaptiveStateMachineWorkflow

    def responder(system, messages, tools):
        return '{"has_question": false}'

    llm = MockLLM(responder=responder)
    wf = AdaptiveStateMachineWorkflow(llm)
    s = wf.new_session()
    answer = wf._handle_user_question(s, "а сколько по времени?")
    assert answer is None


# ---------------------------------------------------------------------------
# Session memory: extract + reconcile
# ---------------------------------------------------------------------------


def test_v2_extract_session_memory_appends_retraction_and_cleans_state():
    from app.workflows.v2_adaptive import AdaptiveStateMachineWorkflow

    def responder(system, messages, tools):
        if "session-memory" in system or "retractions" in system:
            return (
                '{"retractions": [{"type": "channel", "value": "telegram", '
                '"user_quote": "Telegram это не мой канал"}], "preferences": []}'
            )
        return "{}"

    llm = MockLLM(responder=responder)
    wf = AdaptiveStateMachineWorkflow(llm)
    s = wf.new_session()
    # Засеваем состояние «Telegram уже в каналах»: retraction должна его убрать.
    s.state.point_a.channels = ["Telegram", "WhatsApp"]
    s.state.conversation.turn_count = 5

    wf._extract_session_memory(s, "Telegram это не мой канал, не нужно его")

    mem = s.state.conversation.session_memory
    assert len(mem.retractions) == 1
    assert mem.retractions[0]["value"] == "telegram"
    assert mem.retractions[0]["turn"] == 5
    # Reconcile должен убрать Telegram из каналов, оставив WhatsApp.
    assert "Telegram" not in s.state.point_a.channels
    assert "WhatsApp" in s.state.point_a.channels


def test_v2_extract_session_memory_no_ai_preference_clears_llm_fields():
    from app.workflows.v2_adaptive import AdaptiveStateMachineWorkflow

    def responder(system, messages, tools):
        if "session-memory" in system or "retractions" in system:
            return (
                '{"retractions": [], "preferences": [{"topic": "no_ai", '
                '"value": "true", "user_quote": "не хочу ИИ"}]}'
            )
        return "{}"

    llm = MockLLM(responder=responder)
    wf = AdaptiveStateMachineWorkflow(llm)
    s = wf.new_session()
    s.state.resources.has_llm_key = True
    s.state.resources.llm_provider = "OpenAI"

    wf._extract_session_memory(s, "не хочу ИИ — пусть будет всё на скриптах")

    assert s.state.resources.llm_required is False
    assert s.state.resources.has_llm_key is None
    assert s.state.resources.llm_provider is None
    mem = s.state.conversation.session_memory
    assert any(p["topic"] == "no_ai" for p in mem.preferences)


def test_v2_extract_session_memory_skips_without_signal_terms():
    """Сообщение без отрицаний/предпочтений — LLM не вызывается."""
    from app.workflows.v2_adaptive import AdaptiveStateMachineWorkflow

    calls = []

    def responder(system, messages, tools):
        calls.append(system)
        return "{}"

    llm = MockLLM(responder=responder)
    wf = AdaptiveStateMachineWorkflow(llm)
    s = wf.new_session()
    wf._extract_session_memory(s, "у меня магазин в центре")
    assert calls == []
    assert s.state.conversation.session_memory.retractions == []
    assert s.state.conversation.session_memory.preferences == []


# ---------------------------------------------------------------------------
# SUMMARY_TEMPLATE conditional «ИИ для бота» row
# ---------------------------------------------------------------------------


def test_summary_template_hides_ai_row_when_llm_required_false():
    from app.state import Phase, WorkflowState
    from app.workflows.common_prompts import SUMMARY_TEMPLATE

    s = WorkflowState()
    s.point_a.business_type = "кофейня"
    s.resources.monthly_budget_rub = 3000
    s.resources.has_llm_key = False
    s.resources.llm_required = False

    out = SUMMARY_TEMPLATE.render(state=s, phase=Phase.RESOURCES)
    assert "ИИ для бота" not in out


def test_summary_template_hides_ai_row_when_no_quote():
    """AI row appears only when a verbatim user quote is captured."""
    from app.state import Phase, WorkflowState
    from app.workflows.common_prompts import SUMMARY_TEMPLATE

    s = WorkflowState()
    s.point_a.business_type = "автосервис"
    s.resources.monthly_budget_rub = 3000
    s.resources.has_llm_key = False
    s.resources.llm_required = True
    s.resources.llm_subscription_quote = None  # extractor didn't capture

    out = SUMMARY_TEMPLATE.render(state=s, phase=Phase.RESOURCES)
    assert "ИИ для бота" not in out
    assert "подключим в процессе" not in out
    assert "уже подключено" not in out


def test_summary_template_shows_verbatim_ai_quote():
    """Fix 10: when llm_subscription_quote is filled, render it verbatim."""
    from app.state import Phase, WorkflowState
    from app.workflows.common_prompts import SUMMARY_TEMPLATE

    s = WorkflowState()
    s.point_a.business_type = "автосервис"
    s.resources.monthly_budget_rub = 3000
    s.resources.llm_required = True
    s.resources.llm_subscription_quote = "Да, есть подписка на ChatGPT"

    out = SUMMARY_TEMPLATE.render(state=s, phase=Phase.RESOURCES)
    assert "Подписка на ИИ: Да, есть подписка на ChatGPT" in out
    assert "уже подключено" not in out
    assert "подключим в процессе" not in out


def test_summary_template_suppresses_quote_when_no_ai_retraction():
    """Fix 10/1: even with a quote, if the owner retracted AI in
    session_memory, the row is suppressed."""
    from app.state import Phase, WorkflowState
    from app.workflows.common_prompts import SUMMARY_TEMPLATE

    s = WorkflowState()
    s.point_a.business_type = "магазин"
    s.resources.llm_required = True
    s.resources.llm_subscription_quote = "есть подписка"
    s.conversation.session_memory.preferences.append({
        "topic": "no_ai", "value": "rejected", "turn": 5, "user_quote": "никакого ИИ",
    })
    out = SUMMARY_TEMPLATE.render(state=s, phase=Phase.RESOURCES)
    assert "Подписка на ИИ" not in out


def test_summary_template_renders_solution_section():
    """ANALYSIS-фаза lock'ает state.solution; Сводка показывает «Договорились
    строить» с дословным описанием выбранной опции."""
    from app.state import Phase, WorkflowState
    from app.workflows.common_prompts import SUMMARY_TEMPLATE

    s = WorkflowState()
    s.point_a.business_type = "автомойка"
    s.solution.shape = "Telegram-бот для бронирования"
    s.solution.shape_description = "клиенты бронируют слот в TG, владелец видит календарь"
    s.solution.needs_ai = False

    out = SUMMARY_TEMPLATE.render(state=s, phase=Phase.RESOURCES)
    assert "**Договорились строить:**" in out
    assert "Telegram-бот для бронирования" in out
    assert "клиенты бронируют слот в TG" in out


def test_summary_template_omits_solution_section_when_unset():
    from app.state import Phase, WorkflowState
    from app.workflows.common_prompts import SUMMARY_TEMPLATE

    s = WorkflowState()
    s.point_a.business_type = "магазин"
    # solution.shape is None (default)
    out = SUMMARY_TEMPLATE.render(state=s, phase=Phase.POINT_B)
    assert "Договорились строить" not in out


# ---------------------------------------------------------------------------
# Phase.ANALYSIS transitions
# ---------------------------------------------------------------------------


def test_state_advances_point_b_to_analysis_not_resources():
    """POINT_B-complete должен идти в ANALYSIS, не в RESOURCES."""
    from app.state import Phase, WorkflowState

    s = WorkflowState()
    s.conversation.phase = Phase.POINT_B
    # Заполним 75% point_b
    s.point_b.primary_value = "time"
    s.point_b.desired_outcome = "клиенты бронируют сами"
    s.point_b.success_metric = "0 пропущенных звонков"
    s.point_b.out_of_scope = ["оплата онлайн"]
    assert s.next_phase() == Phase.ANALYSIS


def test_state_advances_analysis_to_resources_when_solution_locked():
    from app.state import Phase, WorkflowState

    s = WorkflowState()
    s.conversation.phase = Phase.ANALYSIS
    # Без shape — фаза ещё не закрыта.
    assert s.next_phase() == Phase.ANALYSIS
    # Lock: shape выставлен.
    s.solution.shape = "Telegram-бот"
    assert s.next_phase() == Phase.RESOURCES


# ---------------------------------------------------------------------------
# RESOURCES gating per solution
# ---------------------------------------------------------------------------


def test_resources_skip_ai_fields_when_needs_ai_false():
    """has_llm_key / llm_provider не спрашиваются при solution.needs_ai=False."""
    from app.state import Phase, WorkflowState
    from app.spec_manifest import owner_gaps

    s = WorkflowState()
    s.conversation.phase = Phase.RESOURCES
    s.solution.shape = "напоминалка"
    s.solution.needs_ai = False
    s.solution.needs_chat_ui = False
    # monthly_budget_rub, tech_savviness, maintainer всё ещё нужны.
    gap_names = {f.name for f in owner_gaps(s, Phase.RESOURCES)}
    assert "has_llm_key" not in gap_names
    assert "llm_provider" not in gap_names
    assert "preferred_channel" not in gap_names
    assert "monthly_budget_rub" in gap_names
    assert "tech_savviness" in gap_names
    assert "maintainer" in gap_names


def test_resources_keep_ai_fields_when_needs_ai_true():
    from app.state import Phase, WorkflowState
    from app.spec_manifest import owner_gaps

    s = WorkflowState()
    s.conversation.phase = Phase.RESOURCES
    s.solution.shape = "FAQ-бот в Instagram"
    s.solution.needs_ai = True
    s.solution.needs_chat_ui = True
    gap_names = {f.name for f in owner_gaps(s, Phase.RESOURCES)}
    assert "has_llm_key" in gap_names
    assert "preferred_channel" in gap_names


def test_resources_skip_preferred_channel_when_no_chat_ui():
    from app.state import Phase, WorkflowState
    from app.spec_manifest import owner_gaps

    s = WorkflowState()
    s.conversation.phase = Phase.RESOURCES
    s.solution.shape = "sync без бота"
    s.solution.needs_chat_ui = False
    gap_names = {f.name for f in owner_gaps(s, Phase.RESOURCES)}
    assert "preferred_channel" not in gap_names


# ---------------------------------------------------------------------------
# ANALYSIS handler — pick locks state.solution and propagates to resources
# ---------------------------------------------------------------------------


def test_analysis_lock_solution_propagates_to_resources():
    """После _lock_solution_and_ack: state.resources.llm_required = needs_ai."""
    from app.llm import MockLLM
    from app.workflows import build

    llm = MockLLM(responder=mock_responder)
    wf = build("v2", llm=llm)
    s = wf.new_session()

    # Pre-load options as if scanner+generator already ran.
    s.state.solution.offered_options = [
        {
            "shape": "напоминалка в Sheets",
            "description": "график в гугл-таблице",
            "needs_ai": False,
            "needs_chat_ui": False,
            "needs_external_integrations": False,
            "approx_budget_rub": "до 500",
            "approx_timeline": "3 дня",
            "recommended": True,
        },
    ]
    wf._lock_solution_and_ack(s, 0)
    assert s.state.solution.shape == "напоминалка в Sheets"
    assert s.state.solution.needs_ai is False
    assert s.state.resources.llm_required is False
    # After lock, has_llm_key / llm_provider / quote are scrubbed.
    assert s.state.resources.has_llm_key is None
    assert s.state.resources.llm_provider is None
    assert s.state.resources.llm_subscription_quote is None


def test_analysis_options_renderer_includes_recommended_marker():
    from app.llm import MockLLM
    from app.workflows import build

    llm = MockLLM(responder=mock_responder)
    wf = build("v2", llm=llm)
    options = [
        {
            "shape": "вариант А",
            "description": "описание А",
            "needs_ai": False,
            "approx_budget_rub": "до 1000",
            "approx_timeline": "3 дня",
            "recommended": True,
        },
        {
            "shape": "вариант Б",
            "description": "описание Б",
            "needs_ai": True,
            "approx_budget_rub": "5-10 тыс",
            "approx_timeline": "1 неделя",
            "recommended": False,
        },
    ]
    text = wf._render_options_message(options, intro="Из того, что вы рассказали:")
    assert "Вариант 1" in text
    assert "Вариант 2" in text
    assert "обычно подходит лучше всего" in text  # recommended marker
    assert "до 1000" in text
    assert "5-10 тыс" in text


def test_summary_template_canonicalizes_channels_and_tools():
    from app.state import Phase, WorkflowState
    from app.workflows.common_prompts import SUMMARY_TEMPLATE

    s = WorkflowState()
    # Внутри extractor мы и так канонизируем, но рендер тоже должен делать
    # финальный dedup на случай, если state кто-то поправил вручную.
    s.point_a.channels = ["Telegram", "телеграм"]
    s.point_a.tools_in_use = ["Notion", "нотион"]

    out = SUMMARY_TEMPLATE.render(state=s, phase=Phase.POINT_A)
    # Канонические формы каждого варианта появляются ОДИН раз.
    assert out.count("Telegram") == 1
    assert "телеграм" not in out
    assert out.count("Notion") == 1
    assert "нотион" not in out
