"""Hard guardrails on bot output.

Why this layer exists: prose rules in system prompts are soft. This module
enforces deterministic checks on what V2/V3 emit to the user, complementing
(not replacing) the prompt rules.

Two tools:
- `check(text)` — returns a list of rule violations (for tests / logging)
- `sanitize(text)` — best-effort fix-up for the most common drift patterns

Both accept `lang: str = "ru"` (default) so callers pass the session language
and get language-appropriate enforcement. TG bot never passes `lang` and
therefore always uses "ru" — byte-identical to the old behaviour.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Banned terms — hard filter on bot output (not on user input).
# EN list: "developer" is NOT banned — it reads neutral in EN business copy.
# ---------------------------------------------------------------------------
BANNED_TERMS: dict[str, list[str]] = {
    "ru": [
        r"\bLLM\b",
        r"\bAPI\b",
        r"\bwebhook\b",
        r"\bвебхук\w*",
        r"\bendpoint\b",
        r"\bJTBD\b",
        r"\bMVP\b",
        r"\bSDK\b",
        r"\bORM\b",
        r"\bспек[ауеи]\b",
        r"\bвиджет\w*",
        r"\bхостинг\w*",
        r"\bпрограммн\S+\s+доступ",
        r"\bваше\s+дело\b",  # rude / shifts blame
        # Разработчик в репликах владельцу не упоминается — он не участвует в
        # диалоге. Если нужно сослаться на исполнителя, говорим нейтрально.
        r"\bразработчик\w*",
    ],
    "en": [
        r"\bAPI\b",
        r"\bwebhook\b",
        r"\bendpoint\b",
        r"\bLLM\b",
        r"\bMVP\b",
        r"\bSDK\b",
        r"\bORM\b",
        r"\bJTBD\b",
        r"\bbackend\b",
        r"\bmiddleware\b",
        r"\bcron\s+job\b",
    ],
}

# ---------------------------------------------------------------------------
# Tech-commentary patterns — phrases that belong in the spec, not in the chat.
# EN: "this won't fit the budget" / "we'll need a lighter/smaller model".
# ---------------------------------------------------------------------------
TECH_COMMENTARY_PATTERNS: dict[str, list[str]] = {
    "ru": [
        r"не\s+пройдёт\s+по\s+бюджет",
        r"нужн\S+\s+(?:более\s+)?лёгк\S+\s+модел",
        r"агрессивн\S+\s+к[еэ]шировани",
        r"в\s+этот\s+бюджет\s+пройдёт",
    ],
    "en": [
        r"won'?t\s+fit\s+(?:the\s+)?budget",
        r"we'?ll?\s+need\s+a\s+(?:lighter|smaller)\s+model",
        r"aggressive\s+cach(?:ing|e)",
        r"fits?\s+(?:in(?:to)?\s+)?(?:the\s+)?budget",
    ],
}

# ---------------------------------------------------------------------------
# Vague value-prop patterns — replace with concrete business outcome.
# ---------------------------------------------------------------------------
VAGUE_VALUE_PROP_PATTERNS: dict[str, list[str]] = {
    "ru": [
        r"помогу\s+понять,?\s+что\s+вам\s+стоит",
        r"помогу\s+разобраться,?\s+что\s+вам\s+нужно",
        r"вместе\s+поймём,?\s+что\s+делать",
    ],
    "en": [
        r"help\s+you\s+figure\s+out\s+what\s+you\s+need",
        r"help\s+you\s+understand\s+what\s+(?:you\s+)?(?:should|need\s+to)\s+do",
        r"(?:together\s+)?(?:we'?ll?\s+)?figure\s+out\s+what\s+to\s+do",
    ],
}

# ---------------------------------------------------------------------------
# Leaked dev-split patterns — "отдельно для вас и отдельно для разработчика".
# EN: no equivalent leak risk — "developer" is neutral in EN copy. Empty list.
# ---------------------------------------------------------------------------
LEAKED_DEV_SPLIT_PATTERNS: dict[str, list[str]] = {
    "ru": [
        r"отдельно\s+для\s+(?:вас|бизнеса|владельца)[^.]{1,60}разработчик\w*",
        r"для\s+(?:вас|бизнеса|владельца)\s+и\s+для\s+разработчик\w*",
        r"(?:две|2)\s+(?:версии|плана|спек\w*)[^.]{1,60}разработчик\w*",
    ],
    # EN: "developer" is neutral — there is no analogous RU-to-EN phrasing risk.
    "en": [],
}

# В dev-спеке каждое решение должно быть зафиксировано — никаких «На выбор»,
# «Вариант A / Вариант B», «Рекомендация:». Если такие блоки появляются —
# upstream DEV_COVERAGE phase не закрыл gap; ловим как safety net.
VARIANT_BLOCK_PATTERNS: dict[str, list[re.Pattern]] = {
    "ru": [
        re.compile(r"\*\*На выбор:\*\*"),
        re.compile(r"^[\-\*\s]*\*?\*?Вариант\s+[АABCГ][\s\:.\-—]", re.MULTILINE),
        re.compile(r"^\s*\*?\*?Рекомендация[\s\:]", re.MULTILINE),
    ],
    "en": [
        re.compile(r"\*\*Options?:\*\*", re.IGNORECASE),
        re.compile(r"^[\-\*\s]*\*?\*?Option\s+[ABC][\s\:.\-—]", re.MULTILINE | re.IGNORECASE),
        re.compile(r"^[\-\*\s]*\*?\*?Variant\s+[ABC][\s\:.\-—]", re.MULTILINE | re.IGNORECASE),
        re.compile(r"^\s*\*?\*?Recommendation[\s\:]", re.MULTILINE | re.IGNORECASE),
        re.compile(r"^\s*\*?\*?Choose\s+(?:one|between)[\s\:]", re.MULTILINE | re.IGNORECASE),
    ],
}


@dataclass
class RuleViolation:
    rule: str
    match: str
    severity: str  # "error" | "warn"


def _strip_option_questions(text: str) -> str:
    """Strip enumerated option lines (those starting with '—' or '-') so that the
    remaining text contains only narrative; helps count *real* questions vs
    multiple-choice option phrasings.
    """
    cleaned = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("—", "–", "-", "•")):
            continue
        cleaned.append(line)
    return "\n".join(cleaned)


def count_questions(text: str) -> int:
    """Count question marks in the narrative portion (excluding option lists)."""
    narrative = _strip_option_questions(text)
    # Avoid counting URL / numeric tokens.
    return narrative.count("?")


def check(text: str, lang: str = "ru") -> list[RuleViolation]:
    violations: list[RuleViolation] = []
    banned = BANNED_TERMS.get(lang, BANNED_TERMS["ru"])
    tech_commentary = TECH_COMMENTARY_PATTERNS.get(lang, TECH_COMMENTARY_PATTERNS["ru"])
    vague_value_prop = VAGUE_VALUE_PROP_PATTERNS.get(lang, VAGUE_VALUE_PROP_PATTERNS["ru"])
    leaked_dev_split = LEAKED_DEV_SPLIT_PATTERNS.get(lang, LEAKED_DEV_SPLIT_PATTERNS["ru"])

    for pat in banned:
        for m in re.finditer(pat, text, flags=re.IGNORECASE):
            violations.append(RuleViolation(rule="banned_term", match=m.group(0), severity="error"))
    for pat in tech_commentary:
        for m in re.finditer(pat, text, flags=re.IGNORECASE):
            violations.append(
                RuleViolation(rule="tech_commentary", match=m.group(0), severity="warn")
            )
    for pat in vague_value_prop:
        for m in re.finditer(pat, text, flags=re.IGNORECASE):
            violations.append(
                RuleViolation(rule="vague_value_prop", match=m.group(0), severity="warn")
            )
    for pat in leaked_dev_split:
        for m in re.finditer(pat, text, flags=re.IGNORECASE):
            violations.append(
                RuleViolation(rule="leaked_dev_split", match=m.group(0), severity="error")
            )
    qcount = count_questions(text)
    if qcount > 1:
        violations.append(
            RuleViolation(rule="multiple_questions", match=f"{qcount} questions", severity="warn")
        )
    return violations


def check_variant_blocks(text: str, lang: str = "ru") -> list[str]:
    """Detect forbidden "variant block" patterns in dev spec output.

    Owner requirement: dev specs must lock every decision — no «На выбор:»,
    no «Вариант A / B / C», no «Рекомендация:». If any such block leaks
    through, this returns one violation per match (independent reports for
    «Вариант A» and «Вариант B» both appearing).

    Returns a list of human-readable violation strings, each describing
    which pattern matched plus a snippet (~80 chars) of the matched line.
    Empty list when clean.
    """
    violations: list[str] = []
    patterns = VARIANT_BLOCK_PATTERNS.get(lang, VARIANT_BLOCK_PATTERNS["ru"])
    for pat in patterns:
        for m in pat.finditer(text):
            # Take the full line containing the match for a useful snippet.
            line_start = text.rfind("\n", 0, m.start()) + 1
            line_end = text.find("\n", m.end())
            if line_end == -1:
                line_end = len(text)
            snippet = text[line_start:line_end].strip()
            if len(snippet) > 80:
                snippet = snippet[:77] + "..."
            violations.append(
                f"variant_block (pattern {pat.pattern!r}): {snippet!r}"
            )
    return violations


# ---------------------------------------------------------------------------
# Soft replacements — rewrite jargon to plain language without deleting content.
# EN: "developer" is NOT replaced — it reads neutral in EN business copy.
# ---------------------------------------------------------------------------
SOFT_REPLACEMENTS: dict[str, list[tuple[str, str]]] = {
    "ru": [
        (r"\bAPI[- ]ключ\w*", "доступ к ИИ"),  # «API-ключ» — техжаргон; «доступ к ИИ» нейтральнее
        (r"\bLLM\b", "ИИ"),
        (r"\bпрограммн\S+\s+доступ\w*", "доступ для бота"),
        (r"\bAPI\b", "доступ для бота"),
        (r"\bвиджет\w*", "чат на сайте"),
        (r"\bхостинг\w*", "сервер для бота"),
        (r"\bвебхук\w*", "связку"),
        (r"\bwebhook\w*", "связку"),
        (r"\bваше\s+дело", "от вас нужно"),
        # "спеку/спеки/спеке/спека" → "план/плана/плане/план"
        (r"\bспеку\b", "план"),
        (r"\bспек[иа]\b", "плана"),
        (r"\bспеке\b", "плане"),
        (r"\bтех-спеку\b", "технический план"),
        (r"\bтех-спека\b", "технический план"),
        # Разработчик → исполнитель. Падежи разные, поэтому таблицей.
        (r"\bразработчиками\b", "исполнителями"),
        (r"\bразработчиков\b", "исполнителей"),
        (r"\bразработчикам\b", "исполнителям"),
        (r"\bразработчиках\b", "исполнителях"),
        (r"\bразработчиком\b", "исполнителем"),
        (r"\bразработчики\b", "исполнители"),
        (r"\bразработчику\b", "исполнителю"),
        (r"\bразработчика\b", "исполнителя"),
        (r"\bразработчике\b", "исполнителе"),
        (r"\bразработчик\b", "исполнитель"),
        (r"\bразработческ\w*", "технический"),
        # Single English words that leak through canonicalize_list/extractor into
        # Russian fields (observed: «client» в строке «каналы клиентов»).
        (r"\bclients\b", "клиенты"),
        (r"\bclient\b", "клиент"),
    ],
    "en": [
        # API key before bare API so the longer match wins.
        (r"\bAPI\s+key\b", "access for the bot"),
        (r"\bAPI\b", "access for the bot"),
        (r"\bwebhook\b", "link"),
        (r"\bwidget\b", "website chat"),
        (r"\bhosting\b", "server for the bot"),
        (r"\bspecs?\b", "plan"),
        # "developer" is intentionally NOT replaced in EN — reads neutral.
    ],
}


# English tokens that may appear in a Russian summary as product or service names.
# Values are stored lowercase so matching is case-insensitive.
_SUMMARY_EN_ALLOWLIST: set[str] = {
    canonical.lower()
    for canonical in {
        "ChatGPT", "Claude", "Gemini", "GigaChat", "YandexGPT", "OpenAI", "Anthropic",
        "Telegram", "WhatsApp", "Instagram", "Tilda", "Notion", "OZON", "Wildberries",
        "Make.com", "Zapier", "n8n", "AmoCRM", "Excel", "Word", "Zoom",
        "Google", "Sheets", "Calendar", "Drive",
        "FR", "FAQ", "ru", "en",
    }
} | {"chatgpt", "claude", "gemini", "ии"}


def find_english_singletons(text: str, lang: str = "ru") -> list[str]:
    """Return EN tokens (≥3 chars) in `text` that are not in the allowlist.
    Warn-only — caller decides whether to log/replace. Used to catch new leaks
    («client», «request», «lead») before they hit the owner."""
    if not text:
        return []
    out: list[str] = []
    for m in re.finditer(r"\b[A-Za-z][A-Za-z\.]{2,}\b", text):
        token = m.group(0)
        if token.lower().rstrip(".") in _SUMMARY_EN_ALLOWLIST:
            continue
        out.append(token)
    return out


def sanitize(text: str, lang: str = "ru") -> str:
    """Apply soft replacements. Does not delete content, just rewrites jargon."""
    out = text
    replacements = SOFT_REPLACEMENTS.get(lang, SOFT_REPLACEMENTS["ru"])
    for pat, repl in replacements:
        out = re.sub(pat, repl, out, flags=re.IGNORECASE)
    return out


# Только подмена "разработчик" → "исполнитель" — для тех-плана, где «API»/«webhook»
# нужно оставить, но имя «разработчик» в финальной склейке для владельца — нельзя.
_DEV_WORD_REPLACEMENTS: list[tuple[str, str]] = [
    (r"\bразработчиками\b", "исполнителями"),
    (r"\bразработчиков\b", "исполнителей"),
    (r"\bразработчикам\b", "исполнителям"),
    (r"\bразработчиках\b", "исполнителях"),
    (r"\bразработчиком\b", "исполнителем"),
    (r"\bразработчики\b", "исполнители"),
    (r"\bразработчику\b", "исполнителю"),
    (r"\bразработчика\b", "исполнителя"),
    (r"\bразработчике\b", "исполнителе"),
    (r"\bразработчик\b", "исполнитель"),
    (r"\bразработческ\w*", "технический"),
]


def strip_dev_mentions(text: str, lang: str = "ru") -> str:
    """Снимает упоминания 'разработчик/-ого/-у/...' заменой на 'исполнитель'.
    Оставляет технические термины (API, webhook) как есть — годится для
    тех-плана, который пойдёт через финальную реплику владельцу."""
    out = text
    for pat, repl in _DEV_WORD_REPLACEMENTS:
        out = re.sub(pat, repl, out, flags=re.IGNORECASE)
    return out


# ---------------------------------------------------------------------------
# Canonical names — collapse ru/en transliteration variants into ONE form
# ---------------------------------------------------------------------------
# Case-insensitive deduplication alone cannot collapse Russian and English
# transliterations of the same brand. Normalize common variants via this table.

CANONICAL_NAMES: dict[str, str] = {
    # Notion
    "notion": "Notion", "нотион": "Notion",
    # Telegram
    "telegram": "Telegram", "телеграм": "Telegram", "телега": "Telegram",
    "telegram-бот": "Telegram", "telegram bot": "Telegram",
    # Tilda
    "tilda": "Tilda", "тильда": "Tilda",
    # ЮKassa
    "юкасса": "ЮKassa", "ukassa": "ЮKassa", "yukassa": "ЮKassa",
    "юkassa": "ЮKassa", "yu kassa": "ЮKassa",
    # Тинькофф
    "тинькофф": "Тинькофф", "tinkoff": "Тинькофф", "t-bank": "Тинькофф",
    "тинькофф сбп": "Тинькофф СБП",
    # Google
    "google sheets": "Google Sheets", "гугл таблицы": "Google Sheets",
    "google calendar": "Google Calendar", "гугл календарь": "Google Calendar",
    "google drive": "Google Drive", "гугл диск": "Google Drive",
    # WhatsApp
    "whatsapp": "WhatsApp", "вотсап": "WhatsApp", "вац": "WhatsApp",
    # Instagram
    "instagram": "Instagram", "инстаграм": "Instagram",
    "instagram direct": "Instagram Direct", "директ": "Instagram Direct",
    # Marketplaces
    "ozon": "OZON", "озон": "OZON",
    "wildberries": "Wildberries", "вб": "Wildberries", "wb": "Wildberries",
    # Other tools
    "make.com": "Make.com", "make": "Make.com",
    "zapier": "Zapier",
    "n8n": "n8n",
    "1с": "1С", "1c": "1С",
    "amocrm": "AmoCRM", "амоcrm": "AmoCRM",
    "битрикс24": "Битрикс24", "bitrix24": "Битрикс24",
    "excel": "Excel", "эксель": "Excel",
    "word": "Word", "ворд": "Word",
    "zoom": "Zoom", "зум": "Zoom",
    # Preserve canonical capitalization for model and service names.
    "chatgpt": "ChatGPT", "чатгпт": "ChatGPT",
    "claude": "Claude", "клод": "Claude",
    "gemini": "Gemini", "гемини": "Gemini",
    "yandexgpt": "YandexGPT", "yandex gpt": "YandexGPT", "яндексгпт": "YandexGPT",
    "gigachat": "GigaChat", "гигачат": "GigaChat",
    "openai": "OpenAI",
    "anthropic": "Anthropic",
}


def canonicalize(name: str, lang: str = "ru") -> str:
    """Normalise a brand/tool name. Returns canonical form if recognised, else
    original (stripped). Case-insensitive lookup; punctuation around the name
    is preserved by the caller."""
    if not isinstance(name, str):
        return name
    key = name.strip().lower()
    if key in CANONICAL_NAMES:
        return CANONICAL_NAMES[key]
    # Try without trailing punctuation
    stripped_punct = key.rstrip(".,;:!?")
    if stripped_punct != key and stripped_punct in CANONICAL_NAMES:
        return CANONICAL_NAMES[stripped_punct] + key[len(stripped_punct):]
    return name.strip()


def canonicalize_list(items: list[str], lang: str = "ru") -> list[str]:
    """Canonicalize each item, dedupe by canonical form, preserve insertion order."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if not item:
            continue
        canon = canonicalize(item, lang=lang)
        # Skip whitespace-only strings that strip down to nothing.
        if not canon or not canon.strip():
            continue
        key = canon.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(canon)
    return out


