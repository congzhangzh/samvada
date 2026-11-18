"""VOICEVOX, the third offline engine: Japanese voices from `.vvm` model files.

A VVM is a zip: `manifest.json`, `metas.json` (the speakers and their styles, each with a
style id) and the networks. Reading the speakers needs only the standard library; speaking
needs the `voicevox_core` package, VOICEVOX's ONNX Runtime and the Open JTalk dictionary,
which VOICEVOX's downloader puts side by side:

    voicevox_core/                      (VOICEVOX_ROOT; found by walking up from the model)
      onnxruntime/lib/libvoicevox_onnxruntime.so.1.17.3
      dict/open_jtalk_dic_utf_8-1.11/
      models/vvms/1.vvm ...             (models/README.txt: the terms of every voice)

The engine runs in its own process like Piper (`python -m samvada.voicevox --model M --style
N --out F`, the text on stdin), with `voicevox_core` from Samvada's engines folder
(`<engines>/voicevox/voicevox_core/`) or from this Python.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

LANGS = ("ja",)  # VOICEVOX speaks Japanese only
SPEAKING = ("talk", "streaming_talk")  # the other style types sing


@dataclass
class Style:
    speaker: str
    style: str
    id: int
    vvm: Path

    @property
    def label(self) -> str:
        return f"{self.speaker} {self.style}  ({self.vvm.name} #{self.id})"

    @property
    def key(self) -> str:
        """The name voice notes use: `voicevox:<speaker>/<style>`."""
        return f"voicevox:{self.speaker}/{self.style}"


def styles(vvm: Path) -> list[Style]:
    """The speaking styles in one model file; [] when it is no VVM."""
    try:
        metas = json.loads(zipfile.ZipFile(vvm).read("metas.json"))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return []
    return [Style(sp["name"], s["name"], int(s["id"]), vvm)
            for sp in metas for s in sp.get("styles", [])
            if s.get("type", "talk") in SPEAKING]


def root(vvm: Path, env=os.environ) -> Path | None:
    """The folder with `onnxruntime/` and `dict/`: $VOICEVOX_ROOT, else up from the model."""
    if env.get("VOICEVOX_ROOT"):
        return Path(env["VOICEVOX_ROOT"]).expanduser()
    for d in vvm.resolve().parents:
        if (d / "onnxruntime").is_dir() and (d / "dict").is_dir():
            return d
    return None


def runtime_files(r: Path) -> tuple[Path | None, Path | None]:
    """VOICEVOX's ONNX Runtime library and the Open JTalk dictionary under the root."""
    libs = sorted((r / "onnxruntime" / "lib").glob("*voicevox_onnxruntime*"))
    dicts = sorted(p for p in (r / "dict").glob("open_jtalk_dic_utf_8-*") if p.is_dir())
    return (libs[-1] if libs else None), (dicts[-1] if dicts else None)


def terms(vvm: Path) -> dict[str, tuple[str, str]]:
    """Each speaker's terms and credit line, from the README.txt the downloader puts beside
    the models: {speaker: (terms, credit)}."""
    readme = None
    for d in (vvm.parent, *vvm.parent.parents[:2]):
        if (d / "README.txt").is_file():
            readme = d / "README.txt"
            break
    if readme is None:
        return {}
    text = readme.read_text(encoding="utf-8", errors="replace")
    text = text.split("# 音声ライブラリ利用規約", 1)[-1]
    out = {}
    for block in re.split(r"\n## ", text)[1:]:
        name, _, body = block.partition("\n")
        body = " ".join(l.strip() for l in body.splitlines()
                        if l.strip() and not l.startswith("http") and "詳細は" not in l)
        credit = re.search(r"「(VOICEVOX[^」]*)」", body)
        out[name.strip()] = (body.strip(), credit.group(1) if credit else "")
    return out


class Engine:
    """A loaded VOICEVOX model: load once, speak many times (the warm server keeps one)."""

    def __init__(self, model: str, lang: str = "ja") -> None:
        vvm = Path(model).expanduser()
        r = root(vvm)
        if r is None:
            raise RuntimeError(f"no onnxruntime/ and dict/ above {vvm} (set VOICEVOX_ROOT)")
        lib, dic = runtime_files(r)
        if lib is None or dic is None:
            raise RuntimeError(f"{r}: the ONNX Runtime library or the dictionary is missing")
        from voicevox_core.blocking import Onnxruntime, OpenJtalk, Synthesizer, VoiceModelFile

        self.synth = Synthesizer(Onnxruntime.load_once(filename=str(lib)), OpenJtalk(str(dic)))
        with VoiceModelFile.open(str(vvm)) as m:
            self.synth.load_voice_model(m)

    def speak(self, text: str, voice: str, out: str) -> None:
        Path(out).write_bytes(self.synth.tts(text, int(voice)))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="samvada.voicevox")
    p.add_argument("--model", required=True)
    p.add_argument("--style", required=True, type=int)
    p.add_argument("--out", required=True)
    a = p.parse_args(argv)
    try:
        engine = Engine(a.model)
    except RuntimeError as e:
        print(f"samvada.voicevox: {e}", file=sys.stderr)
        return 1
    engine.speak(sys.stdin.read().strip(), str(a.style), a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
