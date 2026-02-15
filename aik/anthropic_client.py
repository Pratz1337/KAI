from __future__ import annotations

import base64
import json
import logging
import random
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger("aik.anthropic_client")


@dataclass(frozen=True)
class AnthropicResponse:
    raw: dict
    text: str


class AnthropicClient:
    """
    Minimal Anthropic Messages API client (no external SDK dependency).
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        base_url: str = "https://api.anthropic.com",
        anthropic_version: str = "2023-06-01",
        timeout_s: float = 60.0,
        max_retries: int = 4,
        min_interval_s: float = 0.0,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._anthropic_version = anthropic_version
        self._timeout_s = timeout_s
        self._max_retries = int(max_retries)
        self._min_interval_s = float(min_interval_s)
        self._last_request_ts: float | None = None

    def create_message(
        self,
        *,
        system: str,
        user_text: str,
        image_png: bytes | None,
        max_tokens: int = 600,
        temperature: float = 0.2,
    ) -> AnthropicResponse:
        content = []
        if image_png is not None:
            b64 = base64.b64encode(image_png).decode("ascii")
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": b64,
                    },
                }
            )
        content.append({"type": "text", "text": user_text})

        payload = {
            "model": self._model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": [{"role": "user", "content": content}],
        }

        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": self._anthropic_version,
            "content-type": "application/json",
        }

        url = f"{self._base_url}/v1/messages"
        transient_status = {429, 500, 502, 503, 504, 529}

        last_err: Exception | None = None
        with httpx.Client(timeout=self._timeout_s) as client:
            for attempt in range(self._max_retries + 1):
                self._throttle()
                try:
                    resp = client.post(url, headers=headers, json=payload)
                except (httpx.TimeoutException, httpx.NetworkError) as e:
                    last_err = e
                    if attempt >= self._max_retries:
                        raise
                    delay = _compute_backoff_s(attempt)
                    log.warning("Anthropic request failed (%s). Retrying in %.1fs.", type(e).__name__, delay)
                    time.sleep(delay)
                    continue

                if resp.status_code == 200:
                    data = resp.json()
                    text = _extract_text(data)
                    return AnthropicResponse(raw=data, text=text)

                # Retry on transient conditions: rate limiting / overload / intermittent upstream errors.
                if resp.status_code in transient_status and attempt < self._max_retries:
                    retry_after = resp.headers.get("retry-after")
                    delay = _parse_retry_after_s(retry_after) if retry_after else None
                    if delay is None:
                        delay = _compute_backoff_s(attempt)
                    # Add a bit of jitter to avoid thundering herd if multiple runs start together.
                    delay = delay * (0.9 + 0.2 * random.random())
                    body_snip = (resp.text or "").replace("\r", "").replace("\n", " ")[:200]
                    log.warning(
                        "Anthropic HTTP %d. Retrying in %.1fs (attempt %d/%d). Body: %s",
                        resp.status_code,
                        delay,
                        attempt + 1,
                        self._max_retries,
                        body_snip,
                    )
                    time.sleep(delay)
                    continue

                # Non-retryable or out of retries.
                try:
                    resp.raise_for_status()
                except Exception as e:
                    last_err = e
                    raise

        # Should not reach here.
        raise RuntimeError(f"Anthropic request failed: {last_err}")

    def create_message_multiturn(
        self,
        *,
        system: str,
        messages: list[dict],
        image_png: bytes | None = None,
        max_tokens: int = 600,
        temperature: float = 0.2,
    ) -> AnthropicResponse:
        """
        Multi-turn variant: accepts a full list of messages (role/content pairs).

        The *last* user message can optionally have an image prepended to it.
        Prior messages are sent as text-only (keeping token costs manageable).
        """
        api_messages: list[dict] = []

        for i, msg in enumerate(messages):
            role = msg.get("role", "user")
            content = msg.get("content", "")
            is_last = i == len(messages) - 1

            if is_last and role == "user" and image_png is not None:
                # Attach the screenshot to the latest user message only
                b64 = __import__("base64").b64encode(image_png).decode("ascii")
                text_content = content if isinstance(content, str) else str(content)
                api_messages.append({
                    "role": role,
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": b64,
                            },
                        },
                        {"type": "text", "text": text_content},
                    ],
                })
            else:
                # Pass through content as-is if it's already a list (e.g. for multi-image prompts)
                # otherwise normalize to string.
                if isinstance(content, list):
                    final_content = content
                elif isinstance(content, str):
                    final_content = content
                else:
                    final_content = str(content)
                
                api_messages.append({
                    "role": role,
                    "content": final_content,
                })

        payload = {
            "model": self._model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": api_messages,
        }

        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": self._anthropic_version,
            "content-type": "application/json",
        }

        url = f"{self._base_url}/v1/messages"
        transient_status = {429, 500, 502, 503, 504, 529}

        last_err: Exception | None = None
        with httpx.Client(timeout=self._timeout_s) as client:
            for attempt in range(self._max_retries + 1):
                self._throttle()
                try:
                    resp = client.post(url, headers=headers, json=payload)
                except (httpx.TimeoutException, httpx.NetworkError) as e:
                    last_err = e
                    if attempt >= self._max_retries:
                        raise
                    delay = _compute_backoff_s(attempt)
                    log.warning("Anthropic request failed (%s). Retrying in %.1fs.", type(e).__name__, delay)
                    time.sleep(delay)
                    continue

                if resp.status_code == 200:
                    data = resp.json()
                    text = _extract_text(data)
                    return AnthropicResponse(raw=data, text=text)

                if resp.status_code in transient_status and attempt < self._max_retries:
                    retry_after = resp.headers.get("retry-after")
                    delay = _parse_retry_after_s(retry_after) if retry_after else None
                    if delay is None:
                        delay = _compute_backoff_s(attempt)
                    delay = delay * (0.9 + 0.2 * random.random())
                    body_snip = (resp.text or "").replace("\r", "").replace("\n", " ")[:200]
                    log.warning(
                        "Anthropic HTTP %d. Retrying in %.1fs (attempt %d/%d). Body: %s",
                        resp.status_code, delay, attempt + 1, self._max_retries, body_snip,
                    )
                    time.sleep(delay)
                    continue

                try:
                    resp.raise_for_status()
                except Exception as e:
                    last_err = e
                    raise

        raise RuntimeError(f"Anthropic multiturn request failed: {last_err}")

    def _throttle(self) -> None:
        if self._min_interval_s <= 0:
            return
        now = time.monotonic()
        if self._last_request_ts is not None:
            wait = self._min_interval_s - (now - self._last_request_ts)
            if wait > 0:
                time.sleep(wait)
        self._last_request_ts = time.monotonic()


def _extract_text(messages_response: dict) -> str:
    """
    Anthropic messages API returns a list of content blocks; join any text blocks.
    """
    parts: list[str] = []
    for block in messages_response.get("content", []) or []:
        if isinstance(block, dict) and block.get("type") == "text":
            t = block.get("text")
            if isinstance(t, str):
                parts.append(t)
    if parts:
        return "\n".join(parts).strip()

    # Fallback: some clients nest differently; keep it robust.
    if isinstance(messages_response.get("text"), str):
        return str(messages_response["text"]).strip()
    return json.dumps(messages_response, indent=2)


def _compute_backoff_s(attempt: int) -> float:
    # Exponential backoff: 1s, 2s, 4s, 8s... capped.
    base = 1.0
    cap = 20.0
    return min(cap, base * (2**max(0, int(attempt))))


def _parse_retry_after_s(v: str) -> float | None:
    try:
        # Retry-After is typically integer seconds.
        secs = float(v.strip())
        if secs < 0:
            return None
        return min(60.0, secs)
    except Exception:
        return None
