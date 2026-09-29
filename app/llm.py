"""LLM abstraction layer.

Allows the rest of the app to call into Claude (real Anthropic API), Claude
on Vertex AI, or a deterministic mock for tests / offline simulation. The
shape mirrors the relevant subset of the Anthropic Messages API.

Backend selection (in `get_default_llm`):
- `LLM_BACKEND=vertex` (or `VERTEX_PROJECT_ID` set) → AnthropicVertex on GCP.
  Uses Google Cloud credentials and the Vertex AI Anthropic endpoint.
- Default → direct Anthropic API (requires `ANTHROPIC_API_KEY`).
"""
from __future__ import annotations

import json
import logging
import os
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("llm")

# Retry tuning for transient Anthropic / Vertex failures (429 rate-limit,
# 5xx overload). Applied uniformly across both backends.
ANTHROPIC_MAX_RETRIES = 4
ANTHROPIC_BASE_BACKOFF_S = 2.0  # exponential: 2 → 4 → 8 → 16 + jitter
ANTHROPIC_MAX_BACKOFF_S = 30.0


@dataclass
class LLMResponse:
    text: str = ""
    tool_use: dict | None = None  # {"name": ..., "input": {...}}
    stop_reason: str = "end_turn"
    raw: Any = None


class LLM(ABC):
    @abstractmethod
    def complete(
        self,
        *,
        system: str | list[dict],
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.7,
    ) -> LLMResponse:
        ...


def _call_with_retry(client: Any, kwargs: dict[str, Any]) -> Any:
    """Call client.messages.create(**kwargs) with exponential backoff on
    transient failures (429 rate-limit, 5xx server overload, timeouts).

    Used by both AnthropicLLM (direct) and VertexLLM (Anthropic-on-Vertex)
    since they share the same exception classes from the anthropic SDK.
    """
    from anthropic import APIStatusError, APITimeoutError, RateLimitError

    last_exc: Exception | None = None
    for attempt in range(ANTHROPIC_MAX_RETRIES + 1):
        try:
            return client.messages.create(**kwargs)
        except RateLimitError as e:
            last_exc = e
            if attempt >= ANTHROPIC_MAX_RETRIES:
                log.error("LLM 429 — retries exhausted (%d attempts)", attempt + 1)
                raise
            wait = min(
                ANTHROPIC_BASE_BACKOFF_S * (2 ** attempt),
                ANTHROPIC_MAX_BACKOFF_S,
            ) + random.uniform(0, 1.0)
            log.warning(
                "LLM 429 rate-limit, attempt %d/%d, backing off %.1fs",
                attempt + 1, ANTHROPIC_MAX_RETRIES + 1, wait,
            )
            time.sleep(wait)
        except APIStatusError as e:
            status = getattr(e, "status_code", None)
            if status is None or status < 500:
                raise  # 4xx → fail fast
            last_exc = e
            if attempt >= ANTHROPIC_MAX_RETRIES:
                log.error("LLM %s — retries exhausted (%d attempts)", status, attempt + 1)
                raise
            wait = min(
                ANTHROPIC_BASE_BACKOFF_S * (2 ** attempt),
                ANTHROPIC_MAX_BACKOFF_S,
            ) + random.uniform(0, 1.0)
            log.warning(
                "LLM %s server error, attempt %d/%d, backing off %.1fs",
                status, attempt + 1, ANTHROPIC_MAX_RETRIES + 1, wait,
            )
            time.sleep(wait)
        except APITimeoutError as e:
            last_exc = e
            if attempt >= ANTHROPIC_MAX_RETRIES:
                raise
            wait = min(
                ANTHROPIC_BASE_BACKOFF_S * (2 ** attempt),
                ANTHROPIC_MAX_BACKOFF_S,
            ) + random.uniform(0, 1.0)
            log.warning(
                "LLM timeout, attempt %d/%d, backing off %.1fs",
                attempt + 1, ANTHROPIC_MAX_RETRIES + 1, wait,
            )
            time.sleep(wait)
    if last_exc:
        raise last_exc
    raise RuntimeError("LLM retry loop exited without success")


