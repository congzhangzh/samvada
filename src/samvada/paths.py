"""Where Samvada keeps its state, its voices and its engines."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


def _xdg(env: Mapping[str, str], var: str, default: str) -> Path:
    value = env.get(var)
    return Path(value) if value else Path.home() / default


def state_dir(env: Mapping[str, str] = os.environ) -> Path:
    """Mute, languages, online mode, sessions, presence marks."""
    return _xdg(env, "XDG_STATE_HOME", ".local/state") / "samvada"


def legacy_state_dir(env: Mapping[str, str] = os.environ) -> Path:
    """The bash `speak` wrapper's state: read once for migration; its phone mark is still read."""
    return _xdg(env, "XDG_STATE_HOME", ".local/state") / "speak"


def data_dir(env: Mapping[str, str] = os.environ) -> Path:
    return _xdg(env, "XDG_DATA_HOME", ".local/share") / "samvada"


def install_voices_dir(env: Mapping[str, str] = os.environ) -> Path:
    """Where `samvada voices add` installs: $SAMVADA_VOICES, else Samvada's data folder."""
    return Path(env["SAMVADA_VOICES"]) if env.get("SAMVADA_VOICES") else data_dir(env) / "voices"


def voices_dirs(env: Mapping[str, str] = os.environ) -> list[Path]:
    """Where a language's model is looked for, in order; the first folder that has it wins.

    $SAMVADA_VOICES alone when set (a bundle sets it). Else Samvada's own folder, then
    $PIPER_DIR or the folder the `speak` wrapper used, so voices installed by hand keep working.
    The engine runs with that folder as its cwd: Piper's Chinese phonemizer loads g2pW from
    its cwd, and would otherwise download a 113 MB copy into the caller's folder.
    """
    if env.get("SAMVADA_VOICES"):
        return [Path(env["SAMVADA_VOICES"])]
    legacy = Path(env["PIPER_DIR"]) if env.get("PIPER_DIR") else \
        _xdg(env, "XDG_DATA_HOME", ".local/share") / "piper"
    return [data_dir(env) / "voices", legacy]


def engines_dir(env: Mapping[str, str] = os.environ) -> Path:
    """Engine folders made by `samvada voices add`: $SAMVADA_ENGINES (a bundle sets it)."""
    return Path(env["SAMVADA_ENGINES"]) if env.get("SAMVADA_ENGINES") else data_dir(env) / "engines"