# ---------------------------------------------------------------------------
# English-phrase detector (used by localize_foreign_fragments)
# ---------------------------------------------------------------------------

# 3+ consecutive English words (≥3 chars each) → likely an EN phrase, not a
# brand name.
_ENGLISH_PHRASE_RE = re.compile(r"[A-Za-z]{3,}(?:\s+[A-Za-z]{3,}){2,}")

# 3+ consecutive Cyrillic words (≥3 chars each) → likely a RU phrase in EN bot output.
_CYRILLIC_PHRASE_RE = re.compile(r"[А-ЯЁа-яё]{3,}(?:\s+[А-ЯЁа-яё]{3,}){2,}")


def has_english_phrase(text: str) -> bool:
    """True if `text` contains a phrase of 3+ English words (≥3 chars each)."""
    if not text:
        return False
    return bool(_ENGLISH_PHRASE_RE.search(text))


def has_cyrillic_phrase(text: str) -> bool:
    """True if `text` contains a phrase of 3+ Cyrillic words (≥3 chars each)."""
    if not text:
        return False
    return bool(_CYRILLIC_PHRASE_RE.search(text))


def localize_foreign_fragments(text: str, lang: str, llm_complete) -> str:
    """Translate foreign-alphabet fragments inline, preserving native text and brand names.

    - lang == "ru": detect EN phrases (≥3 words) → call LOCALIZE_FRAGMENT_PROMPT (RU).
    - lang == "en": detect Cyrillic phrases (≥3 words) → call LOCALIZE_FRAGMENT_PROMPT_EN.
      If LOCALIZE_FRAGMENT_PROMPT_EN is not yet available (agent A4 hasn't landed),
      returns text unchanged and logs a warning — does not crash.

    `llm_complete` is the bot's existing LLM client. Falls back to identity on
    any exception (graceful degradation).
    """
    if lang == "ru":
        if not has_english_phrase(text):
            return text
        from .common_prompts import LOCALIZE_FRAGMENT_PROMPT
        prompt = LOCALIZE_FRAGMENT_PROMPT
    else:  # "en"
        if not has_cyrillic_phrase(text):
            return text
        try:
            from .common_prompts import LOCALIZE_FRAGMENT_PROMPT_EN
            prompt = LOCALIZE_FRAGMENT_PROMPT_EN
        except ImportError:
            logger.warning(
                "localize_foreign_fragments: LOCALIZE_FRAGMENT_PROMPT_EN not yet available "
                "(agent A4 has not landed); returning text unchanged for lang='en'."
            )
            return text

    try:
        resp = llm_complete(
            system=prompt,
            messages=[{"role": "user", "content": text}],
            max_tokens=4000,
            temperature=0.0,
        )
        result = (resp.text or "").strip()
        # Sanity check: result should be non-empty and the foreign fragment should
        # be gone after translation.
        if lang == "ru":
            translated_ok = result and not has_english_phrase(result)
        else:
            translated_ok = result and not has_cyrillic_phrase(result)
        if translated_ok:
            return result
        return text
    except Exception:
        return text


