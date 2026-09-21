"""The Jarvis voice for the Dispatch: local neural TTS via Kokoro (ONNX).

the operator's rule for the voice was "use the jarvis voice" — the voice assistant project speaks
with Kokoro `bm_george` (British male, en-gb), a fully LOCAL neural model, and
so does a sibling project. This is the ecosystem's shared voice; nothing leaves the Mac.

We reuse the model weights that already live in the voice assistant project
(models/kokoro/) rather than duplicating 340MB — the path is overridable via
DISPATCH_KOKORO_DIR. The model is CPU/RAM only (~400MB resident once loaded),
so it never competes with ComfyUI for GPU memory.

EVERYTHING here is fail-soft: if the library or the weights are missing, if a
render throws, if playback fails — `speak()` returns False and the caller
falls straight back to macOS `say`. The wall must never go mute over a voice
upgrade.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import wave
from pathlib import Path

from .. import applog

log = applog.get(__name__)

# Shared weights from the voice assistant project by default (no 340MB duplicate). Override
# with DISPATCH_KOKORO_DIR to point somewhere self-contained.
KOKORO_DIR = Path(
    os.environ.get(
        "DISPATCH_KOKORO_DIR",
        str(Path.home() / "AI/experiments/the voice assistant project/models/kokoro"),
    )
)
_MODEL = KOKORO_DIR / "kokoro-v0_19.onnx"
_VOICES = KOKORO_DIR / "voices.json"
_RATE = 24_000  # Kokoro's native sample rate

_kokoro = None  # lazily loaded, then kept resident for the daemon's lifetime
_load_failed = False

# Pre-rendered clips awaiting playback, keyed by the exact (text, voice, lang,
# speed) that will be asked for at speak time. Lets the scheduled bulletin be
# synthesized ~30s ahead of its slot (dispatch_speak.maybe_prerender) so "Top
# of the hour" flows straight into the news instead of pausing on Kokoro's
# render time. A cache miss (config changed, or the text drifted because more
# alerts arrived in the last 30s) just falls through to live synthesis below
# — this is a latency optimization, never a correctness dependency.
_prerendered: dict[tuple[str, str, str, float], str] = {}


def available() -> bool:
    """Cheap preflight — the weights are present and the lib imports. Does NOT
    load the 310MB model (that waits for the first real utterance)."""
    if _load_failed:
        return False
    if not (_MODEL.exists() and _VOICES.exists()):
        return False
    try:
        import kokoro_onnx  # noqa: F401
    except Exception:  # noqa: BLE001 — treat any import problem as "unavailable"
        return False
    return True


def _engine():
    """Load once, keep resident. Returns the Kokoro instance or None on failure
    (and latches _load_failed so we don't retry a broken load every bulletin)."""
    global _kokoro, _load_failed
    if _kokoro is not None:
        return _kokoro
    if _load_failed:
        return None
    try:
        from kokoro_onnx import Kokoro

        _kokoro = Kokoro(str(_MODEL), str(_VOICES))
        return _kokoro
    except Exception as exc:  # noqa: BLE001
        _load_failed = True
        log.error("kokoro load failed (%r) — falling back to say", exc)
        return None


def _render_to_wav(text: str, voice: str, lang: str, speed: float) -> str | None:
    """Synthesize `text` and write it to a fresh temp WAV file, returning its
    path (caller owns cleanup) or None on any failure. The one place that
    actually calls into the Kokoro model — shared by prerender() and the
    live-synthesis path in speak()."""
    import numpy as np

    eng = _engine()
    if eng is None:
        return None
    try:
        samples, sr = eng.create(text, voice=voice, speed=speed, lang=lang)
        pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fh:
            wav_path = fh.name
        with wave.open(wav_path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr or _RATE)
            w.writeframes(pcm.tobytes())
        return wav_path
    except Exception as exc:  # noqa: BLE001 — never let TTS crash the daemon
        log.error("kokoro render failed (%r)", exc)
        return None


def prerender(
    text: str, voice: str = "bm_george", lang: str = "en-gb", speed: float = 1.0
) -> None:
    """Synthesize `text` now and stash it so a later speak() call with the
    same (text, voice, lang, speed) plays back instantly instead of paying
    Kokoro's render time at speak time. Fire-and-forget: any failure here
    just means that clip falls back to live synthesis later, same as if it
    had never been attempted."""
    if not available():
        return
    key = (text, voice, lang, speed)
    if key in _prerendered:
        return
    wav_path = _render_to_wav(text, voice, lang, speed)
    if wav_path:
        _prerendered[key] = wav_path


def clear_prerendered() -> None:
    """Drop any cached clips nobody claimed (a skipped bulletin, a slot whose
    text ended up not matching what was prerendered) so temp WAVs never pile
    up. Safe to call any time — playback itself already pops+deletes the
    entries it uses."""
    for wav_path in _prerendered.values():
        try:
            os.unlink(wav_path)
        except OSError:
            pass
    _prerendered.clear()


def speak(
    text: str, voice: str = "bm_george", lang: str = "en-gb", speed: float = 1.0
) -> bool:
    """Say `text` with Kokoro and play it. Returns True on success, False on
    ANY failure so the caller can fall back to `say`. Synchronous: blocks
    until playback finishes, exactly like `say` does. Plays a prerendered
    clip instantly if one is cached for this exact (text, voice, lang,
    speed); otherwise renders live, same as always."""
    key = (text, voice, lang, speed)
    cached = _prerendered.pop(key, None)
    if cached:
        try:
            subprocess.run(["afplay", cached], check=False, timeout=180)
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("cached playback failed (%r) — re-rendering", exc)
        finally:
            try:
                os.unlink(cached)
            except OSError:
                pass

    wav_path = _render_to_wav(text, voice, lang, speed)
    if wav_path is None:
        return False
    try:
        subprocess.run(["afplay", wav_path], check=False, timeout=180)
        return True
    finally:
        try:
            os.unlink(wav_path)
        except OSError:
            pass


def speak_or_say(
    text: str,
    *,
    engine: str = "kokoro",
    voice: str = "bm_george",
    lang: str = "en-gb",
    speed: float = 1.0,
    say_voice: str = "Daniel",
    say_rate: int = 172,
) -> None:
    """Say `text` aloud, preferring the Jarvis voice and falling back to macOS
    `say` on any failure — the ONE code path shared by every voice consumer
    (the scheduled speaker daemon, scripts/dispatch_speak.py, AND the board's
    on-demand "read the news" button) so the fallback rule can't drift between
    them."""
    if engine == "kokoro" and available():
        if speak(text, voice=voice, lang=lang, speed=speed):
            return
    subprocess.run(
        ["say", "-v", say_voice, "-r", str(say_rate), text], check=False, timeout=120
    )
