"""Single source of truth: какие поля нужны для дев-спеки и кто их отвечает.

Каждое поле классифицировано:
  OWNER — только владелец бизнеса может ответить (бюджет, сроки, что НЕ делать,
          какие сервисы у него уже есть). Asker задаёт plain-language вопрос.
  DEV   — решает разработчик/LLM на этапе генерации спеки (архитектура, NFR,
          модель данных, API-контракты, оценка). Владельца не спрашиваем —
          либо инферим из state, либо ставим "Open for dev: …" маркер.
  MIXED — кусок от владельца + кусок от LLM (Goal — бизнес-цель + формулировка;
          User flows — сценарий + формализация).

Манифест драйвит:
  - app/state.py:open_gaps()       — возвращает только OWNER-поля текущей фазы
  - app/workflows/v2_adaptive.py   — _ask_next берёт hint из owner_question[lang]
  - app/workflows/v2_adaptive.py   — _emit_specs формирует pending_decisions список
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .state import Phase, WorkflowState

Classification = Literal["OWNER", "DEV", "MIXED"]


def _q(ru: str, en: str | None = None) -> dict[str, str]:
    """Build a bilingual question dict.

    If `en` is omitted, the EN fallback is the RU text — this is fine for
    DEV-only fields whose owner_question is never read by the asker (they
    serve as descriptive labels for dev-spec generation). For OWNER/MIXED
    fields, pass both languages explicitly.
    """
    return {"ru": ru, "en": en if en is not None else ru}


@dataclass(frozen=True)
class SpecField:
    section: str                          # секция дев-спеки ("Goal", "Architecture sketch", ...)
    name: str                             # ключ поля
    classification: Classification
    state_path: str | None                # "point_a.business_type" — если поле есть в state
    owner_question: dict[str, str] | None  # bilingual plain-language hint для аскера (OWNER/MIXED)
    phase: Phase | None                   # на какой фазе спрашивать; None для DEV
    priority: int                         # порядок внутри фазы
    decision_topic: str | dict[str, str] | None  # тема для «На выбор:» / dev-field question


# ---- Manifest ---------------------------------------------------------------

MANIFEST: list[SpecField] = [
    # ---- POINT A — что есть сейчас ----
    SpecField("Context", "business_type", "OWNER", "point_a.business_type",
              _q(
                  "Расскажите в двух словах — что у вас за дело? Что вы продаёте или какие услуги оказываете?",
                  "Tell me in a sentence — what's your business? What do you sell or what services do you offer?",
              ),
              Phase.POINT_A, 1, None),
    SpecField("Context", "stage", "OWNER", "point_a.stage",
              _q(
                  "Дело уже катится или только-только запустились?\n— давно работает\n— недавно запустила, ещё притираемся\n— только готовлюсь стартовать",
                  "Is the business already running or just starting?\n— running for a while\n— recently launched, still finding our feet\n— still preparing to start",
              ),
              Phase.POINT_A, 2, None),
    SpecField("Context", "team_size", "OWNER", "point_a.team_size",
              _q(
                  "Сколько вас всего в команде, включая вас? Достаточно примерной цифры.",
                  "How many people on the team total, including you? A rough number is fine.",
              ),
              Phase.POINT_A, 3, None),
    SpecField("Context", "channels", "OWNER", "point_a.channels",
              _q(
                  "Откуда обычно приходят клиенты и где с вами общаются?\n— Telegram / WhatsApp / Direct\n— сайт\n— маркетплейсы\n— что-то ещё (расскажите)",
                  "Where do customers usually come from and where do they talk to you?\n— Instagram / WhatsApp / DM\n— website\n— marketplaces\n— something else (tell me)",
              ),
              Phase.POINT_A, 4, None),
    SpecField("Context", "tools_in_use", "OWNER", "point_a.tools_in_use",
              _q(
                  "А где вы ведёте всё «на бумаге» — заявки, клиентов, заказы? Excel, гугл-таблица, в голове, в каком-то приложении?",
                  "Where do you keep everything 'on paper' — inquiries, customers, orders? Excel, Google Sheets, in your head, some app?",
              ),
              Phase.POINT_A, 5, None),
    SpecField("Context", "pain_points", "OWNER", "point_a.pain_points",
              _q(
                  "Что в обычной неделе вас больше всего бесит или съедает время? Любая мелочь — лучше с конкретным примером, что недавно случилось.",
                  "What in a typical week eats up the most time or annoys you? Anything counts — best with a concrete recent example.",
              ),
              Phase.POINT_A, 6, None),
    SpecField("Context", "daily_volume", "OWNER", "point_a.daily_volume",
              _q(
                  "А сколько обращений в день примерно? 5, 50, или совсем «не считала»?",
                  "Roughly how many inquiries per day? 5, 50, or 'haven't counted'?",
              ),
              Phase.POINT_A, 7, None),

    # ---- POINT B — куда хотим прийти ----
    SpecField("Goal", "primary_value", "OWNER", "point_b.primary_value",
              _q(
                  "Если бы я мог волшебной палочкой что-то одно поменять — что бы вы выбрали?\n— сэкономить ваше время\n— заработать больше\n— перестать выгорать\n— чтобы клиентам было приятнее",
                  "If I could wave a magic wand and change one thing, what would you pick?\n— save your time\n— earn more\n— stop burning out\n— make things nicer for customers",
              ),
              Phase.POINT_B, 1, None),
    SpecField("Goal", "desired_outcome", "OWNER", "point_b.desired_outcome",
              _q(
                  "Представьте, что прошло пару месяцев и всё работает как мечталось. Что в обычном дне станет иначе?",
                  "Imagine a couple of months from now and it's all working the way you dreamed. What's different in a regular day?",
              ),
              Phase.POINT_B, 2, None),
    SpecField("Acceptance criteria", "success_metric", "OWNER", "point_b.success_metric",
              _q(
                  "А как мы поймём, что получилось? Какой-нибудь факт или цифра, на которую можно посмотреть и обрадоваться.",
                  "How will we know it worked? Some fact or number you can look at and feel good about.",
              ),
              Phase.POINT_B, 3, None),
    SpecField("Out of scope", "out_of_scope", "OWNER", "point_b.out_of_scope",
              _q(
                  "А что точно НЕ нужно — чтобы проект не раздулся? Например: «не делаем приложение», «не трогаем оплату».",
                  "What definitely should NOT be in scope — so the project doesn't bloat? For example: 'no app', 'don't touch payments'.",
              ),
              Phase.POINT_B, 4, None),
    SpecField("Context", "timeframe", "OWNER", "point_b.timeframe",
              _q(
                  "К какому сроку хотелось бы это запустить — через месяц, квартал, не горит?",
                  "By when would you want to launch this — a month, a quarter, no rush?",
              ),
              Phase.POINT_B, 5, None),
    SpecField("User flows", "audience", "OWNER", "point_b.audience",
              _q(
                  "Кто будет этим пользоваться — вы сами, ваша команда, или клиенты?",
                  "Who will use this — you, your team, or your customers?",
              ),
              Phase.POINT_B, 6, None),
    SpecField("User flows", "user_flows", "MIXED", "point_b.user_flows",
              _q(
                  "Опишите как кино, в один сценарий: вот пришёл клиент — и что дальше? Что должно произойти, чем закончиться?",
                  "Describe it like a movie scene: a customer arrives — then what? What should happen, how does it end?",
              ),
              Phase.POINT_B, 7, None),
    SpecField("Functional requirements", "functional_requirements_owner", "MIXED",
              "point_b.functional_requirements_owner",
              _q(
                  "Что система обязательно должна уметь, чтобы вы сказали «да, это оно»? Перечислите по пунктам, простыми словами — «отвечает ночью сам», «записывает контакт в таблицу».",
                  "What must the system absolutely be able to do for you to say 'yes, this is it'? List them simply — 'replies at night on its own', 'records the contact into a spreadsheet'.",
              ),
              Phase.POINT_B, 8, None),

    # ---- RESOURCES — деньги, доступы, поддержка ----
    SpecField("Resources / credentials", "monthly_budget_rub", "OWNER", "resources.monthly_budget_rub",
              _q(
                  "Сколько в месяц готовы тратить на подписки и инструменты? Просто порядок:\n— до 1000 ₽\n— 1–5 тысяч\n— 5–20 тысяч\n— больше 20 тысяч\n— пока не определилась",
                  "Roughly how much per month can you spend on subscriptions and tools? Just the ballpark in US dollars:\n— under $10\n— $10–50\n— $50–200\n— more than $200\n— not sure yet\n\nNOTE for the model: the state field is named `monthly_budget_rub` but it stores a plain integer (no currency). If the owner says a range like '$40-60' — STORE the upper bound (60). NEVER multiply by 100 / 1000. The number goes in as-is.",
              ),
              Phase.RESOURCES, 1, None),
    SpecField("Resources / credentials", "has_llm_key", "OWNER", "resources.has_llm_key",
              _q(
                  "Подписка на ChatGPT (или Claude, GigaChat) — у вас уже есть или ещё нет?",
                  "Subscription to ChatGPT (or Claude, Gemini) — do you have one already or not yet?",
              ),
              Phase.RESOURCES, 2, None),
    SpecField("Resources / credentials", "llm_provider", "OWNER", "resources.llm_provider",
              _q(
                  "А на какой именно сервис подписка — ChatGPT, Claude, GigaChat, что-то ещё?",
                  "Which service is the subscription for — ChatGPT, Claude, Gemini, something else?",
              ),
              Phase.RESOURCES, 3, None),
    SpecField("Integrations", "integrations_available", "MIXED", "resources.integrations_available",
              _q(
                  "Какие сервисы у вас уже подключены и работают? CRM, рассыльщик, аналитика, конструктор сайта — назовите что вспомните.",
                  "Which services do you already have connected and working? CRM, email-sender, analytics, website builder — name what you can recall.",
              ),
              Phase.RESOURCES, 4, None),
    SpecField("Integrations", "integrations_needed", "MIXED", "resources.integrations_needed",
              _q(
                  "А что вы давно хотели подключить, но руки не дошли?",
                  "What have you been wanting to connect for a while but haven't gotten to?",
              ),
              Phase.RESOURCES, 5, None),
    SpecField("Resources / credentials", "tech_savviness", "OWNER", "resources.tech_savviness",
              _q(
                  "Насколько вам комфортно с техникой?\n— честно говоря, совсем не дружу\n— что-то умею, по инструкции справляюсь\n— уверенно, мне можно показать админку",
                  "How comfortable are you with tech?\n— honestly, not at all\n— I can manage with instructions\n— confident, you can show me the admin panel",
              ),
              Phase.RESOURCES, 6, None),
    SpecField("Resources / credentials", "maintainer", "OWNER", "resources.maintainer",
              _q(
                  "После запуска кто будет за этим присматривать — вы, кто-то из команды, или мы возьмём поддержку на себя?",
                  "After launch, who will keep an eye on it — you, someone on your team, or shall we take over support?",
              ),
              Phase.RESOURCES, 7, None),
    SpecField("Resources / credentials", "deployment_constraints", "OWNER", "resources.deployment_constraints",
              _q(
                  "Есть что-то, чем уже пользуетесь и менять не хочется? Например, сайт на Tilda, или вы привыкли к конкретной CRM. Или наоборот — что-то, что точно не подходит.",
                  "Anything you already use and don't want to change? Like a website on Tilda, or you're used to a specific CRM. Or the opposite — something that definitely doesn't fit.",
              ),
              Phase.RESOURCES, 8, None),
    SpecField("Architecture sketch", "preferred_channel", "OWNER", "resources.preferred_channel",
              _q(
                  "А где клиентам удобнее всего с этим общаться?\n— на сайте в чате\n— в Telegram\n— везде сразу\n— ещё думаю",
                  "Where is it most convenient for customers to interact with this?\n— website chat\n— Telegram\n— everywhere\n— still thinking",
              ),
              Phase.RESOURCES, 9, None),

    # ---- DEV-only поля — владельца не спрашиваем ----
    SpecField("Non-functional requirements", "latency_reliability_security", "DEV",
              None, None, None, 90,
              _q("Какие требования по скорости отклика, надёжности, защите данных нужно зафиксировать?")),
    SpecField("Architecture sketch", "frontend", "DEV",
              None, None, None, 91,
              _q("Какой фронтенд: чат-виджет на сайте, Telegram-бот, оба варианта?")),
    SpecField("Architecture sketch", "backend", "DEV",
              None, None, None, 92,
              _q("Какой стек бэкенда — язык/фреймворк под объёмы и интеграции?")),
    SpecField("Architecture sketch", "storage", "DEV",
              None, None, None, 93,
              _q("Где хранить данные: Sheets / Postgres / Firestore / другое?")),
    SpecField("Architecture sketch", "hosting", "DEV",
              None, None, None, 94,
              _q("Где разворачивать: Render / VPS / serverless / у заказчика на сервере?")),
    SpecField("Data model", "entities", "DEV",
              None, None, None, 95,
              _q("Какие сущности и поля нужны в хранилище под этот сценарий?")),
    SpecField("API contracts", "endpoints", "DEV",
              None, None, None, 96,
              _q("Какие endpoints / webhooks потребуются между компонентами?")),
    SpecField("Estimated effort", "effort", "DEV",
              None, None, None, 97,
              _q("Грубая вилка по срокам разработки в днях/неделях.")),
]


# ---- Helpers ----------------------------------------------------------------

def _read_state_path(state: WorkflowState, path: str):
    """Вернуть значение по 'point_a.channels' и т.п."""
    obj: object = state
    for part in path.split("."):
        obj = getattr(obj, part, None)
        if obj is None:
            return None
    return obj


def _is_filled(value) -> bool:
    if value is None or value == "":
        return False
    if isinstance(value, list):
        return bool(value)
    return True


def fields_for_phase(phase: Phase) -> list[SpecField]:
    """Все OWNER/MIXED-поля текущей фазы, отсортированные по priority."""
    return sorted(
        [f for f in MANIFEST if f.phase == phase and f.classification in ("OWNER", "MIXED")],
        key=lambda f: f.priority,
    )


# Ask RESOURCES fields only when the corresponding aspect is relevant to the
# solution option selected during ANALYSIS.
_SOLUTION_GATED_RESOURCE_FIELDS = {
    "has_llm_key": "needs_ai",
    "llm_provider": "needs_ai",
    "preferred_channel": "needs_chat_ui",
    "integrations_needed": "needs_external_integrations",
}


def field_relevant_for_solution(field_name: str, state: WorkflowState) -> bool:
    """True если поле уместно при выбранной форме решения.

    Для всех полей вне `_SOLUTION_GATED_RESOURCE_FIELDS` всегда True.
    Для гейтированного поля: True когда соответствующий solution-флаг is True
    (или None — пока не знаем точно, спрашиваем на всякий случай).
    False — только когда флаг ЯВНО False (владелец выбрал опцию, где это поле
    не нужно).
    """
    flag_name = _SOLUTION_GATED_RESOURCE_FIELDS.get(field_name)
    if flag_name is None:
        return True
    sol = getattr(state, "solution", None)
    if sol is None:
        return True
    flag_val = getattr(sol, flag_name, None)
    return flag_val is not False


def owner_gaps(state: WorkflowState, phase: Phase) -> list[SpecField]:
    """OWNER/MIXED-поля текущей фазы, ещё не заполненные в state.

    На фазе RESOURCES дополнительно отфильтровываем поля, которые не нужны при
    выбранной форме решения (см. `field_relevant_for_solution`).
    """
    gaps: list[SpecField] = []
    for f in fields_for_phase(phase):
        if f.state_path is None:
            continue
        if not field_relevant_for_solution(f.name, state):
            continue
        if not _is_filled(_read_state_path(state, f.state_path)):
            gaps.append(f)
    return gaps


def next_owner_field(state: WorkflowState, phase: Phase) -> SpecField | None:
    gaps = owner_gaps(state, phase)
    return gaps[0] if gaps else None


def is_phase_complete(state: WorkflowState, phase: Phase) -> bool:
    """Фаза готова, когда все OWNER-поля её заполнены. MIXED-поля не блокируют —
    их кусок дополнит LLM в _emit_specs.

    Гейтированные поля (см. `field_relevant_for_solution`) не учитываются,
    если выбранная форма решения их не требует."""
    for f in fields_for_phase(phase):
        if f.classification != "OWNER" or f.state_path is None:
            continue
        if not field_relevant_for_solution(f.name, state):
            continue
        if not _is_filled(_read_state_path(state, f.state_path)):
            return False
    return True


def collect_pending_decisions(state: WorkflowState) -> list[dict[str, str]]:
    """DEPRECATED: kept for backwards compatibility, always returns [].

    Previously fed «На выбор:» blocks into the dev spec, which the owner
    explicitly banned (decisions affecting client/owner convenience must be
    closed during chat). The DEV_COVERAGE phase now closes those gaps via a
    scanner LLM and stores answers in `state.resources.dev_coverage_qa`. No
    pending-decision list is needed; the dev-spec writer reads from state +
    dev_coverage_qa directly. Returning [] preserves the call signature for
    any existing consumer/test until it can be removed.
    """
    return []