def localize_english_fragments(text: str, llm_complete) -> str:
    """Deprecated: use localize_foreign_fragments(text, lang='ru', llm_complete=llm_complete)."""
    return localize_foreign_fragments(text, lang="ru", llm_complete=llm_complete)


# ---------------------------------------------------------------------------
# Session-memory validator — refuse bot replies that contradict explicit
# retractions or preferences.
# ---------------------------------------------------------------------------

# RU patterns: «без ИИ», «не хочу ИИ», «никакого ИИ», etc.
# EN patterns: "no AI", "without AI", "don't want AI", "skip AI", "not interested in AI".
_NO_AI_PATTERNS: dict[str, list[re.Pattern]] = {
    "ru": [
        re.compile(
            r"(?<![а-яё])(?:никак\w+|без|не\s+нужн\w*|не\s+хочу|не\s+надо|без\s+всяк\w+)\s+"
            r"(?:ии|искусственн\S+\s+интеллект\w*|нейросет\w*|llm|чат[\-\s]?бот\w*\s+(?:с\s+)?ии)\b",
            re.IGNORECASE,
        ),
        re.compile(r"(?<![а-яё])(?:никакого|никакой|без|нет)\s+ии\b", re.IGNORECASE),
        re.compile(r"(?<![а-яё])не\s+(?:нужен|нужна|нужно|надо|хочу|стоит)\s+(?:ии|искусственн\S+|нейросет\w*)\b", re.IGNORECASE),
        re.compile(r"\bобойдёмся\s+без\s+(?:ии|искусственн|нейросет|llm)\w*", re.IGNORECASE),
    ],
    "en": [
        re.compile(r"\bno\s+AI\b", re.IGNORECASE),
        re.compile(r"\bwithout\s+AI\b", re.IGNORECASE),
        re.compile(r"\bdon'?t\s+want\s+(?:any\s+)?AI\b", re.IGNORECASE),
        re.compile(r"\bskip\s+AI\b", re.IGNORECASE),
        re.compile(r"\bnot\s+interested\s+in\s+AI\b", re.IGNORECASE),
    ],
}


