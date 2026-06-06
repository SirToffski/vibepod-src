#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "fastapi",
#     "uvicorn[standard]",
# ]
# ///
"""
VibePod: HomePod <-> Mistral Vibe voice bridge.

Architecture:
    HomePod -> "Hey Siri, <shortcut name>"
        -> iOS Shortcut (speech-to-text)
        -> POST /chat to this server
        -> `vibe -p ... --output text`  (Mistral Vibe CLI, programmatic mode)
        -> reply text returned and spoken aloud (text-to-speech)

Auth: Vibe reads credentials from ~/.vibe/ (set up once with `vibe --setup`).
The service must run as the same user so HOME points at the right ~/.vibe/.

End-of-conversation: detected in the USER'S INPUT, not in the LLM reply.
Matching inputs short-circuit before any vibe call — zero tokens consumed,
instant response. The Shortcut reads end_conversation=true and stops.

Session continuity: each speaker gets a unique working directory under
VIBEPOD_SESSIONS_DIR. Vibe's --continue picks up the latest session for that
directory on each subsequent turn. Saying goodbye clears the directory entry
so the next "Hey Siri" starts a fresh conversation.

Adapted from algal/clawpod. "VibePod" is a local filename, not a product name.
"""

import asyncio
import logging
import os
import shutil
import subprocess
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HOST = os.getenv("VIBEPOD_HOST", "0.0.0.0") # <-- I changed this to my private LAN IP
PORT = int(os.getenv("VIBEPOD_PORT", "7001"))
LOG_LEVEL = os.getenv("VIBEPOD_LOG_LEVEL", "INFO")
API_TOKEN = os.getenv("VIBEPOD_API_TOKEN")  # Optional bearer token

# Vibe runtime settings
VIBE_TIMEOUT = int(os.getenv("VIBEPOD_TIMEOUT", "60"))       # seconds to wait
VIBE_MAX_TURNS = int(os.getenv("VIBEPOD_MAX_TURNS", "1"))    # 1 = single Q&A shot
VIBE_MAX_PRICE = os.getenv("VIBEPOD_MAX_PRICE", "0.10")      # $ cap per call

# Optional: restrict which Vibe tools can run during programmatic mode.
# When set, ALL other tools are disabled (Vibe's --enabled-tools semantics).
# Example: "web_search" for current-events questions, "" for no restriction.
VIBE_ENABLED_TOOLS = os.getenv("VIBEPOD_ENABLED_TOOLS", "web_search").strip()

# Session workdirs: one unique sub-directory per speaker per conversation.
# Lives in /tmp so it's cleared on reboot (sessions start fresh — fine for
# a home assistant). Vibe stores the actual session history in ~/.vibe/sessions/.
SESSION_PREFIX = os.getenv("VIBEPOD_SESSION_PREFIX", "homepod")
SESSIONS_BASE = Path(os.getenv("VIBEPOD_SESSIONS_DIR", "/tmp/vibepod/sessions"))

# Brief voice instruction prepended to every prompt.
# Kept short because it's sent on every turn and accumulates in context.
# If you want a richer system prompt, create a custom Vibe agent instead:
#   ~/.vibe/agents/voice.toml  ->  pass --agent voice  via VIBEPOD_AGENT_NAME
VOICE_INSTRUCTION = os.getenv(
    "VIBEPOD_VOICE_INSTRUCTION",
    "[Voice mode: reply in brief spoken prose. No markdown, no bullet points, no emojis.]",
)

# Optional agent name. Default uses whatever is set as default_agent in ~/.vibe/config.toml.
# Set to "auto-approve" if you enable tools and don't want approval prompts to block calls.
VIBE_AGENT_NAME = os.getenv("VIBEPOD_AGENT_NAME", "").strip()

# End-of-conversation phrases matched against USER INPUT (not LLM output).
# Matching inputs return END_REPLY instantly — zero LLM tokens, zero latency.
# The Shortcut reads end_conversation=true and ends the session.
_END_PHRASES_RAW = os.getenv(
    "VIBEPOD_END_PHRASES",
    "goodbye,bye,bye for now,that's all,thats all,end conversation,stop listening",
)
END_PHRASES = [p.strip().lower() for p in _END_PHRASES_RAW.split(",") if p.strip()]
END_REPLY = os.getenv("VIBEPOD_END_REPLY", "Goodbye!")

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

logging.basicConfig(level=LOG_LEVEL.upper())
logger = logging.getLogger("vibepod")

# Ensure the sessions base directory exists before any request arrives.
SESSIONS_BASE.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="VibePod", version="1.0.0")

# In-memory map: session_key -> active workdir Path for that speaker.
# Cleared per speaker on end-of-conversation; next call creates a new workdir.
_session_workdirs: dict[str, Path] = {}

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    """Incoming request from the iOS Shortcut."""
    text: str
    speaker: str = "Unknown"


class ChatResponse(BaseModel):
    """Response back to the iOS Shortcut."""
    reply: str
    end_conversation: bool = False


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def require_auth(request: Request) -> None:
    """Optional bearer-token check."""
    if not API_TOKEN:
        return
    auth = request.headers.get("authorization", "")
    if auth != f"Bearer {API_TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized")


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------


def get_session_key(speaker: str) -> str:
    """Stable per-speaker key into the local session map."""
    key = speaker.lower().strip().replace(" ", "-")
    if not key or key == "unknown":
        key = "family"
    return f"{SESSION_PREFIX}:{key}"


