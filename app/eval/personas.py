"""Scripted business-owner personas for offline simulation.

Each persona is a deterministic responder: given the bot's last message and the
conversation history, it picks the next user reply from a fact ledger.

Why scripted (not LLM-driven): deterministic replies make offline comparisons
reproducible and help isolate differences in workflow behavior. Live model-driven
personas are available as a separate, explicitly configured evaluation path.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class GroundTruth:
    """The full picture the persona 'has in their head' — judge uses this oracle."""

    business_type: str
    channels: list[str]
    tools_in_use: list[str]
    pain_points: list[str]
    primary_value: str  # time | money | sanity | customer_experience
    desired_outcome: str
    success_metric: str
    out_of_scope: list[str]
    monthly_budget_rub: int
    has_llm_key: bool
    llm_provider: str | None
    tech_savviness: str  # none | basic | confident
    maintainer: str

    def primary_value_label(self) -> str:
        return {
            "time": "сэкономить время",
            "money": "перестать терять деньги/заявки",
            "sanity": "снять головную боль",
            "customer_experience": "сделать клиентам удобнее",
        }[self.primary_value]


# Pattern → handler. Tested in priority order (first match wins).
def _match_any(text: str, patterns: list[str]) -> bool:
    return any(p in text for p in patterns)


@dataclass
class Persona:
    name: str
    intro_message: str  # what they say first when invited to describe their task
    ground_truth: GroundTruth
    style_short_answers: bool = False
    extra_quirks: list[str] = field(default_factory=list)

    def respond(self, bot_message: str, turn_count: int) -> str:
        """Return the persona's next user message in response to the bot."""
        bm = bot_message.lower()
        gt = self.ground_truth

        # --- Turn 1: always emit the intro ---
        if turn_count <= 1:
            return self.intro_message

        # --- Confirmation ("так?", "верно?") ---
        if _match_any(bm, ["так?", "верно?", "правильно?", "это так", "сверим картину"]):
            return "да, всё верно"

        # --- Final spec dump ---
        if "implementation spec" in bm or "что мы делаем — простыми словами" in bm:
            return "отлично, спасибо!"

        # --- Mom-Test probes (story-shaped past behaviour) ---
        if _match_any(bm, [
            "когда последний раз", "расскажите тот случай", "что вы пробовали",
            "как вы это сейчас решаете", "кто ещё это видит", "был последний случай",
        ]):
            return self._story_answer()

        # --- Desired outcome ("идеальный день", "одной фразой") ---
        if _match_any(bm, [
            "идеальный", "как должен выглядеть", "одной фразой", "как должно стать",
            "что должно стать иначе", "что должно измениться", "после внедрения",
        ]):
            return gt.desired_outcome

        # --- Primary value (4-options menu) ---
        if _match_any(bm, [
            "что для вас сейчас главное", "сэкономить время", "что важнее",
            "что главное", "что для вас главное",
        ]) and "—" in bot_message:
            return gt.primary_value_label()

        # --- Success metric ---
        if _match_any(bm, [
            "метрик", "как поймём", "как понять что получилось",
            "по чему", "какая цифра", "что должно измениться в цифрах",
        ]):
            return gt.success_metric

        # --- Out of scope ---
        if _match_any(bm, [
            "не делаем", "точно не нужно", "что не надо", "чего не",
            "что мы исключ", "точно не делать",
        ]):
            return "точно не: " + "; ".join(gt.out_of_scope)

        # --- LLM key / AI provider ---
        # ORDERED BEFORE budget: when bot mentions ChatGPT/Claude/GigaChat, that's an
        # LLM-key question (not money). Without this ordering, the budget pattern's
        # "подписк" / "₽" tokens would false-match on subscription-presence questions
        # and cause the persona to loop on the budget reply.
        if _match_any(bm, [
            "api-ключ", "api ключ", "openai", "chatgpt", "claude", "yandex",
            "gigachat", "ии-модел", "ии модел", "llm", "искусственн",
        ]):
            if gt.has_llm_key:
                return f"да, есть ключ к {gt.llm_provider}."
            return "нет, ключа нет — расскажите как получить?"

        # --- Budget ---
        if _match_any(bm, [
            "бюджет", "подписк", "сколько готов", "₽", "руб", "плати", "цене",
            "до 1000", "до 5000",
        ]):
            return f"готов платить до {gt.monthly_budget_rub} ₽ в месяц."

        # --- Maintainer ---
        if _match_any(bm, [
            "поддержив", "после запуска", "кто будет следить", "кто будет поддерж",
        ]):
            return f"поддерживать буду {gt.maintainer}."

        # --- Tech savviness (3-options menu) ---
        if _match_any(bm, [
            "технически", "разбираетесь", "сами настр", "опыт",
            "не настраивал", "уверенный пользователь",
        ]):
            return {
                "none": "совсем не настраивал ничего сам",
                "basic": "немного, копировал коды",
                "confident": "уверенный пользователь",
            }[gt.tech_savviness]

        # --- Daily volume ---
        if _match_any(bm, [
            "сколько в день", "сколько в неделю", "сколько заявок",
            "сколько клиентов", "объём", "обращений", "сколько обращ",
        ]):
            return "штук 30-50 обращений в день."

        # --- Tools (where you keep customers) ---
        if _match_any(bm, [
            "где вы сейчас ведёте", "куда записыв", "где храните клиент",
            "ведёте клиентов", "amocrm", "crm", "блокнот", "инструмент",
        ]):
            tools = ", ".join(gt.tools_in_use) if gt.tools_in_use else "нигде, в голове"
            return f"веду в {tools}."

        # --- Channels (NB: must be after Tools to avoid 'amocrm' collision) ---
        if _match_any(bm, [
            "клиенты с вами как связыв", "через что", "куда пишут", "откуда приход",
            "связыв", "канал", "клиенты пишут",
        ]):
            return "пишут в " + ", ".join(gt.channels) + "."

        # --- Pain points ---
        if _match_any(bm, [
            "болит", "съедает", "отнимает", "ломается", "теряется",
            "узкое место", "что бесит", "что отнимает",
        ]):
            return "; ".join(gt.pain_points)

        # --- Business type / stage ---
        if _match_any(bm, [
            "чем занимается ваш бизнес", "что у вас за бизнес",
            "чем вы занимаетесь", "расскажите кратко",
        ]):
            return self.intro_message

        # --- Initial open question / catch-all ---
        if _match_any(bm, [
            "расскажите", "что у вас за", "с чего начать", "опишите", "что хотите",
            "какой момент", "натолкнул",
        ]):
            return self.intro_message

        # --- Generic fallback for unknown questions: just acknowledge with intro ---
        # If we already sent intro recently, send a shorter clarifier.
        return "уточните, пожалуйста, что именно хотите узнать?"

    def _story_answer(self) -> str:
        gt = self.ground_truth
        pains = "; ".join(gt.pain_points[:2])
        return (
            f"вчера типичный случай: {pains}. "
            f"уже пробовал {('просить кого-то напоминать' if gt.primary_value == 'sanity' else 'делать вручную')}, "
            "не помогает."
        )


