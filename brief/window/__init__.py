"""Open Window (v1 + v2) — the always-on, LLM-free local dashboard.

Two surfaces, two cadences, "Open Window
(v1)"): the 7:56am Apps Script email is the warm, LLM-synthesized snapshot;
this package is the live window you can pull up any time and see the world
*now* — a sweep loop timer over the existing World Delta engine
(`brief/ingest/world.py`, untouched) plus a small FastAPI app that renders
structured data only. v2 (see the plan's "v2" section) widens this to a
full current-state board (not just deltas) and a headlines panel fed by the
existing RSS ingestor (`brief/ingest/rss.py`) on its own lighter cadence.

Iron rule: nothing under this package imports Ollama, `brief/generate.py`,
or any LLM. That's what lets it sweep all day without competing for memory
against ComfyUI/Studio renders (see [[ollama-memory-pressure-rule]]).
"""
