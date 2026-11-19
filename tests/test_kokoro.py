"""The Kokoro engine: a model file, a voices-*.bin (an npz) beside it, voices by name.

Fake `kokoro_onnx` and `misaki` packages in Samvada's engines folder stand in for the real
ones, so the real runner (`python -m samvada.kokoro`) is what runs."""

from __future__ import annotations

import wave
import zipfile
from pathlib import Path

from samvada import engines, kokoro, paths, pick

FAKE_KOKORO = '''
import os
class Kokoro:
    def __init__(self, model, voices, vocab_config=None):
        self.model, self.cfg = os.path.basename(model), vocab_config
    def create(self, text, voice, speed=1.0, lang="en-us", is_phonemes=False):
        import array
        with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as f:
            f.write(f"kokoro {self.model} {voice} {'ph' if is_phonemes else lang} "
                    f"cfg={'yes' if self.cfg else 'no'} {text}\\n")
        class A(list):
            def clip(self, a, b): return self
            def __mul__(self, k): return self
            def astype(self, t): return self
            def tobytes(self): return b"\\0\\0" * 10
        return A([0.0] * 10), 24000
'''
FAKE_MISAKI_ZH = '''
class ZHG2P:
    def __init__(self, version=None): self.v = version
    def __call__(self, text): return (f"zh{self.v}:" + text, None)
'''
FAKE_MISAKI_JA = '''
class JAG2P:
    def __call__(self, text): return ("ja:" + text, None)
'''


def engine(env) -> None:
    d = Path(env.vars["XDG_DATA_HOME"]) / "samvada" / "engines" / "kokoro"
    (d / "kokoro_onnx").mkdir(parents=True)
    (d / "kokoro_onnx" / "__init__.py").write_text(FAKE_KOKORO, encoding="utf-8")
    (d / "misaki").mkdir()
    (d / "misaki" / "__init__.py").write_text("", encoding="utf-8")
    (d / "misaki" / "zh.py").write_text(FAKE_MISAKI_ZH, encoding="utf-8")
    (d / "misaki" / "ja.py").write_text(FAKE_MISAKI_JA, encoding="utf-8")


def model(base: Path, name: str, voices: list[str], config: bool = False) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    m = base / f"kokoro-{name}.onnx"
    m.write_text("model", encoding="utf-8")
    with zipfile.ZipFile(base / f"voices-{name}.bin", "w") as z:
        for v in voices:
            z.writestr(f"{v}.npy", "x")
    if config:
        (base / "config.json").write_text("{}", encoding="utf-8")
    return m


def full(env) -> dict:
    return {**env.vars, "XDG_CONFIG_HOME": str(Path(env.vars["HOME"]) / ".config")}


def own(env, text: str) -> None:
    d = Path(env.vars["HOME"]) / ".config" / "samvada"
    d.mkdir(parents=True, exist_ok=True)
    (d / "voices.local.toml").write_text(text, encoding="utf-8")


def test_voices_by_language_and_their_notes(tmp_path):
    m = model(tmp_path, "v1.0", ["af_heart", "bf_emma", "zf_xiaoxiao", "jf_alpha", "ff_siwis"])
    assert [v.name for v in kokoro.voices(m, "en")] == ["af_heart", "bf_emma"]
    assert [v.name for v in kokoro.voices(m, "zh")] == ["zf_xiaoxiao"]
    assert kokoro.voices_file(m).name == "voices-v1.0.bin"
    v = kokoro.voices(m, "zh")[0]
    assert v.key == "kokoro:v1.0/zf_xiaoxiao"
    assert v.note == ("Kokoro v1.0, Mandarin Chinese, female; its authors' grade "
                      "(data quality | hours | overall): C | MM minutes | D")
    z = model(tmp_path / "zh", "v1.1-zh", ["zf_001", "af_maple"], config=True)
    assert "LongMaoData" in kokoro.voices(z, "zh")[0].note
    assert "synthetic" in kokoro.voices(z, "en")[0].note


