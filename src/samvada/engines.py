"""The voices: offline engines (Piper, piper-plus) and the optional online one (edge-tts).

Every engine runs as a subprocess in its own environment, so engines with conflicting
dependencies never share a Python (DESIGN 4.1). An engine is found in Samvada's engines
folder first (made by `samvada voices add`, run with this Python), else on PATH.
"""

from __future__ import annotations

import importlib.util
import os
import re
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
    origin: dict[str, str] = field(default_factory=dict)  # key -> the layer it came from
    license: str = ""
    credit: str = ""
    style: str = ""  # VOICEVOX: the style id in the model file
    fallback: "Voice | None" = None  # speaks when this voice's pack is missing or its engine fails


KEYS = ("engine", "model", "online", "env", "license", "license_url", "credit", "style",
        "fallback")
FALLBACK_KEYS = ("engine", "model", "env", "license", "license_url", "credit", "style")
ENGINES = ("piper", "piper-plus", "voicevox", "kokoro")
# engines that are a Python package run by a module of Samvada's: (package, runner)
PY_ENGINES = {"voicevox": ("voicevox_core", "samvada.voicevox"),
              "kokoro": ("kokoro_onnx", "samvada.kokoro")}
REQUIRED = ("engine", "model", "online")


class VoiceMapError(LookupError):
    """A voice map that cannot be used: names the file and the key (exit 2)."""


def _read(path: Path) -> dict:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise VoiceMapError(f"{path}: not TOML: {e}") from None
    for lang, t in data.items():
        if not isinstance(t, dict):
            raise VoiceMapError(f"{path}: {lang} must be a table, [{lang}]")
        for key, value in t.items():
            if key not in KEYS:
                raise VoiceMapError(f"{path}: [{lang}] unknown key {key} "
                                    f"(one of: {' '.join(KEYS)})")
            if key == "env" and not isinstance(value, dict):
                raise VoiceMapError(f"{path}: [{lang}] env must be a table, [{lang}.env]")
            if key == "fallback":
                if not isinstance(value, dict):
                    raise VoiceMapError(f"{path}: [{lang}] fallback must be a table, [{lang}.fallback]")
                for fk, fv in value.items():
                    if fk not in FALLBACK_KEYS:
                        raise VoiceMapError(f"{path}: [{lang}.fallback] unknown key {fk} "
                                            f"(one of: {' '.join(FALLBACK_KEYS)})")
                    if fk == "style" and isinstance(fv, int) and not isinstance(fv, bool):
                        value[fk] = str(fv)
                    elif fk != "env" and not isinstance(value[fk], str):
                        raise VoiceMapError(f"{path}: [{lang}.fallback] {fk} must be a string")
                    if fk == "engine" and fv not in ENGINES:
                        raise VoiceMapError(f"{path}: [{lang}.fallback] unknown engine {fv}")
                continue
            if key == "style" and isinstance(value, int) and not isinstance(value, bool):
                t[key] = str(value)
            elif key not in ("env", "fallback") and not isinstance(value, str):
                raise VoiceMapError(f"{path}: [{lang}] {key} must be a string")
            if key == "engine" and value not in ENGINES:
                raise VoiceMapError(f"{path}: [{lang}] unknown engine {value} "
                                    f"(one of: {' '.join(ENGINES)})")
    return data


def _package_map() -> dict:
    return tomllib.loads(resources.files("samvada").joinpath("voices.toml").read_text(encoding="utf-8"))


def default_engine(lang: str) -> str:
    """The engine the package's own map gives a language ("piper" for one it has not)."""
    return _package_map().get(lang, {}).get("engine", "piper")


def default_voice(lang: str) -> Voice | None:
    """The package's own voice for a language, whatever the owner's files say."""
    t = _package_map().get(lang)
    if not t:
        return None
    return Voice(lang, t["engine"], t["model"], t["online"], dict(t.get("env", {})),
                 {}, t.get("license", ""), t.get("credit", ""), str(t.get("style", "")))


PROFILE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")


