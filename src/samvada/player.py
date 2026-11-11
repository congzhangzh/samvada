"""Play one clip to the end."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

# paplay plays both wav (Piper) and mp3 (edge-tts) through libsndfile; afplay is macOS.
PLAYERS = ("paplay", "pw-play", "afplay")


def play(path: Path) -> bool:
    for name in PLAYERS:
        exe = shutil.which(name)
        if exe:
            try:
                return subprocess.run([exe, str(path)], stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL).returncode == 0
            except OSError:
                continue
    return False
