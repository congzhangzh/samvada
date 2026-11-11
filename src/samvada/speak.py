"""Speak language-text pairs in order: skip what is off, synthesise, play to the end.

Exit codes (as `speak`): 0 played, or silent by decision; 1 some language failed with every
engine allowed; 2 bad usage.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

from . import engines, player
from .engines import Voice
from .state import State


def say(msg: str) -> None:
    print(f"samvada: {msg}", file=sys.stderr)


def speak(pairs: list[tuple[str, str]], *, state: State, voices: dict[str, Voice],
          voices_dirs: list[Path], env: Mapping[str, str] = os.environ,
          save: Path | None = None) -> int:
    """Play each pair, or with `save` write each clip there as <i>-<lang>.<wav|mp3>."""
    debug = bool(env.get("SAMVADA_DEBUG") or env.get("SPEAK_DEBUG"))
    mode = state.online(env).value
    status, played, skipped, hinted = 0, 0, [], False
    runtime = env.get("XDG_RUNTIME_DIR")
    tmp_root = runtime if runtime and os.path.isdir(runtime) else None
    with tempfile.TemporaryDirectory(prefix="samvada.", dir=tmp_root) as tmp:
        for i, (lang, text) in enumerate(pairs, 1):
            voice = voices[lang]
            setting = state.lang(lang)
            if setting.value == "off":
                skipped.append(f"{lang}({setting.where})")
                continue
            home = engines.voice_home(voice, voices_dirs)
            if home is None and mode == "off":
                skipped.append(f"{lang}(no voice pack)")
                continue
            played += 1
            wav, mp3 = Path(tmp, f"{i}-{lang}.wav"), Path(tmp, f"{i}-{lang}.mp3")
            clip = None
            if mode != "always" and home and engines.offline(voice, text, wav, home, debug):
                clip = wav
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
            elif not player.play(clip):
                say(f"{lang}: no player could play it (tried: {', '.join(player.PLAYERS)})")
                status = 1
    if skipped:
        say("skipped " + " ".join(skipped))
    if played == 0:
        say("silent, every language passed is off or has no voice pack")
    return status