def builtin_profiles() -> dict[str, Path]:
    """The profiles shipped in the package (`samvada/profiles/<name>.toml`); `balanced` is the
    package's own map and needs no file."""
    d = resources.files("samvada").joinpath("profiles")
    out = {"balanced": None}
    for f in sorted(d.iterdir(), key=lambda f: f.name) if d.is_dir() else []:
        if f.name.endswith(".toml"):
            out[f.name[:-5]] = Path(str(f))
    return out


def own_profiles(env: Mapping[str, str] = os.environ) -> list[str]:
    d = paths.profiles_dir(env)
    names = sorted(f.stem for f in d.glob("*.toml")) if d.is_dir() else []
    if "like" not in names and paths.picked_voices_file(env).is_file():
        names.append("like")
    return names


def active_profile(env: Mapping[str, str] = os.environ) -> tuple[str, str]:
    """(name, where it was set: env, session, global or default)."""
    from .state import State, session_key
    st = State(paths.state_dir(env), session_key(env)).profile(env)
    return st.value, st.where


def layers(env: Mapping[str, str] = os.environ) -> list[tuple[str, Path | None]]:
    """The voice map's files, lowest first: (layer name, file); None is the package's own.

    $SAMVADA_VOICE_MAP is a whole other map (tests, bundles): it stands alone. Else the
    package's defaults; the profile in force, built in (`profile:<name>`) and then the
    owner's own file of it (`profile:<name> (yours)`); then the owner's `voices.local.toml`,
    which wins over every profile. Each when it exists.
    """
    if env.get("SAMVADA_VOICE_MAP"):
        return [("map", Path(env["SAMVADA_VOICE_MAP"]))]
    out: list[tuple[str, Path | None]] = [("default", None)]
    name, _ = active_profile(env)
    built = builtin_profiles().get(name)
    if built is not None:
        out.append((f"profile:{name}", built))
    own = paths.profile_file(name, env)
    if own.is_file():
        out.append((f"profile:{name} (yours)", own))
    if paths.owner_voices_file(env).is_file():
        out.append(("yours", paths.owner_voices_file(env)))
    return out


SKIPPED: list[str] = []  # profile entries left out by the last load (their model is missing)


def package_map_path() -> Path:
    return Path(str(resources.files("samvada").joinpath("voices.toml")))


def _missing(t: dict, below: dict, env: Mapping[str, str]) -> str:
    """Why a profile's voice cannot speak here ("" when it can): its model, its engine, or
    the files the engine needs beside the model."""
    model, engine = t["model"], t.get("engine", below.get("engine", "piper"))
    if not _model_found(model, env):
        return "not installed here"
    if engine_where(engine, env) == "missing":
        return f"engine {engine} not installed"
    m = Path(model).expanduser()
    if not m.is_absolute():
        m = next(d / model for d in paths.voices_dirs(env) if (d / model).is_file())
    if engine == "kokoro":
        from . import kokoro
        if kokoro.voices_file(m) is None:
            return "no voices*.bin beside it"
        if "v1.1" in m.name and not m.with_name("config.json").is_file():
            return "no config.json beside it"
    if engine == "voicevox":
        from . import voicevox
        r = voicevox.root(m, env)
        if r is None or None in voicevox.runtime_files(r):
            return "VOICEVOX's runtime or dictionary missing"
    return ""


def _model_found(model: str, env: Mapping[str, str]) -> bool:
    m = Path(model).expanduser()
    if m.is_absolute():
        return m.is_file()
    return any((d / model).is_file() for d in paths.voices_dirs(env))