def _user_rejected_ai(transcript: list[dict], lang: str = "ru") -> bool:
    """True if the user has explicitly rejected AI/LLM in any of their turns.

    Scans only USER messages — bot messages may quote/echo «без ИИ» / "no AI" as
    confirmation and that's not a rejection signal.
    """
    if not transcript:
        return False
    patterns = _NO_AI_PATTERNS.get(lang, _NO_AI_PATTERNS["ru"])
    for entry in transcript:
        if entry.get("role") != "user":
            continue
        content = entry.get("content") or ""
        if not isinstance(content, str):
            continue
        for pat in patterns:
            if pat.search(content):
                return True
    return False


def apply_no_ai_if_rejected(state, transcript: list[dict], lang: str = "ru") -> bool:
    """If the user rejected AI in the transcript, force-clear AI-related
    state so the summary template doesn't render the «Подписка на ИИ» row.

    Mutates state in place. Returns True if applied, False otherwise.

    Called before SUMMARY_TEMPLATE.render in the workflow. Belt-and-suspenders
    on top of the LLM-detector (`_detect_llm_requirement`) which can miss
    repeated owner refusals.
    """
    if not _user_rejected_ai(transcript, lang=lang):
        return False
    r = state.resources
    r.llm_required = False
    r.has_llm_key = None
    r.llm_provider = None
    r.llm_subscription_quote = None
    # Also leave a session_memory preference so downstream prompts and
    # check_against_memory honour it on subsequent turns.
    sm = getattr(state.conversation, "session_memory", None)
    if sm is not None:
        already = any(
            (p.get("topic") or "").lower() == "no_ai"
            for p in (sm.preferences or [])
        )
        if not already:
            sm.preferences.append({
                "topic": "no_ai",
                "value": "rejected",
                "turn": getattr(state.conversation, "turn_count", 0),
                "user_quote": "(detected by postprocess scanner)",
            })
    return True


