"""Build the standalone bundle: Samvada with its own Python, engines and voice packs (DESIGN 8.2).

    uv run python release/bundle.py <version> [--langs en,zh] [--out DIR] [--work DIR]
                                              [--cache DIR] [--test | --test-local]
    uv run python release/bundle.py --test-only ARCHIVE [--local] [--langs en,zh]

Built on the system it is for (Linux x86_64 and arm64, macOS arm64, Windows x86_64); the
Windows x86_64 bundle is also the one for Windows on ARM, where it runs under the system's
x64 emulation (piper-tts has no Windows arm64 wheel). It works with no network at all:
unpack it anywhere and run bin/samvada (bin\\samvada.cmd on Windows). Layout of
samvada-<version>-<os>-<arch>/:

    bin/samvada[.cmd] the launcher: sets the folders below, runs python -m samvada
    python/           a standalone CPython (python-build-standalone, through uv)
    lib/              samvada itself, from the wheel
    engines/<name>/   each engine's pinned requirements (separate: both install `piper`)
    voices/           the voice packs, verified against voices.lock
    VOICES.md         where every voice file came from (repo, commit, path)
    BUNDLE.json       what is inside and where it came from

A version is never overwritten: building one that exists stops. Heavy work goes in --work and
--cache (default under $SAMVADA_BUILD_DIR, else ~/.cache/samvada-build; never /tmp, which is
often tmpfs, i.e. RAM).

Tests unpack the archive in a new folder (so it must be relocatable) and speak every language
of the bundle into files: --test inside a container with no network and no Python on the image
(podman or docker, Linux); --test-local on this machine with every proxy pointed at a closed
port and the Hub in offline mode (macOS, Windows, where a container cannot run the bundle).
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
import tomllib
import wave
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = "3.12"  # the engines' wheels (onnxruntime, tokenizers) are proven on it
SCRATCH = Path(os.environ.get("SAMVADA_BUILD_DIR") or Path.home() / ".cache" / "samvada-build")
WINDOWS = sys.platform == "win32"

LAUNCHER_SH = """#!/bin/sh
# Samvada standalone bundle: everything it needs is in this folder; no network needed.
here=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
export SAMVADA_VOICES="${SAMVADA_VOICES:-$here/voices}"
export SAMVADA_ENGINES="${SAMVADA_ENGINES:-$here/engines}"
export PYTHONPATH="$here/lib${PYTHONPATH:+:$PYTHONPATH}"
exec "$here/python/bin/python3" -s -m samvada "$@"
"""

LAUNCHER_CMD = """@echo off
rem Samvada standalone bundle: everything it needs is in this folder; no network needed.
setlocal
set "HERE=%~dp0.."
if not defined SAMVADA_VOICES set "SAMVADA_VOICES=%HERE%\\voices"
if not defined SAMVADA_ENGINES set "SAMVADA_ENGINES=%HERE%\\engines"
set "PYTHONPATH=%HERE%\\lib;%PYTHONPATH%"
set "PYTHONUTF8=1"
"%HERE%\\python\\python.exe" -s -m samvada %*
"""

TEST_TEXT = {"en": "Samvada speaks offline.", "zh": "Samvada 离线说话。",
             "ja": "サンヴァーダはオフラインで話します。", "fr": "Samvada parle hors ligne."}


def plat() -> str:
    os_name = {"linux": "linux", "darwin": "macos", "win32": "windows"}[sys.platform]
    arch = {"amd64": "x86_64", "x86_64": "x86_64", "aarch64": "arm64", "arm64": "arm64"}[
        platform.machine().lower()]
    return f"{os_name}-{arch}"


def run(cmd: list, **kw) -> subprocess.CompletedProcess:
    print("+ " + " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run([str(c) for c in cmd], check=True, **kw)


def git_commit() -> str:
    r = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True)
    dirty = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain", "--", "."],
                           capture_output=True, text=True).stdout.strip()
    return (r.stdout.strip() or "unknown") + ("+dirty" if dirty else "")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def du(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file() and not p.is_symlink())


def python_exe(root: Path) -> Path:
    return root / "python.exe" if WINDOWS else root / "bin" / "python3"


def launcher_of(folder: Path) -> Path:
    return folder / "bin" / ("samvada.cmd" if WINDOWS else "samvada")


def voices_md(langs: list[str]) -> str:
    lock = tomllib.loads((ROOT / "src/samvada/voices.lock").read_text(encoding="utf-8"))
    lines = ["# Voices in this bundle", "",
             "Every file is pinned: a commit on the Hugging Face Hub (or a URL at a commit) and its",
             "sha256 (`samvada voices verify` checks them). Each voice keeps its own license: see its",
             "model card at the source before you redistribute it.", "",
             "| Pack | File | Source |", "|---|---|---|"]
    for f in lock["file"]:
        if f["pack"] in langs:
            src = (f"`{f['repo']}` @ `{f['revision'][:12]}`: `{f['path']}`" if f.get("repo")
                   else f"`{f['url']}`")
            lines.append(f"| {f['pack']} | `{f['dest']}` | {src} |")
    return "\n".join(lines) + "\n"


def build(version: str, langs: list[str], out: Path, work: Path, cache: Path) -> Path:
    name = f"samvada-{version}-{plat()}"
    archive = out / (f"{name}.zip" if WINDOWS else f"{name}.tar.gz")
    if archive.exists() or (out / name).exists():
        sys.exit(f"bundle: {archive} exists; a version is never overwritten (pick the next one)")
    stage = work / name
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    t0 = time.time()

    wheels = work / "wheel"
    shutil.rmtree(wheels, ignore_errors=True)
    run(["uv", "build", "--wheel", "--out-dir", wheels, ROOT])
    wheel = next(wheels.glob("samvada-*.whl"))

    pys = work / "python"
    run(["uv", "python", "install", "--install-dir", pys, PYTHON])
    src = sorted(p for p in pys.glob(f"cpython-{PYTHON}.*") if p.is_dir() and not p.is_symlink())[-1]
    shutil.copytree(src, stage / "python", symlinks=True)
    py = python_exe(stage / "python")

    run(["uv", "pip", "install", "--quiet", "--python", py, "--target", stage / "lib",
         "--no-deps", wheel])
    (stage / "bin").mkdir()
    launcher = launcher_of(stage)
    if WINDOWS:
        launcher.write_text(LAUNCHER_CMD.replace("\n", "\r\n"), newline="")
    else:
        launcher.write_text(LAUNCHER_SH)
        launcher.chmod(0o755)

    env = {**os.environ, "SAMVADA_DOWNLOAD_CACHE": str(cache),
           "XDG_STATE_HOME": str(work / "state"), "XDG_DATA_HOME": str(work / "data")}
    run([launcher, "voices", "add", *langs, "--engine", "always"], env=env)
    run([launcher, "voices", "verify", *langs], env=env)
    shutil.rmtree(work / "state", ignore_errors=True)  # never ship a builder's state

    (stage / "VOICES.md").write_text(voices_md(langs), encoding="utf-8")
    info = {
        "name": name, "version": version, "platform": plat(), "languages": langs,
        "python": src.name, "source_commit": git_commit(),
        "voices_lock_sha256": sha256(ROOT / "src/samvada/voices.lock"),
        "built": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "size_bytes": du(stage),
    }
    (stage / "BUNDLE.json").write_text(json.dumps(info, indent=1, ensure_ascii=False) + "\n",
                                       encoding="utf-8")

    out.mkdir(parents=True, exist_ok=True)
    tmp = archive.with_name(archive.name + ".part")
    if WINDOWS:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for p in sorted(stage.rglob("*")):
                z.write(p, Path(name) / p.relative_to(stage))
    else:
        with tarfile.open(tmp, "w:gz", compresslevel=6) as tar:
            tar.add(stage, arcname=name)
    tmp.replace(archive)
    (out / f"{archive.name}.sha256").write_text(f"{sha256(archive)}  {archive.name}\n")
    print(f"bundle: {archive} ({archive.stat().st_size / 1e6:.0f} MB packed, "
          f"{info['size_bytes'] / 1e6:.0f} MB unpacked) in {time.time() - t0:.0f}s")
    return archive


def wav_seconds(path: Path) -> float:
    with wave.open(str(path)) as w:
        return w.getnframes() / w.getframerate()


def unpack(archive: Path, where: Path) -> Path:
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as z:
            z.extractall(where)
    else:
        with tarfile.open(archive) as tar:
            tar.extractall(where, filter="tar")
    return next(p for p in where.iterdir() if p.is_dir())


def check_clips(out: Path, langs: list[str], ok: bool) -> int:
    for i, lang in enumerate(langs, 1):
        clip = out / f"{i}-{lang}.wav"
        if clip.is_file():
            secs = wav_seconds(clip)
            print(f"bundle test: {lang}: {clip.name} {secs:.2f}s of audio")
            ok &= secs > 0.5
        else:
            print(f"bundle test: {lang}: no clip")
            ok = False
    print(f"bundle test: {'PASSED' if ok else 'FAILED'} (clips in {out})")
    return 0 if ok else 1


def test(archive: Path, langs: list[str], work: Path, local: bool) -> int:
    where = work / f"test-{time.strftime('%Y%m%d-%H%M%S')}"
    where.mkdir(parents=True)
    folder = unpack(archive, where)
    out = where / "out"
    out.mkdir()
    pairs = [x for l in langs for x in (l, TEST_TEXT[l])]
    if not local:
        engine = shutil.which("podman") or shutil.which("docker")
        if not engine:
            sys.exit("bundle test: needs podman or docker (or use --local)")
        if folder.joinpath("bin", "samvada").exists():
            folder.joinpath("bin", "samvada").chmod(0o755)
        cmd = [engine, "run", "--rm", "--network", "none",
               "-v", f"{folder}:/opt/samvada:ro,Z", "-v", f"{out}:/out:Z",
               "-e", "HOME=/tmp", "debian:testing",
               "sh", "-c", 'command -v python3 && echo "python3 found: not a clean machine" && exit 9; '
                           '/opt/samvada/bin/samvada voices verify && '
                           '/opt/samvada/bin/samvada speak --save /out "$@"', "sh", *pairs]
        r = subprocess.run(cmd)
        print(f"bundle test: container exit {r.returncode} (network none, no Python on the image)")
        return check_clips(out, langs, r.returncode == 0)
    # local: no container here; cut the network for this process tree as far as a user can
    home = where / "home"
    home.mkdir()
    closed = "http://127.0.0.1:9"
    env = {k: v for k, v in os.environ.items() if not k.upper().endswith("_PROXY")}
    env |= {"HTTP_PROXY": closed, "HTTPS_PROXY": closed, "ALL_PROXY": closed, "NO_PROXY": "",
            "HF_HUB_OFFLINE": "1", "SAMVADA_ONLINE": "off", "HOME": str(home),
            "USERPROFILE": str(home), "XDG_STATE_HOME": str(home / "state"),
            "XDG_DATA_HOME": str(home / "data"), "PYTHONPATH": ""}
    launcher = launcher_of(folder)
    seen = subprocess.run([str(python_exe(folder / "python")), "-c",
                           "import platform, sys, sysconfig; print(f'built for {sysconfig.get_platform()}, '"
                           "f'on a {platform.machine()} OS, Python {sys.version.split()[0]}')"],
                          capture_output=True, text=True).stdout.strip()
    # platform.machine() is the OS's (Windows asks WMI); sysconfig is what python.exe was built for
    print(f"bundle test: the bundle's Python: {seen}")
    r1 = subprocess.run([str(launcher), "voices", "verify", *langs], env=env)
    r2 = subprocess.run([str(launcher), "speak", "--save", str(out), *pairs], env=env)
    ok = r1.returncode == 0 and r2.returncode == 0
    print(f"bundle test: local exit {r1.returncode}/{r2.returncode} (proxies closed, Hub offline)")
    return check_clips(out, langs, ok)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("version", nargs="?")
    ap.add_argument("--langs", default="en,zh", help="the packs inside (default: en,zh)")
    ap.add_argument("--out", type=Path, default=SCRATCH / "bundles")
    ap.add_argument("--work", type=Path, default=SCRATCH / "work")
    ap.add_argument("--cache", type=Path, default=SCRATCH / "download-cache")
    ap.add_argument("--test", action="store_true", help="also test in a container with no network")
    ap.add_argument("--test-local", action="store_true", help="also test here, network cut by proxy")
    ap.add_argument("--test-only", type=Path, metavar="ARCHIVE", help="only test this archive")
    ap.add_argument("--local", action="store_true", help="with --test-only: test here, not in a container")
    a = ap.parse_args()
    langs = [l for l in a.langs.split(",") if l]
    if a.test_only:
        return test(a.test_only, langs, a.work, a.local)
    if not a.version:
        ap.error("a version is needed to build")
    archive = build(a.version, langs, a.out, a.work, a.cache)
    if a.test or a.test_local:
        return test(archive, langs, a.work, local=a.test_local)
    return 0


if __name__ == "__main__":
    sys.exit(main())
