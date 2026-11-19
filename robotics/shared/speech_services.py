#!/usr/bin/env python3
"""
speech_services.py — Transcription (Whisper ASR) and speech synthesis (edge-tts)
shared by the body drivers (naoqi_driver, static_driver).

Both calls are blocking: the ROS 2 nodes run them in a thread so as not to
block the rclpy loop.

    text = transcribe(WHISPER_URL, audio_bytes)     # raises WhisperError
    if is_meaningful(text): ...
    mp3 = synthesize_mp3(text, "fr-FR-DeniseNeural") # raises ImportError if edge-tts is missing
"""

import asyncio
import io

import requests


class WhisperError(Exception):
    """HTTP error response from the Whisper ASR service."""


def transcribe(whisper_url: str, audio_bytes: bytes, language: str = "fr",
               timeout: float = 30, prompt: str = "") -> str:
    """Sends an audio file to Whisper ASR and returns the text (stripped).

    The onerahmet/openai-whisper-asr-webservice service decodes the audio with
    ffmpeg: WAV, MP3, etc. are accepted. `prompt` (Whisper's initial_prompt)
    guides the spelling: "Ok Nao." helps write the wake word.
    """
    params = {"task": "transcribe", "language": language, "output": "json"}
    if prompt:
        params["initial_prompt"] = prompt
    resp = requests.post(
        f"{whisper_url}/asr",
        params=params,
        files={"audio_file": ("audio.wav", audio_bytes, "audio/wav")},
        timeout=timeout,
    )
    # Whisper returned an HTTP error — the caller logs it and gives up.
    if not resp.ok:
        raise WhisperError(f"Whisper error {resp.status_code}")
    return resp.json().get("text", "").strip()


# What Whisper makes up on noise or silence (video subtitles).
HALLUCINATIONS = ("sous-titr", "merci d'avoir regard", "abonnez-vous", "amara.org")


def is_meaningful(text: str) -> bool:
    """Accept transcription only if it contains meaningful content
    (> 3 chars filters out noise artefacts like "." or "ok", and the usual
    Whisper hallucinations on noise are dropped)."""
    if not text or len(text) <= 3:
        return False
    lower = text.lower()
    return not any(h in lower for h in HALLUCINATIONS)


def synthesize_mp3(text: str, voice: str) -> bytes:
    """Generates an MP3 with edge-tts. Raises ImportError if edge-tts is not installed."""
    import edge_tts

    async def _gen():
        communicate = edge_tts.Communicate(text, voice)
        buf = io.BytesIO()
        async for chunk in communicate.stream():
            # Only audio chunks contain binary data — skip metadata events.
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
        return buf.getvalue()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_gen())
    finally:
        loop.close()
