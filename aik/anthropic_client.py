from __future__ import annotations

import base64
import json
import random
import time
from dataclasses import dataclass

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
        last_exc: Exception | None = None

        # Hackathon-friendly retries for transient 429/5xx.
        for attempt in range(1, 6):
            try:
                with httpx.Client(timeout=self._timeout_s) as client:
                    resp = client.post(url, headers=headers, json=payload)

                if resp.status_code in (429, 500, 502, 503, 504):
                    retry_after = resp.headers.get("retry-after")
                    if retry_after and str(retry_after).strip().isdigit():
                        sleep_s = float(int(str(retry_after).strip()))
                    else:
                        # Exponential backoff with jitter.
                        sleep_s = min(20.0, (2 ** (attempt - 1)) * 0.8) + random.random() * 0.4
                    if attempt >= 5:
                        resp.raise_for_status()
                    time.sleep(sleep_s)
                    continue

                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as e:
                last_exc = e
                if attempt >= 5:
                    raise
                time.sleep(min(10.0, attempt * 0.7) + random.random() * 0.3)
        else:
            raise last_exc or RuntimeError("Anthropic request failed")

        text = _extract_text(data)
        return AnthropicResponse(raw=data, text=text)


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

