"""Kokoro, the fourth offline engine: one ONNX model, many voices in a `voices-*.bin` beside it.

Two models: v1.0 (54 voices in nine languages; its authors grade each voice in VOICES.md)
and v1.1-zh (100 Chinese speakers from LongMaoData's professional data, >100 hours, plus three
English voices; it needs its `config.json` beside the model). Both Apache-2.0.

A voice's name says its language and gender: `zf_xiaoxiao` is Mandarin (z), female (f).
The voices file is an .npz, a zip of one .npy per voice, so the names read with zipfile.

Speaking runs in its own process like Piper (`python -m samvada.kokoro --model M --voice V
--lang L --out F`, the text on stdin), with `kokoro_onnx` and `misaki` (the G2P for Chinese
and Japanese) from `<engines>/kokoro/` or from this Python.
"""

from __future__ import annotations

import argparse
import re
import sys
import wave
import zipfile
from dataclasses import dataclass
from pathlib import Path

PREFIXES = {"en": "ab", "zh": "z", "ja": "j", "fr": "f", "es": "e", "hi": "h", "it": "i", "pt": "p"}
LANG_NAMES = {"a": "American English", "b": "British English", "z": "Mandarin Chinese",
              "j": "Japanese", "f": "French", "e": "Spanish", "h": "Hindi", "i": "Italian",
              "p": "Brazilian Portuguese"}
ESPEAK = {"a": "en-us", "b": "en-gb", "f": "fr-fr", "e": "es", "h": "hi", "i": "it", "p": "pt-br"}
TERMS = "Apache-2.0 (Kokoro-82M by hexgrad)"

# v1.0: the authors' own grades (hexgrad/Kokoro-82M VOICES.md):
# target quality | training duration | overall grade
GRADES_V10 = {
    "af_heart": "- | - | A", "af_alloy": "B | MM minutes | C", "af_aoede": "B | H hours | C+",
    "af_bella": "A | HH hours | A-", "af_jessica": "C | MM minutes | D",
    "af_kore": "B | H hours | C+", "af_nicole": "B | HH hours | B-", "af_nova": "B | MM minutes | C",
    "af_river": "C | MM minutes | D", "af_sarah": "B | H hours | C+",
    "af_sky": "B | M minutes | C-", "am_adam": "D | H hours | F+", "am_echo": "C | MM minutes | D",
    "am_eric": "C | MM minutes | D", "am_fenrir": "B | H hours | C+",
    "am_liam": "C | MM minutes | D", "am_michael": "B | H hours | C+",
    "am_onyx": "C | MM minutes | D", "am_puck": "B | H hours | C+",
    "am_santa": "C | M minutes | D-", "bf_alice": "C | MM minutes | D",
    "bf_emma": "B | HH hours | B-", "bf_isabella": "B | MM minutes | C",
    "bf_lily": "C | MM minutes | D", "bm_daniel": "C | MM minutes | D",
    "bm_fable": "B | MM minutes | C", "bm_george": "B | MM minutes | C",
    "bm_lewis": "C | H hours | D+", "jf_alpha": "B | H hours | C+",
    "jf_gongitsune": "B | MM minutes | C", "jf_nezumi": "B | M minutes | C-",
    "jf_tebukuro": "B | MM minutes | C", "jm_kumo": "B | M minutes | C-",
    "zf_xiaobei": "C | MM minutes | D", "zf_xiaoni": "C | MM minutes | D",
    "zf_xiaoxiao": "C | MM minutes | D", "zf_xiaoyi": "C | MM minutes | D",
    "zm_yunjian": "C | MM minutes | D", "zm_yunxi": "C | MM minutes | D",
    "zm_yunxia": "C | MM minutes | D", "zm_yunyang": "C | MM minutes | D",
    "ff_siwis": "B | <11 hours | B-",
}