def _parse_response(resp: Any) -> LLMResponse:
    text_parts: list[str] = []
    tool_use: dict | None = None
    for block in resp.content:
        t = getattr(block, "type", None)
        if t == "text":
            text_parts.append(block.text)
        elif t == "tool_use":
            tool_use = {"name": block.name, "input": block.input}
    return LLMResponse(
        text="".join(text_parts).strip(),
        tool_use=tool_use,
        stop_reason=getattr(resp, "stop_reason", "end_turn"),
        raw=resp,
    )


class AnthropicLLM(LLM):
    """Wraps the official Anthropic SDK (direct API)."""

    def __init__(self, model: str | None = None):
        from anthropic import Anthropic  # local import so tests don't need creds

        self.client = Anthropic()
        self.model = model or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

    def complete(self, *, system, messages, tools=None, max_tokens=1024, temperature=0.7):
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "extra_body": {"temperature": temperature},
        }
        if tools:
            kwargs["tools"] = tools
        resp = _call_with_retry(self.client, kwargs)
        return _parse_response(resp)


def _call_gemini_with_retry(call: Any) -> Any:
    """Same shape as _call_with_retry but catches google-genai exceptions
    (ClientError 429, ServerError 5xx). Used by GeminiLLM.
    """
    from google.genai import errors as genai_errors

    last_exc: Exception | None = None
    for attempt in range(ANTHROPIC_MAX_RETRIES + 1):
        try:
            return call()
        except genai_errors.ClientError as e:
            code = getattr(e, "code", None)
            if code != 429:
                raise
            last_exc = e
            if attempt >= ANTHROPIC_MAX_RETRIES:
                log.error("Gemini 429 — retries exhausted (%d attempts)", attempt + 1)
                raise
            wait = min(
                ANTHROPIC_BASE_BACKOFF_S * (2 ** attempt),
                ANTHROPIC_MAX_BACKOFF_S,
            ) + random.uniform(0, 1.0)
            log.warning(
                "Gemini 429 rate-limit, attempt %d/%d, backing off %.1fs",
                attempt + 1, ANTHROPIC_MAX_RETRIES + 1, wait,
            )
            time.sleep(wait)
        except genai_errors.ServerError as e:
            last_exc = e
            if attempt >= ANTHROPIC_MAX_RETRIES:
                log.error("Gemini 5xx — retries exhausted (%d attempts)", attempt + 1)
                raise
            wait = min(
                ANTHROPIC_BASE_BACKOFF_S * (2 ** attempt),
                ANTHROPIC_MAX_BACKOFF_S,
            ) + random.uniform(0, 1.0)
            log.warning(
                "Gemini server error, attempt %d/%d, backing off %.1fs",
                attempt + 1, ANTHROPIC_MAX_RETRIES + 1, wait,
            )
            time.sleep(wait)
    if last_exc:
        raise last_exc
    raise RuntimeError("Gemini retry loop exited without success")


