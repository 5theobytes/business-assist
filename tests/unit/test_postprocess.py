"""Tests for the deterministic output guardrails."""
from types import SimpleNamespace

from app.state import WorkflowState
from app.workflows.postprocess import (
    apply_no_ai_if_rejected,
    canonicalize,
    canonicalize_list,
    check,
    check_against_memory,
    check_variant_blocks,
    count_questions,
    filter_channels_to_user_history,
    find_english_singletons,
    has_english_phrase,
    localize_english_fragments,
    recover_budget_from_history,
    sanitize,
    strip_dev_mentions,
)


def test_no_violations_on_clean_text():
    text = "А какая у вас подписка на ИИ-сервис — ChatGPT, Claude, YandexGPT?"
    assert check(text) == []


def test_banned_term_llm():
    v = check("Используем LLM для ответа")
    assert any(rv.rule == "banned_term" and "LLM" in rv.match for rv in v)


def test_banned_term_api():
    v = check("Нужен API-ключ от OpenAI")
    assert any(rv.rule == "banned_term" for rv in v)


def test_tech_commentary_flagged():
    v = check("Это не пройдёт по бюджету, нужна более лёгкая модель.")
    assert any(rv.rule == "tech_commentary" for rv in v)


def test_multiple_questions_flagged():
    v = check("Где общаетесь? И сколько обращений в день?")
    assert any(rv.rule == "multiple_questions" for rv in v)


def test_option_lists_not_counted_as_questions():
    text = (
        "Какая у вас подписка на ИИ?\n"
        "— ChatGPT\n"
        "— Claude\n"
        "— YandexGPT\n"
        "— нет"
    )
    assert count_questions(text) == 1
    assert all(rv.rule != "multiple_questions" for rv in check(text))


def test_sanitize_replaces_llm():
    out = sanitize("Используем LLM для ответа")
    assert "LLM" not in out
    assert "ИИ" in out


def test_sanitize_replaces_api_key():
    out = sanitize("У вас есть API-ключ?")
    assert "API" not in out


def test_sanitize_idempotent_on_clean_text():
    text = "А какая у вас подписка на ИИ-сервис?"
    assert sanitize(text) == text


def test_banned_speka_jargon():
    # "спека/спеку/спеке/спеки" — developer slang, not user-friendly
    for variant in ("спека", "спеку", "спеке", "спеки"):
        v = check(f"финальная {variant} готова")
        assert any(rv.rule == "banned_term" and variant in rv.match for rv in v), variant


def test_sanitize_replaces_speka():
    assert "спеку" not in sanitize("отдайте разработчику тех-спеку")
    assert "спеки" not in sanitize("две спеки готовы")


def test_widget_hosting_webhook_banned():
    for word in ("виджет", "хостинг", "вебхук"):
        v = check(f"настроим {word} на сайте")
        assert any(rv.rule == "banned_term" for rv in v), word


def test_sanitize_replaces_widget_hosting_webhook():
    out = sanitize("Дайте доступ к сайту чтобы встроить виджет, разработчик настроит хостинг и вебхук")
    assert "виджет" not in out.lower()
    assert "хостинг" not in out.lower()
    assert "вебхук" not in out.lower()


def test_vashe_delo_is_banned_and_replaced():
    text = "ваше дело — передать ключи"
    assert any(rv.rule == "banned_term" for rv in check(text))
    assert "ваше дело" not in sanitize(text).lower()


def test_vague_value_prop_warned():
    text = "Помогу понять, что вам стоит сделать в бизнесе."
    v = check(text)
    assert any(rv.rule == "vague_value_prop" for rv in v)


def test_concrete_value_prop_passes():
    text = (
        "Я могу помочь сократить время работы вашей и ваших сотрудников и "
        "увеличить прибыль."
    )
    assert all(rv.rule != "vague_value_prop" for rv in check(text))


def test_leaked_dev_split_phrase_flagged():
    # User-facing summaries should not expose internal divisions of work
    # between the business owner and the developer.
    text = "и в конце соберу понятный план — отдельно для вас и отдельно для разработчика."
    v = check(text)
    assert any(rv.rule == "leaked_dev_split" for rv in v), v


