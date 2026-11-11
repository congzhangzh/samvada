"""Compile the engine packages that PyPI has no wheel for on this system (DESIGN 8.2).

    uv run python release/build_wheels.py [--out DIR] [--work DIR] [--src DIR] [PACKAGE ...]

On Windows arm64, piper-tts (C, links espeak-ng), pyopenjtalk-plus (C++, OpenJTalk) and
sudachipy (Rust) publish only sources. This builds each at the version pinned in
src/samvada/locks/, for a standalone CPython of the same system and arch as the Python that
runs this (bundle.python_request), and puts the wheels in --out for `bundle.py --wheelhouse`.

Each comes from its sdist, except where the sdist cannot build the native part (SOURCES: the
piper-tts sdist has no CMakeLists.txt and no espeakbridge.c, so it would make a pure-Python
wheel without espeak-ng; it is built from upstream git at the release tag's commit instead).
A wheel tagged none-any is refused: every package here has native code.

Windows keeps paths under 260 characters (MAX_PATH): CMake's try-compile objects under pip's
temp folder went past it (rc2: espeak-ng's configure, cl.exe C1083). Git sources are cloned
into --src, which should be short (CI: C:\s), and built in place; CI also sets TEMP short.

Needs the system's compilers: MSVC with the ARM64 tools (from a shell where MSVC comes first
on PATH: not Git Bash, whose /usr/bin/link.exe is coreutils' link), CMake (pulled as a build
dependency), Rust for sudachipy, git. Network is used here, never in the bundle.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bundle  # noqa: E402  (the same Python choice and helpers as the bundle)

DEFAULT = ["piper-tts", "pyopenjtalk-plus", "sudachipy"]

# name -> (version it must be, git repo, commit); the commit is the one the version's tag points at
SOURCES = {
    # tag v1.8.0 (its CMakeLists pins espeak-ng 724808c)
    "piper-tts": ("1.8.0", "https://github.com/OHF-voice/piper1-gpl", "639388b6317fc4731e91d53da42aea68fd4166ff"),
}


def checkout(repo: str, commit: str, where: Path) -> Path:
    if not (where / ".git").is_dir():
        where.parent.mkdir(parents=True, exist_ok=True)
        bundle.run(["git", "clone", "--filter=blob:none", "--no-checkout", repo, where])
    bundle.run(["git", "-C", where, "checkout", "--quiet", "--force", commit])
    head = bundle.run(["git", "-C", where, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    if head != commit:
        sys.exit(f"build_wheels: {where} is at {head}, not {commit}")
    return where


def pinned(name: str) -> str:
    for lock in sorted((bundle.ROOT / "src/samvada/locks").glob("*.txt")):
        for line in lock.read_text(encoding="utf-8").splitlines():
            m = re.match(rf"^{re.escape(name)}==([^\s;]+)", line, re.I)
            if m:
                return m.group(1)
    sys.exit(f"build_wheels: {name} is not pinned in src/samvada/locks/")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("packages", nargs="*", default=DEFAULT)
    ap.add_argument("--out", type=Path, default=bundle.SCRATCH / "wheelhouse")
    ap.add_argument("--work", type=Path, default=bundle.SCRATCH / "wheel-build")
    ap.add_argument("--src", type=Path, help="where git sources are cloned (default: under --work)")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    venv = a.work / f"venv-{bundle.plat()}"
    if not venv.exists():
        bundle.run(["uv", "venv", "--python", bundle.python_request(), venv])
    py = venv / ("Scripts/python.exe" if bundle.WINDOWS else "bin/python")
    bundle.run(["uv", "pip", "install", "--quiet", "--python", py, "pip"])
    bundle.run([py, "-c", "import sysconfig; print('build_wheels: compiling for', sysconfig.get_platform())"])
    for name in a.packages:
        version = pinned(name)
        have = [w for w in a.out.glob("*.whl")
                if w.name.lower().startswith(f"{name.replace('-', '_')}-{version}-".lower())]
        if have:
            print(f"build_wheels: {name}=={version}: {have[0].name} (cached)")
            continue
        if name in SOURCES:
            want, repo, commit = SOURCES[name]
            if want != version:
                sys.exit(f"build_wheels: {name} is locked at {version}, SOURCES has {want}: update both")
            spec = checkout(repo, commit, (a.src or a.work / "src") / name)
        else:
            spec = f"{name}=={version}"
        bundle.run([py, "-m", "pip", "wheel", "--no-deps", "--no-binary", name,
                    "--wheel-dir", a.out, spec])
    bad = [w.name for w in a.out.glob("*.whl") if w.name.endswith("-none-any.whl")]
    if bad:
        sys.exit(f"build_wheels: built without native code (refused): {', '.join(bad)}")
    for w in sorted(a.out.glob("*.whl")):
        print(f"build_wheels: {bundle.sha256(w)}  {w.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