# ---------------------------------------------------------------------------
# Budget recovery — scan user history for a budget figure when the extractor
# missed it. Stores in state.resources.monthly_budget_rub (currency-agnostic
# storage; render layer supplies ₽ vs $ symbol).
# ---------------------------------------------------------------------------

# RU context words: "бюджет / месяц / тратить / подписк / ..."
# EN context words: "budget / monthly / per month / spend / subscription / ..."
_BUDGET_CONTEXT_RE: dict[str, re.Pattern] = {
    "ru": re.compile(
        r"(?:бюджет\w*|в\s+месяц|ежемесяч\w*|трат\w+|подписк\w*|на\s+инструмент\w*|"
        r"готов[а]?\s+(?:платить|тратить)|порядк\w+|до\s+\d|примерно)",
        re.IGNORECASE,
    ),
    "en": re.compile(
        r"(?:budget|monthly|per\s+month|spend|subscription|tools?\b|"
        r"willing\s+to\s+pay|up\s+to|around|about)",
        re.IGNORECASE,
    ),
}

# RU number token: «5000», «5 000», «5к», «5 тыс», «5 тысяч»
# EN number token: «$300», «300», «5k», «5K», «5 thousand», «USD 300», «300 USD»
# EN regex requires a currency/multiplier signal OR context guard (applied in caller).
_BUDGET_NUMBER_RE: dict[str, re.Pattern] = {
    "ru": re.compile(
        r"\b(\d{1,3}(?:\s\d{3})+|\d{3,6}|\d{1,2})"
        r"\s*(к|тыс\.?[а-я]*)?",
        re.IGNORECASE,
    ),
    "en": re.compile(
        r"\$\s*(\d+(?:[,.]\d+)?)\s*(k|K|thousand|USD)?"
        r"|(\d+(?:[,.]\d+)?)\s*(k|K|thousand|USD)\b"
        r"|USD\s+(\d+(?:[,.]\d+)?)",
        re.IGNORECASE,
    ),
}


