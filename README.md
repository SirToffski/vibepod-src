# VibePod

A tiny bridge that lets you talk to a [Mistral Vibe](https://github.com/mistralai/mistral-vibe) agent through an Apple HomePod. You speak to the HomePod, Siri runs a Shortcut on your iPhone, that POSTs to this server, the server shells out to `vibe`, and the reply gets spoken back.

Structurally based on [ClawPod](https://github.com/algal/clawpod) by Alexis Gallagher — the iOS Shortcut is his; this just swaps the brain to Vibe.

📝 **See the [blog post](https://sirtoffski.github.io/vibepod) for the full story, the how, and the why.**

## What's here

| File | What it is | Where it goes |
|------|------------|---------------|
| `vibepod_server.py` | FastAPI proxy that calls `vibe -p` | anywhere on the box (e.g. `~/vibepod/`) |
| `vibepod.service` | systemd unit so it runs on boot | `/etc/systemd/system/` |
| `voice.md` | lean system prompt for voice replies | `~/.vibe/prompts/` |

## Quick usage

1. **Install & log into Vibe** first ([Mistral's guide](https://docs.mistral.ai/vibe/code/cli/install-setup)). The server runs as your user and inherits that login from `~/.vibe/`.
2. **Drop the files** in the locations above. Create `~/.vibe/prompts/` if it doesn't exist.
3. **Edit `vibepod.service`** — at minimum:
   - Replace every `casa` / `/home/casa` with **your username** (`User=`, `Group=`, `HOME=`, `WorkingDirectory=`, `ExecStart=`).
   - Set `VIBEPOD_HOST` to your server's LAN IP (or `0.0.0.0`), and `VIBEPOD_PORT` if `7001` is taken.
   - Check the `uv` path in `ExecStart` matches `which uv`.
   - Pick a model alias for `VIBE_ACTIVE_MODEL` that exists in your `~/.vibe/` config (it matches the **alias**, not the full model name).
4. **Enable it:**
   ```bash
   sudo cp vibepod.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now vibepod.service
   curl http://<your-server-ip>:7001/health
   ```
5. **Point the [ClawPod Shortcut](https://github.com/algal/clawpod)** at `http://<your-server-ip>:7001`. Optionally change the speaker name - nothing else in the Shortcut changes.

## Worth knowing

- **Tuning lives in env vars** in the service file. The combo of `VIBE_SYSTEM_PROMPT_ID=voice`, `VIBE_INCLUDE_PROMPT_DETAIL=false`, the small model, and `VIBEPOD_ENABLED_TOOLS=web_search` is what keeps each query lean (~343 tokens). Adjust to taste.
- **`VIBEPOD_END_PHRASES`** ("goodbye", "bye for now", …) are matched against your *input* and short-circuit before any LLM call — saying one ends the chat for zero tokens.
- **`VIBEPOD_API_TOKEN`** is optional but recommended if the server is ever reachable beyond a trusted LAN (put the matching bearer token in the Shortcut's Authorization header).
- **`voice.md`** tells the model to answer from training data by default and only web-search when you explicitly ask it to (e.g. "look that up").

## Credits

Built in collaboration with [Claude](https://claude.ai). Inspired by [ClawPod](https://github.com/algal/clawpod). Powered by [Mistral Vibe](https://github.com/mistralai/mistral-vibe).
