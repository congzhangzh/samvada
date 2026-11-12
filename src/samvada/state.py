"""Mute, languages and the online mode, in two scopes (DESIGN 6).

Global: files at the top of the state folder. Session: the same files under
sessions/<key>/. A mute in either scope silences; for languages and the online mode the
session wins over the global setting. The file formats are the `speak` wrapper's, so its
state is taken over by a copy.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

ONLINE_MODES = ("off", "fallback", "always")


def session_key(env: Mapping[str, str] = os.environ) -> str | None:
    """Who "this session" is; None when nothing says. The same keys as `speak`."""
    for var in ("SAMVADA_SESSION", "SPEAK_SESSION"):
        if env.get(var):
            return env[var]
    if env.get("CLAUDE_CODE_SESSION_ID"):
        return "claude-" + env["CLAUDE_CODE_SESSION_ID"]
    if env.get("PYS_WINDOW"):
        return "pys-w" + env["PYS_WINDOW"]
    try:
        return "tty" + os.ttyname(0).replace("/", "-")
    except (OSError, AttributeError):  # no tty, or Windows (no os.ttyname)
        return None


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def _read(path: Path, limit: int = 200) -> str | None:
    try:
        return path.read_text()[:limit]
    except OSError:
        return None


@dataclass
class Setting:
    value: str
    where: str  # "session" | "global" | "default" | "env"


class State:
    def __init__(self, root: Path, key: str | None, legacy: Path | None = None):
        self.root = root
        self.key = key
        if legacy is not None:
            migrate(legacy, root)

    # -- scopes ------------------------------------------------------------------------
    @property
    def session_dir(self) -> Path | None:
        return self.root / "sessions" / self.key if self.key else None

    def scope_dir(self, session: bool) -> Path:
        if not session:
            return self.root
        if self.session_dir is None:
            raise LookupError("no session here (no Claude session, pys window or tty); "
                              "set SAMVADA_SESSION")
        return self.session_dir

    def _dirs(self) -> list[tuple[str, Path]]:
        """Session first, then global."""
        dirs = [("session", self.session_dir)] if self.session_dir else []
        return dirs + [("global", self.root)]

    # -- mute --------------------------------------------------------------------------
    def mute(self, session: bool, why: str) -> None:
        _write(self.scope_dir(session) / "mute", why + "\n")

    def unmute(self, session: bool) -> None:
        (self.scope_dir(session) / "mute").unlink(missing_ok=True)

    def muted(self) -> tuple[str, str] | None:
        """(scope, why) of the mute in force; global is checked first, as `speak` does."""
        for scope, d in reversed(self._dirs()):
            why = _read(d / "mute")
            if why is not None:
                return scope, why.strip()
        return None

    # -- languages ---------------------------------------------------------------------
    @staticmethod
    def _langs_in(d: Path) -> dict[str, str]:
        out: dict[str, str] = {}
        for line in (_read(d / "langs", 10_000) or "").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] in ("on", "off"):
                out[parts[0]] = parts[1]
        return out

    def set_langs(self, session: bool, value: str, langs: list[str]) -> None:
        self.apply_langs(session, {lang: value for lang in langs})

    def apply_langs(self, session: bool, changes: Mapping[str, str]) -> None:
        """Turn several languages on or off in one scope, in one write ({lang: on|off})."""
        d = self.scope_dir(session)
        current = self._langs_in(d)
        for lang, value in changes.items():
            if value not in ("on", "off"):
                raise ValueError(f"{lang}: {value} is neither on nor off")
            current.pop(lang, None)
            current[lang] = value
        _write(d / "langs", "".join(f"{k} {v}\n" for k, v in current.items()))

    def reset_langs(self, session: bool) -> None:
        (self.scope_dir(session) / "langs").unlink(missing_ok=True)

    def lang(self, lang: str) -> Setting:
        for scope, d in self._dirs():
            value = self._langs_in(d).get(lang)
            if value:
                return Setting(value, scope)
        return Setting("on", "default")

    def lang_scopes(self, lang: str) -> dict[str, str]:
        """What each scope says about a language ({"session": "on", "global": "off"});
        a scope that says nothing is left out."""
        out = {}
        for scope, d in self._dirs():
            value = self._langs_in(d).get(lang)
            if value:
                out[scope] = value
        return out

    # -- online mode -------------------------------------------------------------------
    def set_online(self, session: bool, mode: str) -> None:
        if mode not in ONLINE_MODES:
            raise ValueError(f"unknown online mode {mode} (one of: {', '.join(ONLINE_MODES)})")
        _write(self.scope_dir(session) / "online", mode + "\n")

    def online(self, env: Mapping[str, str] = os.environ) -> Setting:
        value = env.get("SAMVADA_ONLINE")
        if value in ONLINE_MODES:
            return Setting(value, "env")
        if env.get("SPEAK_ONLINE") and not value:  # the old "online mode" switch
            return Setting("always", "env")
        for scope, d in self._dirs():
            value = (_read(d / "online") or "").strip()
            if value in ONLINE_MODES:
                return Setting(value, scope)
        return Setting("off", "default")

    # -- presence marks ----------------------------------------------------------------
    def mark_phone(self) -> None:
        _write(self.root / "presence" / "phone", "")


def migrate(legacy: Path, root: Path) -> bool:
    """Copy the `speak` wrapper's mute and language settings once, on first run.

    Copies, never moves: the wrapper keeps working while both are installed. The online
    fallback is deliberately not carried over (it was always on in `speak`; it is off here).
    """
    if root.exists() or not legacy.is_dir():
        return False
    root.mkdir(parents=True)
    for rel in ("mute", "langs"):
        if (legacy / rel).is_file():
            shutil.copy2(legacy / rel, root / rel)
    sessions = legacy / "sessions"
    if sessions.is_dir():
        for s in sessions.iterdir():
            for rel in ("mute", "langs"):
                if (s / rel).is_file():
                    (root / "sessions" / s.name).mkdir(parents=True, exist_ok=True)
                    shutil.copy2(s / rel, root / "sessions" / s.name / rel)
    (root / "MIGRATED").write_text(f"copied from {legacy}\n")
    return True
