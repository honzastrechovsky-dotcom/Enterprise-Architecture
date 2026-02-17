"""Mock LLM Provider - mimics the LiteLLM / OpenAI API for offline development.

Exposes:
  POST /chat/completions      - chat completion (streaming + non-streaming)
  POST /completions            - legacy text completion
  POST /embeddings             - returns random unit-normalised vectors
  GET  /models                 - lists available mock models
  GET  /health                 - health check

Use as a drop-in replacement for LITELLM_BASE_URL when you have no API keys
and no local GPU.  The mock never calls any real model; it returns canned
responses deterministically so tests are reproducible.

Start with:
    uvicorn src.testing.mock_llm:app --port 4000 --reload

Or run inline:
    python -m src.testing.mock_llm

Then set in .env.dev:
    LITELLM_BASE_URL=http://localhost:4000
    LITELLM_API_KEY=sk-mock
"""

from __future__ import annotations

import json
import math
import random
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Mock LLM Provider",
    description="Offline LiteLLM/OpenAI-compatible mock for local development",
    version="1.0.0",
    docs_url="/docs",
    redoc_url=None,
)

# ---------------------------------------------------------------------------
# Canned chat responses keyed by simple keyword matching
# ---------------------------------------------------------------------------
_CANNED_RESPONSES: list[tuple[list[str], str]] = [
    (
        ["hello", "hi", "hey", "greet"],
        "Hello! I'm the Mock LLM running in offline dev mode. How can I help you today?",
    ),
    (
        ["connector", "resistance", "contact"],
        (
            "Based on the Connector Quality Standards v3.2, the maximum allowable "
            "contact resistance is 20 mΩ measured at 100 mA using the four-wire "
            "Kelvin method. Connectors exceeding this threshold must be quarantined."
        ),
    ),
    (
        ["maintenance", "predictive", "anomaly", "alert", "sensor"],
        (
            "The predictive maintenance system uses anomaly scores from 0 to 1. "
            "Tier 1 alerts (0.85-0.92) require scheduling within 72 hours. "
            "Tier 2 alerts (0.93-0.97) require scheduling within 24 hours. "
            "Tier 3 alerts (>=0.98) require immediate operator notification."
        ),
    ),
    (
        ["supply chain", "shortage", "inventory", "supplier"],
        (
            "The supply chain disruption playbook classifies severity into four levels. "
            "Level 1 (advisory, 10-14 day supply) requires daily monitoring. "
            "Level 2 (watch, 7-10 days) triggers alternate sourcing. "
            "Level 4 (emergency, <3 days) triggers CEO notification."
        ),
    ),
    (
        ["sop", "assembly", "soldering", "pick", "place", "reflow"],
        (
            "The Assembly Line SOP specifies the reflow soldering profile as: "
            "preheat at 150°C for 60 seconds, peak temperature of 245°C for 10 seconds. "
            "AOI inspection follows reflow with a target reject rate below 0.5%."
        ),
    ),
    (
        ["policy", "ai", "pii", "data", "usage", "allowed", "permitted"],
        (
            "The Enterprise AI Agent Usage Policy permits: quality data analysis, "
            "SOP lookup, predictive maintenance insights, supply chain Q&A, and "
            "document search. Do NOT input PII, financial confidential data, "
            "unreleased product designs, or ITAR/EAR-controlled data."
        ),
    ),
    (
        ["feedback", "rating", "thumbs", "improve"],
        (
            "Users can provide feedback via thumbs-up/thumbs-down ratings or 1-5 scale "
            "ratings on any agent response. All feedback is stored for 90 days and used "
            "to improve model quality and build fine-tuning datasets."
        ),
    ),
    (
        ["summarize", "summarise", "summary", "tldr"],
        (
            "Here is a concise summary: The enterprise AI platform provides multi-tenant "
            "conversational AI with RAG (retrieval-augmented generation), audit logging, "
            "role-based access control, and feedback collection. It integrates with "
            "manufacturing systems for quality, maintenance, and supply chain use cases."
        ),
    ),
]

_DEFAULT_RESPONSE = (
    "I'm the Mock LLM running in offline development mode. "
    "I don't have access to real model inference, but I can return canned "
    "responses for common manufacturing-domain queries. "
    "Please check the seed documents for available topics."
)


def _pick_response(messages: list[dict[str, Any]]) -> str:
    """Select a canned response based on the last user message content."""
    last_user_content = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            last_user_content = str(msg.get("content", "")).lower()
            break

    for keywords, response in _CANNED_RESPONSES:
        if any(kw in last_user_content for kw in keywords):
            return response

    return _DEFAULT_RESPONSE


def _make_embedding(dim: int = 1536) -> list[float]:
    """Return a random unit-normalised dense vector."""
    vec = [random.gauss(0, 1) for _ in range(dim)]
    magnitude = math.sqrt(sum(x * x for x in vec))
    if magnitude == 0:
        magnitude = 1.0
    return [x / magnitude for x in vec]


def _token_count(text: str) -> int:
    """Rough token count approximation (words / 0.75)."""
    return max(1, int(len(text.split()) / 0.75))


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "mode": "mock"}