def test_leaked_dev_split_variant_flagged():
    # Variant phrasings should also be caught.
    for text in [
        "Будут две спеки — для вас и для разработчика.",
        "Сделаю два плана — отдельно для бизнеса и отдельно для разработчика.",
        "На выходе — отдельно для владельца и для разработчика.",
    ]:
        v = check(text)
        assert any(rv.rule == "leaked_dev_split" for rv in v), text


def test_clean_plan_phrasing_passes():
    # Same intent, no leaked split — should NOT trigger leaked_dev_split.
    text = "и в конце соберу понятный план с конкретными шагами и расходами."
    assert all(rv.rule != "leaked_dev_split" for rv in check(text))


def test_dev_word_is_banned_in_owner_facing():
    """Слово «разработчик» в реплике владельцу запрещено: он в проекте не участвует."""
    for variant in ("разработчик", "разработчику", "разработчика", "разработчики"):
        v = check(f"передайте {variant} план")
        assert any(rv.rule == "banned_term" and variant in rv.match for rv in v), variant


def test_sanitize_replaces_dev_with_executor():
    """sanitize заменяет «разработчик/-у/-а/...» → «исполнитель/-ю/-я/...»"""
    cases = [
        ("передайте разработчику план", "передайте исполнителю план"),
        ("вторая для разработчика", "вторая для исполнителя"),
        ("два разработчика придут", "два исполнителя придут"),
        ("разработчики уже знают", "исполнители уже знают"),
        ("разработческий план готов", "технический план готов"),
    ]
    for raw, expected in cases:
        out = sanitize(raw)
        assert out == expected, f"{raw!r} → {out!r}, expected {expected!r}"


def test_strip_dev_mentions_keeps_tech_terms():
    """В тех-плане API/webhook оставляем — strip_dev_mentions трогает только «разработчик»."""
    raw = "Разработчик настраивает webhook на API endpoint бота."
    out = strip_dev_mentions(raw)
    # "разработчик" → "исполнитель"
    assert "разработчик" not in out.lower()
    assert "исполнитель" in out.lower()
    # технические термины остались
    assert "webhook" in out
    assert "API" in out
    assert "endpoint" in out


def test_check_variant_blocks_detects_na_vybor():
    """Текст с **На выбор:** должен дать одно нарушение."""
    text = (
        "Нужно решить как уведомлять клиентов.\n"
        "**На выбор:**\n"
        "- только telegram\n"
        "- telegram + email\n"
    )
    v = check_variant_blocks(text)
    assert len(v) == 1, v
    assert "На выбор" in v[0]


def test_check_variant_blocks_detects_variant_letters():
    """Два «Вариант A / Вариант B» — два независимых violation."""
    text = (
        "Каналы уведомлений:\n"
        "Вариант A: только telegram\n"
        "Вариант B: telegram + email\n"
    )
    v = check_variant_blocks(text)
    assert len(v) == 2, v
    # Проверяем, что оба варианта попали в snippets.
    joined = " | ".join(v)
    assert "Вариант A" in joined or "только telegram" in joined
    assert "Вариант B" in joined or "telegram + email" in joined


def test_check_variant_blocks_detects_recommendation():
    """«Рекомендация:» в начале строки — нарушение."""
    text = (
        "Каналы уведомлений: telegram + email.\n"
        "Рекомендация: Вариант A\n"
    )
    v = check_variant_blocks(text)
    # Должен поймать как минимум «Рекомендация:».
    assert any("Рекомендация" in item for item in v), v


def test_check_variant_blocks_clean_text():
    """Типичный dev-spec текст без variant-маркеров — пустой список."""
    text = (
        "## Архитектура\n"
        "Бот принимает сообщения из telegram, обращается к ChatGPT,\n"
        "результат отправляет в Bitrix24 через webhook.\n"
        "\n"
        "## Бюджет\n"
        "~3000-5000 ₽/мес на ChatGPT API.\n"
    )
    assert check_variant_blocks(text) == []


def test_check_variant_blocks_ignores_phrase_in_quotes():
    """Слово «варианты» в обычном предложении — не section marker, не флагать."""
    text = (
        "Мы рассмотрели разные варианты архитектуры и зафиксировали один.\n"
        "Бот работает только через telegram.\n"
    )
    assert check_variant_blocks(text) == []


