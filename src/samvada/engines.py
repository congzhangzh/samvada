"""The voices: offline engines (Piper, piper-plus) and the optional online one (edge-tts).

Every engine runs as a subprocess in its own environment, so engines with conflicting
dependencies never share a Python (DESIGN 4.1). An engine is found in Samvada's engines
folder first (made by `samvada voices add`, run with this Python), else on PATH.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from . import paths
from .packs import engine_ok

TIMEOUT = 180  # seconds for one clip


@dataclass
class Voice:
    lang: str
    engine: str
    model: str
    online: str
    env: dict[str, str] = field(default_factory=dict)


def load_voices(path: Path | None = None) -> dict[str, Voice]:
    """The voice map, in file order. $SAMVADA_VOICE_MAP may point at another file."""
    if path is None and os.environ.get("SAMVADA_VOICE_MAP"):
        path = Path(os.environ["SAMVADA_VOICE_MAP"])
    data = tomllib.loads(path.read_text() if path else
                         resources.files("samvada").joinpath("voices.toml").read_text())
    return {lang: Voice(lang, t["engine"], t["model"], t["online"], dict(t.get("env", {})))
            for lang, t in data.items()}


def voice_home(voice: Voice, dirs: list[Path]) -> Path | None:
    """The first voices folder that has this language's model; None: no voice pack."""
    for d in dirs:
        if (d / voice.model).is_file():
            return d
    return None


def has_pack(voice: Voice, dirs: list[Path]) -> bool:
    return voice_home(voice, dirs) is not None


def engine_cmd(engine: str, env: Mapping[str, str] = os.environ) -> tuple[list[str], dict] | None:
    """How to start an engine, and the environment it adds; None when it is not installed."""
    d = paths.engines_dir(env) / engine
    if engine_ok(d):
        pp = env.get("PYTHONPATH")
        return [sys.executable, "-s", "-m", "piper"], {
            "PYTHONPATH": str(d) + (os.pathsep + pp if pp else "")}
    exe = shutil.which(engine, path=env.get("PATH"))
    return ([exe], {}) if exe else None


def engine_where(engine: str, env: Mapping[str, str] = os.environ) -> str:
    if engine_ok(paths.engines_dir(env) / engine):
        return "samvada"
    return "PATH" if shutil.which(engine, path=env.get("PATH")) else "missing"


def _run(cmd: list[str], *, cwd: Path | None, env: Mapping[str, str], stdin: str | None,
         debug: bool) -> bool:
    try:
        # UTF-8 both ways, whatever the locale: Windows would send Chinese text as cp1252
        r = subprocess.run(cmd, cwd=cwd, env={**env, "PYTHONUTF8": "1"}, input=stdin,
                           encoding="utf-8", errors="replace",
                           stdout=None if debug else subprocess.DEVNULL,
                           stderr=None if debug else subprocess.DEVNULL, timeout=TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as e:
        if debug:
            print(f"samvada: {cmd[0]}: {e}", file=sys.stderr)
        return False
    return r.returncode == 0


def _ok(out: Path) -> bool:
    return out.is_file() and out.stat().st_size > 0


def offline(voice: Voice, text: str, out: Path, home: Path, debug: bool = False) -> bool:
    found = engine_cmd(voice.engine)
    if found is None:
        if debug:
            print(f"samvada: {voice.engine} is not installed (samvada voices add {voice.lang})",
                  file=sys.stderr)
        return False
    base, extra = found
    env = {**os.environ, **voice.env, **extra}
    if (home / "hf").is_dir():  # the pack's own Hugging Face cache (the zh tokenizer)
        env["HF_HOME"] = str(home / "hf")
    if (home / "nltk_data").is_dir():  # the ja pack's NLTK data
        env["NLTK_DATA"] = str(home / "nltk_data")
    if voice.engine == "piper-plus" and not os.access(home, os.W_OK):
        # piper-plus saves an optimized copy beside the model on its first load
        # (<model>.cpu.opt.onnx) and fails when it cannot: a bundle on a read-only mount
        env.setdefault("PIPER_DISABLE_CACHE", "1")
    if voice.engine == "piper":
        cmd, stdin = base + ["-m", voice.model, "-f", str(out)], text + "\n"
    elif voice.engine == "piper-plus":
        cmd, stdin = base + ["-m", voice.model, "-f", str(out), text], None
    else:
        return False
    return _run(cmd, cwd=home, env=env, stdin=stdin, debug=debug) and _ok(out)


def online_available() -> bool:
    return bool(shutil.which("edge-tts") or importlib.util.find_spec("edge_tts"))


def online(voice: Voice, text: str, out: Path, debug: bool = False) -> bool:
    exe = shutil.which("edge-tts")
    if exe:
        base = [exe]
    elif importlib.util.find_spec("edge_tts"):
        base = [sys.executable, "-m", "edge_tts"]
    else:
        if debug:
            print("samvada: edge-tts is not installed (pip install 'samvada[online]')",
                  file=sys.stderr)
        return False
    cmd = base + ["--voice", voice.online, "--text", text, "--write-media", str(out)]
    return _run(cmd, cwd=None, env=os.environ, stdin=None, debug=debug) and _ok(out)
