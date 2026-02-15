from __future__ import annotations

import base64
import json
from dataclasses import dataclass

import random
import time

import httpx


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
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._anthropic_version = anthropic_version
        self._timeout_s = timeout_s

    def create_message(
        self,
        *,
        system: str,
        user_text: str,
        image_png: bytes | None,
        max_tokens: int = 600,
        temperature: float = 0.2,
    ) -> AnthropicResponse:
        messages = self._build_single_user_message(user_text=user_text, image_png=image_png)
        payload = self._build_payload(
            system=system,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )

        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": self._anthropic_version,
            "content-type": "application/json",
        }

        data = self._post_with_retries(path="/v1/messages", headers=headers, payload=payload)

        text = _extract_text(data)
        return AnthropicResponse(raw=data, text=text)

    def create_message_with_history(
        self,
        *,
        system: str,
        messages: list[dict],
        max_tokens: int = 600,
        temperature: float = 0.2,
    ) -> AnthropicResponse:
        payload = self._build_payload(
            system=system,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )

        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": self._anthropic_version,
            "content-type": "application/json",
        }

        data = self._post_with_retries(path="/v1/messages", headers=headers, payload=payload)

        text = _extract_text(data)
        return AnthropicResponse(raw=data, text=text)

    def _post_with_retries(self, *, path: str, headers: dict, payload: dict) -> dict:
        url = f"{self._base_url}{path}"
        backoff_s = 1.0
        last_exc: Exception | None = None
        with httpx.Client(timeout=self._timeout_s) as client:
            for attempt in range(1, 9):
                try:
                    resp = client.post(url, headers=headers, json=payload)
                    if resp.status_code in {429, 500, 502, 503, 504, 529}:
                        retry_after = resp.headers.get("retry-after")
                        if retry_after:
                            try:
                                backoff_s = max(backoff_s, float(retry_after))
                            except ValueError:
                                pass
                        # Add a bit of jitter to avoid thundering herd.
                        jitter = random.uniform(0.0, min(1.0, backoff_s / 3.0))
                        _sleep_interruptibly(backoff_s + jitter)
                        backoff_s = min(backoff_s * 2.0, 30.0)
                        continue
                    resp.raise_for_status()
                    return resp.json()
                except Exception as exc:
                    last_exc = exc
                    jitter = random.uniform(0.0, min(1.0, backoff_s / 3.0))
                    _sleep_interruptibly(backoff_s + jitter)
                    backoff_s = min(backoff_s * 2.0, 30.0)

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Anthropic request failed with unknown error")

    def _build_payload(
        self,
        *,
        system: str,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
    ) -> dict:
        return {
            "model": self._model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": messages,
        }

    @staticmethod
    def _build_single_user_message(*, user_text: str, image_png: bytes | None) -> list[dict]:
        content: list[dict] = []
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
        return [{"role": "user", "content": content}]


def _sleep_interruptibly(total_seconds: float) -> None:
    """Sleep in small chunks so Ctrl+C interrupts quickly and without long hangs."""
    remaining = max(0.0, float(total_seconds))
    while remaining > 0:
        chunk = 0.25 if remaining > 0.25 else remaining
        time.sleep(chunk)
        remaining -= chunk


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