def load_voices(path: Path | None = None,
                env: Mapping[str, str] = os.environ) -> dict[str, Voice]:
    """The voice map, merged by language, then by key (env by key too); the highest layer wins.

    A language the owner adds needs engine, model and online. A model may be a bare file
    name (looked for in the voices folders), or a path: `~` is expanded.
    """
    found = [("map", path)] if path else layers(env)
    merged: dict[str, dict] = {}
    origin: dict[str, dict[str, str]] = {}
    SKIPPED.clear()
    for name, f in found:
        data = _read(f) if f else tomllib.loads(
            resources.files("samvada").joinpath("voices.toml").read_text(encoding="utf-8"))
        for lang, t in data.items():
            if name.startswith("profile:") and "model" in t:
                missing = _missing(t, merged.get(lang, {}), env)
                if missing:  # a profile names voices that may not be installed here
                    SKIPPED.append(f"{lang}: {name} names {t['model']}: {missing}")
                    continue
            if lang not in merged:
                missing = [k for k in REQUIRED if k not in t]
                if missing:
                    raise VoiceMapError(f"{f}: [{lang}] is a new language: it needs "
                                        f"{', '.join(missing)}")
                merged[lang], origin[lang] = {"env": {}}, {}
            if lang in origin and ("model" in t or (
                    "style" in t and t["style"] != merged[lang].get("style")
                    and t.get("engine", merged[lang].get("engine")) in PY_ENGINES)):
                # a new model, or another speaker in the same file (VOICEVOX, Kokoro):
                # the terms, credit and style of the old one do not carry over
                for key in ("license", "license_url", "credit", "style"):
                    if key not in t:
                        merged[lang].pop(key, None)
                        origin[lang].pop(key, None)
                if "engine" in t and t["engine"] != merged[lang].get("engine"):
                    merged[lang]["env"] = {}  # engine settings belong to the engine
                    for k in [k for k in origin[lang] if k.startswith("env.")]:
                        origin[lang].pop(k)
            for key, value in t.items():
                if key == "env":
                    merged[lang]["env"].update(value)
                    origin[lang].update({f"env.{k}": name for k in value})
                elif key == "fallback":
                    fb = merged[lang].setdefault("fallback", {})
                    if "model" in value:  # another fallback voice: the old one's terms go
                        for k in ("license", "license_url", "credit", "style"):
                            if k not in value:
                                fb.pop(k, None)
                    fb.update(value)
                    origin[lang].update({f"fallback.{k}": name for k in value})
                else:
                    merged[lang][key] = value
                    origin[lang][key] = name
    out = {}
    for lang, t in merged.items():
        model = t["model"]
        if model.startswith("~"):
            model = str(Path(model).expanduser())
        if t["engine"] in PY_ENGINES and not t.get("style"):
            what = "the style id" if t["engine"] == "voicevox" else "the voice name"
            raise VoiceMapError(f"[{lang}] engine {t['engine']} needs a style ({what}; "
                                "`samvada voices pick` lists them)")
        v = Voice(lang, t["engine"], model, t["online"], dict(t["env"]),
                  origin[lang], t.get("license", ""), t.get("credit", ""), t.get("style", ""))
        v.fallback = _fallback(lang, t.get("fallback"), v)
        out[lang] = v
    return out


def _fallback(lang: str, fb: dict | None, primary: Voice) -> Voice | None:
    """The language's fallback voice; when it is the very voice the owner made the primary
    (Kokoro as the default), the package's own primary (Piper) takes its place."""
    if not fb or not fb.get("engine") or not fb.get("model"):
        return None
    model = fb["model"]
    if model.startswith("~"):
        model = str(Path(model).expanduser())
    if fb["engine"] in PY_ENGINES and not fb.get("style"):
        raise VoiceMapError(f"[{lang}.fallback] engine {fb['engine']} needs a style")
    v = Voice(lang, fb["engine"], model, primary.online, dict(fb.get("env", {})), {},
              fb.get("license", ""), fb.get("credit", ""), str(fb.get("style", "")))
    if (v.engine, v.model, v.style) == (primary.engine, primary.model, primary.style):
        base = default_voice(lang)
        if base is None or (base.engine, base.model) == (primary.engine, primary.model):
            return None
        return base
    return v


def usable_home(voice: Voice | None, dirs: list[Path],
                env: Mapping[str, str] = os.environ) -> Path | None:
    """Where this voice can speak from: its pack is there and its engine installed; else None."""
    if voice is None:
        return None
    home = voice_home(voice, dirs)
    return home if home is not None and engine_where(voice.engine, env) != "missing" else None