def _strip_schema_for_gemini(schema: dict | None) -> dict[str, Any]:
    """Gemini's FunctionDeclaration.parameters accepts a subset of JSON Schema.
    `additionalProperties` and `$schema` cause errors in some Gemini versions —
    strip them recursively. Keep type/properties/items/required/enum/description.
    """
    if not isinstance(schema, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in schema.items():
        if k in ("additionalProperties", "$schema", "$defs", "definitions"):
            continue
        if isinstance(v, dict):
            out[k] = _strip_schema_for_gemini(v)
        elif isinstance(v, list):
            out[k] = [_strip_schema_for_gemini(item) if isinstance(item, dict) else item for item in v]
        else:
            out[k] = v
    return out


class GeminiLLM(LLM):
    """Google Gemini on Vertex AI via the `google-genai` SDK.

    Translates between our Anthropic-shaped LLM interface and Gemini's API:
    - `system: str` → `system_instruction`
    - `messages: [{role, content}]` → `contents=[Content(role, parts=[Part(text)])]`
      (Gemini uses "model" instead of "assistant" for the bot role)
    - `tools: [{name, description, input_schema}]` → `Tool(function_declarations=...)`
    - Response: text parts and function_call → LLMResponse(text, tool_use)

    Auth uses Google Application Default Credentials; app.bootstrap can
    materialise them from the configured GCP_* environment variables.
    """

    DEFAULT_LOCATION = "us-central1"
    DEFAULT_MODEL = "gemini-2.5-pro"

    def __init__(
        self,
        *,
        model: str | None = None,
        location: str | None = None,
        project_id: str | None = None,
    ):
        from google import genai

        self.location = (
            location
            or os.environ.get("GEMINI_LOCATION")
            or os.environ.get("VERTEX_REGION")
            or self.DEFAULT_LOCATION
        )
        self.project_id = (
            project_id
            or os.environ.get("VERTEX_PROJECT_ID")
            or os.environ.get("GCP_PROJECT_ID")
        )
        if not self.project_id:
            raise RuntimeError(
                "GeminiLLM: project_id missing. Set VERTEX_PROJECT_ID or GCP_PROJECT_ID."
            )
        self.client = genai.Client(
            vertexai=True, project=self.project_id, location=self.location,
        )
        self.model = model or os.environ.get("GEMINI_MODEL", self.DEFAULT_MODEL)

    def complete(self, *, system, messages, tools=None, max_tokens=1024, temperature=0.7):
        from google.genai import types

        # System prompt — Gemini accepts a single string or list of Parts.
        if isinstance(system, str):
            sys_text: str | None = system or None
        elif isinstance(system, list):
            sys_text = "\n".join(
                s.get("text", "") if isinstance(s, dict) else str(s) for s in system
            ) or None
        else:
            sys_text = None

        # Messages — translate roles. Gemini uses "model" for assistant turns.
        contents: list[Any] = []
        for m in messages:
            anthropic_role = m.get("role") or "user"
            gemini_role = "model" if anthropic_role == "assistant" else "user"
            text = m.get("content") or ""
            contents.append(
                types.Content(role=gemini_role, parts=[types.Part(text=text)]),
            )

        # Tools — Anthropic's `tools=[{name, description, input_schema}]`
        # becomes Gemini's `Tool(function_declarations=...)`. Force "ANY" mode
        # so Gemini reliably emits a function_call (matches the workflow's
        # expectation that the extractor always invokes update_state).
        gemini_tools: list[Any] | None = None
        tool_config: Any | None = None
        if tools:
            decls = []
            for t in tools:
                params = _strip_schema_for_gemini(t.get("input_schema"))
                decls.append(
                    types.FunctionDeclaration(
                        name=t["name"],
                        description=t.get("description", ""),
                        parameters=params,
                    )
                )
            gemini_tools = [types.Tool(function_declarations=decls)]
            tool_config = types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode="ANY"),
            )

        config = types.GenerateContentConfig(
            system_instruction=sys_text,
            max_output_tokens=max_tokens,
            temperature=temperature,
            tools=gemini_tools,
            tool_config=tool_config,
        )

        resp = _call_gemini_with_retry(
            lambda: self.client.models.generate_content(
                model=self.model, contents=contents, config=config,
            ),
        )

        text_parts: list[str] = []
        tool_use: dict | None = None
        finish_reason = "end_turn"
        for cand in resp.candidates or []:
            finish_reason = str(getattr(cand, "finish_reason", "") or finish_reason)
            if not cand.content or not cand.content.parts:
                continue
            for part in cand.content.parts:
                t = getattr(part, "text", None)
                fc = getattr(part, "function_call", None)
                if t:
                    text_parts.append(t)
                elif fc:
                    tool_use = {
                        "name": fc.name,
                        "input": dict(fc.args or {}),
                    }
        return LLMResponse(
            text="".join(text_parts).strip(),
            tool_use=tool_use,
            stop_reason=finish_reason,
            raw=resp,
        )


