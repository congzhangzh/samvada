"""The VOICEVOX engine: model files (.vvm), styles, the runner, the picker and the doctor.

A fake `voicevox_core` package in Samvada's engines folder stands in for the real one, so
the real runner (`python -m samvada.voicevox`) is what runs."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from samvada import engines, paths, pick, voicevox

FAKE_CORE = '''
import os
class Onnxruntime:
    @staticmethod
    def load_once(filename):
        assert os.path.exists(filename), filename
        return Onnxruntime()
class OpenJtalk:
    def __init__(self, d):
        assert os.path.isdir(d), d
class VoiceModelFile:
    def __init__(self, p): self.p = p
    @staticmethod
    def open(p): return VoiceModelFile(p)
    def __enter__(self): return self
    def __exit__(self, *a): return False
class Synthesizer:
    def __init__(self, ort, ojt): self.model = None
    def load_voice_model(self, m): self.model = m.p
    def tts(self, text, style):
        with open(os.environ["FAKE_LOG"], "a") as f:
            f.write(f"voicevox {os.path.basename(self.model)} {style} {text}\\n")
        return b"RIFF"
'''

README = """# VOICEVOX 音声モデル 利用規約

---

# 音声ライブラリ利用規約

## 冥鳴ひまり

冥鳴ひまりの音声ライブラリを用いて生成した音声は、
「VOICEVOX:冥鳴ひまり」とクレジットを記載すれば、商用・非商用で利用可能です。

利用規約の詳細は以下をご確認ください。
https://example.invalid/terms
"""


def vv_tree(base: Path) -> Path:
    """A VOICEVOX download: onnxruntime/, dict/, models/vvms/1.vvm with Himari and a song."""
    r = base / "voicevox_core"
    (r / "onnxruntime" / "lib").mkdir(parents=True)
    (r / "onnxruntime" / "lib" / "libvoicevox_onnxruntime.so.1.17.3").write_text("lib")
    (r / "dict" / "open_jtalk_dic_utf_8-1.11").mkdir(parents=True)
    vvms = r / "models" / "vvms"
    vvms.mkdir(parents=True)
    (r / "models" / "README.txt").write_text(README, encoding="utf-8")
    metas = [{"name": "冥鳴ひまり", "styles": [{"name": "ノーマル", "id": 14}]},
             {"name": "四国めたん", "styles": [{"name": "ノーマル", "id": 3002, "type": "frame_decode"}]}]
    with zipfile.ZipFile(vvms / "1.vvm", "w") as z:
        z.writestr("metas.json", json.dumps(metas, ensure_ascii=False))
        z.writestr("manifest.json", "{}")
    return vvms / "1.vvm"


def engine(env) -> None:
    d = Path(env.vars["XDG_DATA_HOME"]) / "samvada" / "engines" / "voicevox" / "voicevox_core"
    d.mkdir(parents=True)
    (d / "__init__.py").write_text("")
    (d / "blocking.py").write_text(FAKE_CORE)


def full(env) -> dict:
    return {**env.vars, "XDG_CONFIG_HOME": str(Path(env.vars["HOME"]) / ".config")}


def own(env, text: str) -> None:
    d = Path(env.vars["HOME"]) / ".config" / "samvada"
    d.mkdir(parents=True, exist_ok=True)
    (d / "voices.local.toml").write_text(text, encoding="utf-8")


def test_styles_skip_singing_and_read_the_terms(tmp_path):
    vvm = vv_tree(tmp_path)
    st = voicevox.styles(vvm)
    assert [(s.speaker, s.id) for s in st] == [("冥鳴ひまり", 14)]
    assert st[0].label == "冥鳴ひまり ノーマル  (1.vvm #14)"
    t, credit = voicevox.terms(vvm)["冥鳴ひまり"]
    assert credit == "VOICEVOX:冥鳴ひまり" and "商用・非商用で利用可能" in t
    assert voicevox.root(vvm) == tmp_path / "voicevox_core"


def test_speak_with_himari(env):
    vvm = vv_tree(env.tmp)
    engine(env)
    own(env, f'[ja]\nengine = "voicevox"\nmodel = "{vvm}"\nstyle = 14\n')
    r = env.run("speak", "ja", "こんにちは")
    assert r.returncode == 0, r.stderr
    log = env.lines()
    assert "voicevox 1.vvm 14 こんにちは" in log and any(l.startswith("play ") for l in log)


def test_voicevox_needs_a_style_and_a_known_engine(env):
    own(env, '[ja]\nengine = "voicevox"\nmodel = "1.vvm"\n')
    r = env.run("status")
    assert r.returncode == 2 and "engine voicevox needs a style" in r.stderr
    own(env, '[ja]\nengine = "voicevoxx"\n')
    r = env.run("status")
    assert r.returncode == 2 and "unknown engine voicevoxx" in r.stderr


def test_picker_lists_himari_and_saves_engine_style_credit(env):
    vvm = vv_tree(env.tmp / "trials" / "ja")
    engine(env)
    e = full(env)
    voices = engines.load_voices(env=e)
    cs = pick.candidates("ja", voices["ja"], paths.voices_dirs(e), [env.tmp / "trials"])
    assert [c.name for c in cs] == ["tsukuyomi-chan-6lang-fp16.onnx", "冥鳴ひまり ノーマル  (1.vvm #14)"]
    assert cs[0].engine == "piper-plus"
    h = cs[1]
    assert (h.engine, h.style, h.credit) == ("voicevox", "14", "VOICEVOX:冥鳴ひまり")
    pick.save("ja", h, e)
    v = engines.load_voices(env=e)["ja"]
    assert (v.engine, v.model, v.style, v.credit) == ("voicevox", str(vvm), "14", "VOICEVOX:冥鳴ひまり")
    assert h.same(v) and not cs[0].same(v)
    # with Himari chosen, Tsukuyomi is still offered with its own engine, and goes back cleanly
    cs = pick.candidates("ja", v, paths.voices_dirs(e), [env.tmp / "trials"])
    assert cs[0].name.startswith("冥鳴ひまり") and cs[1].engine == "piper-plus"
    pick.save("ja", cs[1], e)
    v = engines.load_voices(env=e)["ja"]
    assert (v.engine, v.style, v.credit) == ("piper-plus", "", "■Piper Plus:つくよみちゃん")


def test_picker_plays_himari(env):
    vv_tree(env.tmp / "trials" / "ja")
    engine(env)
    r = env.run("voices", "pick", "ja", "--dir", str(env.tmp / "trials"), "--plain", input="2\nq\n")
    assert r.returncode == 0, r.stderr
    assert "credit: VOICEVOX:冥鳴ひまり" in r.stdout
    assert any(l.startswith("voicevox 1.vvm 14 ") for l in env.lines())


def test_doctor_checks_voicevox(env):
    vvm = vv_tree(env.tmp)
    for m in env.voices.glob("*.onnx"):
        Path(f"{m}.json").write_text("{}")
    own(env, f'[ja]\nengine = "voicevox"\nmodel = "{vvm}"\nstyle = 14\n')
    r = env.run("doctor")
    assert r.returncode == 1 and "engine voicevox not installed" in r.stdout
    engine(env)
    r = env.run("doctor")
    assert r.returncode == 0, r.stdout
    assert "冥鳴ひまり ノーマル" in r.stdout and "credit: " not in r.stdout.split("ja ")[0]
    (env.tmp / "voicevox_core" / "dict" / "open_jtalk_dic_utf_8-1.11").rmdir()
    r = env.run("doctor")
    assert r.returncode == 1 and "the dictionary is missing" in r.stdout


def test_picking_the_voice_now_keeps_its_credit(env):
    e = full(env)
    v = engines.load_voices(env=e)["ja"]
    now = pick.candidates("ja", v, paths.voices_dirs(e))[0]
    pick.save("ja", now, e)
    assert engines.load_voices(env=e)["ja"].credit == "■Piper Plus:つくよみちゃん"


def test_a_speaker_with_a_space_and_nemo_and_unknown_terms(tmp_path):
    vvm = vv_tree(tmp_path)
    readme = vvm.parent.parent / "README.txt"
    readme.write_text(readme.read_text(encoding="utf-8") + """
