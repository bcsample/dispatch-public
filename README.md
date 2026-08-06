# Dispatch

A self-hosted, local-first news and situational-awareness dashboard. Runs
entirely on your own machine — no cloud account, no subscription required to
get started, and nothing you don't explicitly opt into ever leaves your
network.

## What it does

- **Aggregates news** from free Google News RSS search + curated topic feeds
  (breaking, world, business, tech, ...), with an optional NewsAPI.ai key for
  extra sources if you want them. Headlines are automatically clustered —
  the same story from five outlets collapses into one line with a source
  count, not five near-duplicate entries.
- **Detects convergence** — when a place suddenly has multiple stories,
  a coverage spike, *and* it matches something you care about all at once,
  it surfaces as a distinct "BREAKING" event on the ticker, not just another
  headline in the pile.
- **Tracks world events on a live map** — earthquakes, wildfires, sanctions
  list changes, known-exploited-vulnerability disclosures — click any dot
  for the full story. A "what changed since you last looked" ticker, not
  just a static headline list.
- **Shows nearby aircraft in real time** via the free adsb.fi API, no signup
  required (optional OpenSky credentials for a higher rate limit) — flight
  number, route, and altitude, updating live on its own radar-style map.
- **Speaks a scheduled voice bulletin** (local neural TTS, nothing sent to
  any cloud) at the top and bottom of every hour, or on demand via a button
  on the board — respecting quiet hours, meetings (mic-in-use detection),
  and Focus/Do Not Disturb automatically.
- **Optionally ranks headlines by relevance** to your own interests using a
  local Ollama model — entirely offline, tunable via one config file
  (`config/profile.yaml`).

Everything above is independently optional. The dashboard runs and shows
something useful with zero configuration; every feature beyond the free
Google News feed is opt-in via an API key, a config value, or just leaving
it turned off.

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env                        # fill in whatever keys you want (all optional)
cp config/profile.yaml.example config/profile.yaml   # edit to your own interests
.venv/bin/python -m brief.window
```

Then open <http://localhost:8808/>.

**See [INSTALL.md](INSTALL.md) for the full setup guide**, including the
voice/TTS setup, running it as a background service at login, and what each
config file controls.

## Architecture, briefly

Python 3, FastAPI + uvicorn, SQLite. No build step for the frontend — the
dashboard is a single server-rendered HTML page with plain JS modules. No
required cloud dependency: with zero API keys configured, the dashboard still
runs on the free Google News RSS feed and the free World Delta sources.

```
Sources (RSS, Google News, world-event feeds, aircraft)
        │
   ingest → dedupe → optional relevance scoring (local LLM) → store
        │
   ┌────┴─────────────┬─────────────────────┐
   ▼                   ▼                     ▼
Dashboard board    Scheduled voice        On-demand voice
(live, 10s poll)   bulletin (local TTS)   (click a button)
```

## Configuration

- `config/profile.yaml` (copy from `.example`) — your interests, used for
  optional headline relevance scoring and the spoken briefing tone.
- `config/window.yaml` — service settings: ports, fetch intervals, quiet
  hours, which voice/TTS engine to use. Heavily commented; read through it
  once.
- `config/sources.yaml` / `config/news_firehose.yaml` — the RSS feed roster.
  Add or remove feeds freely, no code changes needed.
- `.env` (copy from `.env.example`) — API keys. Every single one is optional.

## Tests

```bash
.venv/bin/python -m pytest -q
```

## License

MIT — see [LICENSE](LICENSE).
