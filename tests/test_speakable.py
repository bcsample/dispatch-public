"""_is_speakable() -- excludes headlines in a non-Latin script from being
spoken (the operator, 2026-07-21: "one of the sources was in Arabic or Farsi, and
the system really choked on that"). Board/ticker display is unaffected;
only speech-selection points check this."""

from __future__ import annotations

from brief.window import service


def test_plain_english_is_speakable():
    assert service._is_speakable("US military identifies soldier killed in Iraq")


def test_arabic_headline_is_not_speakable():
    assert not service._is_speakable("الشرق الأوسط يشهد توترات جديدة")


def test_farsi_headline_is_not_speakable():
    assert not service._is_speakable("تنش‌های جدید در خاورمیانه")


def test_hebrew_headline_is_not_speakable():
    assert not service._is_speakable("המתיחות במזרח התיכון גוברת")


def test_cyrillic_headline_is_not_speakable():
    assert not service._is_speakable("Новые санкции против России вступают в силу")


def test_cjk_headline_is_not_speakable():
    assert not service._is_speakable("中东地区紧张局势升级")


def test_a_lone_embedded_non_latin_word_amid_english_still_speaks():
    # A headline that's overwhelmingly English with one non-Latin-script word
    # embedded (e.g. a name quoted in its native script) shouldn't be
    # silently dropped -- only titles DOMINATED by a non-Latin script are.
    assert service._is_speakable("Iran's supreme leader خامنه‌ای meets with officials")


def test_empty_or_none_title_is_speakable():
    # No letters at all -- nothing to mangle, so don't exclude it outright
    # (the caller's own empty-title handling deals with this separately).
    assert service._is_speakable("")
    assert service._is_speakable(None)
