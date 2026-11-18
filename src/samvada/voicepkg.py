"""Voice packages: the voice packs as Python packages on PyPI (DESIGN 8.1).

One package per language, `samvada-voice-<lang>` (module `samvada_voice_<lang>`), holding the
pack's files byte for byte as pinned in voices.lock, under `data/<dest>`, with an index. A voice
model hardly compresses (its weights are floats), so en and zh are wheels over PyPI's default
100 MiB a file; their projects have a raised limit (DESIGN 8.1).

Until PyPI raises those limits, the same wheels are assets of samvada's GitHub release
`voices-<version>`, pinned in `voice-packages.toml` (file, size, sha256 and the pack's digest):
`packs.add` downloads the language's wheel from there and reads the files straight from its zip
(`Wheel`), then falls back to the Hub file by file for whatever it could not give.

Samvada never runs a voice from the package: `packs.add` takes each file from here instead of
the network, checks it against voices.lock (size, sha256) and installs it in the voices folder
as any pack. So a package that lies about its content is refused like a bad download.
`release/voice_packages.py` builds them.

    index.json  {"format": 2, "lang": "en", "version": "...", "digest": "<pack digest>",
                 "files": [{"dest": ..., "sha256": ..., "size": ..., "path": "data/<dest>"}, ...]}

$SAMVADA_VOICE_PACKAGES=off ignores them, installed or on GitHub (tests, or to force the Hub).
$SAMVADA_VOICES_URL replaces the GitHub release's address (a mirror, or tests);
$SAMVADA_VOICE_PINS another voice-packages.toml (tests).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import tomllib
import zipfile
from collections.abc import Iterable, Mapping
from importlib import resources
from pathlib import Path

FORMAT = 2
INDEX = "index.json"
DATA = "data"
PINS = "voice-packages.toml"


def module(lang: str) -> str:
    return f"samvada_voice_{lang}"


def project(lang: str) -> str:
    return f"samvada-voice-{lang}"


def digest(files: Iterable) -> str:
    """One pack's identity: every file's dest, size and sha256, in voices.lock order."""
    h = hashlib.sha256()
    for f in files:
        h.update(f"{f.dest}\t{f.size}\t{f.sha256}\n".encode())
    return h.hexdigest()


def _enabled(env: Mapping[str, str]) -> bool:
    return env.get("SAMVADA_VOICE_PACKAGES", "").lower() not in ("0", "off", "no", "false")


def _folder(mod: str) -> Path | None:
    try:
        spec = importlib.util.find_spec(mod)
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.submodule_search_locations:
        return None
    return Path(next(iter(spec.submodule_search_locations)))


class Package:
    """An installed voice package: its index and its folder."""

    def __init__(self, folder: Path, index: dict):
        self.folder = folder
        self.index = index
        self.lang = index["lang"]
        self.version = index["version"]
        self.by_sha = {f["sha256"]: f for f in index["files"]}

    @property
    def name(self) -> str:
        return project(self.lang)

    def _path(self, entry: dict) -> Path:
        return self.folder / entry["path"]

    def missing(self) -> list[str]:
        """The dests the index names that are not in the folder at their size."""
        out = []
        for e in self.index["files"]:
            p = self._path(e)
            if not (p.is_file() and p.stat().st_size == e["size"]):
                out.append(e["dest"])
        return out

    def has(self, sha256: str) -> bool:
        return sha256 in self.by_sha

    def complete(self, files: Iterable) -> bool:
        """Every one of `files` is in the index and every indexed file is in the folder."""
        return all(self.has(f.sha256) for f in files) and not self.missing()

    def extract(self, sha256: str, out: Path) -> bool:
        """Copy the file with this sha256 to `out`; False when the package lacks it.
        The caller verifies the bytes against voices.lock."""
        entry = self.by_sha.get(sha256)
        if entry is None or not self._path(entry).is_file():
            return False
        shutil.copyfile(self._path(entry), out)
        return True


def find(lang: str, env: Mapping[str, str] = os.environ) -> Package | None:
    """The installed `samvada-voice-<lang>`, or None (not installed, off, or unreadable)."""
    if not _enabled(env):
        return None
    d = _folder(module(lang))
    if d is None:
        return None
    try:
        index = json.loads((d / INDEX).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if index.get("format") != FORMAT or index.get("lang") != lang:
        return None
    return Package(d, index)


class Wheel(Package):
    """A voice package's wheel, downloaded and not installed: the same index and files, read
    from the zip. A member is read only up to the size the index gives it."""

    def __init__(self, path: Path, index: dict, zf: zipfile.ZipFile):
        super().__init__(path, index)
        self.zip = zf
        self.members = {i.filename: i for i in zf.infolist()}

    def _member(self, entry: dict) -> str:
        return f"{module(self.lang)}/{entry['path']}"

    def missing(self) -> list[str]:
        out = []
        for e in self.index["files"]:
            i = self.members.get(self._member(e))
            if i is None or i.file_size != e["size"]:
                out.append(e["dest"])
        return out

    def extract(self, sha256: str, out: Path) -> bool:
        entry = self.by_sha.get(sha256)
        if entry is None or self._member(entry) not in self.members:
            return False
        left = entry["size"]
        with self.zip.open(self._member(entry)) as src, out.open("wb") as dst:
            while left > 0 and (chunk := src.read(min(left, 1 << 20))):
                dst.write(chunk)
                left -= len(chunk)
        return True

    def close(self) -> None:
        self.zip.close()


def open_wheel(path: Path, lang: str) -> Wheel | None:
    """The voice package wheel at `path`, or None when it is not one for `lang`."""
    try:
        zf = zipfile.ZipFile(path)
        index = json.loads(zf.read(f"{module(lang)}/{INDEX}").decode("utf-8"))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return None
    if index.get("format") != FORMAT or index.get("lang") != lang:
        zf.close()
        return None
    return Wheel(path, index, zf)


def pins(env: Mapping[str, str] = os.environ) -> dict:
    """voice-packages.toml: the version, the release's address and each language's wheel."""
    try:
        if env.get("SAMVADA_VOICE_PINS"):
            text = Path(env["SAMVADA_VOICE_PINS"]).read_text(encoding="utf-8")
        else:
            text = resources.files("samvada").joinpath(PINS).read_text(encoding="utf-8")
        return tomllib.loads(text)
    except (OSError, ValueError):
        return {}


def release_wheel(lang: str, files: Iterable, env: Mapping[str, str] = os.environ) -> dict | None:
    """Where to download `lang`'s wheel: {"url", "file", "size", "sha256", "version"}, or None
    when packages are off, nothing is pinned for it, or its pack is not this voices.lock's."""
    if not _enabled(env):
        return None
    p = pins(env)
    w = p.get("wheels", {}).get(lang)
    if not w or w.get("digest") != digest(files):
        return None
    base = env.get("SAMVADA_VOICES_URL") or p.get("url", "")
    if not base:
        return None
    return {"url": base.rstrip("/") + "/" + w["file"], "file": w["file"], "size": int(w["size"]),
            "sha256": w["sha256"], "version": p.get("version", "")}
