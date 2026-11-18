"""Build the voice packages: each voice pack as one wheel for PyPI (DESIGN 8.1, src/samvada/voicepkg.py).

    uv run python release/voice_packages.py --out DIR [--langs en,zh,ja,fr] [--cache DIR]
    uv run python release/voice_packages.py --out DIR --write-pins
    uv run python release/voice_packages.py --out DIR --pypi-plan UPLOAD_DIR

Until PyPI raises en's and zh's limit, the wheels are assets of samvada's GitHub release
`voices-<version>` (the release pipeline makes it), and `src/samvada/voice-packages.toml` pins
each one (file, size, sha256, the pack's digest) for `samvada voices add`. A build of all four
checks its wheels against those pins and stops when they differ; --write-pins writes them
from the build.

--pypi-plan asks PyPI about every wheel of the version: one PyPI does not have is copied into
UPLOAD_DIR (the release uploads it); one it has with the same sha256 is left out; one it has
with another sha256 stops the release (a version on PyPI never changes: bump the version).

One package per language, `samvada-voice-<lang>`: the pack's files, the exact pinned bytes, under
`data/<dest>`, with an index. Not compressed beyond the wheel's own zip: a voice model's weights
are floats and hardly compress (xz -9e gives 108.6 -> 100.1 MiB for en, the same as deflate). So
en and zh are over PyPI's default 100 MiB a file; LIMITS holds the limit PyPI gave each project,
and a wheel over its project's limit stops the build.

The version is `[tool.samvada] voice-packages` in pyproject.toml, one for every language; bump
it when voices.lock changes a pack. The wheels are reproducible (hatchling pinned, its fixed
timestamps), so a rebuild of a version gives the same sha256 and the release pipeline can tell
a version already on PyPI from a changed one. DIR/<version>/ is never overwritten: when it is
complete (SHA256SUMS) it is checked and reused; the work folder holds the downloads.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from samvada import packs, voicepkg  # noqa: E402

HATCHLING = "hatchling==1.33.0"
MiB = 1024 * 1024
PYPI_MAX = 100 * MiB  # warehouse/constants.py MAX_FILESIZE, the default a file
# The limit asked for each project over the default (pypi/support#13115 en, #13116 zh; not
# granted yet: PyPI refuses those two wheels until it is, the GitHub release takes them)
LIMITS = {"samvada-voice-en": 200 * MiB,  # pypi/support#13115
          "samvada-voice-zh": 200 * MiB}  # pypi/support#13116
NAMES = {"en": "English", "zh": "Chinese", "ja": "Japanese", "fr": "French"}
SOURCE = "https://github.com/congzhangzh/samvada"
PINS = ROOT / "src/samvada" / voicepkg.PINS
ALL = ("en", "zh", "ja", "fr")


def release_url(v: str) -> str:
    return f"{SOURCE}/releases/download/voices-{v}/"


def version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["tool"]["samvada"]["voice-packages"]


def voice_map() -> dict:
    return tomllib.loads((ROOT / "src/samvada/voices.toml").read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def source_of(f: packs.File) -> str:
    return f"`{f.repo}` @ `{f.revision[:12]}`: `{f.path}`" if f.repo else f"`{f.url}`"


def terms(lang: str, voice: dict) -> list[str]:
    out = [f"- Terms: {voice.get('license', 'see the model card at the source')}",
           f"  ({voice['license_url']})" if voice.get("license_url") else ""]
    if voice.get("credit"):
        out.append(f"- Credit, required when you use this voice: **{voice['credit']}**")
    return [l for l in out if l]


def readme(lang: str, v: str, files: list[packs.File], voice: dict, digest: str) -> str:
    lines = [f"# {voicepkg.project(lang)}", "",
             f"The {NAMES.get(lang, lang)} voice for [Samvada]({SOURCE}), offline text to speech "
             f"for AI agents: `{voice['model']}` ({voice['engine']}), version {v}. "
             "Data only: nothing here is meant to be imported.", "",
             "`pip install samvada` brings the English and Chinese voices; "
             f"`pip install 'samvada[{lang}]'` this one. Samvada installs the voice from it on "
             "first use, or with `samvada voices add " + lang + "`, and checks every byte "
             "against its `voices.lock` (size and sha256) first.", ""]
    lines += ["## The voice's own terms", "",
              "These files are the voice's, unchanged, redistributed under its own terms; "
              "Samvada's MIT license does not cover them.", "", *terms(lang, voice), "",
              "## Files", "", "| File | Size | sha256 | Source |", "|---|---|---|---|"]
    lines += [f"| `{f.dest}` | {packs.mb(f.size)} | `{f.sha256[:16]}…` | {source_of(f)} |"
              for f in files]
    lines += ["", f"Pack digest: `{digest}`", ""]
    return "\n".join(lines)


def license_md(lang: str, voice: dict, files: list[packs.File]) -> str:
    return "\n".join([
        f"# The {NAMES.get(lang, lang)} voice's terms", "",
        "This package redistributes the voice files below, byte for byte as published at their",
        "source. They keep their own terms, which you accept by using them; Samvada's MIT",
        "license does not cover them.", "", *terms(lang, voice), "",
        *[f"- `{f.dest}`: {source_of(f)}" for f in files], ""])


def pyproject(lang: str, v: str, voice: dict) -> str:
    desc = (f"The {NAMES.get(lang, lang)} voice for Samvada ({voice['model'].removesuffix('.onnx')})"
            ": data only")
    return f'''[project]
name = "{voicepkg.project(lang)}"
version = "{v}"
description = "{desc}"
readme = "README.md"
license-files = ["LICENSE.md"]
requires-python = ">=3.11"
dependencies = []

[project.urls]
Homepage = "{SOURCE}"
Source = "{SOURCE}"

[build-system]
requires = ["{HATCHLING}"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/{voicepkg.module(lang)}"]
'''


def layout(lang: str, files: list[packs.File], blobs: dict[str, Path], v: str, voice: dict,
           root: Path) -> Path:
    """Write the package project of one language into root/<project>/; return its folder.
    blobs: sha256 -> the downloaded, verified file."""
    digest = voicepkg.digest(files)
    d = root / voicepkg.project(lang)
    pkg = d / "src" / voicepkg.module(lang)
    pkg.mkdir(parents=True)
    (d / "pyproject.toml").write_text(pyproject(lang, v, voice), encoding="utf-8")
    (d / "README.md").write_text(readme(lang, v, files, voice, digest), encoding="utf-8")
    (d / "LICENSE.md").write_text(license_md(lang, voice, files), encoding="utf-8")
    (pkg / "__init__.py").write_text(
        f'"""Data for Samvada: {voicepkg.project(lang)} {v}. Nothing to import; see README."""\n')
    index_files = []
    for f in files:
        rel = f"{voicepkg.DATA}/{f.dest}"
        (pkg / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(blobs[f.sha256], pkg / rel)
        index_files.append({"dest": f.dest, "sha256": f.sha256, "size": f.size, "path": rel})
    index = {"format": voicepkg.FORMAT, "lang": lang, "version": v, "digest": digest,
             "files": index_files}
    (pkg / voicepkg.INDEX).write_text(json.dumps(index, indent=1) + "\n", encoding="utf-8")
    return d


def limit(project: str) -> int:
    """The largest file PyPI takes for this project."""
    return LIMITS.get(project, PYPI_MAX)


def fetch_all(lang: str, lock: packs.Lock, work: Path, cache: Path) -> dict[str, Path]:
    env = {**os.environ, "SAMVADA_DOWNLOAD_CACHE": str(cache), "SAMVADA_VOICE_PACKAGES": "off"}
    blobs = {}
    work.mkdir(parents=True, exist_ok=True)
    for f in lock.pack(lang):
        p = work / f.sha256
        if not (p.is_file() and p.stat().st_size == f.size and sha256(p) == f.sha256):
            packs.fetch(f, p, env, log=print)
        blobs[f.sha256] = p
    return blobs


def check_sums(d: Path) -> bool:
    sums = d / "SHA256SUMS"
    if not sums.is_file():
        return False
    for line in sums.read_text().splitlines():
        h, name = line.split(maxsplit=1)
        if not (d / name).is_file() or sha256(d / name) != h:
            raise SystemExit(f"voice packages: {d / name} differs from SHA256SUMS")
    return True


def pypi_json(project: str, v: str) -> dict | None:
    """PyPI's JSON for one version of a project; None when it has no such version."""
    req = urllib.request.Request(f"https://pypi.org/pypi/{project}/{v}/json",
                                 headers={"User-Agent": "samvada-release"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def pypi_plan(dest: Path, upload: Path,
              ask: Callable[[str, str], dict | None] = pypi_json) -> list[str]:
    """Copy into `upload` the wheels of dest/ that PyPI lacks; return their names."""
    upload.mkdir(parents=True, exist_ok=True)
    todo, problems = [], []
    for line in (dest / "SHA256SUMS").read_text().splitlines():
        h, name = line.split(maxsplit=1)
        project_, v = name.split("-")[0].replace("_", "-"), name.split("-")[1]
        info = ask(project_, v)
        if info is None:
            shutil.copyfile(dest / name, upload / name)
            todo.append(name)
            print(f"  {name}: not on PyPI, to upload")
            continue
        theirs = {u["filename"]: u["digests"]["sha256"] for u in info.get("urls", [])}
        if theirs.get(name) == h:
            print(f"  {name}: on PyPI, the same sha256")
        else:
            problems.append(f"{name}: PyPI has {project_} {v} with "
                            + (f"sha256 {theirs[name]}" if name in theirs else "other files")
                            + f", ours is {h}")
    if problems:
        raise SystemExit("voice packages differ from PyPI; bump [tool.samvada] voice-packages:\n  "
                         + "\n  ".join(problems))
    return todo


def pins_text(dest: Path, v: str) -> str:
    """voice-packages.toml for the build in `dest`."""
    langs = {e["lang"]: e for e in json.loads((dest / "voice-packages.json").read_text())["langs"]}
    out = ["# Generated by release/voice_packages.py --write-pins: do not edit by hand.",
           "# Each language's voice package wheel on samvada's GitHub release voices-<version>;",
           "# `samvada voices add` downloads it, checks it and reads the pack's files from it.",
           f'version = "{v}"', f'url = "{release_url(v)}"', ""]
    for line in (dest / "SHA256SUMS").read_text().splitlines():
        h, name = line.split(maxsplit=1)
        lang = name.split("-")[0].rsplit("_", 1)[1]
        out += [f"[wheels.{lang}]", f'file = "{name}"', f"size = {(dest / name).stat().st_size}",
                f'sha256 = "{h}"', f'digest = "{langs[lang]["digest"]}"', ""]
    return "\n".join(out).rstrip() + "\n"


def check_pins(dest: Path, v: str) -> None:
    """The build of all four must be what voice-packages.toml pins."""
    if not PINS.is_file() or PINS.read_text() != pins_text(dest, v):
        raise SystemExit(f"{PINS.relative_to(ROOT)} does not pin the voice packages {v} built in "
                         f"{dest}: run release/voice_packages.py --out {dest.parent} --write-pins, "
                         "and commit it")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, required=True, help="the wheels go in OUT/<version>/")
    ap.add_argument("--langs", default="en,zh,ja,fr")
    ap.add_argument("--cache", type=Path, default=Path(os.environ.get(
        "SAMVADA_BUILD_DIR", Path.home() / ".cache/samvada-build")) / "download-cache")
    ap.add_argument("--work", type=Path, help="default OUT/.work")
    ap.add_argument("--pypi-plan", type=Path, metavar="UPLOAD_DIR",
                    help="after the build: the wheels PyPI does not have go to UPLOAD_DIR")
    ap.add_argument("--write-pins", action="store_true",
                    help=f"after the build: write {PINS.relative_to(ROOT)} from it")
    a = ap.parse_args()
    v = version()
    langs = a.langs.split(",")
    dest = a.out / v
    if a.write_pins:
        if tuple(langs) != ALL:
            raise SystemExit("--write-pins needs the build of every language")
        build_or_reuse(a, v, langs, dest, pinned=False)
        PINS.write_text(pins_text(dest, v))
        print(f"voice packages {v}: {PINS.relative_to(ROOT)} written")
        return 0
    if a.pypi_plan:
        build_or_reuse(a, v, langs, dest)
        todo = pypi_plan(dest, a.pypi_plan)
        print(f"voice packages {v}: {len(todo)} to upload")
        return 0
    return build_or_reuse(a, v, langs, dest)


def build_or_reuse(a, v: str, langs: list[str], dest: Path, pinned: bool = True) -> int:
    build(a, v, langs, dest)
    if pinned and tuple(langs) == ALL:
        check_pins(dest, v)
        print(f"voice packages {v}: the same as {PINS.relative_to(ROOT)}")
    return 0


def build(a, v: str, langs: list[str], dest: Path) -> int:
    if check_sums(dest):
        try:
            fmt = json.loads((dest / "voice-packages.json").read_text()).get("format")
        except (OSError, ValueError):
            fmt = None
        if fmt != voicepkg.FORMAT:
            raise SystemExit(f"{dest} holds voice packages of format {fmt}, not "
                             f"{voicepkg.FORMAT}: move it aside (never reused across formats)")
        print(f"voice packages {v}: already built in {dest}, checked against SHA256SUMS")
        return 0
    if dest.exists():
        raise SystemExit(f"{dest} exists without SHA256SUMS: a half-built version; look, then move it aside")
    work = a.work or a.out / ".work"
    lock = packs.load_lock(ROOT / "src/samvada/voices.lock")
    voices = voice_map()
    staging = work / f"projects-{v}"
    shutil.rmtree(staging, ignore_errors=True)
    built = work / f"wheels-{v}"
    shutil.rmtree(built, ignore_errors=True)
    built.mkdir(parents=True)
    summary = []
    for lang in langs:
        files = lock.pack(lang)
        if not files:
            raise SystemExit(f"no pack {lang} in voices.lock")
        print(f"voice packages {v}: {lang}, {len(files)} files, {packs.mb(lock.size(lang))}")
        blobs = fetch_all(lang, lock, work / "files", a.cache)
        d = layout(lang, files, blobs, v, voices[lang], staging / lang)
        subprocess.run(["uv", "build", "--quiet", "--wheel", "--out-dir", str(built), str(d)],
                       check=True)
        summary.append({"lang": lang, "digest": voicepkg.digest(files), "project": d.name})
    lines = []
    for w in sorted(built.glob("*.whl")):
        project_ = w.name.split("-")[0].replace("_", "-")
        if w.stat().st_size > limit(project_):
            raise SystemExit(f"{w.name}: {w.stat().st_size} bytes, over {project_}'s PyPI limit "
                             f"of {limit(project_)} (LIMITS)")
        lines.append(f"{sha256(w)}  {w.name}")
        print(f"  {w.name}  {w.stat().st_size / 2**20:.1f} MiB")
    (built / "voice-packages.json").write_text(json.dumps(
        {"format": voicepkg.FORMAT, "version": v, "langs": summary}, indent=1) + "\n")
    (built / "SHA256SUMS").write_text("\n".join(lines) + "\n")
    dest.parent.mkdir(parents=True, exist_ok=True)
    built.replace(dest)
    shutil.rmtree(staging, ignore_errors=True)
    print(f"voice packages {v}: {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
