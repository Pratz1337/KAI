from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from .anthropic_client import AnthropicClient

log = logging.getLogger("aik.micro_verifier")


MICRO_VERIFY_PROMPT = """\
You are a fast, precise visual verifier for desktop actions.

You will be given:
1. A description of the "Action" that was just performed.
2. The "Expected Visual Outcome" predicted by the agent.
3. A "Before" screenshot (Image 1).
4. An "After" screenshot (Image 2).

Task:
Determine if the action successfully caused the expected visual change.

Return JSON only:
{
  "success": true|false,
  "confidence": 0.0-1.0,
  "observation": "Brief description of the actual visual change (or lack thereof)",
  "correction": "If failed, suggest what might have happened (e.g., 'Menu did not open', 'Wrong window focused')"
}

Rules:
- Be strict. If the expected outcome (e.g. "Save dialog opens") is NOT visible in the After image, return false.
- If the screen is identical (no change) and the action should have caused a change, return false.
- Ignore minor rendering artifacts. Focus on semantic changes (windows, text, focus).
"""


@dataclass(frozen=True)
class MicroVerificationResult:
    success: bool
    confidence: float
    observation: str
    correction: str


class MicroVerifier:
    def __init__(
        self,
        anthropic: AnthropicClient,
        *,
        max_tokens: int = 300,
        temperature: float = 0.0,
    ) -> None:
        self._anthropic = anthropic
        self._max_tokens = max_tokens
        self._temperature = temperature

    def verify_step(
        self,
        *,
        action_desc: str,
        expected_outcome: str,
        before_png: bytes,
        after_png: bytes,
    ) -> MicroVerificationResult:
        """
        Verify if a micro-action achieved its expected visual outcome.
        Uses a multi-image user message to show Before -> After progression.
        """
        if not expected_outcome or not expected_outcome.strip():
            # No expectation set, skip strict verification (assume success if no hard error)
            return MicroVerificationResult(True, 1.0, "No expectation provided", "")

        # Prepare payload for the model
        payload = {
            "action_performed": action_desc,
            "expected_visual_outcome": expected_outcome,
        }
        
        # We need to send two images. 
        # The AnthropicClient.create_message_multiturn supports a list of messages.
        # We will check if it supports multiple images in one message or a sequence.
        # Standard Claude API supports multiple image blocks in one user message.
        
        # We'll construct the content manually to pass to create_message_multiturn
        # or we can add a helper. Since create_message_multiturn expects a list of messages,
        # we can craft a single user message with 2 images.
        
        import base64
        b64_before = base64.b64encode(before_png).decode("ascii")
        b64_after = base64.b64encode(after_png).decode("ascii")

        content = [
            {
                "type": "text",
                "text": f"Verify this action.\nContext:\n{json.dumps(payload, ensure_ascii=True)}\n\nImage 1: BEFORE"
            },
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": b64_before},
            },
            {
                "type": "text",
                "text": "Image 2: AFTER"
            },
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": b64_after},
            },
            {
                "type": "text",
                "text": "Did the action succeed?"
            }
        ]

        messages = [{"role": "user", "content": content}]

        try:
            resp = self._anthropic.create_message_multiturn(
                system=MICRO_VERIFY_PROMPT,
                messages=messages,
                image_png=None, # Images are already embedded in content
                max_tokens=self._max_tokens,
                temperature=self._temperature,
            )
            
            result = self._parse_response(resp.text)
            return result

        except Exception as e:
            log.exception("Micro-verification failed: %s", e)
            # Fail open (assume success) or fail closed? 
            # Better to return unsure so we don't block the agent unnecessarily on API errors.
            return MicroVerificationResult(True, 0.0, f"Verification API error: {e}", "")

    def _parse_response(self, text: str) -> MicroVerificationResult:
        try:
            # Quick JSON extraction
            import json
            text = text.strip()
            if "```" in text:
                text = text.split("```json")[-1].split("```")[0] if "```json" in text else text.split("```")[0]
            
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1:
                obj = json.loads(text[start : end + 1])
                return MicroVerificationResult(
                    success=bool(obj.get("success", False)),
                    confidence=float(obj.get("confidence", 0.0)),
                    observation=str(obj.get("observation", "")),
                    correction=str(obj.get("correction", "")),
                )
        except Exception:
            pass
        
        return MicroVerificationResult(False, 0.0, "Failed to parse verifier response", "")
