"""Build the standalone bundle: Samvada with its own Python, engines and voice packs (DESIGN 8.2).

    uv run python release/bundle.py <version> [--langs en,zh] [--out DIR] [--work DIR]
                                              [--cache DIR] [--test]

The result works on a machine with no network: unpack it anywhere and run bin/samvada.
Layout of samvada-<version>-linux-x86_64/:

    bin/samvada      the launcher (sh): sets the folders below, runs python -m samvada
    python/          a standalone CPython (python-build-standalone, through uv)
    lib/             samvada itself, from the wheel
    engines/<name>/  each engine's pinned requirements (separate: both install `piper`)
    voices/          the voice packs, verified against voices.lock
    BUNDLE.json      what is inside and where it came from

A version is never overwritten: building one that exists stops. Heavy work goes in --work and
--cache (default under $SAMVADA_BUILD_DIR, else ~/.cache/samvada-build; never /tmp, which is
often tmpfs, i.e. RAM). --test unpacks the archive
in a new folder and speaks en and zh inside a container with no network (podman or docker).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = "3.12"  # the engines' wheels (onnxruntime, tokenizers) are proven on it
SCRATCH = Path(os.environ.get("SAMVADA_BUILD_DIR") or Path.home() / ".cache" / "samvada-build")

LAUNCHER = """#!/bin/sh
# Samvada standalone bundle: everything it needs is in this folder; no network needed.
here=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
export SAMVADA_VOICES="${SAMVADA_VOICES:-$here/voices}"
export SAMVADA_ENGINES="${SAMVADA_ENGINES:-$here/engines}"
export PYTHONPATH="$here/lib${PYTHONPATH:+:$PYTHONPATH}"
exec "$here/python/bin/python3" -s -m samvada "$@"
"""

TEST_TEXT = {"en": "Samvada speaks offline.", "zh": "Samvada 离线说话。",
             "ja": "サンヴァーダはオフラインで話します。", "fr": "Samvada parle hors ligne."}


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print("+ " + " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run(cmd, check=True, **kw)


def git_commit() -> str:
    r = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True)
    dirty = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain", "--", "."],
                           capture_output=True, text=True).stdout.strip()
    return r.stdout.strip() + ("+dirty" if dirty else "")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def du(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file() and not p.is_symlink())


def build(version: str, langs: list[str], out: Path, work: Path, cache: Path) -> Path:
    plat = f"{platform.system().lower()}-{platform.machine()}"
    name = f"samvada-{version}-{plat}"
    archive = out / f"{name}.tar.gz"
    if archive.exists() or (out / name).exists():
        sys.exit(f"bundle: {archive} exists; a version is never overwritten (pick the next one)")
    stage = work / name
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    t0 = time.time()

    wheels = work / "wheel"
    shutil.rmtree(wheels, ignore_errors=True)
    run(["uv", "build", "--wheel", "--out-dir", str(wheels), str(ROOT)])
    wheel = next(wheels.glob("samvada-*.whl"))

    pys = work / "python"
    run(["uv", "python", "install", "--install-dir", str(pys), PYTHON])
    src = sorted(p for p in pys.glob(f"cpython-{PYTHON}.*") if p.is_dir() and not p.is_symlink())[-1]
    shutil.copytree(src, stage / "python", symlinks=True)
    py = stage / "python" / "bin" / "python3"

    run(["uv", "pip", "install", "--quiet", "--python", str(py), "--target", str(stage / "lib"),
         "--no-deps", str(wheel)])
    (stage / "bin").mkdir()
    launcher = stage / "bin" / "samvada"
    launcher.write_text(LAUNCHER)
    launcher.chmod(0o755)

    env = {**os.environ, "SAMVADA_DOWNLOAD_CACHE": str(cache),
           "XDG_STATE_HOME": str(work / "state"), "XDG_DATA_HOME": str(work / "data")}
    run([str(launcher), "voices", "add", *langs, "--engine", "always"], env=env)
    run([str(launcher), "voices", "verify", *langs], env=env)
    shutil.rmtree(work / "state", ignore_errors=True)  # never ship a builder's state

    info = {
        "name": name, "version": version, "languages": langs, "python": src.name,
        "source_commit": git_commit(), "voices_lock_sha256": sha256(ROOT / "src/samvada/voices.lock"),
        "built": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "size_bytes": du(stage),
    }
    (stage / "BUNDLE.json").write_text(json.dumps(info, indent=1, ensure_ascii=False) + "\n")

    out.mkdir(parents=True, exist_ok=True)
    tmp = archive.with_name(archive.name + ".part")
    with tarfile.open(tmp, "w:gz", compresslevel=6) as tar:
        tar.add(stage, arcname=name)
    tmp.replace(archive)
    (out / f"{name}.tar.gz.sha256").write_text(f"{sha256(archive)}  {archive.name}\n")
    print(f"bundle: {archive} ({archive.stat().st_size / 1e6:.0f} MB packed, "
          f"{info['size_bytes'] / 1e6:.0f} MB unpacked) in {time.time() - t0:.0f}s")
    return archive


def wav_seconds(path: Path) -> float:
    with wave.open(str(path)) as w:
        return w.getnframes() / w.getframerate()


def test(archive: Path, langs: list[str], work: Path) -> int:
    """Unpack in a new folder (another path: it must be relocatable) and speak in a container
    with no network, on an image with no Python at all."""
    engine = shutil.which("podman") or shutil.which("docker")
    if not engine:
        sys.exit("bundle test: needs podman or docker")
    where = work / f"test-{time.strftime('%Y%m%d-%H%M%S')}"
    where.mkdir(parents=True)
    with tarfile.open(archive) as tar:
        tar.extractall(where, filter="tar")
    folder = next(p for p in where.iterdir() if p.is_dir())
    out = where / "out"
    out.mkdir()
    pairs = [x for l in langs for x in (l, TEST_TEXT[l])]
    cmd = [engine, "run", "--rm", "--network", "none",
           "-v", f"{folder}:/opt/samvada:ro,Z", "-v", f"{out}:/out:Z",
           "-e", "HOME=/tmp", "debian:testing",
           "sh", "-c", 'command -v python3 && echo "python3 found: not a clean machine" && exit 9; '
                       '/opt/samvada/bin/samvada voices verify && '
                       '/opt/samvada/bin/samvada speak --save /out "$@"', "sh", *pairs]
    r = subprocess.run(cmd)
    print(f"bundle test: container exit {r.returncode}")
    ok = r.returncode == 0
    for i, lang in enumerate(langs, 1):
        clip = out / f"{i}-{lang}.wav"
        if clip.is_file():
            print(f"bundle test: {lang}: {clip.name} {wav_seconds(clip):.2f}s of audio")
            ok &= wav_seconds(clip) > 0.5
        else:
            print(f"bundle test: {lang}: no clip")
            ok = False
    print(f"bundle test: {'PASSED' if ok else 'FAILED'} (network none; clips in {out})")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("version")
    ap.add_argument("--langs", default="en,zh", help="the packs inside (default: en,zh)")
    ap.add_argument("--out", type=Path, default=SCRATCH / "bundles")
    ap.add_argument("--work", type=Path, default=SCRATCH / "work")
    ap.add_argument("--cache", type=Path, default=SCRATCH / "download-cache")
    ap.add_argument("--test", action="store_true", help="also run the offline container test")
    ap.add_argument("--test-only", type=Path, metavar="ARCHIVE", help="only test this archive")
    a = ap.parse_args()
    langs = [l for l in a.langs.split(",") if l]
    if a.test_only:
        return test(a.test_only, langs, a.work)
    archive = build(a.version, langs, a.out, a.work, a.cache)
    return test(archive, langs, a.work) if a.test else 0


if __name__ == "__main__":
    sys.exit(main())
