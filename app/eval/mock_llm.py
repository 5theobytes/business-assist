"""Deterministic LLM responder for offline tests and competition.

Strategy: route by inspecting the system prompt and tool list.
- If `tools` contains `update_state` → act as extractor (regex-based fact picker).
- If system contains MOMTEST_SYSTEM marker → emit a Mom-Test follow-up.
- If system contains BUSINESS_SPEC_PROMPT marker → emit business spec from JSON.
- If system contains DEV_SPEC_PROMPT marker → emit dev spec from JSON.
- If system contains "## Текущая фаза" (asker_system) → ask one question for top gap.
- Else (V1 orchestrator) → behave as one-shot funnel.

Not as smart as real Claude, but deterministic and good enough to:
  (a) Drive end-to-end workflow tests
  (b) Produce a sensible 'baseline' transcript when no API key is available
"""
from __future__ import annotations

import json
import re
from typing import Any

from ..llm import LLMResponse


# -------- pattern → field extractor --------

CHANNEL_TOKENS = {
    "telegram": "telegram",
    "телеграм": "telegram",
    "instagram": "instagram",
    "инстаграм": "instagram",
    "директ": "instagram direct",
    "whatsapp": "whatsapp",
    "вотсап": "whatsapp",
    "сайт": "сайт",
    "лендинг": "лендинг",
    "тильда": "tilda",
    "tilda": "tilda",
    "авито": "авито",
    "телефон": "телефон",
    "сарафан": "сарафан",
}

TOOL_TOKENS = {
    "excel": "excel",
    "таблиц": "google sheets",
    "google sheets": "google sheets",
    "блокнот": "блокнот",
    "amocrm": "amocrm",
    "битрикс": "битрикс24",
    "1с": "1c",
    "юkassa": "юkassa",
    "юкасса": "юkassa",
    "tilda": "tilda",
    "тильда": "tilda",
    "telegram": "telegram",
    "заметки": "заметки в телефоне",
}

PAIN_KEYWORDS = ["теря", "забы", "не успева", "ошиб", "пропуска", "уход", "тон", "руко", "вруч"]