def get_or_create_workdir(session_key: str) -> tuple[Path, bool]:
    """
    Return (workdir, should_continue).

    First call for a session_key: creates a fresh unique directory and returns
    should_continue=False (Vibe starts a new session there).
    Subsequent calls: returns the same directory and should_continue=True
    (Vibe's --continue finds the latest session for that working directory).
    """
    if session_key in _session_workdirs:
        return _session_workdirs[session_key], True
    workdir = SESSIONS_BASE / session_key / uuid.uuid4().hex[:8]
    workdir.mkdir(parents=True, exist_ok=True)
    _session_workdirs[session_key] = workdir
    return workdir, False


def clear_session(session_key: str) -> None:
    """Drop the active workdir so the next call starts a fresh conversation."""
    _session_workdirs.pop(session_key, None)


# ---------------------------------------------------------------------------
# End-of-conversation detection
# ---------------------------------------------------------------------------


def is_end_phrase(text: str) -> bool:
    """Check if the user's input is an end-of-conversation phrase.

    Substring match so "okay goodbye" and "goodbye!" both trigger,
    without requiring exact phrasing from speech recognition.
    """
    t = text.lower().strip()
    return any(phrase in t for phrase in END_PHRASES)


# ---------------------------------------------------------------------------
# Vibe integration
# ---------------------------------------------------------------------------


def find_vibe() -> str:
    """Locate the Vibe CLI binary."""
    candidates = [
        "/usr/bin/vibe",
        os.path.expanduser("~/.local/bin/vibe"),
        os.path.expanduser("~/.venv/bin/vibe"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    path = shutil.which("vibe")
    if path:
        return path
    raise RuntimeError("vibe not found in PATH or common install locations")


async def call_vibe(message: str, session_key: str, speaker: str) -> str:
    """
    Call Vibe in programmatic mode and return the plain-text reply.

    Uses --output text: stdout IS the assistant's reply, no JSON parsing needed.
    The VOICE_INSTRUCTION prefix is sent with every prompt to keep replies
    brief and TTS-friendly without modifying the Vibe agent system prompt.
    """
    vibe_bin = find_vibe()
    workdir, should_continue = get_or_create_workdir(session_key)

    full_prompt = f"{VOICE_INSTRUCTION}\n\n[Speaker: {speaker}]\n{message}"

    cmd = [
        vibe_bin,
        "-p", full_prompt,         # programmatic mode: send prompt, output, exit
        "--output", "text",        # stdout = assistant reply text, nothing else
        "--trust",                 # skip folder-trust prompt (required for headless)
        "--workdir", str(workdir), # unique per speaker per conversation
        "--max-turns", str(VIBE_MAX_TURNS),
        "--max-price", VIBE_MAX_PRICE,
    ]
    if should_continue:
        cmd.append("--continue")   # resume this speaker's thread
    if VIBE_ENABLED_TOOLS:
        cmd += ["--enabled-tools", VIBE_ENABLED_TOOLS]
    if VIBE_AGENT_NAME:
        cmd += ["--agent", VIBE_AGENT_NAME]

    # Pass the full environment through.
    # VIBE_* env vars override ~/.vibe/config.toml fields — useful for model
    # selection without editing config: set VIBE_ACTIVE_MODEL in the unit.
    env = os.environ.copy()

    logger.info(
        f"Calling vibe: speaker={speaker} resume={should_continue} "
        f"workdir={workdir.name}"
    )
    logger.debug(f"Command: {' '.join(str(a) for a in cmd)}")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=VIBE_TIMEOUT + 10,
        )

        if proc.returncode != 0:
            err = stderr.decode().strip()
            logger.error(f"vibe error (exit {proc.returncode}): {err!r}")
            clear_session(session_key)   # ← add this
            return "Sorry, I'm having trouble right now. Try again in a moment."

        reply = stdout.decode().strip()
        if (err := stderr.decode().strip()):
            logger.debug(f"vibe stderr: {err!r}")

        if not reply:
            logger.warning("Empty reply from vibe")
            return "I'm not sure how to respond to that."

        return reply

    except asyncio.TimeoutError:
        logger.error("vibe timed out")
        return "Sorry, that took too long. Try again?"
    except Exception as e:
        logger.error(f"vibe call failed: {e}")
        return "Sorry, something went wrong."


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    """Health check — no auth required."""
    return {"status": "healthy", "service": "vibepod"}


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, _: None = Depends(require_auth)):
    """
    Process voice input and return Vibe's response.

    The iOS Shortcut sends:
        text:    transcribed speech from Siri
        speaker: recognized speaker name (if available)

    Returns:
        reply:            text to be spoken aloud by the HomePod
        end_conversation: True if the session should end
    """
    text = request.text.strip()
    speaker = request.speaker.strip() or "Unknown"

    if not text:
        raise HTTPException(status_code=400, detail="Text is required")

    logger.info(f"Chat: speaker={speaker!r} text={text[:60]!r}")

    session_key = get_session_key(speaker)

    # End-of-conversation fast path: detect in USER INPUT, not LLM output.
    # No vibe call at all — zero tokens, instant reply.
    if is_end_phrase(text):
        clear_session(session_key)
        logger.info(f"End phrase in input for {speaker!r} — session cleared")
        return ChatResponse(reply=END_REPLY, end_conversation=True)

    reply = await call_vibe(text, session_key, speaker)
    logger.info(f"Reply: {reply[:60]!r}")
    return ChatResponse(reply=reply, end_conversation=False)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    logger.info(
        f"Starting VibePod on {HOST}:{PORT} | "
        f"max_turns={VIBE_MAX_TURNS} max_price=${VIBE_MAX_PRICE} | "
        f"sessions={SESSIONS_BASE}"
    )
    uvicorn.run(app, host=HOST, port=PORT, log_level=LOG_LEVEL.lower())