def test_speak_every_language_with_the_right_reader(env):
    engine(env)
    m10 = model(env.tmp / "k", "v1.0", ["af_heart", "jf_alpha", "ff_siwis"])
    m11 = model(env.tmp / "kzh", "v1.1-zh", ["zf_001"], config=True)
    own(env, f'[en]\nengine = "kokoro"\nmodel = "{m10}"\nstyle = "af_heart"\n'
             f'[zh]\nengine = "kokoro"\nmodel = "{m11}"\nstyle = "zf_001"\n'
             f'[ja]\nengine = "kokoro"\nmodel = "{m10}"\nstyle = "jf_alpha"\n'
             f'[fr]\nengine = "kokoro"\nmodel = "{m10}"\nstyle = "ff_siwis"\n')
    r = env.run("speak", "en", "hi", "zh", "你好", "ja", "やあ", "fr", "salut")
    assert r.returncode == 0, r.stderr
    log = [l for l in env.lines() if l.startswith("kokoro ")]
    assert log == ["kokoro kokoro-v1.0.onnx af_heart en-us cfg=no hi",
                   "kokoro kokoro-v1.1-zh.onnx zf_001 ph cfg=yes zh1.1:你好",
                   "kokoro kokoro-v1.0.onnx jf_alpha ph cfg=no ja:やあ",
                   "kokoro kokoro-v1.0.onnx ff_siwis fr-fr cfg=no salut"]


def test_the_runner_writes_a_real_wav(env, tmp_path):
    engine(env)
    m = model(tmp_path, "v1.0", ["af_heart"])
    out = tmp_path / "x.wav"
    import os, subprocess, sys
    e = {**env.vars, "PYTHONPATH": f"{Path(env.vars['XDG_DATA_HOME'])}/samvada/engines/kokoro"
                                   f"{os.pathsep}{Path(__file__).resolve().parents[1] / 'src'}"}
    r = subprocess.run([sys.executable, "-m", "samvada.kokoro", "--model", str(m), "--voice",
                        "af_heart", "--lang", "en", "--out", str(out)], input="hi", text=True, encoding="utf-8", errors="replace",
                       env=e, capture_output=True)
    assert r.returncode == 0, r.stderr
    with wave.open(str(out)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (24000, 1, 2)


def test_picker_lists_kokoro_per_language_and_saves(env):
    engine(env)  # a profile's voice applies only where its engine is installed
    root = env.tmp / "trials"
    model(root / "kokoro", "v1.0", ["af_heart", "zf_xiaoxiao", "jf_alpha"])
    model(root / "kokoro-zh", "v1.1-zh", ["zf_001", "zm_010"], config=True)
    e = full(env)
    voices = engines.load_voices(env=e)
    zh = pick.candidates("zh", voices["zh"], paths.voices_dirs(e), [root])
    assert [c.name for c in zh] == ["zh_CN-xiao_ya-medium.onnx", "zf_xiaoxiao  (Kokoro v1.0)",
                                    "zf_001  (Kokoro v1.1-zh)", "zm_010  (Kokoro v1.1-zh)"]
    c = zh[2]
    assert pick.terms_short(c) == "free (Apache-2.0)" and pick.about_short(c) == "Kokoro, female, pro data, >100 h"
    pick.save("zh", c, e)
    v = engines.load_voices(env=e)["zh"]
    assert (v.engine, v.style, v.credit, v.env) == ("kokoro", "zf_001", "", {})
    assert c.same(v)
    assert pick.about_short(zh[1]) == "Kokoro, female, grade D"


def test_doctor_checks_kokoro(env):
    for m in env.voices.glob("*.onnx"):
        Path(f"{m}.json").write_text("{}", encoding="utf-8")
    m = model(env.tmp / "kzh", "v1.1-zh", ["zf_001"])
    own(env, f'[zh]\nengine = "kokoro"\nmodel = "{m}"\nstyle = "zf_999"\n')
    r = env.run("doctor")
    assert r.returncode == 1
    assert "engine kokoro not installed" in r.stdout
    assert "voice zf_999 is not in voices-v1.1-zh.bin" in r.stdout
    assert "kokoro-v1.1-zh.onnx needs its config.json beside it" in r.stdout
    engine(env)
    (m.parent / "config.json").write_text("{}", encoding="utf-8")
    own(env, f'[zh]\nengine = "kokoro"\nmodel = "{m}"\nstyle = "zf_001"\n')
    r = env.run("doctor")
    assert r.returncode == 0, r.stdout
