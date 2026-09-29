"""Persona behaviour: deterministic, aligned with ground truth."""
from app.eval.personas import PERSONAS, GroundTruth


def _anna():
    return next(p for p in PERSONAS if "Анна" in p.name)


def test_first_turn_returns_intro():
    p = _anna()
    msg = p.respond("Привет! Расскажите про задачу.", turn_count=1)
    assert msg == p.intro_message


def test_confirmation_question_yields_yes():
    p = _anna()
    out = p.respond("Сверим картину: вы делаете маникюр, так?", turn_count=4)
    assert out.startswith("да")


def test_channel_question_returns_channels():
    p = _anna()
    out = p.respond("А клиенты с вами как связываются — телеграм, инстаграм, ещё что-то?",
                    turn_count=2)
    assert "instagram direct" in out and "whatsapp" in out


def test_tools_question_returns_tools():
    p = _anna()
    out = p.respond("Где вы сейчас ведёте клиентов — Excel, блокнот, какая-то программа?",
                    turn_count=2)
    assert "excel" in out.lower()


def test_desired_outcome_question():
    p = _anna()
    out = p.respond("Опишите одной фразой: как должен выглядеть идеальный обычный день?",
                    turn_count=5)
    assert "директа" in out.lower() and "telegram" in out.lower()


def test_metric_question_returns_metric():
    p = _anna()
    out = p.respond("Как поймём, что получилось — какая цифра должна измениться?", turn_count=6)
    assert "не терять" in out.lower() or "1 заявки" in out.lower()


def test_budget_question_returns_budget():
    p = _anna()
    out = p.respond("Какую месячную подписку готовы платить — до 1000 ₽, до 5000 ₽?", turn_count=7)
    assert "3000" in out


def test_llm_key_when_absent_asks_for_instructions():
    p = _anna()
    out = p.respond("У вас уже есть API-ключ к ChatGPT/Claude?", turn_count=8)
    assert "нет" in out.lower() or "получить" in out.lower()


def test_llm_key_when_present_returns_provider():
    dmitriy = next(p for p in PERSONAS if "Дмитрий" in p.name)
    out = dmitriy.respond("У вас уже есть API-ключ к ChatGPT/Claude?", turn_count=8)
    assert "openai" in out.lower()


def test_persona_does_not_loop_on_unknown_question():
    p = _anna()
    out = p.respond("Какой у вас любимый цвет?", turn_count=5)
    # Falls through to a clarifier, not the long intro
    assert len(out) < 200


def test_ground_truth_label_mapping():
    gt = GroundTruth(
        business_type="x", channels=[], tools_in_use=[], pain_points=[],
        primary_value="time", desired_outcome="x", success_metric="x",
        out_of_scope=[], monthly_budget_rub=0, has_llm_key=False,
        llm_provider=None, tech_savviness="basic", maintainer="me",
    )
    assert "время" in gt.primary_value_label()