@dataclass
class KVoice:
    name: str
    model: Path

    @property
    def version(self) -> str:
        m = re.search(r"v\d+(\.\d+)*(-zh)?", self.model.name)
        return m.group(0) if m else "?"

    @property
    def key(self) -> str:
        return f"kokoro:{self.version}/{self.name}"

    @property
    def note(self) -> str:
        lang = LANG_NAMES.get(self.name[:1], "?")
        sex = {"f": "female", "m": "male"}.get(self.name[1:2], "?")
        out = f"Kokoro {self.version}, {lang}, {sex}"
        if self.version == "v1.0" and self.name in GRADES_V10:
            out += f"; its authors' grade (data quality | hours | overall): {GRADES_V10[self.name]}"
        elif self.version == "v1.1-zh" and self.name[:1] == "z":
            out += "; one of 100 speakers from LongMaoData's professional Chinese data (>100 h)"
        elif self.version == "v1.1-zh":
            out += "; trained on crowdsourced synthetic English (its README)"
        return out


def voices_file(model: Path) -> Path | None:
    """`voices-v1.0.bin` beside `kokoro-v1.0[.fp16|.int8].onnx`; else the only voices*.bin."""
    stem = re.sub(r"\.(fp16|int8)$", "", model.stem).replace("kokoro", "voices", 1)
    exact = model.with_name(f"{stem}.bin")
    if exact.is_file():
        return exact
    found = sorted(model.parent.glob("voices*.bin"))
    return found[0] if len(found) == 1 else None


def voices(model: Path, lang: str | None = None) -> list[KVoice]:
    """The voices of one model file (for one language, by the name's first letter)."""
    vf = voices_file(model)
    if vf is None:
        return []
    try:
        names = [n[:-4] for n in zipfile.ZipFile(vf).namelist() if n.endswith(".npy")]
    except (OSError, zipfile.BadZipFile):
        return []
    want = PREFIXES.get(lang, "") if lang else None
    return [KVoice(n, model) for n in names if want is None or n[:1] in want]


def _g2p(lang: str, voice: str, model: Path):
    """Phonemes for Chinese and Japanese through misaki; None: kokoro's own espeak reads it."""
    if lang == "zh":
        from misaki import zh
        return zh.ZHG2P(version="1.1" if "v1.1" in model.name else None)
    if lang == "ja":
        from misaki import ja
        return ja.JAG2P()
    return None


class Engine:
    """A loaded Kokoro model and its text reader for one language: load once, speak many
    times (the warm server keeps one)."""

    def __init__(self, model: str, lang: str) -> None:
        self.model, self.lang = Path(model).expanduser(), lang
        vf = voices_file(self.model)
        if vf is None:
            raise RuntimeError(f"no voices*.bin beside {self.model}")
        from kokoro_onnx import Kokoro

        cfg = self.model.with_name("config.json")  # v1.1 models need theirs; v1.0 never takes one
        vocab = str(cfg) if "v1.1" in self.model.name and cfg.is_file() else None
        self.k = None
        try:  # ONNX Runtime's CPU arena keeps buffers sized for the longest sentence: off
            import onnxruntime as rt
            opts = rt.SessionOptions()
            opts.enable_cpu_mem_arena = False
            sess = rt.InferenceSession(str(self.model), sess_options=opts,
                                       providers=["CPUExecutionProvider"])
            self.k = Kokoro.from_session(sess, str(vf), vocab_config=vocab)
        except (ImportError, AttributeError, TypeError):
            self.k = None  # an older kokoro_onnx, or a test's stand-in: its own session
        if self.k is None:
            self.k = Kokoro(str(self.model), str(vf), vocab_config=vocab)
        self.g2p = _g2p(lang, "", self.model)

    def speak(self, text: str, voice: str, out: str) -> None:
        if self.g2p is not None:
            phonemes, _ = self.g2p(text)
            audio, rate = self.k.create(phonemes, voice=voice, is_phonemes=True)
        else:
            audio, rate = self.k.create(text, voice=voice, lang=ESPEAK.get(voice[:1], "en-us"))
        pcm = (audio.clip(-1, 1) * 32767).astype("<i2").tobytes()
        with wave.open(out, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(pcm)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="samvada.kokoro")
    p.add_argument("--model", required=True)
    p.add_argument("--voice", required=True)
    p.add_argument("--lang", required=True)
    p.add_argument("--out", required=True)
    a = p.parse_args(argv)
    try:
        engine = Engine(a.model, a.lang)
    except RuntimeError as e:
        print(f"samvada.kokoro: {e}", file=sys.stderr)
        return 1
    engine.speak(sys.stdin.read().strip(), a.voice, a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
