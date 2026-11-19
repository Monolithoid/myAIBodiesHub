#!/usr/bin/env python3
"""
speech_services.py — Transcription (Whisper ASR) and speech synthesis (edge-tts)
shared by the body drivers (naoqi_driver, static_driver).

Both calls are blocking: the ROS 2 nodes run them in a thread so as not to
block the rclpy loop.

    text = transcribe(WHISPER_URL, audio_bytes)     # raises WhisperError
    if is_meaningful(text): ...
    mp3 = synthesize_mp3(text, "fr-FR-DeniseNeural") # raises ImportError if edge-tts is missing
    seconds = mp3_duration(mp3)                      # sentence duration, None if unreadable
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


# ─────────────────────────────────────────────────────────────────────────────
# Durée d'un MP3 (pour caler les gestes du NAO sur la parole)
# ─────────────────────────────────────────────────────────────────────────────

# MPEG Layer III : débits (kbit/s) par version, fréquences d'échantillonnage.
_L3_BITRATES = {
    "1": (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    "2": (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
_SAMPLE_RATES = {"1": (44100, 48000, 32000), "2": (22050, 24000, 16000), "2.5": (11025, 12000, 8000)}


def mp3_duration(mp3: bytes) -> float | None:
    """Durée en secondes d'un MP3 (Layer III, celui d'edge-tts), en comptant ses trames.

    Python standard, sans décodage.  None si le fichier n'est pas lisible ainsi :
    l'appelant garde alors son estimation.
    """
    data = memoryview(mp3)
    i, n = 0, len(data)
    if n >= 10 and bytes(data[:3]) == b"ID3":           # étiquette ID3v2 au début
        size = ((data[6] & 0x7F) << 21) | ((data[7] & 0x7F) << 14) | ((data[8] & 0x7F) << 7) | (data[9] & 0x7F)
        i = 10 + size
    seconds, frames = 0.0, 0
    while i + 4 <= n:
        h = int.from_bytes(data[i:i + 4], "big")
        if (h >> 21) & 0x7FF != 0x7FF:
            i += 1                                       # pas une trame : on cherche la suivante
            continue
        version = {0: "2.5", 2: "2", 3: "1"}.get((h >> 19) & 3)
        layer = (h >> 17) & 3
        br_idx, sr_idx, pad = (h >> 12) & 0xF, (h >> 10) & 3, (h >> 9) & 1
        if version is None or layer != 1 or br_idx in (0, 15) or sr_idx == 3:
            i += 1
            continue
        bitrate = _L3_BITRATES["1" if version == "1" else "2"][br_idx] * 1000
        rate = _SAMPLE_RATES[version][sr_idx]
        samples = 1152 if version == "1" else 576
        length = (samples // 8) * bitrate // rate + pad
        if length < 4:
            i += 1
            continue
        seconds += samples / float(rate)
        frames += 1
        i += length
    return round(seconds, 3) if frames else None
