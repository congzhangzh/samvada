"""English words inside Chinese text, with Kokoro's Chinese model (v1.1-zh).

misaki's Chinese reader gives each run of Latin letters to an `en_callable`; without one it
puts ❓ there and the word is not spoken. Samvada gives it kokoro-onnx's own espeak (en-us),
the r-coloured vowels spelled out for the model's vocabulary.

Fakes of kokoro_onnx, misaki and phonemizer stand in for the real ones; the last test runs the
real engine when $SAMVADA_REAL_KOKORO_ENGINE (an engines/kokoro folder) and
$SAMVADA_REAL_KOKORO_ZH (kokoro-v1.1-zh.onnx, its voices and config.json beside it) are set,
with $SAMVADA_REAL_KOKORO_PYTHON when that folder was installed for another Python."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from samvada import kokoro

from test_kokoro import model, own

FAKE_KOKORO = '''
import os
class _Tok:
    vocab = {c: i for i, c in enumerate("abcdefghijklmnopqrstuvwxyz əɹ")}
class Kokoro:
    def __init__(self, model, voices, vocab_config=None):
        self.model, self.cfg, self.tokenizer = os.path.basename(model), vocab_config, _Tok()
    def create(self, text, voice, speed=1.0, lang="en-us", is_phonemes=False):
        with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as f:
            f.write(f"kokoro {self.model} {voice} {text}\\n")
        class A(list):
            def clip(self, a, b): return self
            def __mul__(self, k): return self
            def astype(self, t): return self
            def tobytes(self): return b"\\0\\0" * 10
        return A([0.0] * 10), 24000
'''
FAKE_TOKENIZER = "import threading\n_espeak_lock = threading.Lock()\n"
# Like misaki's: each run of Latin letters goes to en_callable, or becomes ❓ without one.
FAKE_MISAKI_ZH = '''
import re
class ZHG2P:
    def __init__(self, version=None, unk="❓", en_callable=None):
        self.v, self.unk, self.en = version, unk, en_callable
    def __call__(self, text):
        out = []
        for en, zh in re.findall(r"([A-Za-z \\'-]*[A-Za-z][A-Za-z \\'-]*)|([^A-Za-z]+)", text):
            en, zh = en.strip(), zh.strip()
            out.append(zh if zh else (self.en(en) if self.en else self.unk))
        return (f"zh{self.v}:" + " ".join(out), None)
'''
# espeak, faked: lower case, an r-coloured vowel at the end, a sign the vocabulary lacks.
FAKE_PHONEMIZER = '''
import os
class EspeakBackend:
    def __init__(self, lang, preserve_punctuation=False, with_stress=False):
        with open(os.environ.get("FAKE_LOG", os.devnull), "a", encoding="utf-8") as f:
            f.write(f"espeak {lang}\\n")
    def phonemize(self, texts):
        return [t.lower() + "ɚˈ " for t in texts]
'''


def fakes(base: Path) -> Path:
    d = base / "samvada" / "engines" / "kokoro"
    for path, text in {"kokoro_onnx/__init__.py": FAKE_KOKORO,
                       "kokoro_onnx/tokenizer.py": FAKE_TOKENIZER,
                       "misaki/__init__.py": "", "misaki/zh.py": FAKE_MISAKI_ZH,
                       "phonemizer/__init__.py": "",
                       "phonemizer/backend/__init__.py": FAKE_PHONEMIZER}.items():
        (d / path).parent.mkdir(parents=True, exist_ok=True)
        (d / path).write_text(text, encoding="utf-8")
    return d


def test_the_english_reader_spells_r_vowels_and_keeps_to_the_vocabulary():
    read = kokoro.english_reader(set("pˈaɪəɹ "), lambda t: "pˈaɪpɚ! ")
    assert read("Piper!") == "pˈaɪpəɹ"  # ɚ → əɹ; "!" is not in the vocabulary: dropped


def test_english_inside_chinese_is_read_not_dropped(env):
    fakes(Path(env.vars["XDG_DATA_HOME"]))
    m11 = model(env.tmp / "kzh", "v1.1-zh", ["zf_001"], config=True)
    own(env, f'[zh]\nengine = "kokoro"\nmodel = "{m11}"\nstyle = "zf_001"\n')
    r = env.run("speak", "zh", "我用 GitHub 发布了 samvada。")
    assert r.returncode == 0, r.stderr
    log = [l for l in env.lines() if l.startswith("kokoro ")]
    assert log == ["kokoro kokoro-v1.1-zh.onnx zf_001 zh1.1:我用 githubəɹ 发布了 samvadaəɹ 。"]
    assert "❓" not in log[0]


FAKED = ("kokoro_onnx", "misaki", "phonemizer")


def test_espeak_is_made_once_per_engine(tmp_path, monkeypatch):
    """The warm engine keeps one Engine; its espeak is made at the first English word only."""
    d = fakes(tmp_path)
    saved = {n: sys.modules.pop(n) for n in list(sys.modules) if n.split(".")[0] in FAKED}
    monkeypatch.syspath_prepend(str(d))
    log = tmp_path / "log"
    monkeypatch.setenv("FAKE_LOG", str(log))
    try:
        e = kokoro.Engine(str(model(tmp_path / "kzh", "v1.1-zh", ["zf_001"], config=True)), "zh")
        for text in ("你好", "请看 PyPI 上的 README。", "今天的 release 通过了 CI。"):
            e.speak(text, "zf_001", str(tmp_path / "x.wav"))
    finally:  # the fakes must not outlive this test: later ones ask whether kokoro_onnx is there
        for n in [n for n in sys.modules if n.split(".")[0] in FAKED]:
            del sys.modules[n]
        sys.modules.update(saved)
    lines = log.read_text(encoding="utf-8").splitlines()
    assert [l for l in lines if l.startswith("espeak")] == ["espeak en-us"]
    assert lines[-1].endswith("zh1.1:今天的 releaseəɹ 通过了 ciəɹ 。")


REAL_ENGINE = os.environ.get("SAMVADA_REAL_KOKORO_ENGINE", "")
REAL_ZH = os.environ.get("SAMVADA_REAL_KOKORO_ZH", "")
REAL_PY = os.environ.get("SAMVADA_REAL_KOKORO_PYTHON", sys.executable)  # the engine's own Python
MIXED = {"我用 GitHub 发布了 samvada。": ["ɡˈɪt hˈʌb", "sæmvˈɑːdə"],
         "请看 PyPI 上的 README。": ["pˈaɪ", "ɹˈiːdmiː"],
         "今天的 release 已经通过了 CI 测试。": ["ɹᵻlˈiːs"],
         "这是 Kokoro 后备语音，不是 Piper。": ["kəkˈɔːɹoʊ", "pˈaɪpəɹ"]}


@pytest.mark.skipif(not (REAL_ENGINE and REAL_ZH and Path(REAL_ZH).is_file()),
                    reason="set SAMVADA_REAL_KOKORO_ENGINE and SAMVADA_REAL_KOKORO_ZH")
def test_the_real_engine_reads_english_inside_chinese():
    src = Path(__file__).resolve().parents[1] / "src"
    code = ("import json, sys, warnings; warnings.filterwarnings('ignore')\n"
            "from samvada import kokoro\n"
            "e = kokoro.Engine(sys.argv[1], 'zh')\n"
            "print(json.dumps({t: e.g2p(t)[0] for t in json.loads(sys.argv[2])}, ensure_ascii=False))")
    import json
    r = subprocess.run([REAL_PY, "-c", code, REAL_ZH, json.dumps(list(MIXED), ensure_ascii=False)],
                       env={**os.environ, "PYTHONPATH": f"{REAL_ENGINE}{os.pathsep}{src}",
                            "PYTHONUTF8": "1"},
                       capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout.strip().splitlines()[-1])
    for text, want in MIXED.items():
        assert "❓" not in got[text], (text, got[text])
        for w in want:
            assert w in got[text], (text, got[text])