def _parse_budget_amount_ru(num_str: str, suffix: str | None) -> int | None:
    cleaned = re.sub(r"[\s ]", "", num_str)
    try:
        n = int(cleaned)
    except ValueError:
        return None
    if suffix:
        s = suffix.lower().rstrip(".")
        if s.startswith("к") or s.startswith("тыс"):
            n = n * 1000
    if n < 500 or n > 10_000_000:
        return None  # Implausible — skip.
    return n


# Keep old name as alias for backward compat.
_parse_budget_amount = _parse_budget_amount_ru


def _parse_budget_amount_en(m: re.Match) -> int | None:
    """Extract and normalise the numeric value from an EN budget regex match.

    Groups layout for _BUDGET_NUMBER_RE["en"]:
      Alt 1 ($NNN[k|thousand|USD]): group(1)=number, group(2)=suffix
      Alt 2 (NNN k|thousand|USD):  group(3)=number, group(4)=suffix
      Alt 3 (USD NNN):             group(5)=number
    """
    num_str: str | None = m.group(1) or m.group(3) or m.group(5)
    suffix: str | None = m.group(2) or m.group(4)
    if num_str is None:
        return None
    cleaned = re.sub(r"[,\s]", "", num_str)
    try:
        n = float(cleaned)
    except ValueError:
        return None
    if suffix and suffix.lower() in ("k", "thousand"):
        n = n * 1000
    n = int(round(n))
    # EN budget plausibility: $10 – $10,000,000 (in USD, ~600 RUB/USD).
    if n < 10 or n > 10_000_000:
        return None
    return n