## VOICEVOX Nemo

VOICEVOX Nemo の音声ライブラリを用いて生成した音声は、「VOICEVOX Nemo」とクレジットを記載すれば、商用・非商用で利用可能です。
""", encoding="utf-8")
    metas = [{"name": "†聖騎士 紅桜†", "styles": [{"name": "ノーマル", "id": 51}]},
             {"name": "雨晴はう", "styles": [{"name": "ノーマル", "id": 10}]}]
    with zipfile.ZipFile(vvm.parent / "0.vvm", "w") as z:
        z.writestr("metas.json", json.dumps(metas, ensure_ascii=False))
    with zipfile.ZipFile(vvm.parent / "n0.vvm", "w") as z:
        z.writestr("metas.json", json.dumps([{"name": "女声1", "styles": [{"name": "ノーマル", "id": 10005}]}],
                                            ensure_ascii=False))
    cs = pick._vv("ja", sorted(vvm.parent.glob("*.vvm")))
    by = {c.key: c for c in cs}
    assert by["voicevox:女声1/ノーマル"].credit == "VOICEVOX Nemo"
    hau = by["voicevox:雨晴はう/ノーマル"]
    assert hau.credit == "" and "not in the downloader's README" in hau.note
    v = engines.Voice("ja", "voicevox", "x", "x", style="0")
    t = pick.table(["ja"], {"ja": v}, {"ja": cs})
    assert "| †聖騎士 紅桜† | ノーマル (51) |" in t
    assert "| 雨晴はう | ノーマル (10) | unknown |  |" in t


def test_another_speaker_in_the_same_file_drops_the_old_credit(env):
    vvm = vv_tree(env.tmp)
    engine(env)  # a profile's voice applies only where its engine is installed
    d = Path(env.vars["HOME"]) / ".config" / "samvada" / "profiles"
    d.mkdir(parents=True)
    (d / "balanced.toml").write_text(f'[ja]\nengine = "voicevox"\nmodel = "{vvm}"\nstyle = 3\ncredit = "VOICEVOX:other"\n')
    # the local file names only another style: the credit of style 3's speaker must not stay
    own(env, '[ja]\nstyle = "14"\n')
    v = engines.load_voices(env=full(env))["ja"]
    assert v.style == "14" and v.credit == ""
