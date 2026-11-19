"""Voice profiles: balanced (the package's defaults, what a release ships), learn (built in),
the owner's own (like, or any name); each of the owner's files overrides the built-in profile
of its name, key by key; voices.local.toml wins over every profile."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from samvada import engines, pick


def cfg(env) -> Path:
    d = Path(env.vars["HOME"]) / ".config" / "samvada"
    d.mkdir(parents=True, exist_ok=True)
    return d


def full(env, **extra) -> dict:
    return {**env.vars, "XDG_CONFIG_HOME": str(cfg(env).parent), **extra}


LEARN = """[en]
engine = "kokoro"
model = "kokoro/v1.0/kokoro-v1.0.onnx"
style = "af_heart"
[zh]
engine = "kokoro"
model = "kokoro/v1.1-zh/kokoro-v1.1-zh.onnx"
style = "zf_001"
[fr]
engine = "kokoro"
model = "kokoro/v1.0/kokoro-v1.0.onnx"
style = "ff_siwis"
"""


def own_profile(env, name: str = "learn", text: str = LEARN) -> Path:
    d = cfg(env) / "profiles"
    d.mkdir(exist_ok=True)
    (d / f"{name}.toml").write_text(text, encoding="utf-8")
    return d / f"{name}.toml"


def builtin(monkeypatch, tmp_path, text: str = LEARN):
    """A built-in profile `learn` for the in-process tests (the package ships none but balanced)."""
    f = tmp_path / "builtin-learn.toml"
    f.write_text(text, encoding="utf-8")
    monkeypatch.setattr(engines, "builtin_profiles", lambda: {"balanced": None, "learn": f})


def kokoro_engine(env) -> None:
    (Path(env.vars["XDG_DATA_HOME"]) / "samvada" / "engines" / "kokoro" / "kokoro_onnx").mkdir(parents=True)


def learn_files(env, engine: bool = True) -> None:
    """The models `learn` names, in the voices folder (and the Kokoro engine)."""
    if engine:
        kokoro_engine(env)
    k = env.voices / "kokoro" / "v1.0"
    k.mkdir(parents=True)
    (k / "kokoro-v1.0.onnx").write_text("m", encoding="utf-8")
    with zipfile.ZipFile(k / "voices-v1.0.bin", "w") as z:
        for v in ("af_heart", "ff_siwis"):
            z.writestr(f"{v}.npy", "x")


def test_balanced_is_the_default_and_needs_nothing(env):
    assert engines.active_profile(full(env)) == ("balanced", "default")
    v = engines.load_voices(env=full(env))
    assert v["en"].model == "en_US-lessac-high.onnx" and set(v["en"].origin.values()) == {"default"}
    r = env.run("profile")
    assert r.returncode == 0 and "profile: balanced (default)" in r.stdout
    assert "● balanced" in r.stdout and "learn" not in r.stdout   # nothing of anyone's ships


def test_learn_uses_what_is_installed_and_leaves_out_the_rest(env):
    learn_files(env)
    own_profile(env)
    r = env.run("profile", "use", "learn")
    assert r.returncode == 0, r.stderr
    v = engines.load_voices(env=full(env))
    assert (v["en"].engine, v["en"].style) == ("kokoro", "af_heart")
    assert v["en"].origin["model"] == "profile:learn (yours)"
    assert (v["fr"].engine, v["fr"].style) == ("kokoro", "ff_siwis")
    assert v["zh"].model == "zh_CN-xiao_ya-medium.onnx"            # v1.1-zh not here: default
    assert any(s.startswith("zh: profile:learn (yours) names kokoro/v1.1-zh") for s in engines.SKIPPED)
    assert "note: zh: profile:learn (yours) names" in env.run("profile").stdout


def test_yours_overrides_the_built_in_profile_key_by_key(env, monkeypatch, tmp_path):
    learn_files(env)
    builtin(monkeypatch, tmp_path)      # the mechanism stays, though the package ships none
    env.run("profile", "use", "learn")
    own_profile(env, text='[en]\nstyle = "af_bella"\n')
    v = engines.load_voices(env=full(env))["en"]
    assert (v.engine, v.style) == ("kokoro", "af_bella")
    assert v.origin["style"] == "profile:learn (yours)" and v.origin["model"] == "profile:learn"
    (cfg(env) / "voices.local.toml").write_text('[en]\nstyle = "af_nicole"\n', encoding="utf-8")
    assert engines.load_voices(env=full(env))["en"].style == "af_nicole"   # local wins over all


def test_a_session_profile_and_switching_back(env):
    learn_files(env)
    env.run("profile", "use", "learn")
    assert engines.active_profile(full(env)) == ("learn", "global")
    env.run("--session", "profile", "use", "balanced")
    assert engines.active_profile(full(env)) == ("balanced", "session")
    assert engines.load_voices(env=full(env))["en"].engine == "piper"
    env.run("--session", "profile", "reset")
    assert engines.active_profile(full(env)) == ("learn", "global")
    assert engines.active_profile(full(env, SAMVADA_PROFILE="x")) == ("x", "env")


def test_the_picker_fills_the_profile_in_force(env):
    env.run("profile", "use", "like")
    e = full(env)
    voices = engines.load_voices(env=e)
    c = pick.candidates("en", voices["en"], [env.voices])[0]
    f = pick.save("en", c, e)
    assert f == cfg(env) / "profiles" / "like.toml"
    assert engines.load_voices(env=e)["en"].origin["model"] == "profile:like (yours)"
    env.run("profile", "use", "balanced")
    assert engines.load_voices(env=full(env))["en"].origin["model"] == "default"


def test_the_older_picker_file_is_the_like_profile(env):
    (cfg(env) / "voices.toml").write_text('[en]\nmodel = "en_US-lessac-high.onnx"\ncredit = "old"\n', encoding="utf-8")
    env.run("profile", "use", "like")
    v = engines.load_voices(env=full(env))["en"]
    assert v.credit == "old" and v.origin["credit"] == "profile:like (yours)"
    # the first pick writes profiles/like.toml from it, and leaves voices.toml in place
    c = pick.candidates("fr", engines.load_voices(env=full(env))["fr"], [env.voices])[0]
    pick.save("fr", c, full(env))
    like = (cfg(env) / "profiles" / "like.toml").read_text(encoding="utf-8")
    assert "[en]" in like and "[fr]" in like and (cfg(env) / "voices.toml").is_file()


def test_a_bad_profile_name(env):
    r = env.run("profile", "use", "../x")
    assert r.returncode == 2 and "a profile name is" in r.stderr


def test_a_profile_voice_without_its_engine_leaves_the_default(env):
    learn_files(env, engine=False)
    own_profile(env)
    env.run("profile", "use", "learn")
    v = engines.load_voices(env=full(env))
    assert v["en"].engine == "piper"
    assert any(x == "en: profile:learn (yours) names kokoro/v1.0/kokoro-v1.0.onnx: engine kokoro not installed"
               for x in engines.SKIPPED), engines.SKIPPED