def _extract_patch(user_text: str, prev_bot_text: str = "") -> dict:
    """Extract structured update. Optionally uses the last bot question as context
    to disambiguate (e.g. "идеальный день?" → user reply maps to desired_outcome).
    """
    txt = user_text.lower()
    bot = prev_bot_text.lower()
    patch: dict[str, Any] = {"point_a": {}, "point_b": {}, "resources": {}}
    pa = patch["point_a"]
    pb = patch["point_b"]
    pr = patch["resources"]

    # Context-aware: the bot's last question tells us what slot to fill.
    if any(p in bot for p in ["идеальный", "как должен выглядеть", "одной фразой",
                                "что должно стать иначе", "после внедрения"]):
        pb["desired_outcome"] = user_text.strip()
    if any(p in bot for p in ["метрик", "как поймём", "какая цифра"]):
        pb["success_metric"] = user_text.strip()
    if "не делаем" in bot or "точно не нужно" in bot or "не надо" in bot:
        # split by ; or , after "точно не:" prefix
        m = re.search(r"точно не[:\-—\s]+(.+)", txt)
        if m:
            items = [s.strip() for s in re.split(r"[;,]", m.group(1)) if s.strip()]
            if items:
                pb["out_of_scope"] = items
    if any(p in bot for p in ["что для вас сейчас главное", "что главное", "что важнее"]):
        if "время" in txt:
            pb["primary_value"] = "time"
        elif "деньг" in txt or "заявок" in txt or "терять" in txt:
            pb["primary_value"] = "money"
        elif "голову" in txt or "головн" in txt:
            pb["primary_value"] = "sanity"
        elif "удобн" in txt or "клиент" in txt:
            pb["primary_value"] = "customer_experience"
    if any(p in bot for p in ["технически", "разбираетесь"]):
        if "не настраивал" in txt or "совсем не" in txt:
            pr["tech_savviness"] = "none"
        elif "немного" in txt or "копировал" in txt:
            pr["tech_savviness"] = "basic"
        elif "уверен" in txt:
            pr["tech_savviness"] = "confident"
    if "поддержив" in bot or "после запуска" in bot:
        m = re.search(r"поддерж\S*\s+буду\s+(.+?)(?:[.;\n]|$)", txt)
        if m:
            pr["maintainer"] = m.group(1).strip()

    # business_type
    for kw, val in [("маникюр", "студия маникюра"), ("курс", "онлайн-курсы"),
                     ("сантехник", "частный сантехник"), ("магазин", "интернет-магазин"),
                     ("услуг", "услуги")]:
        if kw in txt:
            pa["business_type"] = val
            break

    # channels
    chans = []
    for token, label in CHANNEL_TOKENS.items():
        if token in txt and label not in chans:
            chans.append(label)
    if chans:
        pa["channels"] = chans

    # tools
    tools = []
    for token, label in TOOL_TOKENS.items():
        if token in txt and label not in tools:
            tools.append(label)
    if tools:
        pa["tools_in_use"] = tools

    # pain_points (use full sentences containing pain keywords)
    pains: list[str] = []
    for sentence in re.split(r"[.;!\n]+", user_text):
        s = sentence.strip()
        if not s:
            continue
        if any(k in s.lower() for k in PAIN_KEYWORDS):
            pains.append(s)
    if pains:
        pa["pain_points"] = pains

    # daily_volume
    m = re.search(r"(\d+\s*[-–—]\s*\d+|\d+)\s*(заявок|заказов|обращ|клиент|штук)", txt)
    if m:
        pa["daily_volume"] = m.group(0)

    # primary_value
    if any(k in txt for k in ["сэконом", "время", "часов в", "автоматиз"]):
        pb["primary_value"] = "time"
    elif any(k in txt for k in ["терять деньг", "терять заявк", "выручк", "продаж"]):
        pb["primary_value"] = "money"
    elif any(k in txt for k in ["голову", "из головы", "забы", "перестать держать"]):
        pb["primary_value"] = "sanity"
    elif any(k in txt for k in ["удобн", "клиент", "experience"]):
        pb["primary_value"] = "customer_experience"

    # desired outcome
    if any(k in txt for k in ["автоматич", "сами падают", "приходило уведомлен", "получал инвайт"]):
        m = re.search(r"(?:чтобы|хочу,?|нужно,?)\s*(.+?)(?:[.;\n]|$)", user_text)
        if m:
            pb["desired_outcome"] = m.group(1).strip()

    # success metric
    m = re.search(r"(?:0|не\s*более|не\s*больше)\s+\S+\s+\S+", user_text.lower())
    if m:
        pb["success_metric"] = m.group(0)

    # out_of_scope
    if "не делаем" in txt or "точно не" in txt or "не нужно" in txt or "не надо" in txt:
        items = []
        for token in ["сайт", "crm", "приложен", "мобильн"]:
            if token in txt:
                items.append({"сайт": "сайт", "crm": "CRM", "приложен": "приложение",
                               "мобильн": "мобильное приложение"}[token])
        if items:
            pb["out_of_scope"] = items

    # budget
    m = re.search(r"(\d{3,6})\s*(?:₽|руб)", txt)
    if m:
        pr["monthly_budget_rub"] = int(m.group(1))

    # llm key
    if "есть ключ" in txt or "ключ к openai" in txt or "ключ к claude" in txt:
        pr["has_llm_key"] = True
    if "нет ключа" in txt or "ключа нет" in txt:
        pr["has_llm_key"] = False
    for prov, label in [("openai", "OpenAI"), ("anthropic", "Anthropic"),
                          ("yandex", "YandexGPT"), ("gigachat", "GigaChat")]:
        if prov in txt:
            pr["llm_provider"] = label
            break

    # tech savviness
    if "не технич" in txt or "совсем не" in txt:
        pr["tech_savviness"] = "none"
    elif "немного" in txt or "копировал" in txt:
        pr["tech_savviness"] = "basic"
    elif "уверен" in txt:
        pr["tech_savviness"] = "confident"

    # maintainer
    m = re.search(r"поддерж\S*\s+будет?\s+(\S+)", txt)
    if m:
        pr["maintainer"] = m.group(1)
    if "сама" in txt and "буду поддерж" not in txt:
        pass  # too noisy
    if "наш техлид" in txt or "наш team lead" in txt:
        pr["maintainer"] = "наш техлид"

    # confirmation
    if re.match(r"^\s*(да|верно|так|правильно|подтверждаю)\b", txt):
        patch["user_confirmed_summary"] = True

    return patch


# -------- response routers --------


