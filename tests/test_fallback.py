"""Kokoro as the fallback for en and zh: Piper first; Kokoro when Piper's voice is missing or
fails; a user can make Kokoro the default (Piper then falls back); --kokoro installs it."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from samvada import engines, paths

FAKE_KOKORO = '''
import os
class Kokoro:
    def __init__(self, model, voices, vocab_config=None): self.model = os.path.basename(model)
    @classmethod
    def from_session(cls, s, voices, vocab_config=None): raise TypeError("fake")
    def create(self, text, voice, speed=1.0, lang="en-us", is_phonemes=False):
        with open(os.environ["FAKE_LOG"], "a") as f:
            f.write(f"kokoro {self.model} {voice} {text}\\n")
        class A(list):
            def clip(self, a, b): return self
            def __mul__(self, k): return self
            def astype(self, t): return self
            def tobytes(self): return b"\\0\\0" * 10
        return A([0.0] * 10), 24000
'''


def kokoro_installed(env, langs=("en", "zh")) -> None:
    d = Path(env.vars["XDG_DATA_HOME"]) / "samvada" / "engines" / "kokoro"
    (d / "kokoro_onnx").mkdir(parents=True, exist_ok=True)
    (d / "kokoro_onnx" / "__init__.py").write_text(FAKE_KOKORO)
    (d / "misaki").mkdir(exist_ok=True)
    (d / "misaki" / "__init__.py").write_text("")
    (d / "misaki" / "zh.py").write_text("class ZHG2P:\n    def __init__(self, version=None): pass\n"
                                        "    def __call__(self, t): return (t, None)\n")
    for lang, (folder, name, voice) in {"en": ("v1.0", "v1.0", "af_heart"),
                                        "zh": ("v1.1-zh", "v1.1-zh", "zf_001")}.items():
        if lang not in langs:
            continue
        m = env.voices / "kokoro" / folder
        m.mkdir(parents=True, exist_ok=True)
        (m / f"kokoro-{name}.onnx").write_text("m")
        with zipfile.ZipFile(m / f"voices-{name}.bin", "w") as z:
            z.writestr(f"{voice}.npy", "x")
        (m / "config.json").write_text("{}")


def kokoro_lines(env) -> list[str]:
    return [l for l in env.lines() if l.startswith("kokoro ")]


def test_piper_speaks_first(env):
    kokoro_installed(env)
    r = env.run("speak", "en", "hi")
    assert r.returncode == 0 and "used kokoro" not in r.stderr
    lines = env.lines()
    assert any(l.startswith("piper en_US-lessac-high") for l in lines)
    assert not [l for l in lines if l.startswith("kokoro ")]


def test_kokoro_when_piper_is_missing(env):
    kokoro_installed(env)
    (env.voices / "en_US-lessac-high.onnx").unlink()
    r = env.run("speak", "en", "hi")
    assert r.returncode == 0, r.stderr
    assert "en: piper has no voice pack, used kokoro af_heart" in r.stderr
    assert kokoro_lines(env) == ["kokoro kokoro-v1.0.onnx af_heart hi"]


def test_kokoro_when_piper_fails(env):
    kokoro_installed(env)
    r = env.run("speak", "zh", "你好", FAKE_FAIL="piper:zh")
    assert r.returncode == 0, r.stderr
    assert "zh: piper failed, used kokoro zf_001" in r.stderr
    assert kokoro_lines(env) == ["kokoro kokoro-v1.1-zh.onnx zf_001 你好"]


def test_kokoro_when_piper_is_not_installed(env):
    """The pack is there, the engine is not: say so, as status does, not "failed"."""
    kokoro_installed(env)
    (env.bin / "piper").unlink()
    r = env.run("speak", "en", "hi")
    assert r.returncode == 0, r.stderr
    assert "en: piper engine not installed (samvada voices add en), used kokoro af_heart" in r.stderr
    assert kokoro_lines(env) == ["kokoro kokoro-v1.0.onnx af_heart hi"]


def test_no_kokoro_installed_means_the_old_way(env):
    (env.voices / "en_US-lessac-high.onnx").unlink()
    r = env.run("speak", "en", "hi")
    assert "skipped en(no voice pack)" in r.stderr and not kokoro_lines(env)
    r = env.run("speak", "zh", "你好", FAKE_FAIL="piper:zh")
    assert r.returncode == 1 and "zh failed offline" in r.stderr


def test_kokoro_as_the_default_and_piper_as_its_fallback(env):
    kokoro_installed(env)
    d = Path(env.vars["HOME"]) / ".config" / "samvada"
    d.mkdir(parents=True)
    (d / "voices.local.toml").write_text('[en]\nengine = "kokoro"\nmodel = "kokoro/v1.0/kokoro-v1.0.onnx"\n'
                                         'style = "af_heart"\n')
    v = engines.load_voices(env={**env.vars, "XDG_CONFIG_HOME": str(d.parent)})["en"]
    assert (v.engine, v.fallback.engine, v.fallback.model) == ("kokoro", "piper", "en_US-lessac-high.onnx")
    r = env.run("speak", "en", "hi")
    assert r.returncode == 0 and kokoro_lines(env) == ["kokoro kokoro-v1.0.onnx af_heart hi"]
    (env.voices / "kokoro" / "v1.0" / "kokoro-v1.0.onnx").unlink()
    r = env.run("speak", "en", "hi")
    assert "en: kokoro has no voice pack, used piper en_US-lessac-high" in r.stderr


def test_doctor_and_status_show_the_order(env):
    for m in env.voices.glob("*.onnx"):
        Path(f"{m}.json").write_text("{}")
    r = env.run("doctor")
    assert "order: piper en_US-lessac-high → kokoro af_heart (no voice pack; samvada voices add en --kokoro) → online: off" in r.stdout
    kokoro_installed(env)
    r = env.run("status")
    assert "  zh: piper zh_CN-xiao_ya-medium → kokoro zf_001 → online: off" in r.stdout


def test_the_fallback_table_is_checked(env):
    d = Path(env.vars["HOME"]) / ".config" / "samvada"
    d.mkdir(parents=True)
    (d / "voices.local.toml").write_text('[en.fallback]\nmodle = "x"\n')
    r = env.run("status")
    assert r.returncode == 2 and "[en.fallback] unknown key modle" in r.stderr
    (d / "voices.local.toml").write_text('[en.fallback]\nstyle = "af_bella"\n')
    v = engines.load_voices(env={**env.vars, "XDG_CONFIG_HOME": str(d.parent)})["en"]
    assert v.fallback.style == "af_bella" and v.fallback.model == "kokoro/v1.0/kokoro-v1.0.onnx"
