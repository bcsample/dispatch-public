"""Turn the top-N scored items into the brief. ONE LLM call over the already
code-filtered shortlist — the model only reasons/prioritizes/writes; it never sees
the firehose. That's the cost/quality discipline the whole design hinges on."""

from __future__ import annotations

from datetime import datetime

from . import llm
from .config import load_prompt
from .models import Item


def _format_items(items: list[Item]) -> str:
    lines = []
    for i, it in enumerate(items, 1):
        topics = ", ".join(it.topics + it.entities) or "—"
        lines.append(
            f"[{i}] (score {it.relevance_score}) {it.title}\n"
            f"    source: {it.source_name} ({it.trust} trust) | matched: {topics}\n"
            f"    url: {it.url}\n"
            f"    snippet: {it.raw_text[:400]}"
        )
    return "\n\n".join(lines)


def generate_brief(items: list[Item], profile: dict) -> str:
    if not items:
        return (
            f"# Good morning, {profile.get('honorific','there')}.\n\n"
            "Nothing cleared the relevance threshold from today's sources. Quiet morning."
        )
    system = load_prompt("daily_brief.md").format(
        honorific=profile.get("honorific", "there"),
        persona=profile.get("persona", "a busy professional."),
        flag_programs=", ".join(profile.get("flag_programs", [])) or "(none)",
    )
    user = (
        f"Today is {datetime.now().strftime('%A, %B %d, %Y')}.\n"
        f"Here are {len(items)} pre-scored items. Build the briefing.\n\n"
        f"{_format_items(items)}"
    )
    return llm.chat(system, user)
