"""Shared OpenRouter (OpenAI-compatible) chat transport.

This is the one tested place that talks to the LLM, factored out of
:mod:`intake` so both intake and the recipe features (:mod:`recipes_llm`) reuse
the same request shape, JSON parsing, and error handling. The runtime stays
dependency-free (stdlib :mod:`urllib`), matching the project rule.

The HTTP call is isolated behind an injectable ``transport`` so callers can be
unit-tested without a network or an API key.
"""

import json
from typing import Any, Callable, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

Transport = Callable[[str, Dict[str, str], bytes, int], str]


class LlmError(RuntimeError):
    """A recoverable LLM failure (bad model output, transport error)."""


class LlmNotConfigured(LlmError):
    """Raised when the LLM is used without an OpenRouter API key configured."""


def post_chat_json(
    *,
    settings: Any,
    model: str,
    system: str,
    user: str,
    transport: Optional[Transport] = None,
    timeout: int = 30,
) -> dict:
    """POST one chat completion (``response_format=json_object``) and return the
    parsed JSON object the model produced.

    ``model`` is passed explicitly so callers can override the configured
    default (the recipe features let the client pick a model). Raises
    :class:`LlmNotConfigured` when no API key is set and :class:`LlmError` for
    transport/parse failures or an error envelope from OpenRouter.
    """
    if not getattr(settings, "openrouter_api_key", None):
        raise LlmNotConfigured(
            "this feature needs FOODBRAIN_OPENROUTER_API_KEY to be set"
        )

    body = json.dumps(
        {
            "model": model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
    ).encode("utf-8")

    url = settings.openrouter_base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
        "X-Title": "FoodBrain",
    }
    send = transport or http_post
    raw = send(url, headers, body, timeout)
    content = extract_message_content(raw)
    return parse_json_object(content)


def http_post(
    url: str, headers: Dict[str, str], body: bytes, timeout: int, retries: int = 1
) -> str:
    request = Request(url, data=body, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8")
    except HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8")[:300]
        except Exception:  # pragma: no cover - best-effort error detail
            pass
        raise LlmError(
            f"OpenRouter request failed with HTTP {exc.code}: {detail}".rstrip(": ")
        ) from exc
    except URLError as exc:
        # A completion has no side effects, so a transient transport failure
        # (timeout / connection refused) is safe to retry once.
        if retries > 0:
            return http_post(url, headers, body, timeout, retries=retries - 1)
        raise LlmError(f"OpenRouter request failed: {exc.reason}") from exc


def extract_message_content(raw: str) -> str:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LlmError("OpenRouter response was not valid JSON") from exc
    if isinstance(payload, dict) and payload.get("error"):
        error = payload["error"]
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise LlmError(f"OpenRouter error: {message}")
    try:
        return payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LlmError("OpenRouter response had no message content") from exc


def parse_json_object(content: str) -> dict:
    data = json.loads(strip_fences(content)) if content else {}
    if not isinstance(data, dict):
        raise LlmError("model did not return a JSON object")
    return data


def strip_fences(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else text
        if text.endswith("```"):
            text = text[: -len("```")]
        # Drop a leading language tag like "json" left on the first line.
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[len("json") :]
    return text.strip()
