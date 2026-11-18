"""Speak language-text pairs in order: skip what is off, synthesise, play to the end.

Exit codes (as `speak`): 0 played, or silent by decision; 1 some language failed with every
engine allowed; 2 bad usage.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path

from . import engines, packs, paths, player, playq
from .engines import Voice
from .state import State


def say(msg: str) -> None:
    print(f"samvada: {msg}", file=sys.stderr)


def from_package(voice: Voice, env: Mapping[str, str]) -> Path | None:
    """A language with no voice folder whose samvada-voice-<lang> package is installed (as
    `pip install samvada` brings en and zh): install the pack from it, never the network.
    The folder it is now in, or None."""
    try:
        lock = packs.load_lock()
        pkg = packs.from_package(voice.lang, lock, env)
        if pkg is None:
            return None
        home = paths.install_voices_dir(env)
        packs.add(voice.lang, lock, home, env, log=lambda *a: None, offline=True)
    except (packs.PackError, OSError) as e:
        say(f"{voice.lang}: its voice package could not be installed: {e}")
        return None
    say(f"{voice.lang}: voice installed from {pkg.name} {pkg.version} into {home}")
    if engines.engine_where(voice.engine, env) == "missing":
        say(f"{voice.lang}: engine {voice.engine} is not installed: samvada voices add {voice.lang}")
    return engines.voice_home(voice, [home])


def _why_not(voice: engines.Voice, home: Path | None, env: Mapping[str, str]) -> str:
    """Why the primary voice did not speak, in the words `status` and `doctor` use."""
    if home is None:
        return "has no voice pack"
    if engines.engine_where(voice.engine, env) == "missing":
        return f"engine not installed (samvada voices add {voice.lang})"
    return "failed"


def _wants_wait(env: Mapping[str, str]) -> bool:
    return str(env.get("SAMVADA_WAIT", "")).lower() in ("1", "yes", "on", "true")


def _queue_dir(env: Mapping[str, str]) -> tuple[Path, bool]:
    """A folder for one message's clips: in the private runtime folder (the background player
    may outlive this process), else a temporary one; True when the background player may run."""
    try:
        from .warm import runtime_dir
        d = runtime_dir(env)
    except ImportError:  # pragma: no cover
        d = None
    if d is not None:
        playq.sweep(d)
        return Path(tempfile.mkdtemp(prefix="msg.", dir=d)), sys.platform != "win32"
    runtime = env.get("XDG_RUNTIME_DIR")
    tmp_root = runtime if runtime and os.path.isdir(runtime) else None
    return Path(tempfile.mkdtemp(prefix="samvada.", dir=tmp_root)), False


def _start_player(qdir: Path, env: Mapping[str, str], background: bool):
    """The player of this message's clips: a detached background process, or (when the
    caller waits) a child the caller waits for."""
    cmd = [sys.executable, "-m", "samvada.playq", str(qdir)]
    if background:
        log = qdir.parent / "player.log"
        with open(log, "ab") as out:
            return subprocess.Popen(cmd, env=dict(env), stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=out, start_new_session=True, close_fds=True)
    return subprocess.Popen(cmd, env=dict(env), stdin=subprocess.DEVNULL)


def speak(pairs: list[tuple[str, str]], *, state: State, voices: dict[str, Voice],
          voices_dirs: list[Path], env: Mapping[str, str] = os.environ,
          save: Path | None = None, wait: bool = False) -> int:
    """Make each pair's clip and have it played; with `save` write each clip there as
    <i>-<lang>.<wav|mp3> and play nothing.

    The clips go to a queue folder; a background player (samvada.playq) starts with the first
    clip, waits for the play turn and plays them in order while the later ones are still being
    made. This call returns when its clips are made, never waiting for another session's
    audio. `wait` (or $SAMVADA_WAIT=1) waits for this message to be heard to its end."""
    debug = bool(env.get("SAMVADA_DEBUG") or env.get("SPEAK_DEBUG"))
    mode = state.online(env).value
    status, played, skipped, hinted = 0, 0, [], False
    wait = wait or _wants_wait(env)
    qdir, can_detach = _queue_dir(env)
    background = can_detach and not wait and save is None
    proc = None
    try:
        for i, (lang, text) in enumerate(pairs, 1):
            voice = voices[lang]
            setting = state.lang(lang)
            if setting.value == "off":
                skipped.append(f"{lang}({setting.where})")
                continue
            home = engines.voice_home(voice, voices_dirs)
            if home is None and mode != "always":
                home = from_package(voice, env)
            fb = voice.fallback
            fb_home = engines.usable_home(fb, voices_dirs, env) if mode != "always" else None
            if home is None and fb_home is None and mode == "off":
                skipped.append(f"{lang}(no voice pack)")
                continue
            played += 1
            wav, mp3 = Path(qdir, f"{i}-{lang}.wav"), Path(qdir, f"{i}-{lang}.mp3")
            clip = None
            if mode != "always" and home and engines.offline(voice, text, wav, home, debug):
                clip = wav
            elif fb_home is not None and engines.offline(fb, text, wav, fb_home, debug):
                clip = wav  # the fallback: Kokoro when Piper's voice is missing or fails
                say(f"{lang}: {voice.engine} {_why_not(voice, home, env)}, "
                    f"used {fb.engine} {fb.style or Path(fb.model).stem}")
            elif mode != "off":
                if engines.online(voice, text, mp3, debug):
                    clip = mp3
                    if mode == "fallback":
                        say(f"{lang} {'offline failed' if home else 'has no voice pack'}, used edge-tts")
            if clip is None:
                if mode == "off":
                    say(f"{lang} failed offline")
                    if not hinted:
                        say("the online fallback is off; to allow it: samvada online fallback")
                        hinted = True
                else:
                    say(f"{lang} failed ({'online' if mode == 'always' else 'offline and online'})")
                status = 1
                continue
            if save is not None:
                save.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(clip, save / clip.name)
                continue
            playq.mark_ready(qdir, clip)
            if proc is None:  # the first clip: its player starts now, while the rest are made
                proc = _start_player(qdir, env, background)
    finally:
        playq.mark_done(qdir)
        if proc is None:
            shutil.rmtree(qdir, ignore_errors=True)  # nothing to play
        elif not background:
            code = proc.wait()
            if code == 3:
                say(f"not played: its turn did not come within {playq.max_wait(env):.0f} s "
                    "(others were speaking)")
            elif code != 0:
                say(f"a clip could not be played (tried: {', '.join(player.PLAYERS)})")
                status = 1
    if skipped:
        say("skipped " + " ".join(skipped))
    if played == 0:
        say("silent, every language passed is off or has no voice pack")
    return status
