"""Play one clip to the end."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

# Linux: paplay plays wav (Piper) and mp3 (edge-tts) through libsndfile; pw-play, aplay (wav).
# macOS: afplay. Windows: winsound, built into Python (wav only; online mp3 needs a player).
PLAYERS = ("paplay", "pw-play", "afplay", "aplay")


def _winsound(path: Path) -> bool:
    if path.suffix.lower() != ".wav":
        return False
    try:
        import winsound
        winsound.PlaySound(str(path), winsound.SND_FILENAME)  # blocks until the clip ends
        return True
    except (ImportError, RuntimeError, OSError):
        return False


def _players(path: Path):
    for name in PLAYERS:
        if name == "aplay" and path.suffix.lower() != ".wav":
            continue
        exe = shutil.which(name)
        if exe:
            yield exe


def play(path: Path) -> bool:
    if sys.platform == "win32":
        return _winsound(path)
    for exe in _players(path):
        try:
            return subprocess.run([exe, str(path)], stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL).returncode == 0
        except OSError:
            continue
    return False


def start(path: Path) -> subprocess.Popen | None:
    """Start playing and return at once, so the caller can stop it (`terminate()`); None
    when no player can (and on Windows, where winsound has no process to stop)."""
    if sys.platform == "win32":
        return None
    for exe in _players(path):
        try:
            return subprocess.Popen([exe, str(path)], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        except OSError:
            continue
    return None