def recover_budget_from_history(state, transcript: list[dict], lang: str = "ru") -> bool:
    """If state.resources.monthly_budget_rub is None, scan user messages for
    a budget figure near a budget-context word and fill the field.

    Mutates state in place. Returns True if applied. Doesn't overwrite an
    existing value — extractor wins when it landed something.

    Stored value is always the raw number (USD or RUB) — render layer adds symbol.
    """
    r = getattr(state, "resources", None)
    if r is None or r.monthly_budget_rub is not None:
        return False
    if not transcript:
        return False

    context_re = _BUDGET_CONTEXT_RE.get(lang, _BUDGET_CONTEXT_RE["ru"])
    number_re = _BUDGET_NUMBER_RE.get(lang, _BUDGET_NUMBER_RE["ru"])

    for entry in transcript:
        if entry.get("role") != "user":
            continue
        content = entry.get("content") or ""
        if not isinstance(content, str) or not content.strip():
            continue

        has_context = bool(context_re.search(content))
        if not has_context:
            # Cheap-and-cheerful: also accept bare numeric answers if the
            # message is short (≤ 30 chars). Owner often replies just «5000» or «$300».
            if len(content.strip()) > 30:
                continue

        for m in number_re.finditer(content):
            if lang == "en":
                amount = _parse_budget_amount_en(m)
            else:
                amount = _parse_budget_amount_ru(m.group(1), m.group(2))
            if amount is None:
                continue
            r.monthly_budget_rub = amount
            return True
    return False


