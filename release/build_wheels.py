"""Compile the engine packages that PyPI has no wheel for on this system (DESIGN 8.2).

    uv run python release/build_wheels.py [--out DIR] [--work DIR] [PACKAGE ...]

On Windows arm64, piper-tts (C, links espeak-ng), pyopenjtalk-plus (C++, OpenJTalk) and
sudachipy (Rust) publish only sources. This builds each from its sdist, at the version pinned
in src/samvada/locks/, for a standalone CPython of the same system and arch as the Python that
runs this (bundle.python_request), and puts the wheels in --out for
`bundle.py --wheelhouse`. Needs the system's compilers: MSVC with the ARM64 tools, CMake (pulled
as a build dependency), Rust for sudachipy. Network is used here, never in the bundle.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bundle  # noqa: E402  (the same Python choice and helpers as the bundle)

DEFAULT = ["piper-tts", "pyopenjtalk-plus", "sudachipy"]


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
        bundle.run([py, "-m", "pip", "wheel", "--no-deps", "--no-binary", name,
                    "--wheel-dir", a.out, f"{name}=={version}"])
    for w in sorted(a.out.glob("*.whl")):
        print(f"build_wheels: {bundle.sha256(w)}  {w.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