class VertexLLM(LLM):
    """Anthropic Claude served via Google Cloud Vertex AI.

    Uses the same `anthropic` SDK but with the Vertex variant of the client.
    Auth is via Application Default Credentials — `app.bootstrap` already
    materialises a JSON keyfile from GCP_* env vars and points
    `GOOGLE_APPLICATION_CREDENTIALS` at it before any google import.

    Vertex model IDs are different from the direct-API ones (date-stamped).
    Set `VERTEX_MODEL` when the default model identifier is not available in
    the configured project or region.
    """

    DEFAULT_REGION = "us-east5"
    DEFAULT_MODEL = "claude-sonnet-4-5@20250929"

    def __init__(
        self,
        *,
        model: str | None = None,
        region: str | None = None,
        project_id: str | None = None,
    ):
        from anthropic import AnthropicVertex

        self.region = region or os.environ.get("VERTEX_REGION", self.DEFAULT_REGION)
        self.project_id = (
            project_id
            or os.environ.get("VERTEX_PROJECT_ID")
            or os.environ.get("GCP_PROJECT_ID")
        )
        if not self.project_id:
            raise RuntimeError(
                "VertexLLM: project_id missing. Set VERTEX_PROJECT_ID or GCP_PROJECT_ID."
            )
        self.client = AnthropicVertex(region=self.region, project_id=self.project_id)
        self.model = model or os.environ.get("VERTEX_MODEL", self.DEFAULT_MODEL)

    def complete(self, *, system, messages, tools=None, max_tokens=1024, temperature=0.7):
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "extra_body": {"temperature": temperature},
        }
        if tools:
            kwargs["tools"] = tools
        resp = _call_with_retry(self.client, kwargs)
        return _parse_response(resp)


@dataclass
class MockLLM(LLM):
    """Deterministic mock used for unit tests and offline simulation.

    `responder` is a function (system, messages, tools) -> LLMResponse.
    The convenience helpers in `app.eval.mock_responders` build common ones.
    """

    responder: Any  # callable
    calls: list[dict] = field(default_factory=list)

    def complete(self, *, system, messages, tools=None, max_tokens=1024, temperature=0.7):
        self.calls.append(
            {
                "system": system,
                "messages": messages,
                "tools": tools,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        result = self.responder(system, messages, tools)
        if isinstance(result, str):
            return LLMResponse(text=result)
        if isinstance(result, dict):
            return LLMResponse(**result)
        return result


def get_default_llm() -> LLM:
    """Pick the LLM backend at runtime.

    Selection order:
    1. `LLM_BACKEND=gemini` → GeminiLLM (Vertex AI Gemini, no Anthropic
       marketplace subscription needed; uses GCP quotas).
    2. `LLM_BACKEND=vertex` → VertexLLM (Anthropic Claude via Vertex —
       requires marketplace activation in GCP project).
    3. `LLM_BACKEND=anthropic` → direct AnthropicLLM (subject to
       Anthropic-tier rate limits).
    4. Auto-detect: GCP project + creds → GeminiLLM; else
       ANTHROPIC_API_KEY → AnthropicLLM.
    5. Otherwise → RuntimeError, caller must inject MockLLM in tests.
    """
    backend = (os.environ.get("LLM_BACKEND") or "").strip().lower()
    if backend == "gemini":
        return GeminiLLM()
    if backend == "vertex":
        return VertexLLM()
    if backend == "anthropic":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "LLM_BACKEND=anthropic but ANTHROPIC_API_KEY is not set."
            )
        return AnthropicLLM()
    # Auto-detect: prefer Gemini-on-Vertex if GCP project is wired up.
    if os.environ.get("VERTEX_PROJECT_ID") or os.environ.get("GCP_PROJECT_ID"):
        return GeminiLLM()
    if os.environ.get("ANTHROPIC_API_KEY"):
        return AnthropicLLM()
    raise RuntimeError(
        "No LLM backend configured. Set LLM_BACKEND=gemini (with GCP creds), "
        "LLM_BACKEND=vertex (with marketplace activation), or "
        "LLM_BACKEND=anthropic (with ANTHROPIC_API_KEY)."
    )


def to_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)
