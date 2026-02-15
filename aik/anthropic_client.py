from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass

import random
import time

import httpx

log = logging.getLogger("aik.anthropic_client")


@dataclass(frozen=True)
class AnthropicResponse:
    raw: dict
    text: str


class AnthropicClient:
    """
    Minimal Anthropic Messages API client with round-robin API key load balancing.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        extra_api_keys: list[str] | None = None,
        base_url: str = "https://api.anthropic.com",
        anthropic_version: str = "2023-06-01",
        timeout_s: float = 60.0,
    ) -> None:
        # Build the key pool — deduplicate while preserving order
        all_keys: list[str] = []
        seen: set[str] = set()
        for k in [api_key] + (extra_api_keys or []):
            k = k.strip()
            if k and k not in seen:
                all_keys.append(k)
                seen.add(k)
        self._api_keys = all_keys
        self._key_index = 0
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._anthropic_version = anthropic_version
        self._timeout_s = timeout_s
        # Track per-key rate-limit state: key -> earliest usable time
        self._key_cooldowns: dict[str, float] = {}
        log.info("Anthropic client initialised with %d API key(s)", len(self._api_keys))

    @property
    def _api_key(self) -> str:
        """Return the next usable key via round-robin, skipping rate-limited ones."""
        now = time.monotonic()
        n = len(self._api_keys)
        for _ in range(n):
            key = self._api_keys[self._key_index % n]
            cooldown = self._key_cooldowns.get(key, 0.0)
            if now >= cooldown:
                return key
            self._key_index = (self._key_index + 1) % n
        # All keys on cooldown — return the one with the soonest cooldown
        return min(self._api_keys, key=lambda k: self._key_cooldowns.get(k, 0.0))

    def _rotate_key(self) -> None:
        """Advance to the next key in the pool."""
        self._key_index = (self._key_index + 1) % len(self._api_keys)

    def _mark_key_rate_limited(self, key: str, backoff_s: float) -> None:
        """Mark a key as rate-limited until now + backoff."""
        self._key_cooldowns[key] = time.monotonic() + backoff_s
        log.info("Key ...%s rate-limited for %.1fs, rotating", key[-6:], backoff_s)

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
                # Pick the best key for this attempt
                current_key = self._api_key
                headers["x-api-key"] = current_key
                try:
                    resp = client.post(url, headers=headers, json=payload)
                    if resp.status_code in {429, 529}:
                        # Rate limited — mark this key and rotate immediately
                        retry_after = resp.headers.get("retry-after")
                        cooldown = backoff_s
                        if retry_after:
                            try:
                                cooldown = max(backoff_s, float(retry_after))
                            except ValueError:
                                pass
                        self._mark_key_rate_limited(current_key, cooldown)
                        self._rotate_key()
                        # If we have multiple keys, try the next one right away
                        if len(self._api_keys) > 1:
                            jitter = random.uniform(0.1, 0.5)
                            _sleep_interruptibly(jitter)
                        else:
                            jitter = random.uniform(0.0, min(1.0, backoff_s / 3.0))
                            _sleep_interruptibly(backoff_s + jitter)
                        backoff_s = min(backoff_s * 2.0, 30.0)
                        continue
                    if resp.status_code in {500, 502, 503, 504}:
                        jitter = random.uniform(0.0, min(1.0, backoff_s / 3.0))
                        _sleep_interruptibly(backoff_s + jitter)
                        backoff_s = min(backoff_s * 2.0, 30.0)
                        continue
                    resp.raise_for_status()
                    return resp.json()
                except Exception as exc:
                    last_exc = exc
                    self._rotate_key()
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

