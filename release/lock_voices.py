"""Write src/samvada/voices.lock: every file a voice pack needs, pinned (DESIGN 8.3, 8.4).

The sources live here, each pinned to a commit. For each file the Hub's API gives the size
and, for files in LFS, the sha256; a small file outside LFS is downloaded once to hash it.
Also writes the engines' requirement locks (src/samvada/locks/*.txt) with `uv pip compile`.

    uv run python release/lock_voices.py          # network needed; nothing is installed
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "src" / "samvada"
HF = "https://huggingface.co"

PIPER_VOICES = ("rhasspy/piper-voices", "model", "cf910fb59f1d79ab3b625a63993a108751c6c25d")
CHECKPOINTS = ("rhasspy/piper-checkpoints", "dataset", "8d418f0288a50aed603547ee751193fa7910bfe3")
BERT_ZH = ("google-bert/bert-base-chinese", "model", "8f23c25b06e129b6c986331a13d8d025a92cf0ea")
TSUKUYOMI = ("ayousanz/piper-plus-tsukuyomi-chan", "model", "619ad26fcbd5635b3d70a27eac919f4c2ce892b1")
NLTK = "https://raw.githubusercontent.com/nltk/nltk_data/550b6625bcef1f2abff2ff770a5a0d272c9c6b2a"

# The tokenizer goes into a Hugging Face cache layout under the voices folder, named as
# Piper asks for it ("bert-base-chinese"), so from_pretrained finds it with HF_HUB_OFFLINE=1.
BERT_SNAP = f"hf/hub/models--bert-base-chinese/snapshots/{BERT_ZH[2]}"


def piper_voice(pack: str, folder: str, name: str) -> list[dict]:
    return [{"pack": pack, "hf": PIPER_VOICES, "path": f"{folder}/{name}{ext}", "dest": name + ext}
            for ext in (".onnx", ".onnx.json")]


SOURCES: list[dict] = [
    *piper_voice("en", "en/en_US/lessac/high", "en_US-lessac-high"),
    *piper_voice("zh", "zh/zh_CN/xiao_ya/medium", "zh_CN-xiao_ya-medium"),
    {"pack": "zh", "hf": CHECKPOINTS, "path": "zh/zh_CN/_resources/g2pw.tar.gz",
     "dest": "g2pW", "unpack": "tar"},
    *[{"pack": "zh", "hf": BERT_ZH, "path": f, "dest": f"{BERT_SNAP}/{f}"}
      for f in ("tokenizer_config.json", "tokenizer.json", "vocab.txt")],
    {"pack": "ja", "hf": TSUKUYOMI, "path": "tsukuyomi-chan-6lang-fp16.onnx",
     "dest": "tsukuyomi-chan-6lang-fp16.onnx"},
    {"pack": "ja", "hf": TSUKUYOMI, "path": "config.json",
     "dest": "tsukuyomi-chan-6lang-fp16.onnx.json"},
    {"pack": "ja", "url": f"{NLTK}/packages/taggers/averaged_perceptron_tagger_eng.zip",
     "dest": "nltk_data/taggers", "unpack": "zip"},
    {"pack": "ja", "url": f"{NLTK}/packages/corpora/cmudict.zip",
     "dest": "nltk_data/corpora", "unpack": "zip"},
    *piper_voice("fr", "fr/fr_FR/siwis/medium", "fr_FR-siwis-medium"),
]

# Kokoro, the fallback for en and zh (DESIGN 14): full fp32 models, installed only with
# `samvada voices add en zh --kokoro`. The ONNX models come from kokoro-onnx's GitHub
# releases (fixed asset URLs); a big file there is pinned from its known size and sha256
# (checked against GitHub's own digest where it publishes one) instead of being downloaded
# into memory to hash it. v1.1-zh's config.json comes from the Hub at a commit.
KOKORO_REL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download"
KOKORO_ZH = ("hexgrad/Kokoro-82M-v1.1-zh", "model", "01e7505bd6a7a2ac4975463114c3a7650a9f7218")
SOURCES += [
    {"pack": "en-kokoro", "url": f"{KOKORO_REL}/model-files-v1.0/kokoro-v1.0.onnx",
     "dest": "kokoro/v1.0/kokoro-v1.0.onnx", "size": 325532387,
     "sha256": "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5"},
    {"pack": "en-kokoro", "url": f"{KOKORO_REL}/model-files-v1.1/voices-v1.0.bin",
     "dest": "kokoro/v1.0/voices-v1.0.bin", "size": 28214398,
     "sha256": "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d"},  # = GitHub's digest
    {"pack": "zh-kokoro", "url": f"{KOKORO_REL}/model-files-v1.1/kokoro-v1.1-zh.onnx",
     "dest": "kokoro/v1.1-zh/kokoro-v1.1-zh.onnx", "size": 325506167,
     "sha256": "859f9ded9f53be16c24857cdab3254a45da53c3afd5ba6ef134c7de3f822e326"},  # = GitHub's digest
    {"pack": "zh-kokoro", "url": f"{KOKORO_REL}/model-files-v1.1/voices-v1.1-zh.bin",
     "dest": "kokoro/v1.1-zh/voices-v1.1-zh.bin", "size": 53815880,
     "sha256": "14cb6186c99e4f6016871405f62046c5df863ae27465cbdc4ee08be7dd703acd"},
    {"pack": "zh-kokoro", "hf": KOKORO_ZH, "path": "config.json", "dest": "kokoro/v1.1-zh/config.json"},
]

ENGINES = {
    "piper": "piper-tts[zh]==1.8.0",
    "piper-plus": "piper-plus==1.13.0",
    # English through Kokoro's own espeak-ng; Chinese through misaki (jieba, pypinyin)
    "kokoro": "kokoro-onnx==0.6.1\nmisaki[zh]==0.9.4",
}


def get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "samvada-lock"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def tree(repo: str, kind: str, rev: str, folder: str) -> dict[str, dict]:
    prefix = "datasets/" if kind == "dataset" else "models/"
    url = f"{HF}/api/{prefix}{repo}/tree/{rev}/{folder}".rstrip("/")
    return {f["path"]: f for f in json.loads(get(url))}


def resolve(src: dict) -> dict:
    out = {"pack": src["pack"]}
    if "hf" in src:
        repo, kind, rev = src["hf"]
        folder = src["path"].rpartition("/")[0]
        info = tree(repo, kind, rev, folder)[src["path"]]
        out |= {"repo": repo, "repo_type": kind, "revision": rev, "path": src["path"]}
        size, sha = info["size"], (info.get("lfs") or {}).get("oid")
        if not sha:  # a small file outside LFS: hash it
            prefix = "datasets/" if kind == "dataset" else ""
            data = get(f"{HF}/{prefix}{repo}/resolve/{rev}/{src['path']}")
            assert len(data) == size, src
            sha = hashlib.sha256(data).hexdigest()
    elif "sha256" in src:  # a big release asset, pinned from its known size and sha256
        out["url"] = src["url"]
        size, sha = src["size"], src["sha256"]
    else:
        data = get(src["url"])
        out["url"] = src["url"]
        size, sha = len(data), hashlib.sha256(data).hexdigest()
    out |= {"dest": src["dest"], "unpack": src.get("unpack", ""), "size": size, "sha256": sha}
    return out


def toml_value(v) -> str:
    return str(v) if isinstance(v, int) else json.dumps(v, ensure_ascii=False)


def main(argv: list[str] | None = None) -> int:
    """`lock_voices.py [--only pack,pack] [--engines name,name]`: with --only, re-resolve just
    those packs and keep every other pinned file as it is (no other pin moves)."""
    import argparse
    import tomllib
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--engines", default="")
    a = ap.parse_args(argv)
    only = {x for x in a.only.split(",") if x}
    engines = {x for x in a.engines.split(",") if x} or set(ENGINES)
    old = tomllib.loads((PKG / "voices.lock").read_text(encoding="utf-8")) if only else {}
    lines = ["# Generated by release/lock_voices.py: do not edit by hand.",
             "# Every file a voice pack needs, pinned: a Hub commit (or a URL at a commit),",
             "# its size and sha256. `samvada voices add` downloads and verifies against it.", ""]
    kept = [f for f in old.get("file", []) if f["pack"] not in only]
    for f in kept:  # --only: everything else exactly as it was
        lines.append("[[file]]")
        lines += [f"{k} = {toml_value(v)}" for k, v in f.items()]
        lines.append("")
    for src in SOURCES:
        if only and src["pack"] not in only:
            continue
        f = resolve(src)
        print(f"{f['pack']}  {f['size']:>11}  {f['sha256'][:12]}  {f['dest']}", file=sys.stderr)
        lines.append("[[file]]")
        lines += [f"{k} = {toml_value(v)}" for k, v in f.items()]
        lines.append("")
    lines += ["[engines]"] + [f"{k} = {toml_value(v)}" for k, v in ENGINES.items()]
    (PKG / "voices.lock").write_text("\n".join(lines) + "\n", encoding="utf-8")

    locks = PKG / "locks"
    locks.mkdir(exist_ok=True)
    for name, req in ENGINES.items():
        if name not in engines:
            continue
        out = locks / f"{name}.txt"
        subprocess.run(["uv", "pip", "compile", "--universal", "--quiet", "--no-header",
                        "--python-version", "3.12", "-o", str(out), "-"],
                       input=req + "\n", text=True, encoding="utf-8", errors="replace", check=True)
        print(f"engine {name}: {out.relative_to(ROOT)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
