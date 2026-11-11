"""Fake engines, player and desktop: no audio, no network, no GNOME needed.

Each fake writes one line to $FAKE_LOG ("<tool> <arg>"), so a test sees what was synthesised
and played, in order. $FAKE_FAIL lists engines that should fail (e.g. "piper:zh edge").
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

FAKE_PIPER = r"""#!/bin/sh
# piper -m MODEL -f OUT  (text on stdin)   |  piper-plus -m MODEL -f OUT TEXT
tool=$(basename "$0"); model=$2; out=$4
case " $FAKE_FAIL " in *" $tool:${model%%_*} "*|*" $tool "*) exit 1 ;; esac
cat >/dev/null 2>&1
echo "$tool $model cwd=$(pwd)${HF_HOME:+ hf=$HF_HOME}${NLTK_DATA:+ nltk=$NLTK_DATA}" >> "$FAKE_LOG"
printf 'RIFF' > "$out"
"""

FAKE_EDGE = r"""#!/bin/sh
# edge-tts --voice V --text T --write-media OUT
case " $FAKE_FAIL " in *" edge "*) exit 1 ;; esac
echo "edge $2" >> "$FAKE_LOG"
printf 'ID3' > "$6"
"""

FAKE_PLAY = r"""#!/bin/sh
echo "play $(basename "$1")" >> "$FAKE_LOG"
"""

# gdbus call --session --dest D ...: idle from $FAKE_IDLE_MS, lock from $FAKE_LOCKED
FAKE_GDBUS = r"""#!/bin/sh
case "$*" in
  *ScreenSaver*) [ -n "$FAKE_LOCKED" ] && echo "(true,)" || echo "(false,)" ;;
  *IdleMonitor*) [ -n "$FAKE_IDLE_MS" ] && echo "(uint64 $FAKE_IDLE_MS,)" || exit 1 ;;
esac
"""

MODELS = ["en_US-lessac-high.onnx", "zh_CN-xiao_ya-medium.onnx",
          "tsukuyomi-chan-6lang-fp16.onnx", "fr_FR-siwis-medium.onnx"]


class Env:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.bin = tmp / "bin"
        self.voices = tmp / "voices"
        self.log = tmp / "log"
        self.state = tmp / "state"
        for d in (self.bin, self.voices, self.state):
            d.mkdir()
        for name, body in (("piper", FAKE_PIPER), ("piper-plus", FAKE_PIPER),
                           ("edge-tts", FAKE_EDGE), ("paplay", FAKE_PLAY), ("gdbus", FAKE_GDBUS)):
            p = self.bin / name
            p.write_text(body)
            p.chmod(0o755)
        for m in MODELS:
            (self.voices / m).write_text("model")
        self.vars = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(tmp),
            "XDG_STATE_HOME": str(self.state),
            "XDG_DATA_HOME": str(tmp / "data"),
            "XDG_RUNTIME_DIR": str(tmp),
            "SAMVADA_VOICES": str(self.voices),
            "PIPER_DIR": str(self.voices),
            "SAMVADA_SESSION": "test-session",
            "SPEAK_SESSION": "test-session",  # the bash wrapper, in the parity tests
            "FAKE_LOG": str(self.log),
            "FAKE_IDLE_MS": "1000",
        }

    def run(self, *args: str, cmd: list[str] | None = None, **extra: str):
        env = {**self.vars, **extra}
        base = cmd or [sys.executable, "-m", "samvada"]
        env.setdefault("PYTHONPATH", str(ROOT / "src"))
        return subprocess.run(base + list(args), env=env, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL)

    def lines(self) -> list[str]:
        if not self.log.exists():
            return []
        out = self.log.read_text().splitlines()
        self.log.unlink()
        return out

    def played(self) -> list[str]:
        """The languages played, in order (clips are named <i>-<lang>.<ext>)."""
        return [l.split()[1].split("-")[1].split(".")[0] for l in self.lines()
                if l.startswith("play ")]


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)