# ---------------------------------------------------------------------------
# Models list
# ---------------------------------------------------------------------------
@app.get("/models")
async def list_models() -> dict[str, Any]:
    models = [
        "mock/dev-model",
        "mock/fast-model",
        "mock/embed-model",
        "openai/gpt-4o-mini",
        "openai/text-embedding-3-small",
        "ollama/llama3.2",
        "ollama/nomic-embed-text",
    ]
    return {
        "object": "list",
        "data": [
            {
                "id": m,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "mock-provider",
            }
            for m in models
        ],
    }


# ---------------------------------------------------------------------------
# Chat completions  (POST /chat/completions)
# ---------------------------------------------------------------------------
@app.post("/chat/completions")
async def chat_completions(request: Request) -> Any:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    messages: list[dict[str, Any]] = body.get("messages", [])
    model: str = body.get("model", "mock/dev-model")
    stream: bool = body.get("stream", False)
    max_tokens: int = body.get("max_tokens", 256)

    response_text = _pick_response(messages)
    # Respect max_tokens (truncate by words as rough proxy)
    words = response_text.split()
    if max_tokens < len(words):
        response_text = " ".join(words[:max_tokens]) + "..."

    completion_id = f"chatcmpl-mock-{uuid.uuid4().hex[:12]}"
    created_at = int(time.time())
    prompt_tokens = sum(_token_count(str(m.get("content", ""))) for m in messages)
    completion_tokens = _token_count(response_text)

    if stream:
        return StreamingResponse(
            _stream_chat_chunks(completion_id, created_at, model, response_text),
            media_type="text/event-stream",
        )

    return JSONResponse(
        {
            "id": completion_id,
            "object": "chat.completion",
            "created": created_at,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": response_text},
                    "finish_reason": "stop",
                    "logprobs": None,
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }
    )


async def _stream_chat_chunks(
    completion_id: str,
    created_at: int,
    model: str,
    text: str,
) -> AsyncIterator[str]:
    """Yield SSE chunks that mimic the OpenAI streaming format."""
    words = text.split()
    # Emit 3-word chunks to simulate real streaming
    chunk_size = 3
    for i in range(0, len(words), chunk_size):
        chunk_text = " ".join(words[i : i + chunk_size])
        if i + chunk_size < len(words):
            chunk_text += " "
        chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created_at,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": chunk_text},
                    "finish_reason": None,
                }
            ],
        }
        yield f"data: {json.dumps(chunk)}\n\n"

    # Final chunk with finish_reason=stop
    final = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created_at,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {},
                "finish_reason": "stop",
            }
        ],
    }
    yield f"data: {json.dumps(final)}\n\n"
    yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# Legacy text completions  (POST /completions)
# ---------------------------------------------------------------------------
@app.post("/completions")
async def text_completions(request: Request) -> Any:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    prompt: str = body.get("prompt", "")
    model: str = body.get("model", "mock/dev-model")
    # Wrap prompt as a chat message for response selection
    messages = [{"role": "user", "content": prompt}]
    response_text = _pick_response(messages)

    return JSONResponse(
        {
            "id": f"cmpl-mock-{uuid.uuid4().hex[:12]}",
            "object": "text_completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "text": response_text,
                    "index": 0,
                    "logprobs": None,
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": _token_count(prompt),
                "completion_tokens": _token_count(response_text),
                "total_tokens": _token_count(prompt) + _token_count(response_text),
            },
        }
    )


# ---------------------------------------------------------------------------
# Embeddings  (POST /embeddings)
# ---------------------------------------------------------------------------
@app.post("/embeddings")
async def embeddings(request: Request) -> Any:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    model: str = body.get("model", "mock/embed-model")
    raw_input = body.get("input", "")

    # Normalise input to a list of strings
    if isinstance(raw_input, str):
        inputs = [raw_input]
    elif isinstance(raw_input, list):
        inputs = [str(item) for item in raw_input]
    else:
        inputs = [""]

    # Derive embedding dimension from model name (default 1536)
    dim = 1536
    if "small" in model:
        dim = 1536
    elif "large" in model or "3-large" in model:
        dim = 3072
    elif "nomic" in model or "embed" in model:
        dim = 768

    data = []
    total_tokens = 0
    for idx, text in enumerate(inputs):
        # Use text hash as RNG seed for reproducibility across runs
        random.seed(hash(text) % (2**32))
        embedding = _make_embedding(dim)
        # Reset random after each seeded call
        random.seed()
        data.append(
            {
                "object": "embedding",
                "index": idx,
                "embedding": embedding,
            }
        )
        total_tokens += _token_count(text)

    return JSONResponse(
        {
            "object": "list",
            "data": data,
            "model": model,
            "usage": {
                "prompt_tokens": total_tokens,
                "total_tokens": total_tokens,
            },
        }
    )


# ---------------------------------------------------------------------------
# Catch-all for LiteLLM proxy endpoints we don't implement
# ---------------------------------------------------------------------------
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def catch_all(path: str, request: Request) -> Any:
    return JSONResponse(
        {"error": f"Mock provider: endpoint '/{path}' not implemented", "status": "mock"},
        status_code=404,
    )


# ---------------------------------------------------------------------------
# Entrypoint for running directly: python -m src.testing.mock_llm
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    print("Starting Mock LLM Provider on http://localhost:4000")
    print("Set LITELLM_BASE_URL=http://localhost:4000 in your .env.dev")
    uvicorn.run("src.testing.mock_llm:app", host="0.0.0.0", port=4000, reload=True)