# ---------------------------------------------------------------------------
# Channels validator — drop entries from state.point_a.channels that the
# user never literally mentioned. Defends against extractor «hallucinating»
# example channels listed in the asker prompt.
# ---------------------------------------------------------------------------


def filter_channels_to_user_history(state, transcript: list[dict], lang: str = "ru") -> bool:
    """Keep only channels the owner actually mentioned in their messages.
    Mutation: replaces state.point_a.channels in-place.
    Returns True if anything was dropped.
    """
    a = getattr(state, "point_a", None)
    if a is None or not a.channels:
        return False
    if not transcript:
        return False

    user_blob_lower = " ".join(
        (e.get("content") or "")
        for e in transcript
        if e.get("role") == "user" and isinstance(e.get("content"), str)
    ).lower()
    if not user_blob_lower.strip():
        return False

    kept: list[str] = []
    dropped = False
    for ch in a.channels:
        if not ch:
            continue
        canon = canonicalize(ch, lang=lang)
        # Build the set of forms to check: the canonical, the original, and
        # any aliases that map to the same canonical name.
        canon_lower = canon.lower()
        forms = {canon_lower, ch.strip().lower()}
        for alias, can_name in CANONICAL_NAMES.items():
            if can_name.lower() == canon_lower:
                forms.add(alias.lower())
        if any(f and f in user_blob_lower for f in forms):
            kept.append(ch)
        else:
            dropped = True
    if dropped:
        a.channels = kept
    return dropped


def check_against_memory(text: str, session_memory, lang: str = "ru") -> list[str]:
    """Return a list of violation strings if `text` re-introduces retracted
    entities or contradicts declared preferences. Empty list = clean.

    `session_memory` is the SessionMemory pydantic instance from
    state.conversation.session_memory.
    """
    if session_memory is None or not text:
        return []
    violations: list[str] = []
    text_lower = text.lower()

    for retraction in (getattr(session_memory, "retractions", None) or []):
        value = (retraction.get("value") or "").strip().lower()
        if not value or len(value) < 3:
            continue
        # Look up canonical form too — owner said «Telegram-бот», later forms
        # could be «телеграм» or «Telegram».
        forms = {value}
        canon = canonicalize(value, lang=lang).lower()
        if canon != value:
            forms.add(canon)
        # Also any inverse-canonical aliases
        for alias, canonical in CANONICAL_NAMES.items():
            if canonical.lower() == canon:
                forms.add(alias)
        for form in forms:
            if re.search(rf"\b{re.escape(form)}\b", text_lower):
                violations.append(
                    f"retracted_entity_returned: {retraction.get('value')!r} "
                    f"(retracted at turn {retraction.get('turn', '?')})"
                )
                break

    for pref in (getattr(session_memory, "preferences", None) or []):
        topic = (pref.get("topic") or "").strip().lower()
        if topic == "no_ai":
            # User said «не хочу ИИ» / "no AI" — bot reply should not propose AI.
            # Use simple heuristic: if reply mentions AI-related terms WITHOUT a
            # negation nearby — flag.
            ai_hits = list(re.finditer(
                r"\b(ИИ|ChatGPT|Claude|Gemini|YandexGPT|GigaChat|нейросет\w*|"
                r"искусственн\w+\s+интеллект|artificial\s+intelligence|AI)\b",
                text,
                flags=re.IGNORECASE,
            ))
            negation_re = (
                r"\b(не|без|нет|никак\w*)\b"
                if lang == "ru"
                else r"\b(no|not|without|don'?t|never)\b"
            )
            for m in ai_hits:
                window_start = max(0, m.start() - 30)
                window = text[window_start:m.end() + 30].lower()
                if not re.search(negation_re, window):
                    violations.append(
                        f"contradicts_preference 'no_ai': mentions {m.group(0)!r} "
                        "in positive context after user opted out"
                    )
                    break
    return violations