# ---------------------------------------------------------------------------
# Canonical names — ru/en transliteration dedup
# ---------------------------------------------------------------------------


def test_canonicalize_ru_to_en_brand():
    assert canonicalize("нотион") == "Notion"
    assert canonicalize("Тильда") == "Tilda"
    assert canonicalize("телеграм") == "Telegram"


def test_canonicalize_en_already_canonical():
    assert canonicalize("Notion") == "Notion"
    assert canonicalize("Telegram") == "Telegram"


def test_canonicalize_unknown_passthrough():
    # Unknown brand — return stripped original, not lowercased.
    assert canonicalize("CustomService") == "CustomService"
    assert canonicalize("  custombrand  ") == "custombrand"


def test_canonicalize_list_collapses_variants():
    items = ["Notion", "нотион", "Tilda", "тильда", "WhatsApp", "вотсап"]
    out = canonicalize_list(items)
    assert out == ["Notion", "Tilda", "WhatsApp"]


def test_canonicalize_list_preserves_insertion_order():
    items = ["тильда", "Notion", "Tilda"]
    out = canonicalize_list(items)
    assert out == ["Tilda", "Notion"]


def test_canonicalize_list_skips_empty():
    assert canonicalize_list(["", None, "Notion", "  "]) == ["Notion"]


# ---------------------------------------------------------------------------
# English-fragment detection + localizer
# ---------------------------------------------------------------------------


def test_has_english_phrase_detects_three_words():
    assert has_english_phrase("tax consulting for small businesses")
    assert has_english_phrase("Notion plus Automated debt reminders for clients")


def test_has_english_phrase_ignores_brand_names():
    # Brand names alone are 1-2 short words — should not trigger.
    assert not has_english_phrase("Используем Notion и Telegram")
    assert not has_english_phrase("ChatGPT и YandexGPT")


def test_localize_english_fragments_skips_when_clean():
    """If no English phrase detected, llm_complete must NOT be called."""
    calls = []

    def fake_complete(**kwargs):
        calls.append(kwargs)
        raise AssertionError("should not be called")

    text = "Используем Notion и Telegram, всё на русском."
    assert localize_english_fragments(text, fake_complete) == text
    assert calls == []