PERSONAS: list[Persona] = [
    Persona(
        name="Анна — студия маникюра",
        intro_message=(
            "у меня небольшая студия маникюра в спальном районе. в команде я и две мастера. "
            "клиенты пишут в директ инстаграма и в whatsapp, я сама записываю их в excel-табличку. "
            "теряю заявки, когда занята — вижу сообщения через 3-4 часа, к этому моменту половина "
            "уже ушла к конкурентам. хочу с этим разобраться."
        ),
        ground_truth=GroundTruth(
            business_type="студия маникюра",
            channels=["instagram direct", "whatsapp"],
            tools_in_use=["excel", "блокнот"],
            pain_points=[
                "теряю заявки из директа когда занята с клиентом",
                "путаю записи между Excel и WhatsApp",
                "забываю напоминать клиенткам за день",
            ],
            primary_value="money",
            desired_outcome=(
                "новые сообщения из директа и WhatsApp в течение минуты падают в одну таблицу, "
                "а мне в Telegram приходит уведомление с готовым ответом-шаблоном"
            ),
            success_metric="не терять больше 1 заявки в неделю (сейчас теряем ~10)",
            out_of_scope=["новый сайт", "своя CRM", "приложение"],
            monthly_budget_rub=3000,
            has_llm_key=False,
            llm_provider=None,
            tech_savviness="basic",
            maintainer="сама",
        ),
    ),
    Persona(
        name="Дмитрий — онлайн-курсы",
        intro_message=(
            "продаю онлайн-курсы по дизайну, есть лендинг на тильде, оплата через юкассу, после оплаты "
            "клиента вручную добавляем в закрытый телеграм-чат. в команде 4 человека. на пиковых неделях "
            "300-400 продаж — мы тонем, добавляем по 2 часа в день только на это."
        ),
        ground_truth=GroundTruth(
            business_type="онлайн-курсы по дизайну",
            channels=["лендинг tilda", "telegram-чат"],
            tools_in_use=["tilda", "юkassa", "telegram", "google sheets"],
            pain_points=[
                "вручную добавляем оплативших в закрытый telegram",
                "по 2 часа в день уходит на ручную модерацию",
            ],
            primary_value="time",
            desired_outcome=(
                "после оплаты в юkassa клиент автоматически получает приглашение в "
                "telegram-чат и письмо с инструкцией"
            ),
            success_metric="0 минут ручной работы по добавлению в чат, 100% инвайтов в течение 2 минут",
            out_of_scope=["переход с tilda на свой сайт", "мобильное приложение"],
            monthly_budget_rub=8000,
            has_llm_key=True,
            llm_provider="OpenAI",
            tech_savviness="confident",
            maintainer="наш техлид",
        ),
    ),
    Persona(
        name="Игорь — сантехник",
        intro_message=(
            "я частный мастер-сантехник. работаю один. заявки идут с авито и через сарафан, на телефон. "
            "забываю кому когда обещал перезвонить, и к концу недели пара клиентов уходят к другому мастеру. "
            "хочу что-то простое, чтобы не вылетать из головы."
        ),
        ground_truth=GroundTruth(
            business_type="частный сантехник",
            channels=["авито", "сарафан", "телефон"],
            tools_in_use=["заметки в телефоне"],
            pain_points=[
                "забываю кому обещал перезвонить",
                "в неделю 1-2 клиента уходят из-за этого",
            ],
            primary_value="sanity",
            desired_outcome=(
                "после звонка / сообщения в авито записываю в одно место и получаю напоминание "
                "за час до обещанного времени"
            ),
            success_metric="0 пропущенных перезвонов в неделю",
            out_of_scope=["сайт", "CRM с воронками", "сложные интеграции"],
            monthly_budget_rub=500,
            has_llm_key=False,
            llm_provider=None,
            tech_savviness="none",
            maintainer="сам",
        ),
        style_short_answers=True,
    ),
]
