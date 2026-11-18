"""`samvada doctor`: check everything a voice needs, say what is wrong and how to fix it.

Checks only; it writes nothing and plays nothing. Exit 0 when every language that is on can
speak offline, 1 when one cannot, 2 when a voice map file cannot be read.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path

from . import engines, paths, player
from .state import State

OK, BAD, NOTE = "✓", "✗", "·"


def _player() -> str:
    if sys.platform == "win32":
        return "winsound (wav)"
    found = [p for p in player.PLAYERS if shutil.which(p)]
    return ", ".join(found)


def run(state: State, env: Mapping[str, str] = os.environ, out=print) -> int:
    status = 0
    out("config")
    for name, f in engines.layers(env):
        out(f"  {OK} {name:<8} {f if f else 'samvada voices.toml (the defaults)'}")
    for f in (paths.picked_voices_file(env), paths.owner_voices_file(env)):
        if env.get("SAMVADA_VOICE_MAP") and f.is_file():
            out(f"  {NOTE} {f}: not read, $SAMVADA_VOICE_MAP stands alone")
    pname, pwhere = engines.active_profile(env)
    out(f"  {NOTE} profile {pname} ({pwhere})")
    older = paths.picked_voices_file(env)
    if older.is_file() and pname != "like":
        out(f"  {NOTE} {older} is your older picks: read only as profile like "
            f"(samvada profile use like), not now")
    try:
        voices = engines.load_voices(env=env)
    except engines.VoiceMapError as e:
        out(f"  {BAD} {e}")
        return 2

    dirs = paths.voices_dirs(env)
    out("voices")
    for lang, v in voices.items():
        on = state.lang(lang)
        head = f"  {lang}  ({on.value}, {on.where})  {v.engine}  {v.model}  [{v.origin.get('model', 'default')}]"
        problems: list[str] = []
        home = engines.voice_home(v, dirs)
        if home is None:
            problems.append(f"model not found in {', '.join(map(str, dirs))}"
                            f" (samvada voices add {lang}, or `voices pick {lang}`)")
        elif v.engine == "piper" and not Path(f"{home / v.model}.json").is_file():
            problems.append(f"{v.model}.json is missing beside the model")
        elif v.engine == "voicevox":
            from . import voicevox
            vvm = home / v.model
            names = {str(st.id): st for st in voicevox.styles(vvm)}
            if v.style not in names:
                problems.append(f"style {v.style} is not a speaking style in {vvm.name}")
            else:
                head += f"  {names[v.style].speaker} {names[v.style].style}"
            r = voicevox.root(vvm, env)
            lib, dic = voicevox.runtime_files(r) if r else (None, None)
            if r is None:
                problems.append(f"no onnxruntime/ and dict/ above {vvm} (set VOICEVOX_ROOT)")
            elif lib is None or dic is None:
                problems.append(f"{r}: VOICEVOX's ONNX Runtime library or the dictionary is missing")
        elif v.engine == "kokoro":
            from . import kokoro
            model = Path(v.model) if Path(v.model).is_absolute() else home / v.model
            names = {k.name for k in kokoro.voices(model)}
            if kokoro.voices_file(model) is None:
                problems.append(f"no voices*.bin beside {model}")
            elif v.style not in names:
                problems.append(f"voice {v.style} is not in {kokoro.voices_file(model).name}")
            if "v1.1" in model.name and not model.with_name("config.json").is_file():
                problems.append(f"{model.name} needs its config.json beside it")
        where = engines.engine_where(v.engine, env)
        if where == "missing":
            problems.append(
                f"engine {v.engine} not installed: put the voicevox_core wheel in "
                f"{paths.engines_dir(env) / 'voicevox'} (uv pip install --target <that folder> "
                "<wheel from github.com/VOICEVOX/voicevox_core/releases>)"
                if v.engine == "voicevox" else
                f"engine kokoro not installed: uv pip install --target "
                f"{paths.engines_dir(env) / 'kokoro'} kokoro-onnx 'misaki[zh]' fugashi jaconv "
                "mojimoji pyopenjtalk unidic-lite"
                if v.engine == "kokoro" else
                f"engine {v.engine} not installed (samvada voices add {lang})")
        mark = BAD if problems and on.value == "on" else NOTE if problems else OK
        out(f"{mark}{head}")
        for p in problems:
            out(f"      {p}")
        if home is not None:
            place = v.model if Path(v.model).is_absolute() else f"in {home}"
            out(f"      {place}; engine from {where}")
        if v.origin.get("model", "default") != "default" and not v.license:
            out("      licence: not given (your choice of model: see its own terms)")
        out(f"      order: " + " → ".join(engines.order(v, dirs, state.online(env).value, env)))
        if v.license:
            out(f"      licence: {v.license}")
        if v.credit:
            out(f"      credit: {v.credit}")
        if problems and on.value == "on":
            status = 1

    out("sound")
    found = _player()
    out(f"  {OK if found else BAD} player: {found or 'none of ' + ', '.join(player.PLAYERS)}")
    if not found:
        status = 1
    mode = state.online(env)
    online = ("edge-tts installed" if engines.online_available()
              else "edge-tts not installed")
    out(f"  {NOTE} online: {mode.value} ({mode.where}); {online}")
    return status