def test_localize_english_fragments_calls_llm_on_english():
    """If detected, the LLM must be called and its return used."""
    captured = []

    def fake_complete(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(text="налоговый консалтинг для малого бизнеса")

    text = "tax consulting for small businesses"
    result = localize_english_fragments(text, fake_complete)
    assert result == "налоговый консалтинг для малого бизнеса"
    assert len(captured) == 1


def test_localize_english_fragments_falls_back_on_failed_translation():
    """If LLM still returns English, fall back to original text."""

    def fake_complete(**kwargs):
        # Translation actually failed — still 3+ English words.
        return SimpleNamespace(text="tax consulting for clients again")

    text = "tax consulting for small businesses"
    assert localize_english_fragments(text, fake_complete) == text


def test_localize_english_fragments_swallows_llm_exception():
    def fake_complete(**kwargs):
        raise RuntimeError("network down")

    text = "tax consulting for small businesses"
    assert localize_english_fragments(text, fake_complete) == text


# ---------------------------------------------------------------------------
# Session-memory validator
# ---------------------------------------------------------------------------


def _memory(retractions=None, preferences=None):
    return SimpleNamespace(
        retractions=retractions or [], preferences=preferences or [],
    )


def test_check_against_memory_flags_returned_retraction():
    mem = _memory(retractions=[{
        "type": "channel", "value": "telegram",
        "turn": 3, "user_quote": "Telegram это не мой канал",
    }])
    text = "Будем принимать заявки в Telegram и WhatsApp."
    violations = check_against_memory(text, mem)
    assert any("retracted_entity_returned" in v for v in violations)


def test_check_against_memory_canonical_form_match():
    """Memory has lowercase value; bot output uses canonical capitalised form."""
    mem = _memory(retractions=[{
        "type": "tool", "value": "notion",
        "turn": 5, "user_quote": "не пользуюсь нотионом",
    }])
    text = "В качестве базы возьмём Notion."
    violations = check_against_memory(text, mem)
    assert any("retracted_entity_returned" in v for v in violations)


def test_check_against_memory_no_ai_preference():
    mem = _memory(preferences=[{
        "topic": "no_ai", "value": "true",
        "turn": 4, "user_quote": "не хочу ИИ",
    }])
    text = "Будем использовать ChatGPT для авто-ответов."
    violations = check_against_memory(text, mem)
    assert any("contradicts_preference" in v for v in violations)


def test_check_against_memory_no_ai_allows_negation():
    """Bot acknowledging «без ИИ» must NOT trigger the no_ai preference flag."""
    mem = _memory(preferences=[{
        "topic": "no_ai", "value": "true", "turn": 4, "user_quote": "не хочу ИИ",
    }])
    text = "Понял, без ИИ — соберём на скриптах и связках."
    violations = check_against_memory(text, mem)
    assert violations == []


def test_check_against_memory_clean_text_no_violations():
    mem = _memory(retractions=[{
        "type": "channel", "value": "telegram", "turn": 3, "user_quote": "не мой канал",
    }])
    text = "Расскажите, какой у вас бюджет на инструменты."
    assert check_against_memory(text, mem) == []


def test_check_against_memory_empty_memory():
    assert check_against_memory("любой текст", _memory()) == []
    assert check_against_memory("", _memory(retractions=[{
        "type": "tool", "value": "x", "turn": 1, "user_quote": "",
    }])) == []


# ---------------------------------------------------------------------------
# Fix 6 — single English-token replacement (client → клиент)
# ---------------------------------------------------------------------------


def test_sanitize_replaces_client_singleton():
    out = sanitize("каналы клиентов: client; сайт")
    assert "client" not in out
    assert "клиент" in out  # both «клиентов» (already there) and «client» → «клиент»


def test_sanitize_replaces_clients_plural():
    out = sanitize("список: clients и партнёры")
    assert "clients" not in out
    assert "клиенты" in out


def test_find_english_singletons_skips_brand_allowlist():
    text = "Будем использовать ChatGPT, Claude, Notion и Tilda."
    assert find_english_singletons(text) == []


def test_find_english_singletons_catches_unknown_word():
    text = "В Сводке: client, ChatGPT, request."
    leaks = find_english_singletons(text)
    assert "client" in leaks
    assert "request" in leaks
    assert "ChatGPT" not in leaks


# ---------------------------------------------------------------------------
# Fix 1 — apply_no_ai_if_rejected
# ---------------------------------------------------------------------------


def _state_with(**resources):
    s = WorkflowState()
    for k, v in resources.items():
        setattr(s.resources, k, v)
    return s


def test_apply_no_ai_clears_fields_when_rejected():
    s = _state_with(
        llm_required=True,
        has_llm_key=True,
        llm_provider="OpenAI",
        llm_subscription_quote="есть подписка на ChatGPT",
    )
    transcript = [
        {"role": "assistant", "content": "Нужен ли ИИ?"},
        {"role": "user", "content": "Никакого ИИ, делайте на скриптах."},
    ]
    assert apply_no_ai_if_rejected(s, transcript) is True
    assert s.resources.llm_required is False
    assert s.resources.has_llm_key is None
    assert s.resources.llm_provider is None
    assert s.resources.llm_subscription_quote is None


def test_apply_no_ai_detects_various_phrasings():
    for phrase in [
        "никакого ИИ не нужно",
        "без ИИ обойдёмся",
        "не нужен ИИ в этом проекте",
        "не хочу ИИ",
        "обойдёмся без искусственного интеллекта",
    ]:
        s = _state_with(llm_required=True)
        transcript = [{"role": "user", "content": phrase}]
        assert apply_no_ai_if_rejected(s, transcript) is True, phrase
        assert s.resources.llm_required is False, phrase


def test_apply_no_ai_noop_when_not_rejected():
    s = _state_with(llm_required=True, llm_subscription_quote="есть подписка")
    transcript = [{"role": "user", "content": "Да, бюджет 5000."}]
    assert apply_no_ai_if_rejected(s, transcript) is False
    assert s.resources.llm_required is True
    assert s.resources.llm_subscription_quote == "есть подписка"


def test_apply_no_ai_ignores_bot_echo():
    """Bot may echo «без ИИ» as confirmation — that's not the user rejecting."""
    s = _state_with(llm_required=True)
    transcript = [
        {"role": "user", "content": "Давайте просто пересылку заявок."},
        {"role": "assistant", "content": "Понял, без ИИ — на скриптах."},
    ]
    assert apply_no_ai_if_rejected(s, transcript) is False
    assert s.resources.llm_required is True


def test_apply_no_ai_records_session_memory_preference():
    s = _state_with(llm_required=True)
    transcript = [{"role": "user", "content": "никакого ИИ"}]
    apply_no_ai_if_rejected(s, transcript)
    prefs = s.conversation.session_memory.preferences
    assert any(p.get("topic") == "no_ai" for p in prefs)


# ---------------------------------------------------------------------------
# Fix 2 — recover_budget_from_history
# ---------------------------------------------------------------------------


def test_recover_budget_simple_thousand():
    s = WorkflowState()
    transcript = [{"role": "user", "content": "бюджет на инструменты 5000"}]
    assert recover_budget_from_history(s, transcript) is True
    assert s.resources.monthly_budget_rub == 5000


def test_recover_budget_short_bare_number():
    """If the answer is just «5000», accept it (short message gets pass)."""
    s = WorkflowState()
    transcript = [
        {"role": "assistant", "content": "Сколько в месяц готовы тратить?"},
        {"role": "user", "content": "5000"},
    ]
    assert recover_budget_from_history(s, transcript) is True
    assert s.resources.monthly_budget_rub == 5000


def test_recover_budget_thousand_suffix():
    s = WorkflowState()
    transcript = [{"role": "user", "content": "Готова платить 5 тысяч в месяц"}]
    assert recover_budget_from_history(s, transcript) is True
    assert s.resources.monthly_budget_rub == 5000


def test_recover_budget_k_suffix():
    s = WorkflowState()
    transcript = [{"role": "user", "content": "бюджет порядка 10к ежемесячно"}]
    assert recover_budget_from_history(s, transcript) is True
    assert s.resources.monthly_budget_rub == 10000


def test_recover_budget_does_not_overwrite_existing():
    s = WorkflowState()
    s.resources.monthly_budget_rub = 8000
    transcript = [{"role": "user", "content": "бюджет 5000"}]
    assert recover_budget_from_history(s, transcript) is False
    assert s.resources.monthly_budget_rub == 8000


def test_recover_budget_no_signal_long_message():
    """Random number in a long message — no budget context — don't extract."""
    s = WorkflowState()
    transcript = [{"role": "user", "content": (
        "У нас в команде 5 человек, в день примерно 50 заявок, "
        "и сезон пиковый длится 3 месяца — обычно с июля по сентябрь."
    )}]
    assert recover_budget_from_history(s, transcript) is False
    assert s.resources.monthly_budget_rub is None


# ---------------------------------------------------------------------------
# Fix 3b — filter_channels_to_user_history
# ---------------------------------------------------------------------------


def test_filter_channels_drops_hallucinated():
    s = WorkflowState()
    s.point_a.channels = ["Telegram", "WhatsApp", "сайт"]
    transcript = [{"role": "user", "content": "Клиенты пишут на сайт через форму."}]
    assert filter_channels_to_user_history(s, transcript) is True
    # Telegram & WhatsApp not mentioned by the user — dropped.
    assert s.point_a.channels == ["сайт"]


def test_filter_channels_keeps_aliases():
    """User typed «телеграм» (ru) — extractor stored canonical «Telegram». Keep."""
    s = WorkflowState()
    s.point_a.channels = ["Telegram"]
    transcript = [{"role": "user", "content": "пишут в телеграм и иногда на почту"}]
    assert filter_channels_to_user_history(s, transcript) is False
    assert s.point_a.channels == ["Telegram"]


def test_filter_channels_noop_when_clean():
    s = WorkflowState()
    s.point_a.channels = ["сайт", "Telegram"]
    transcript = [{"role": "user", "content": "и сайт, и telegram, оба используем"}]
    assert filter_channels_to_user_history(s, transcript) is False
    assert s.point_a.channels == ["сайт", "Telegram"]


def test_filter_channels_noop_when_no_channels():
    s = WorkflowState()
    s.point_a.channels = []
    assert filter_channels_to_user_history(s, [{"role": "user", "content": "x"}]) is False
