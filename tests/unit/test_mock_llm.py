"""Mock LLM responder behaviour."""
import pytest

from app.eval.mock_llm import _extract_patch, _gap_question, mock_responder
from app.eval.personas import PERSONAS
from app.eval.simulator import simulate
from app.workflows.v2_adaptive import EXTRACTOR_TOOL


def test_extractor_picks_up_channels_and_tools():
    p = _extract_patch("пишут в директ инстаграма и whatsapp, веду в excel")
    assert "instagram direct" in p["point_a"]["channels"]
    assert "whatsapp" in p["point_a"]["channels"]
    assert "excel" in p["point_a"]["tools_in_use"]


def test_extractor_picks_up_pain_points():
    p = _extract_patch("теряю заявки когда занята; забываю напоминать клиентам")
    pains = p["point_a"]["pain_points"]
    assert any("теря" in s.lower() for s in pains)
    assert any("забы" in s.lower() for s in pains)


def test_extractor_uses_bot_context_for_desired_outcome():
    p = _extract_patch(
        "новые сообщения автоматически приходят мне в телеграм",
        prev_bot_text="Опишите одной фразой: как должен выглядеть идеальный день?",
    )
    assert p["point_b"]["desired_outcome"]


def test_extractor_uses_bot_context_for_success_metric():
    p = _extract_patch("0 пропущенных в неделю",
                        prev_bot_text="Как поймём, что получилось — какая цифра?")
    assert p["point_b"]["success_metric"]


def test_extractor_picks_budget():
    p = _extract_patch("готов платить до 3000 ₽ в месяц")
    assert p["resources"]["monthly_budget_rub"] == 3000


def test_extractor_detects_no_llm_key():
    p = _extract_patch("нет, ключа нет, расскажите как получить")
    assert p["resources"]["has_llm_key"] is False


def test_extractor_detects_llm_provider():
    p = _extract_patch("есть ключ к openai")
    assert p["resources"]["llm_provider"] == "OpenAI"


def test_extractor_detects_confirmation():
    p = _extract_patch("да, всё верно")
    assert p["user_confirmed_summary"] is True


def test_gap_question_starts_with_business_type():
    q = _gap_question({})
    assert "чем занимается" in q.lower()


def test_gap_question_progresses_with_filled_state():
    state = {
        "point_a": {
            "business_type": "магазин",
            "channels": ["telegram"],
            "tools_in_use": ["excel"],
            "pain_points": ["теряю"],
        },
        "point_b": {},
        "resources": {},
    }
    q = _gap_question(state)
    # should now ask about primary_value (it's the next gap after pain_points)
    assert "главное" in q.lower()


def test_responder_routes_to_extractor_when_tools_present():
    resp = mock_responder(
        "Ты — STRUCTURED-EXTRACTOR.",
        [{"role": "assistant", "content": "Где ведёте клиентов?"},
         {"role": "user", "content": "веду в excel"}],
        [EXTRACTOR_TOOL],
    )
    assert resp.tool_use is not None
    assert resp.tool_use["name"] == "update_state"
    assert "excel" in resp.tool_use["input"]["point_a"]["tools_in_use"]


@pytest.mark.parametrize("persona", PERSONAS, ids=lambda persona: persona.name)
def test_v4_offline_simulation_completes(persona):
    result = simulate("v4", persona)
    assert result.completed, result.final_state["conversation"]["phase"]
    assert result.business_spec
    assert result.dev_spec
