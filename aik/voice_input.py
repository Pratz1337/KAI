"""Reusable voice recognition module — Sarvam AI with Google fallback.

Extracted from tools/voice_type_terminal.py for use in the glass overlay mic button.

Usage::

    vr = VoiceRecognizer(sarvam_api_key="sk_...")
    text = vr.recognize_once()  # blocks until speech captured + transcribed
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

log = logging.getLogger("aik.voice_input")


# ── Sarvam helpers ───────────────────────────────────────────────────────────

def _extract_sarvam_text(response: object) -> str:
    """Extract transcript text from a Sarvam AI response object."""
    if response is None:
        return ""
    if isinstance(response, str):
        return response.strip()
    if isinstance(response, dict):
        for key in ("transcript", "text", "output_text"):
            value = response.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""
    for key in ("transcript", "text", "output_text"):
        value = getattr(response, key, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _try_sarvam_transcribe(
    sarvam_client: object,
    file_handle: object,
    model: str,
    mode: str,
    language_code: str,
) -> object:
    """Call Sarvam transcription API with automatic parameter fallback."""
    attempts = (
        {"model": model, "mode": mode, "language_code": language_code},
        {"model": model, "mode": mode},
        {"model": model, "language_code": language_code},
    )
    last_error: Exception | None = None
    for kwargs in attempts:
        try:
            file_handle.seek(0)
            return sarvam_client.speech_to_text.transcribe(file=file_handle, **kwargs)
        except TypeError as exc:
            last_error = exc

    fallback_model = "saarika:v2.5" if model == "saaras:v3" else model
    fallback_attempts = (
        {"model": fallback_model, "language_code": language_code},
        {"model": fallback_model, "mode": mode},
    )
    for kwargs in fallback_attempts:
        try:
            file_handle.seek(0)
            return sarvam_client.speech_to_text.transcribe(file=file_handle, **kwargs)
        except TypeError as exc:
            last_error = exc

    if last_error is not None:
        raise last_error
    raise RuntimeError("No valid Sarvam transcription method found.")


# ── Public class ─────────────────────────────────────────────────────────────

class VoiceRecognizer:
    """Microphone capture + Sarvam AI / Google STT transcription.

    Lazy-initialises the microphone and recognizer on first call so
    construction is cheap and safe even when audio hardware is absent.
    """

    def __init__(
        self,
        provider: str = "sarvam",
        sarvam_api_key: str = "",
        sarvam_model: str = "saaras:v3",
        language_codes: list[str] | None = None,
    ) -> None:
        self._provider = provider
        self._sarvam_model = sarvam_model
        self._language_codes = language_codes or ["en-IN"]
        self._sarvam_client: object | None = None
        self._recognizer: object | None = None
        self._mic: object | None = None
        self._calibrated = False

        if provider == "sarvam" and sarvam_api_key:
            try:
                from sarvamai import SarvamAI
                self._sarvam_client = SarvamAI(api_subscription_key=sarvam_api_key)
            except Exception as exc:
                log.warning("Sarvam SDK init failed: %s — voice may fall back to Google", exc)

    # ── properties ───────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        """True if the recognizer can process audio."""
        if self._provider == "google":
            return True
        return self._sarvam_client is not None

    # ── public API ───────────────────────────────────────────────────

    def recognize_once(
        self,
        *,
        timeout: float = 8.0,
        phrase_time_limit: float = 7.0,
    ) -> str | None:
        """Capture one phrase from microphone and return transcription text.

        Blocks until speech is captured (up to *timeout* seconds).
        Returns ``None`` if nothing was understood.
        """
        import speech_recognition as sr  # noqa: local import keeps module light

        self._ensure_ready()
        assert self._recognizer is not None
        assert self._mic is not None

        with self._mic as source:
            audio = self._recognizer.listen(
                source, timeout=timeout, phrase_time_limit=phrase_time_limit,
            )

        if self._provider == "google":
            return self._recognize_google(audio)
        return self._recognize_sarvam(audio)

    # ── private ──────────────────────────────────────────────────────

    def _ensure_ready(self) -> None:
        """Lazy-init microphone and recognizer on first use."""
        if self._recognizer is None:
            import speech_recognition as sr
            self._recognizer = sr.Recognizer()
        if self._mic is None:
            import speech_recognition as sr
            self._mic = sr.Microphone()
        if not self._calibrated:
            with self._mic as source:
                self._recognizer.adjust_for_ambient_noise(source, duration=0.6)
            self._calibrated = True

    def _recognize_google(self, audio: object) -> str | None:
        import speech_recognition as sr

        for code in self._language_codes:
            try:
                text = self._recognizer.recognize_google(audio, language=code)
                if isinstance(text, str) and text.strip():
                    return text.strip()
            except sr.UnknownValueError:
                continue
        return None

    def _recognize_sarvam(self, audio: object) -> str | None:
        if self._sarvam_client is None:
            log.error("Sarvam client not initialised — cannot transcribe")
            return None

        wav_data = audio.get_wav_data()
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(wav_data)
                tmp_path = Path(f.name)

            with tmp_path.open("rb") as fh:
                for code in self._language_codes:
                    try:
                        response = _try_sarvam_transcribe(
                            self._sarvam_client, fh, self._sarvam_model,
                            "transcribe", code,
                        )
                        text = _extract_sarvam_text(response)
                        if text:
                            return text
                    except Exception as exc:
                        log.warning("Sarvam transcribe failed for lang=%s: %s", code, exc)
            return None
        finally:
            if tmp_path is not None and tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
