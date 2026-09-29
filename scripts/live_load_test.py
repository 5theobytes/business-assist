"""Live load + discovery test driver.

Sends synthetic conversations to an explicitly selected BizWare API:
  POST /api/intake  -> session_id
  POST /api/chat    -> reply, done   (loop until done or max_turns)

Designed to be run *only* against an environment where TURNSTILE_SECRET_KEY is
unset (otherwise /api/intake rejects without a valid Turnstile token). The
script does NOT attempt to bypass captcha — it just omits the token field, which
the server treats as success when captcha is disabled (see app/captcha.py).

Usage:
    python -m scripts.live_load_test --base-url http://127.0.0.1:8000 --confirm-write --case "сантехник"
    python -m scripts.live_load_test --base-url https://your-staging-host --confirm-write --all-cases

Outputs to tmp/live_load_test/<ISO_ts>/:
    <sid>.json          full transcript + per-turn timing
    summary.txt         human-readable run summary
    created_ids.txt     list of created session_ids (for cleanup)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.eval.personas import PERSONAS as STOCK_PERSONAS, GroundTruth, Persona
from scripts.llm_personas import (
    CHARACTER_BRIEFS,
    LLMPersonaSession,
    archetype_for,
    build_business_brief,
)

# Use a reserved example address and fictional name for generated staging records.
NAME = "BizWare synthetic test"
TEST_EMAIL = "bizware-live-test@example.invalid"

SECTORS = ["services", "goods", "online_education", "production", "b2b", "other"]
TIME_EATERS = ["client_comms", "sales", "production", "admin", "marketing", "other"]
AGE_RANGES = ["<25", "25-35", "35-45", "45-55", "55+"]
GENDERS = ["female", "male", "not_specified"]


EXTRA_PERSONAS: list[Persona] = [
    Persona(
        name="Марина — кофейня",
        intro_message=(
            "у меня кофейня в спальном районе, доставка через telegram-бота сторонней службы. "
            "клиенты пишут заказы в директ instagram и в whatsapp, я их вручную перебиваю в "
            "блокнот для бариста. часто путаются модификаторы (без сахара, овсяное молоко) — "
            "клиенты ругаются. хочу избавиться от ручного переноса заказов."
        ),
        ground_truth=GroundTruth(
            business_type="кофейня с доставкой",
            channels=["instagram direct", "whatsapp", "telegram"],
            tools_in_use=["блокнот", "сторонний telegram-бот доставки"],
            pain_points=[
                "вручную переношу заказы из директа в блокнот для бариста",
                "путаются модификаторы — клиенты получают не то что заказали",
                "к концу смены 2-3 жалобы в день",
            ],
            primary_value="customer_experience",
            desired_outcome=(
                "заказ из любого канала падает в одну табличку с явными модификаторами, "
                "бариста сразу видит правильный список"
            ),
            success_metric="0 жалоб на путаницу в модификаторах за неделю",
            out_of_scope=["своё мобильное приложение", "интеграция с эквайрингом"],
            monthly_budget_rub=2500,
            has_llm_key=False,
            llm_provider=None,
            tech_savviness="basic",
            maintainer="сама",
        ),
    ),
    Persona(
        name="Сергей — репетитор английского",
        intro_message=(
            "я репетитор английского, работаю онлайн через zoom. у меня сейчас 18 учеников, "
            "расписание веду в google-календаре, оплаты приходят на тинькофф. забываю напомнить "
            "ученикам за час до урока, и пара раз в неделю кто-то опаздывает или не приходит. "
            "хочу автоматические напоминания и сборку оплат в одно место."
        ),
        ground_truth=GroundTruth(
            business_type="онлайн-репетитор",
            channels=["telegram", "whatsapp"],
            tools_in_use=["google calendar", "zoom", "tinkoff", "google sheets"],
            pain_points=[
                "забываю напомнить за час до урока",
                "1-2 раза в неделю ученик не приходит",
                "оплаты разбросаны по уведомлениям тинькофф",
            ],
            primary_value="time",
            desired_outcome=(
                "автоматическое напоминание ученику за 60 минут до zoom-урока в WhatsApp, "
                "и одна табличка где видно кто оплатил а кто нет"
            ),
            success_metric="0 пропусков уроков в неделю по причине забытого напоминания",
            out_of_scope=["LMS", "своя платформа", "видеоуроки"],
            monthly_budget_rub=1500,
            has_llm_key=False,
            llm_provider=None,
            tech_savviness="basic",
            maintainer="сам",
        ),
    ),
    Persona(
        name="Елена — handmade на маркетплейсах",
        intro_message=(
            "я делаю handmade-украшения, продаю на ozon и wildberries. вопросы покупателей идут в "
            "чаты обоих маркетплейсов плюс в директ instagram. я отвечаю руками, по 2-3 часа в день. "
            "вопросы повторяются: размеры, материалы, сроки доставки. хочу частично автоматизировать ответы."
        ),
        ground_truth=GroundTruth(
            business_type="handmade-украшения на маркетплейсах",
            channels=["ozon чат", "wildberries чат", "instagram direct"],
            tools_in_use=["личный кабинет ozon", "личный кабинет wb", "блокнот"],
            pain_points=[
                "отвечаю на одинаковые вопросы в трёх местах по 2-3 часа в день",
                "иногда отвечаю поздно — теряю продажи на маркетплейсах из-за рейтинга ответов",
            ],
            primary_value="time",
            desired_outcome=(
                "стандартные вопросы (размер, материал, сроки) автоматически получают шаблонный ответ, "
                "только нестандартные приходят мне на ручной разбор"
            ),
            success_metric="меньше 1 часа в день на чаты вместо 2-3",
            out_of_scope=["свой интернет-магазин", "TikTok"],
            monthly_budget_rub=2000,
            has_llm_key=False,
            llm_provider=None,
            tech_savviness="basic",
            maintainer="сама",
        ),
    ),
    Persona(
        name="Павел — налоговый консультант",
        intro_message=(
            "я консультирую малый бизнес по налогам. на каждое коммерческое предложение трачу 4 часа: "
            "собираю обороты клиента, считаю варианты налоговых режимов, оформляю в word. "
            "кп делаю 10-15 в месяц. хочу сократить эту рутину, чтобы тратить время на саму консультацию."
        ),
        ground_truth=GroundTruth(
            business_type="налоговый консалтинг b2b",
            channels=["email", "telegram"],
            tools_in_use=["microsoft word", "excel", "1c"],
            pain_points=[
                "4 часа на каждое кп — формат и расчёты повторяются",
                "10-15 кп в месяц — это 40-60 часов рутины",
            ],
            primary_value="time",
            desired_outcome=(
                "ввожу основные параметры клиента (оборот, режим, регион), получаю готовый word-кп "
                "за 10 минут вместо 4 часов"
            ),
            success_metric="время на одно кп — меньше 30 минут",
            out_of_scope=["онлайн-личный-кабинет для клиентов", "автоматическая сдача отчётности"],
            monthly_budget_rub=10000,
            has_llm_key=True,
            llm_provider="OpenAI",
            tech_savviness="confident",
            maintainer="сам",
        ),
    ),
    Persona(
        name="Алина — SMM-агентство",
        intro_message=(
            "у нас smm-агентство, в команде 3 человека, ведём 12 клиентов. контент-планы храним в notion, "
            "дедлайны теряются — каждую неделю обнаруживаем что забыли согласовать пост или опоздали с "
            "выкладкой. клиенты ругаются. хочу одно место с дедлайнами и напоминаниями всей команде."
        ),
        ground_truth=GroundTruth(
            business_type="smm-агентство",
            channels=["telegram", "email"],
            tools_in_use=["notion", "google drive", "telegram-чаты с клиентами"],
            pain_points=[
                "дедлайны теряются между notion и telegram",
                "1-2 раза в неделю забываем согласовать или опаздываем",
                "клиенты ругаются на срывы",
            ],
            primary_value="sanity",
            desired_outcome=(
                "одна доска с дедлайнами всех 12 клиентов, автоматические напоминания исполнителю "
                "за 24 часа до дедлайна"
            ),
            success_metric="0 пропущенных дедлайнов за неделю",
            out_of_scope=["биллинг клиентов", "креативная часть — её делаем как и раньше"],
            monthly_budget_rub=5000,
            has_llm_key=False,
            llm_provider=None,
            tech_savviness="basic",
            maintainer="наш проджект",
        ),
    ),
]


ALL_PERSONAS: list[Persona] = list(STOCK_PERSONAS) + EXTRA_PERSONAS


@dataclass
class TurnRecord:
    role: str
    content: str
    latency_s: float = 0.0
    http_status: int | None = None


@dataclass
class SessionRecord:
    persona_name: str
    sector: str
    time_eater: str
    age_range: str
    gender: str
    email: str
    session_id: str | None = None
    intake_latency_s: float = 0.0
    intake_status: int | None = None
    turns: list[TurnRecord] = field(default_factory=list)
    done: bool = False
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    qa_logs: list[dict] = field(default_factory=list)
    started_at: str = ""
    ended_at: str = ""

    @property
    def total_chat_s(self) -> float:
        return sum(t.latency_s for t in self.turns if t.role == "user_send")

    @property
    def slowest_turn_s(self) -> float:
        candidates = [t.latency_s for t in self.turns if t.role == "user_send"]
        return max(candidates) if candidates else 0.0


async def _post_with_retry(
    client: httpx.AsyncClient,
    url: str,
    payload: dict,
    *,
    max_429_retries: int = 1,
    max_5xx_retries: int = 2,
    backoff_429_s: float = 65.0,
    backoff_5xx_s: float = 8.0,
) -> tuple[int, dict | None, str | None]:
    """Returns (status, json_or_none, error_text_or_none).

    Retries:
    - 429 (rate limit): backoff_429_s, max_429_retries times.
    - 500/502/503/504 (transient server / Anthropic overload): exponential
      backoff starting at backoff_5xx_s, max_5xx_retries times. Anthropic
      occasionally returns 529 Overloaded which surfaces as 500 from our chat
      endpoint — retry handles it.
    """
    attempt_429 = 0
    attempt_5xx = 0
    while True:
        try:
            r = await client.post(url, json=payload, timeout=120.0)
        except httpx.HTTPError as e:
            return 0, None, f"network: {type(e).__name__}: {e}"
        if r.status_code == 429 and attempt_429 < max_429_retries:
            attempt_429 += 1
            await asyncio.sleep(backoff_429_s)
            continue
        if r.status_code in (500, 502, 503, 504) and attempt_5xx < max_5xx_retries:
            attempt_5xx += 1
            await asyncio.sleep(backoff_5xx_s * (2 ** (attempt_5xx - 1)))
            continue
        try:
            data = r.json()
        except Exception:
            data = None
        return r.status_code, data, None if r.status_code < 400 else r.text


async def run_one_session(
    *,
    client: httpx.AsyncClient,
    base_url: str,
    persona: Persona,
    index: int,
    max_turns: int,
    qa_strictness: str = "strict",
    persona_llm: object | None = None,
) -> SessionRecord:
    rec = SessionRecord(
        persona_name=persona.name,
        sector=random.choice(SECTORS),
        time_eater=random.choice(TIME_EATERS),
        age_range=random.choice(AGE_RANGES),
        gender=random.choice(GENDERS),
        email=TEST_EMAIL,
        started_at=datetime.now(timezone.utc).isoformat(),
    )

    intake_payload = {
        "name": NAME,
        "email": rec.email,
        "age_range": rec.age_range,
        "gender": rec.gender,
        "sector": rec.sector,
        "time_eater": rec.time_eater,
    }
    t0 = time.monotonic()
    status, data, err = await _post_with_retry(
        client, f"{base_url}/api/intake", intake_payload,
    )
    rec.intake_latency_s = time.monotonic() - t0
    rec.intake_status = status
    if status != 200 or not data:
        rec.error = f"intake failed: {status} {err}"
        rec.ended_at = datetime.now(timezone.utc).isoformat()
        return rec
    rec.session_id = data["session_id"]

    # Build the LLM persona session. Routes through app.llm.LLM so the persona
    # uses the same backend as prod (Gemini / Vertex / Anthropic) — no separate
    # SDK / quota burn. Required for both 'strict' and 'lenient' QA modes.
    if persona_llm is None:
        rec.error = "persona_llm not provided — internal config error"
        rec.ended_at = datetime.now(timezone.utc).isoformat()
        return rec
    archetype = archetype_for(persona.name)
    llm_session = LLMPersonaSession(
        name=persona.name,
        character_brief=CHARACTER_BRIEFS[archetype],
        business_brief=build_business_brief(persona),
        llm=persona_llm,
        intro_message=persona.intro_message,
    )

    user_msg = persona.intro_message
    last_persona_reply: str | None = None
    for turn in range(1, max_turns + 1):
        t0 = time.monotonic()
        status, data, err = await _post_with_retry(
            client,
            f"{base_url}/api/chat",
            {"session_id": rec.session_id, "message": user_msg},
        )
        latency = time.monotonic() - t0
        rec.turns.append(TurnRecord(
            role="user_send", content=user_msg, latency_s=latency, http_status=status,
        ))
        if status != 200 or not data:
            rec.error = f"chat turn {turn} failed: {status} {err}"
            break
        bot_reply = data["reply"]
        rec.turns.append(TurnRecord(role="assistant", content=bot_reply))
        if _has_significant_latin(bot_reply):
            rec.warnings.append(f"turn {turn}: bot reply contains non-Russian text")
        if data.get("done"):
            rec.done = True
            break

        # LLM persona — QA strict (3 attempts with reviewer) or lenient (1 attempt, no reviewer).
        run_qa = qa_strictness == "strict"
        next_msg, qa_log = await asyncio.to_thread(
            llm_session.respond, bot_reply, run_qa=run_qa,
        )
        rec.qa_logs.append({"turn": turn, "log": qa_log})

        # Loop-guard: if persona would repeat the exact same reply twice in a row,
        # swap in a generic clarifier to nudge the bot to a different question.
        if next_msg == last_persona_reply:
            next_msg = "уточните, пожалуйста, я не понял предыдущий вопрос"
        last_persona_reply = next_msg
        user_msg = next_msg

    rec.ended_at = datetime.now(timezone.utc).isoformat()
    return rec


_LATIN_RE = re.compile(r"[A-Za-z]")
_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")


def _has_significant_latin(text: str) -> bool:
    """True if the bot reply is suspiciously English-heavy.

    Russian replies that mention brand names (ChatGPT, Claude, GigaChat, Tilda,
    YuKassa) trip naive ratio thresholds. We require BOTH:
    - >50% latin chars in the alphabetic content (a true English sentence is
      ~95%, a Russian sentence with 1-3 brand names is ~20-45%).
    - >40 latin chars total (single brand name ≈ 6-10 chars; cap of 40 means
      brand-list sentences pass but a multi-sentence English block doesn't).
    """
    if not text:
        return False
    latin = len(_LATIN_RE.findall(text))
    cyrillic = len(_CYRILLIC_RE.findall(text))
    if latin < 40:
        return False
    total = latin + cyrillic
    if total == 0:
        return False
    return (latin / total) > 0.50


async def run_batch(
    *,
    base_url: str,
    personas: list[Persona],
    concurrency: int,
    max_turns: int,
    qa_strictness: str = "strict",
    persona_llm: object | None = None,
) -> list[SessionRecord]:
    sem = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient() as client:
        async def bounded(idx: int, p: Persona) -> SessionRecord:
            async with sem:
                return await run_one_session(
                    client=client,
                    base_url=base_url,
                    persona=p,
                    index=idx,
                    max_turns=max_turns,
                    qa_strictness=qa_strictness,
                    persona_llm=persona_llm,
                )

        tasks = [bounded(i, p) for i, p in enumerate(personas, start=1)]
        return await asyncio.gather(*tasks)


def write_outputs(records: list[SessionRecord], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    ids_file = out_dir / "created_ids.txt"
    ids_file.write_text(
        "\n".join(r.session_id for r in records if r.session_id),
        encoding="utf-8",
    )

    for r in records:
        sid = r.session_id or f"failed_{r.persona_name[:20].replace(' ', '_')}"
        path = out_dir / f"{sid}.json"
        payload = {
            "persona_name": r.persona_name,
            "sector": r.sector,
            "time_eater": r.time_eater,
            "age_range": r.age_range,
            "gender": r.gender,
            "email": r.email,
            "session_id": r.session_id,
            "intake_latency_s": round(r.intake_latency_s, 3),
            "intake_status": r.intake_status,
            "done": r.done,
            "error": r.error,
            "warnings": list(r.warnings),
            "qa_logs": list(r.qa_logs),
            "started_at": r.started_at,
            "ended_at": r.ended_at,
            "turns": [
                {
                    "role": t.role,
                    "content": t.content,
                    "latency_s": round(t.latency_s, 3),
                    "http_status": t.http_status,
                }
                for t in r.turns
            ],
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
        )

    summary = _format_summary(records)
    (out_dir / "summary.txt").write_text(summary, encoding="utf-8")
    print(summary)


def _format_summary(records: list[SessionRecord]) -> str:
    successes = [r for r in records if r.done and not r.error]
    fails = [r for r in records if not r.done or r.error]
    all_turn_latencies = [
        t.latency_s for r in records for t in r.turns if t.role == "user_send"
    ]
    all_turn_latencies.sort()

    def pct(p: float) -> float:
        if not all_turn_latencies:
            return 0.0
        idx = min(len(all_turn_latencies) - 1, int(p * len(all_turn_latencies)))
        return all_turn_latencies[idx]

    lines = [
        f"Run finished at: {datetime.now(timezone.utc).isoformat()}",
        f"Total sessions: {len(records)}  success: {len(successes)}  fail: {len(fails)}",
        "",
        "Per session:",
        f"{'sid':<14} {'case':<32} {'turns':>5} {'done':>5} {'total_s':>8} {'slow_turn_s':>11} {'error':<40}",
    ]
    for r in records:
        chat_turns = sum(1 for t in r.turns if t.role == "user_send")
        sid_short = (r.session_id or "-")[:12]
        case = r.persona_name[:30]
        err = (r.error or "")[:38]
        lines.append(
            f"{sid_short:<14} {case:<32} {chat_turns:>5} {str(r.done):>5} "
            f"{r.total_chat_s:>8.1f} {r.slowest_turn_s:>11.1f} {err:<40}"
        )
    lines += [
        "",
        f"Turn latency: p50={pct(0.5):.1f}s  p95={pct(0.95):.1f}s  max={(all_turn_latencies[-1] if all_turn_latencies else 0):.1f}s",
        f"HTTP status counts: " + _status_histogram(records),
    ]
    warnings_total = sum(len(r.warnings) for r in records)
    if warnings_total:
        lines.append(f"Warnings (non-Russian / loop guard): {warnings_total}")
        for r in records:
            for w in r.warnings:
                lines.append(f"  [{(r.session_id or '-')[:12]}] {w}")
    return "\n".join(lines)


def _status_histogram(records: list[SessionRecord]) -> str:
    buckets: dict[int | None, int] = {}
    for r in records:
        buckets[r.intake_status] = buckets.get(r.intake_status, 0) + 1
        for t in r.turns:
            if t.http_status is not None:
                buckets[t.http_status] = buckets.get(t.http_status, 0) + 1
    return ", ".join(f"{k}:{v}" for k, v in sorted(buckets.items(), key=lambda kv: (kv[0] or 0)))


_ARCHETYPE_KEYWORDS: dict[str, list[str]] = {
    # Match human-friendly names to archetype keys; first persona in
    # PERSONA_CHARACTER_MAP with this archetype wins.
    "confident_techie": ["уверенн", "технар", "техник"],
    "vague": ["расплывч"],
    "skeptic": ["скептик"],
    "rusher": ["спеша", "торопыга"],
    "technophobe": ["технофоб"],
    "rambler": ["болту"],
}


def _select_personas(args: argparse.Namespace) -> list[Persona]:
    if args.all_cases:
        return list(ALL_PERSONAS)
    if args.case:
        case_lower = args.case.lower()
        match = [p for p in ALL_PERSONAS if case_lower in p.name.lower()]
        # Archetype-keyword fallback: e.g. --case "Уверенный" -> confident_techie
        if not match:
            from scripts.llm_personas import PERSONA_CHARACTER_MAP
            for arch_key, keywords in _ARCHETYPE_KEYWORDS.items():
                if any(kw in case_lower for kw in keywords):
                    match = [
                        p for p in ALL_PERSONAS
                        if PERSONA_CHARACTER_MAP.get(p.name) == arch_key
                    ]
                    if match:
                        break
        if not match:
            print(f"No persona matching '{args.case}'. Available:", file=sys.stderr)
            for p in ALL_PERSONAS:
                print(f"  - {p.name}", file=sys.stderr)
            sys.exit(2)
        # Repeat the matched persona to fill --sessions
        return [random.choice(match) for _ in range(args.sessions)]
    return [random.choice(ALL_PERSONAS) for _ in range(args.sessions)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Your own staging BizWare URL; never use a production service")
    parser.add_argument("--confirm-write", action="store_true", required=True, help="Confirm that this run creates persistent sessions on your staging service")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--sessions", type=int, default=1)
    parser.add_argument("--max-turns", type=int, default=25)
    parser.add_argument("--case", default=None,
                        help="substring match against persona name; overrides random selection")
    parser.add_argument("--all-cases", action="store_true",
                        help="run exactly one session per persona in ALL_PERSONAS")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument(
        "--qa-strictness",
        choices=["strict", "lenient"],
        default="strict",
        help=(
            "strict (default): LLM persona + QA-reviewer regen loop. "
            "lenient: LLM persona, no QA reviewer."
        ),
    )
    args = parser.parse_args()

    # Load .env.test for backend creds — picks up GCP_*, ANTHROPIC_API_KEY (if
    # used), VERTEX_*, GEMINI_* etc. Then materialise GCP credentials so the
    # Vertex/Gemini SDK can authenticate without further setup.
    try:
        from dotenv import load_dotenv
        load_dotenv(".env.test")
    except ImportError:
        pass
    import app.bootstrap  # noqa: F401  side-effect: GCP creds JSON file
    from app.llm import get_default_llm

    # Persona LLM uses the SAME backend as prod (set by LLM_BACKEND env). Avoids
    # silent quota burn on a different backend during testing.
    persona_llm = get_default_llm()
    print(f"Persona LLM backend: {type(persona_llm).__name__}", file=sys.stderr)

    if args.seed is not None:
        random.seed(args.seed)

    personas = _select_personas(args)

    out_dir = Path("tmp/live_load_test") / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    print(f"Base URL : {args.base_url}")
    print(f"Sessions : {len(personas)}  Concurrency: {args.concurrency}  Max turns: {args.max_turns}")
    print(f"QA mode  : {args.qa_strictness}")
    print(f"Out dir  : {out_dir}")
    print()

    records = asyncio.run(run_batch(
        base_url=args.base_url,
        personas=personas,
        concurrency=args.concurrency,
        max_turns=args.max_turns,
        qa_strictness=args.qa_strictness,
        persona_llm=persona_llm,
    ))
    write_outputs(records, out_dir)


if __name__ == "__main__":
    main()
