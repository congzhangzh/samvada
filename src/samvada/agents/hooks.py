"""Hooks run by the agent harness (DESIGN 7.3): deterministic checks, no model call.

`samvada hook prompt` (UserPromptSubmit): note the turn's start; when the voice is off, add one
line of context so the agent does not compose speech nobody will hear.
`samvada hook stop` (Stop): when the voice is on and `samvada speak` was not called since the
turn started, ask the agent once to speak its closing.

The harness sends the event as JSON on stdin (session_id, hook_event_name, stop_hook_active).
A hook never breaks a session: any error is swallowed and the exit is 0.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from ..state import State

TURN, SPOKE, NUDGED = "turn", "spoke", "nudged"


def _session_dir(state: State, session_id: str) -> Path | None:
    if not session_id:
        return state.session_dir
    return state.root / "sessions" / f"claude-{session_id}"


def _stamp(d: Path, name: str) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(f"{time.time():.3f}\n")


def _read(d: Path, name: str) -> float:
    try:
        return float((d / name).read_text().strip())
    except (OSError, ValueError):
        return 0.0


def mark_spoke(state: State, env=os.environ) -> None:
    """Called by `samvada speak`: the agent spoke this turn (whatever was heard).

    Written under the session key and, inside Claude Code, under its session id too: the Stop
    hook knows only the id, and the key may come from $SAMVADA_SESSION instead."""
    dirs = {state.session_dir}
    if env.get("CLAUDE_CODE_SESSION_ID"):
        dirs.add(_session_dir(state, env["CLAUDE_CODE_SESSION_ID"]))
    for d in dirs - {None}:
        try:
            _stamp(d, SPOKE)
        except OSError:
            pass


def prompt(event: dict, state: State, decide) -> str | None:
    d = _session_dir(state, event.get("session_id", ""))
    if d is None:
        return None
    _stamp(d, TURN)
    (d / NUDGED).unlink(missing_ok=True)
    decision = decide(state)
    if decision.on:
        return None
    return (f"Samvada: the voice is off now ({decision.why}). Keep the corrections and the "
            "table; skip `samvada speak` this turn unless the owner asks for the voice.")


def stop(event: dict, state: State, decide) -> str | None:
    d = _session_dir(state, event.get("session_id", ""))
    if d is None or event.get("stop_hook_active"):
        return None
    turn = _read(d, TURN)
    if not turn or _read(d, SPOKE) >= turn or _read(d, NUDGED) >= turn:
        return None  # no turn seen, already spoke, or already asked once this turn
    if not decide(state).on:
        return None
    _stamp(d, NUDGED)
    return ("Samvada: you did not call `samvada speak` this turn. Speak the closing now, one "
            "call with every language (what was done, what is open, what needs the owner; "
            "no symbols or paths), then stop.")


def run(kind: str, state: State, decide) -> int:
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except (ValueError, OSError):
        event = {}
    try:
        if kind == "prompt":
            text = prompt(event, state, decide)
            if text:
                print(json.dumps({"hookSpecificOutput": {
                    "hookEventName": event.get("hook_event_name", "UserPromptSubmit"),
                    "additionalContext": text}}, ensure_ascii=False))
        elif kind == "stop":
            reason = stop(event, state, decide)
            if reason:
                print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))
    except Exception as e:  # never break the session over a hook
        print(f"samvada hook {kind}: {e}", file=sys.stderr)
    return 0

