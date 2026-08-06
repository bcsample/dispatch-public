# Installation Guide

This guide walks you through getting Dispatch running on your machine. The dashboard works out of the box with zero configuration — every optional feature can be skipped.

## Prerequisites

- **Python 3.11 or later** (macOS, Linux, or Windows with WSL)
- **Git** (to clone the repo)
- **A terminal and text editor** (you'll edit a couple of config files)

That's it. No npm, no build step, no required API keys.

**Optional:** If you want headlines ranked by your interests, you'll need [Ollama](https://ollama.ai) running locally. This is entirely optional — headlines are shown unranked without it.

## Quick Start (5 minutes)

### 1. Clone and set up the virtual environment

```bash
git clone https://github.com/bcsample/dispatch-public.git
cd dispatch-public
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2. Copy the config template

```bash
cp config/profile.yaml.example config/profile.yaml
```

(The profile.yaml file is optional and gitignored, so it's yours alone. Leave it with the example values for now — the dashboard runs fine without any edits.)

### 3. First run

```bash
.venv/bin/python -m brief.window
```

You should see:

```
INFO: Uvicorn running on http://0.0.0.0:8808
```

Open your browser and go to **http://localhost:8808/**. You'll see a live news dashboard pulling from Google News (free, no key required).

To stop the dashboard, press `Ctrl+C`.

---

## Optional Features

Each of these is independent. Skip any you don't want.

### NewsAPI.ai (Extra news sources)

By default, Dispatch pulls news from Google News RSS (free, no quota). For additional sources, you can add a [NewsAPI.ai](https://newsapi.ai) key.

1. Sign up for a free NewsAPI.ai account at https://newsapi.ai
2. Copy your API key
3. Create a `.env` file in the repo root:

```bash
cp .env.example .env
```

4. Edit `.env` and fill in your key:

```
NEWSAPI_KEY=your_key_here
```

5. Restart the dashboard (stop with `Ctrl+C`, then run `python -m brief.window` again)

The dashboard will now fetch from additional news sources on top of Google News.

### NASA FIRMS (Wildfire tracking)

FIRMS tracks active fire detections worldwide. It's free and requires instant signup.

1. Get your map key from https://firms.modaps.eosdis.nasa.gov/api/map_key/
2. Edit `.env` and add:

```
NASA_FIRMS_MAP_KEY=your_key_here
```

3. Restart the dashboard

The "World Delta" panel on the dashboard will now show active wildfires.

### OpenSky Network (Higher flight-tracking rate limit)

The "Skies" board shows nearby aircraft in real time via the free [adsb.fi](https://adsb.fi) API — no signup required. If you register with [OpenSky Network](https://opensky-network.org) (free), you get a higher request rate limit.

1. Sign up for a free OpenSky account
2. Note your client ID and secret
3. Edit `.env`:

```
OPENSKY_CLIENT_ID=your_client_id
OPENSKY_CLIENT_SECRET=your_secret
```

4. Restart the dashboard

Without this, the Skies board still works; it just refreshes less frequently.

### Kokoro Local Voice / TTS Setup

Dispatch can speak headlines and alerts using Kokoro, a local neural text-to-speech engine. Everything runs on your machine — nothing is sent to the cloud.

**Prerequisites:**
- Model weights (about 340 MB, not bundled in this repo)
- The `kokoro-onnx` Python library (already in `requirements.txt`)

**Setup:**

1. **Download the Kokoro model weights.** Search for "Kokoro TTS onnx model weights" and download the weights files (`kokoro-v0_19.onnx` and `voices.json`).

2. **Place the weights where Dispatch expects them:**

   By default, Dispatch looks for the weights in `./models/kokoro/` (relative to the repo root). Create the directory:

   ```bash
   mkdir -p models/kokoro
   ```

   Move the downloaded `kokoro-v0_19.onnx` and `voices.json` files into `models/kokoro/`.

   If you want to store the weights elsewhere, set the environment variable:

   ```bash
   export DISPATCH_KOKORO_DIR=/path/to/your/weights
   ```

   (Or add this to your `.env` file if running as a launchd service.)

3. **Test it:**

   Start the dashboard and click the "Read news" button. You should hear a voice read the latest news.

   If the weights are missing or Kokoro fails to load, the dashboard falls back gracefully to macOS's built-in `say` command (no neural voice, but still usable).

**Voice options:**

The default voice is `bm_george` (British male). You can change it by editing `config/window.yaml`:

```yaml
speak_kokoro_voice: bm_george      # other options: af_bella, af_sarah, am_adam, etc.
speak_kokoro_lang: en-gb           # language code
speak_kokoro_speed: 1.0            # 1.0 = natural speed
```

See the Kokoro library docs for the full list of available voices.

### Local LLM Curation (Headline ranking by your interests)

Dispatch can automatically rank headlines against your interests using a local [Ollama](https://ollama.ai) model. Headlines you care about get a "BEAT" badge and are prioritized.

**Prerequisites:**
- Ollama installed and running locally (`http://127.0.0.1:11434` by default)
- A model pulled in Ollama (e.g., `ollama pull qwen2.5:9b`)

**Setup:**

1. **Install Ollama** from https://ollama.ai and start the service.

2. **Pull a model:**

   ```bash
   ollama pull qwen3.5:9b
   ```

   (That's the built-in default — you can use any model you prefer.)

3. **Edit `config/profile.yaml`** and fill in your interests:

   ```yaml
   honorific: Your Name
   persona: A description of you and what you care about
   primary_topics:
     - your first interest
     - your second interest
   watch_entities:
     - Company or person you follow
   ```

4. **Restart the dashboard.**

   The curation system will now score headlines against your profile. Stories that match get a "BEAT" badge on the board.

**Quiet hours for curation:**

By default, curation runs only from 9 AM to 6 PM. Edit `config/window.yaml`:

```yaml
curation_enabled: true
curation_quiet_start_hour: 18      # 6 PM
curation_quiet_end_hour: 9         # 9 AM
```

**Model tuning:**

To use a different model, edit `config/window.yaml`:

```yaml
curation_model: your-model-name
```

---

## Running as a Background Service (macOS)

This is optional. If you want Dispatch to start automatically at login and keep running in the background, you can use macOS's `launchd`.

### The main dashboard service

1. **Edit the plist file** to replace placeholder paths.

   Open `com.local.dispatch.plist` and replace `/PATH/TO/dispatch-public` with the actual path to your repo. For example, if you cloned it to `/Users/alice/projects/dispatch-public`, the file should read:

   ```xml
   <string>/Users/alice/projects/dispatch-public/.venv/bin/python</string>
   ```

   and

   ```xml
   <string>/Users/alice/projects/dispatch-public</string>
   ```

2. **Install the launchd service:**

   ```bash
   cp com.local.dispatch.plist ~/Library/LaunchAgents/
   launchctl load ~/Library/LaunchAgents/com.local.dispatch.plist
   ```

3. **Verify it's running:**

   ```bash
   launchctl list | grep dispatch
   ```

   You should see `com.local.dispatch` in the list.

4. **View logs:**

   ```bash
   tail -f /tmp/dispatch.out
   tail -f /tmp/dispatch.err
   ```

5. **Restart after code changes:**

   ```bash
   launchctl kickstart -k gui/$(id -u)/com.local.dispatch
   ```

6. **Stop the service:**

   ```bash
   launchctl unload ~/Library/LaunchAgents/com.local.dispatch.plist
   ```

### The speaker service (optional, for automated voice)

The speaker daemon reads headlines and alerts aloud on a schedule (top and bottom of each hour, or when you click "Speak now").

1. **Edit `com.local.dispatch-speaker.plist`** and replace `/PATH/TO/dispatch-public` with your actual repo path.

2. **Install:**

   ```bash
   cp com.local.dispatch-speaker.plist ~/Library/LaunchAgents/
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.local.dispatch-speaker.plist
   ```

3. **Stop it (if it's too noisy):**

   ```bash
   launchctl bootout gui/$(id -u)/com.local.dispatch-speaker
   ```

### The kiosk launcher (optional, for wall display)

If you want the dashboard to open in fullscreen Chrome at login (e.g., for a wall display), use the kiosk launcher.

1. **Edit `com.local.dispatch-kiosk.plist`** and replace `/Users/YOUR_USERNAME` with your actual username (two places).

2. **Install:**

   ```bash
   cp com.local.dispatch-kiosk.plist ~/Library/LaunchAgents/
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.local.dispatch-kiosk.plist
   ```

3. **Quit the kiosk at any time:** Press `Cmd+Q`.

4. **Prevent display sleep:** The kiosk doesn't control power settings. If the screen keeps sleeping, open System Settings > Displays and enable "Prevent automatic sleeping on power adapter", or run `caffeinate -d` in a terminal.

---

## Troubleshooting

### Dashboard won't start

**Check the virtual environment is set up:**

```bash
.venv/bin/python --version
```

Should print `Python 3.11.x` or later. If it fails, reinstall the venv:

```bash
rm -rf .venv
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

**Check the port is free:**

The dashboard binds to port 8808 by default. If something else is using it:

```bash
lsof -i :8808
```

Either stop the other process or change the port in `config/window.yaml`:

```yaml
port: 9000   # use a different port
```

**View the error:**

If the dashboard crashes silently, try:

```bash
.venv/bin/python -m brief.window
```

and read the error message printed to the terminal.

### launchd service isn't running

Check the logs:

```bash
tail -f /tmp/dispatch.out
tail -f /tmp/dispatch.err
```

Common issues:
- Paths in the plist are wrong (must be absolute)
- The virtual environment path doesn't exist
- The working directory path is wrong

### Kokoro voice isn't working

**Check the weights are installed:**

```bash
ls models/kokoro/
```

Should show `kokoro-v0_19.onnx` and `voices.json`.

**Check the library loaded:**

```bash
.venv/bin/python -c "import kokoro_onnx; print('OK')"
```

If it fails, reinstall:

```bash
.venv/bin/pip install --upgrade kokoro-onnx==0.2.3
```

**Falls back to `say`:**

If Kokoro isn't available, Dispatch automatically uses macOS's built-in `say` command instead. You'll see a log message but the dashboard keeps working.

### Ollama isn't feeding the curation

**Check Ollama is running:**

```bash
curl http://127.0.0.1:11434/api/tags
```

Should return a list of models you've pulled.

**Check the model is available:**

The default model is `qwen3.5:9b`. If you don't have it:

```bash
ollama pull qwen3.5:9b
```

**Check the config:**

Make sure `config/window.yaml` has:

```yaml
curation_enabled: true
```

If curation is disabled, it won't run. You can also check the logs:

```bash
tail -f /tmp/dispatch.out | grep curat
```

### Tests are failing

Run the test suite:

```bash
.venv/bin/python -m pytest -q
```

If tests fail, check:
- All required packages are installed: `.venv/bin/pip install -r requirements.txt`
- Python version is 3.11+: `.venv/bin/python --version`

---

## Quick Reference: Editing Common Settings

| What to change | File | Key(s) |
| --- | --- | --- |
| Your location (for aircraft tracking) | `config/window.yaml` | `flights_lat`, `flights_lon` |
| Your interests (for headline ranking) | `config/profile.yaml` | `honorific`, `persona`, `primary_topics`, `watch_entities` |
| Quiet hours (when to stop speaking) | `config/window.yaml` | `speak_quiet_start_hour`, `speak_quiet_end_hour` |
| Dashboard port | `config/window.yaml` | `port` |
| How often to check for news | `config/window.yaml` | `news_interval_seconds` |
| How often to check for world events | `config/window.yaml` | `sweep_interval_seconds` |
| Voice (Kokoro voices) | `config/window.yaml` | `speak_kokoro_voice` |
| Curation model (Ollama) | `config/window.yaml` | `curation_model` |
| NewsAPI key | `.env` | `NEWSAPI_KEY` |
| NASA FIRMS key | `.env` | `NASA_FIRMS_MAP_KEY` |
| OpenSky credentials | `.env` | `OPENSKY_CLIENT_ID`, `OPENSKY_CLIENT_SECRET` |
| Ollama host (if not localhost) | `.env` | `OLLAMA_HOST` |
| Kokoro weights location | Environment | `DISPATCH_KOKORO_DIR` |

---

## Next Steps

- **Read the README** for an overview of what Dispatch does
- **Explore `config/window.yaml`** — it's heavily commented with every tunable option
- **Edit `config/profile.yaml`** with your own interests for better headline ranking
- **Join the community** (GitHub discussions or issues) if you have questions

Enjoy the dashboard!