def _last_user_text(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return m.get("content", "") if isinstance(m.get("content"), str) else ""
    return ""


def _system_text(system) -> str:
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "\n".join(b.get("text", "") for b in system if isinstance(b, dict))
    return ""


def _gap_question(state_json: dict) -> str:
    """Pick a templated question targeting the first open gap."""
    pa = state_json.get("point_a") or {}
    pb = state_json.get("point_b") or {}
    pr = state_json.get("resources") or {}
    if not pa.get("business_type"):
        return "Расскажите кратко: чем занимается ваш бизнес?"
    if not pa.get("channels"):
        return "А клиенты с вами как связываются — телеграм, инстаграм, сайт, ещё что-то?"
    if not pa.get("tools_in_use"):
        return "Где вы сейчас ведёте клиентов — Excel, блокнот, какая-то программа, или нигде?"
    if not pa.get("pain_points"):
        return "А что больше всего отнимает время или нервы в обычный день?"
    if not pb.get("primary_value"):
        return ("Что для вас сейчас главное?\n"
                "— Сэкономить время\n— Перестать терять заявки/деньги\n"
                "— Снять головную боль\n— Сделать клиентам удобнее")
    if not pb.get("desired_outcome"):
        return "Опишите одной фразой: как должен выглядеть идеальный обычный день после внедрения?"
    if not pb.get("success_metric"):
        return "Как поймём, что получилось — какая цифра должна измениться?"
    if not pb.get("out_of_scope"):
        return "А чего точно делать НЕ надо? (что мы исключаем из работ)"
    if pr.get("monthly_budget_rub") is None:
        return ("Какую месячную подписку на инструменты вы готовы платить?\n"
                "— До 1000 ₽\n— До 5000 ₽\n— До 20000 ₽\n— Не ограничено")
    if pr.get("has_llm_key") is None:
        return "У вас уже есть API-ключ к ChatGPT / Claude / другой ИИ-модели?"
    if not pr.get("tech_savviness"):
        return ("Насколько вы технически уверены?\n"
                "— Совсем не настраивал ничего сам\n— Немного, копировал коды\n— Уверенный пользователь")
    if not pr.get("maintainer"):
        return "Кто будет поддерживать систему после запуска — вы сами, кто-то из команды, подрядчик?"
    return "Похоже, всё собрано. Сверим картину?"


def _state_from_messages(messages: list[dict]) -> dict:
    """Extract the embedded state JSON from the asker_system input message."""
    for m in messages:
        c = m.get("content")
        if isinstance(c, str) and c.strip().startswith("{") and "point_a" in c:
            try:
                return json.loads(c)
            except Exception:
                pass
    return {}


def _state_from_system(system_text: str) -> dict:
    _, marker, state_text = system_text.partition("## Current state (internal only):")
    if marker:
        try:
            state, _ = json.JSONDecoder().raw_decode(state_text.lstrip())
            return state if isinstance(state, dict) else {}
        except json.JSONDecodeError:
            return {}
    m = re.search(r"State \(только для тебя\):\s*(\{.*?\})\s*\n\nЗадай", system_text, re.DOTALL)
    if not m:
        m = re.search(r"состояние \(только для тебя.*?\):\s*(\{.*?\})\s*\n\nЗадай", system_text,
                       re.DOTALL | re.IGNORECASE)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            return {}
    return {}


def _render_business_spec(state: dict) -> str:
    pa = state.get("point_a") or {}
    pb = state.get("point_b") or {}
    pr = state.get("resources") or {}
    pains = "\n".join(f"- {p}" for p in pa.get("pain_points") or ["—"])
    channels = ", ".join(pa.get("channels") or ["—"])
    tools = ", ".join(pa.get("tools_in_use") or ["—"])
    out = "\n".join(f"- {x}" for x in pb.get("out_of_scope") or ["—"])
    budget = pr.get("monthly_budget_rub")
    return f"""# Что мы делаем — простыми словами

## Зачем
{pb.get('desired_outcome') or 'Снять боль из текущей рутины'}

## Главная цель
{pb.get('success_metric') or 'Сократить ручную работу'}

## Как сейчас (точка А)
- Бизнес: {pa.get('business_type') or '—'}
- Каналы клиентов: {channels}
- Инструменты: {tools}
- Болит:
{pains}

## Как будет (точка Б)
- {pb.get('desired_outcome') or '—'}
- Метрика успеха: {pb.get('success_metric') or '—'}

## Что мы НЕ делаем
{out}

## Что нужно от вас
- Доступы к: {channels}
- API-ключ к LLM: {'есть ('+ (pr.get('llm_provider') or 'провайдер уточним') +')' if pr.get('has_llm_key') else 'нужно получить, дам инструкцию'}
- Поддержка после запуска: {pr.get('maintainer') or 'уточнить'}

## Сколько денег и времени
- Подписки на инструменты: ~{budget if budget is not None else '—'} ₽/мес

## Как поймём, что получилось
- {pb.get('success_metric') or '—'}
"""


def _render_dev_spec(state: dict) -> str:
    pa = state.get("point_a") or {}
    pb = state.get("point_b") or {}
    pr = state.get("resources") or {}
    return f"""# Implementation Spec — {pa.get('business_type') or 'project'}

## Goal
{pb.get('desired_outcome') or 'TBD'}

## Context
Бизнес: {pa.get('business_type') or 'TBD'}; стадия: {pa.get('stage') or 'TBD'}; объём: {pa.get('daily_volume') or 'TBD'}.

## User flows
1. {pb.get('desired_outcome') or 'TBD'}

## Functional requirements
- FR-1: интеграция с каналами: {', '.join(pa.get('channels') or ['TBD'])}
- FR-2: хранение в {', '.join(pa.get('tools_in_use') or ['TBD'])}
- FR-3: метрика: {pb.get('success_metric') or 'TBD'}

## Non-functional requirements
- Локаль: ru-RU
- Latency: уведомление до 60 сек после события
- Reliability: ретрай до 3 раз при сбое канала

## Architecture sketch
- Backend: Python (FastAPI) либо n8n/Make для no-code оркестрации
- Storage: {(pa.get('tools_in_use') or ['Google Sheets'])[0]}
- External APIs: {', '.join(pa.get('channels') or ['TBD'])}
- LLM: {pr.get('llm_provider') or 'OpenAI / Anthropic'} ({'ключ есть' if pr.get('has_llm_key') else 'ключ получить'})

## Integrations
| Сервис | Назначение | Доступ есть? | Что настроить |
| --- | --- | --- | --- |
{chr(10).join(f"| {ch} | приём заявок | TBC | webhook / API |" for ch in (pa.get('channels') or ['TBD']))}

## Out of scope
{chr(10).join(f"- {x}" for x in (pb.get('out_of_scope') or ['—']))}

## Acceptance criteria
- AC-1: {pb.get('success_metric') or 'TBD'}

## Resources / credentials
- Бюджет: ~{pr.get('monthly_budget_rub') or 'TBD'} ₽/мес
- Поддержка: {pr.get('maintainer') or 'TBD'}
- Tech savviness заказчика: {pr.get('tech_savviness') or 'TBD'}

## Open questions
- Точные тарифы и лимиты выбранных провайдеров
"""


# -------- main responder --------


def mock_responder(system, messages: list[dict], tools: list[dict] | None) -> LLMResponse:
    sys_text = _system_text(system)
    last_user = _last_user_text(messages)

    # 1. Extractor mode
    if tools and any(t.get("name") == "update_state" for t in tools):
        prev_bot = ""
        # find the last assistant message before the last user
        seen_user = False
        for m in reversed(messages):
            if m.get("role") == "user" and not seen_user:
                seen_user = True
                continue
            if m.get("role") == "assistant" and seen_user:
                prev_bot = m.get("content", "") if isinstance(m.get("content"), str) else ""
                break
        patch = _extract_patch(last_user, prev_bot)
        return LLMResponse(text="", tool_use={"name": "update_state", "input": patch})

    # 2a. DEV_COVERAGE scanner — return complete=true so the simulator drives the
    #     workflow through to spec emission without needing case-specific question
    #     handling in offline tests. (Real Claude exercises the full scanner loop.)
    if "проверить достаточно ли информации" in sys_text or "DEV_COVERAGE" in sys_text:
        return LLMResponse(text='{"complete": true}')

    # 2a.5 ANALYSIS scanner — always go straight to propose_options (no gap-fill
    #      in mock; live runs exercise the full scanner loop with real LLM).
    if (
        "сформулировать 2–3 ОСМЫСЛЕННЫХ варианта" in sys_text
        or "ANALYSIS_SCANNER" in sys_text
    ):
        return LLMResponse(text='{"action": "propose_options", "rationale": "mock"}')

    # 2a.6 ANALYSIS options-generator — emit a single non-AI option matching the
    #      persona's domain so the simulator drives forward to RESOURCES.
    if (
        "сформулировать 2–3 ВАРИАНТА решения" in sys_text
        or "ANALYSIS_OPTIONS" in sys_text
    ):
        # Pull a tiny bit of state context for a realistic-looking shape name.
        state = _state_from_messages(messages)
        biz = (state.get("point_a") or {}).get("business_type") or "ваш бизнес"
        stub_option = {
            "shape": "напоминалка в Google Sheets",
            "description": (
                f"Простой график в Google-таблице со встроенными напоминаниями для {biz}. "
                "Без бота, без подписок, без ИИ."
            ),
            "needs_ai": False,
            "needs_chat_ui": False,
            "needs_external_integrations": False,
            "approx_budget_rub": "до 500 ₽/мес",
            "approx_timeline": "3-5 рабочих дней",
            "recommended": True,
        }
        return LLMResponse(text=json.dumps(
            {"options": [stub_option], "intro": "Вот вариант, который подходит:"},
            ensure_ascii=False,
        ))

    # 2a.7 ANALYSIS PM — auto-pick the first option so simulator advances.
    if (
        "Бот только что показал владельцу 2–3 варианта" in sys_text
        or "ANALYSIS_PM" in sys_text
    ):
        return LLMResponse(text='{"intent": "pick", "chosen_index": 1}')

    # 2b. PM_REVIEW (final-stage Q+A on SPEC_REVIEW). Default to confirm so
    #     offline simulator runs reach DONE; live runs use real LLM.
    if "проджект-менеджер" in sys_text and "user_message" in sys_text:
        return LLMResponse(text='{"intent": "confirm"}')

    # 2. Spec writers
    if "Implementation Spec" in sys_text and "senior solutions architect" in sys_text:
        try:
            state = json.loads(last_user)
        except Exception:
            state = {}
        return LLMResponse(text=_render_dev_spec(state))
    if "Что мы делаем — простыми словами" in sys_text and "технический писатель" in sys_text:
        try:
            state = json.loads(last_user)
        except Exception:
            state = {}
        return LLMResponse(text=_render_business_spec(state))

    # 3. Mom-Test follow-up
    if "Mom Test" in sys_text:
        templates = [
            "А когда последний раз это было больно? Расскажите тот случай поподробнее.",
            "Как вы это сейчас решаете?",
            "Что вы уже пробовали с этим делать?",
        ]
        # rotate based on count of bot turns so far
        bot_turns = sum(1 for m in messages if m.get("role") == "assistant")
        return LLMResponse(text=templates[bot_turns % len(templates)])

    # 4. Asker (V2/V3/V4)
    if "Текущая фаза" in sys_text or "## State" in sys_text:
        state = _state_from_system(sys_text)
        return LLMResponse(text=_gap_question(state))

    # 5. V1 orchestrator: opening or generic next-question
    if "Business Assist" in sys_text and "фазам" in sys_text.lower():
        # opening?
        if last_user and "[служебное]" in last_user:
            return LLMResponse(text=(
                "Привет! Я помогу разобраться, что вашему бизнесу нужно автоматизировать или построить, "
                "и в конце соберу понятный план с конкретными шагами и расходами.\n\n"
                "С чего удобнее начать?\n"
                "— У меня магазин и теряются заявки\n"
                "— Я оказываю услуги, хочу убрать рутину\n"
                "— Хочу телеграм-бота, но не знаю какого\n"
                "— Не знаю, чего хочу — давайте разбираться вместе"
            ))
        # treat as asker over the conversation: best-effort by extracting state from history and asking next
        # build a state from all user messages
        from ..state import WorkflowState
        state = WorkflowState()
        prev_bot = ""
        for m in messages:
            content = m.get("content", "")
            if not isinstance(content, str):
                continue
            if m.get("role") == "assistant":
                prev_bot = content
                continue
            if m.get("role") == "user":
                p = _extract_patch(content, prev_bot)
                for sec in ("point_a", "point_b", "resources"):
                    target = getattr(state, sec)
                    for k, v in (p.get(sec) or {}).items():
                        cur = getattr(target, k, None)
                        if isinstance(cur, list):
                            for it in v:
                                if it not in cur:
                                    cur.append(it)
                        elif cur in (None, ""):
                            setattr(target, k, v)
        # if seems complete-ish, emit final spec
        if state.coverage_a() >= 0.75 and state.coverage_b() >= 0.5 and state.coverage_resources() >= 0.5:
            return LLMResponse(text=(
                _render_business_spec(state.model_dump(mode="json"))
                + "\n\n---\n\n"
                + _render_dev_spec(state.model_dump(mode="json"))
            ))
        return LLMResponse(text=_gap_question(state.model_dump(mode="json")))

    # Fallback
    return LLMResponse(text="(mock) Расскажите подробнее, пожалуйста.")
