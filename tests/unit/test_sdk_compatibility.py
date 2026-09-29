"""Exercise provider adapters through the real SDK with an offline transport."""
import json

import anthropic
import httpx2
import pytest

from app.llm import AnthropicLLM, VertexLLM


@pytest.mark.parametrize("backend", ["anthropic", "vertex"])
def test_sdk_serializes_discovery_request_without_network(monkeypatch, backend):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx2.Response(200, json={
            "id": "msg_synthetic",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-6",
            "content": [{"type": "text", "text": "Which process takes the most time?"}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 12, "output_tokens": 9},
        })

    with httpx2.Client(transport=httpx2.MockTransport(respond)) as transport:
        if backend == "anthropic":
            sdk_class = anthropic.Anthropic
            monkeypatch.setattr(anthropic, "Anthropic", lambda: sdk_class(
                api_key="synthetic-validation-key",
                base_url="https://provider.example.invalid",
                http_client=transport,
            ))
            llm = AnthropicLLM()
        else:
            sdk_class = anthropic.AnthropicVertex
            monkeypatch.setattr(anthropic, "AnthropicVertex", lambda **kwargs: sdk_class(
                **kwargs,
                access_token="synthetic-validation-token",
                base_url="https://provider.example.invalid",
                http_client=transport,
            ))
            llm = VertexLLM(project_id="synthetic-project")

        result = llm.complete(
            system="Ask one business discovery question.",
            messages=[{"role": "user", "content": "I run a fictional repair shop."}],
            max_tokens=128,
            temperature=0.2,
        )

    assert result.text == "Which process takes the most time?"
    assert result.stop_reason == "end_turn"
    assert len(requests) == 1
    assert requests[0]["temperature"] == 0.2
    assert requests[0]["max_tokens"] == 128
    assert requests[0]["system"] == "Ask one business discovery question."
    assert requests[0]["messages"] == [
        {"role": "user", "content": "I run a fictional repair shop."}
    ]
