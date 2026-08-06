"""The local neural voice (Kokoro) and its fail-soft fallback to macOS `say`.

The hard rule: a voice problem must never mute the wall. So kokoro_tts is
fail-soft everywhere, and the speaker falls straight through to `say` when the
neural model is disabled, absent, or errors. These tests never load the 310MB
model — they exercise the branches, not the weights.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from brief.window import kokoro_tts

SPEAK_PATH = Path(__file__).resolve().parents[1] / "scripts" / "dispatch_speak.py"


@pytest.fixture(autouse=True)
def _clear_prerender_cache():
    # _prerendered is module-level state shared across every test in this
    # file -- reset it so one test's cached clip can't leak into the next.
    kokoro_tts._prerendered.clear()
    yield
    kokoro_tts._prerendered.clear()


def _load_speaker():
    spec = importlib.util.spec_from_file_location("dispatch_speak", SPEAK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- kokoro_tts preflight is fail-soft --------------------------------------


def test_available_false_when_weights_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(kokoro_tts, "_MODEL", tmp_path / "nope.onnx")
    monkeypatch.setattr(kokoro_tts, "_VOICES", tmp_path / "nope.json")
    monkeypatch.setattr(kokoro_tts, "_load_failed", False)
    assert kokoro_tts.available() is False


def test_available_true_when_weights_present(monkeypatch, tmp_path):
    m, v = tmp_path / "k.onnx", tmp_path / "voices.json"
    m.write_text("x")
    v.write_text("x")
    monkeypatch.setattr(kokoro_tts, "_MODEL", m)
    monkeypatch.setattr(kokoro_tts, "_VOICES", v)
    monkeypatch.setattr(kokoro_tts, "_load_failed", False)
    # kokoro_onnx is installed in the venv, so with files present -> available.
    assert kokoro_tts.available() is True


def test_speak_returns_false_when_engine_wont_load(monkeypatch):
    # A broken/absent model must yield False (caller falls back), never raise.
    monkeypatch.setattr(kokoro_tts, "_engine", lambda: None)
    assert kokoro_tts.speak("hello") is False


# --- speak_or_say: the ONE fallback path shared by every voice consumer -----
# (the scheduled speaker AND the board's on-demand "read the news" button)


def test_speak_or_say_uses_kokoro_when_available(monkeypatch):
    calls = {}
    monkeypatch.setattr(kokoro_tts, "available", lambda: True)
    monkeypatch.setattr(
        kokoro_tts, "speak", lambda *a, **k: calls.setdefault("kokoro", True) or True
    )
    monkeypatch.setattr(
        kokoro_tts.subprocess, "run", lambda *a, **k: calls.setdefault("say", True)
    )
    kokoro_tts.speak_or_say("hello")
    assert "kokoro" in calls and "say" not in calls


def test_speak_or_say_falls_back_when_unavailable(monkeypatch):
    said = {}
    monkeypatch.setattr(kokoro_tts, "available", lambda: False)
    monkeypatch.setattr(
        kokoro_tts.subprocess, "run", lambda a, **k: said.setdefault("cmd", a)
    )
    kokoro_tts.speak_or_say("hello", say_voice="Daniel", say_rate=172)
    assert said["cmd"] == ["say", "-v", "Daniel", "-r", "172", "hello"]


def test_speak_or_say_engine_say_skips_kokoro_entirely(monkeypatch):
    # engine="say" (an explicit config override) must never even ask kokoro.
    said = {}
    monkeypatch.setattr(
        kokoro_tts,
        "available",
        lambda: (_ for _ in ()).throw(AssertionError("should not be called")),
    )
    monkeypatch.setattr(
        kokoro_tts.subprocess, "run", lambda a, **k: said.setdefault("cmd", a)
    )
    kokoro_tts.speak_or_say("hello", engine="say")
    assert said["cmd"][0] == "say"


# --- the speaker prefers Kokoro, falls back to say --------------------------


def test_speaker_uses_kokoro_when_available(monkeypatch):
    spk = _load_speaker()
    calls = {}
    monkeypatch.setattr(spk.kokoro_tts, "available", lambda: True)
    monkeypatch.setattr(
        spk.kokoro_tts,
        "speak",
        lambda *a, **k: calls.setdefault("kokoro", (a, k)) or True,
    )
    monkeypatch.setattr(
        spk.kokoro_tts.subprocess, "run", lambda *a, **k: calls.setdefault("say", a)
    )
    spk.speak("Top of the hour.")
    assert "kokoro" in calls  # spoken by the neural voice
    assert "say" not in calls  # and `say` was NOT used


def test_speaker_falls_back_to_say_when_kokoro_unavailable(monkeypatch):
    spk = _load_speaker()
    said = {}
    monkeypatch.setattr(spk.kokoro_tts, "available", lambda: False)
    monkeypatch.setattr(
        spk.kokoro_tts.subprocess, "run", lambda a, **k: said.setdefault("cmd", a)
    )
    spk.speak("Top of the hour.")
    assert said["cmd"][:3] == ["say", "-v", "Daniel"]


def test_speaker_falls_back_when_kokoro_render_fails(monkeypatch):
    spk = _load_speaker()
    said = {}
    monkeypatch.setattr(spk.kokoro_tts, "available", lambda: True)
    monkeypatch.setattr(spk.kokoro_tts, "speak", lambda *a, **k: False)  # render failed
    monkeypatch.setattr(
        spk.kokoro_tts.subprocess, "run", lambda a, **k: said.setdefault("cmd", a)
    )
    spk.speak("Top of the hour.")
    assert said["cmd"][0] == "say"  # fell through to the safety net


# --- prerender cache: the "no gap after Top of the hour" optimization -------
# maybe_prerender (dispatch_speak.py) synthesizes a bulletin ~30s early and
# stashes it here; speak() at the real slot should play that clip instantly
# instead of paying Kokoro's render time again.


def test_prerender_caches_and_speak_plays_it_without_rendering_again(monkeypatch, tmp_path):
    monkeypatch.setattr(kokoro_tts, "available", lambda: True)
    wav = tmp_path / "clip.wav"
    wav.write_bytes(b"RIFF....")
    render_calls = []
    monkeypatch.setattr(
        kokoro_tts,
        "_render_to_wav",
        lambda text, voice, lang, speed: render_calls.append(text) or str(wav),
    )
    play_calls = []
    monkeypatch.setattr(
        kokoro_tts.subprocess, "run", lambda a, **k: play_calls.append(a)
    )
    monkeypatch.setattr(kokoro_tts.os, "unlink", lambda p: None)

    kokoro_tts.prerender("Big story.", voice="bm_george", lang="en-gb", speed=1.0)
    assert render_calls == ["Big story."]  # rendered exactly once, ahead of time

    ok = kokoro_tts.speak("Big story.", voice="bm_george", lang="en-gb", speed=1.0)
    assert ok is True
    assert render_calls == ["Big story."]  # NOT rendered again at speak time
    assert play_calls and play_calls[0][0] == "afplay"


def test_prerender_is_a_noop_when_kokoro_unavailable(monkeypatch):
    monkeypatch.setattr(kokoro_tts, "available", lambda: False)
    monkeypatch.setattr(
        kokoro_tts,
        "_render_to_wav",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not render")),
    )
    kokoro_tts.prerender("Big story.")  # must not raise, must not cache anything
    assert ("Big story.", "bm_george", "en-gb", 1.0) not in kokoro_tts._prerendered


def test_speak_falls_back_to_live_render_on_cache_miss(monkeypatch, tmp_path):
    # A cache miss (nothing prerendered, or the text drifted) must still work
    # exactly like before -- render live, right at speak time.
    wav = tmp_path / "live.wav"
    wav.write_bytes(b"RIFF....")
    monkeypatch.setattr(kokoro_tts, "_render_to_wav", lambda *a, **k: str(wav))
    play_calls = []
    monkeypatch.setattr(
        kokoro_tts.subprocess, "run", lambda a, **k: play_calls.append(a)
    )
    monkeypatch.setattr(kokoro_tts.os, "unlink", lambda p: None)

    ok = kokoro_tts.speak("Never prerendered.")
    assert ok is True
    assert play_calls and play_calls[0][0] == "afplay"


def test_clear_prerendered_drops_unclaimed_cache_entries(monkeypatch, tmp_path):
    wav = tmp_path / "unclaimed.wav"
    wav.write_bytes(b"RIFF....")
    monkeypatch.setattr(kokoro_tts, "available", lambda: True)
    monkeypatch.setattr(kokoro_tts, "_render_to_wav", lambda *a, **k: str(wav))

    kokoro_tts.prerender("Never spoken.")
    assert ("Never spoken.", "bm_george", "en-gb", 1.0) in kokoro_tts._prerendered
    assert wav.exists()

    kokoro_tts.clear_prerendered()
    assert kokoro_tts._prerendered == {}
    assert not wav.exists()  # temp file cleaned up, not left behind