def order(voice: Voice, dirs: list[Path], online_mode: str,
          env: Mapping[str, str] = os.environ) -> list[str]:
    """The voices a language tries, in order, each with what it needs (doctor, status)."""
    def label(v: Voice) -> str:
        name = v.style or Path(v.model).stem
        if usable_home(v, dirs, env) is not None:
            return f"{v.engine} {name}"
        what = "no voice pack" if voice_home(v, dirs) is None else f"engine {v.engine} not installed"
        hint = f"; samvada voices add {v.lang} --kokoro" if v.engine == "kokoro" else ""
        return f"{v.engine} {name} ({what}{hint})"
    out = [label(voice)]
    if voice.fallback is not None:
        out.append(label(voice.fallback))
    out.append(f"online {voice.online}" if online_mode != "off" else "online: off")
    return out


def voice_home(voice: Voice, dirs: list[Path]) -> Path | None:
    """The first voices folder that has this language's model; None: no voice pack.

    A model given as a path is found where it is; the engine still runs in the first voices
    folder that exists (Piper's Chinese phonemizer loads g2pW from its cwd), else beside it.
    """
    m = Path(voice.model)
    if m.is_absolute():
        if not m.is_file():
            return None
        return next((d for d in dirs if d.is_dir()), m.parent)
    for d in dirs:
        if (d / voice.model).is_file():
            return d
    return None


def has_pack(voice: Voice, dirs: list[Path]) -> bool:
    return voice_home(voice, dirs) is not None


def _py_engine_cmd(engine: str, env: Mapping[str, str]) -> tuple[list[str], dict] | None:
    """The engine's package from Samvada's engines folder (<engines>/<engine>/), else from
    this Python."""
    package, runner = PY_ENGINES[engine]
    d = paths.engines_dir(env) / engine
    if (d / package).is_dir():
        pp = env.get("PYTHONPATH")
        return [sys.executable, "-s", "-m", runner], {
            "PYTHONPATH": str(d) + (os.pathsep + pp if pp else "")}
    if importlib.util.find_spec(package):
        return [sys.executable, "-m", runner], {}
    return None


def engine_cmd(engine: str, env: Mapping[str, str] = os.environ) -> tuple[list[str], dict] | None:
    """How to start an engine, and the environment it adds; None when it is not installed."""
    if engine in PY_ENGINES:
        return _py_engine_cmd(engine, env)
    d = paths.engines_dir(env) / engine
    if engine_ok(d):
        pp = env.get("PYTHONPATH")
        return [sys.executable, "-s", "-m", "piper"], {
            "PYTHONPATH": str(d) + (os.pathsep + pp if pp else "")}
    exe = shutil.which(engine, path=env.get("PATH"))
    return ([exe], {}) if exe else None


def engine_where(engine: str, env: Mapping[str, str] = os.environ) -> str:
    if engine in PY_ENGINES:
        if (paths.engines_dir(env) / engine / PY_ENGINES[engine][0]).is_dir():
            return "samvada"
        return "python" if importlib.util.find_spec(PY_ENGINES[engine][0]) else "missing"
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
    elif voice.engine == "voicevox":
        model = voice.model if Path(voice.model).is_absolute() else str(home / voice.model)
        cmd, stdin = base + ["--model", model, "--style", voice.style, "--out", str(out)], text
    elif voice.engine == "kokoro":
        model = voice.model if Path(voice.model).is_absolute() else str(home / voice.model)
        cmd, stdin = base + ["--model", model, "--voice", voice.style, "--lang", voice.lang,
                             "--out", str(out)], text
    else:
        return False
    if voice.engine in PY_ENGINES:  # a warm engine first: the model stays loaded between clips
        from . import warm
        if warm.speak(voice.engine, base, {}, env, model, voice.lang, voice.style, text, out,
                      debug):
            return True
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
