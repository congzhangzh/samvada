"""Voice packs: download, verify, install and remove what a language needs (DESIGN 4.2, 8.4).

A pack is every file of one language in voices.lock, plus the engine environment it runs in.
Files come from the Hugging Face Hub at a pinned commit (or a URL at a commit) and are checked
against their size and sha256 before they are put in place. No dependency: plain HTTP to the
Hub's resolve URLs, which honours HF_ENDPOINT (a mirror) and HF_TOKEN (rate limits).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tomllib
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from . import __version__

CHUNK = 1 << 20
MARK = ".samvada-packs.json"  # in the voices folder: what each pack installed, by sha256


@dataclass
class File:
    pack: str
    dest: str
    size: int
    sha256: str
    unpack: str = ""
    repo: str = ""
    repo_type: str = "model"
    revision: str = ""
    path: str = ""
    url: str = ""

    def source_url(self, env: Mapping[str, str]) -> str:
        if self.url:
            return self.url
        endpoint = (env.get("HF_ENDPOINT") or "https://huggingface.co").rstrip("/")
        prefix = "datasets/" if self.repo_type == "dataset" else ""
        return (f"{endpoint}/{prefix}{self.repo}/resolve/{self.revision}/"
                + urllib.parse.quote(self.path))


@dataclass
class Lock:
    files: list[File]
    engines: dict[str, str]

    def pack(self, lang: str) -> list[File]:
        return [f for f in self.files if f.pack == lang]

    def size(self, lang: str) -> int:
        return sum(f.size for f in self.pack(lang))


def load_lock(path: Path | None = None) -> Lock:
    """voices.lock from the package; $SAMVADA_VOICE_LOCK may point at another (tests)."""
    if path is None and os.environ.get("SAMVADA_VOICE_LOCK"):
        path = Path(os.environ["SAMVADA_VOICE_LOCK"])
    text = path.read_text() if path else \
        resources.files("samvada").joinpath("voices.lock").read_text()
    data = tomllib.loads(text)
    return Lock([File(**f) for f in data.get("file", [])], dict(data.get("engines", {})))


def engine_requirements(engine: str) -> str:
    return resources.files("samvada").joinpath("locks", f"{engine}.txt").read_text()


class PackError(Exception):
    pass


def mb(n: int) -> str:
    return f"{n / 1e6:.0f} MB"


# -- download ------------------------------------------------------------------------------
def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def fetch(f: File, out: Path, env: Mapping[str, str], log=print) -> None:
    """Download one file to `out`, resuming a partial download, and verify it.

    $SAMVADA_DOWNLOAD_CACHE (a folder of files named by sha256) is used first and filled,
    so building bundles again never downloads twice.
    """
    cache = Path(env["SAMVADA_DOWNLOAD_CACHE"]) if env.get("SAMVADA_DOWNLOAD_CACHE") else None
    if cache and (cache / f.sha256).is_file():
        shutil.copyfile(cache / f.sha256, out)
        if out.stat().st_size == f.size and _sha256(out) == f.sha256:
            return
        out.unlink()
    part = out.with_name(out.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    if have > f.size:
        part.unlink()
        have = 0
    if have < f.size:
        url = f.source_url(env)
        headers = {"User-Agent": f"samvada/{__version__}"}
        if f.repo and env.get("HF_TOKEN"):
            headers["Authorization"] = f"Bearer {env['HF_TOKEN']}"
        if have:
            headers["Range"] = f"bytes={have}-"
        log(f"  {f.pack}: {f.dest} ({mb(f.size)}) ← {url.split('?')[0]}")
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                mode = "ab" if have and r.status == 206 else "wb"
                with part.open(mode) as fh:
                    while chunk := r.read(CHUNK):
                        fh.write(chunk)
        except OSError as e:
            raise PackError(f"{f.dest}: download failed: {e}") from e
    if part.stat().st_size != f.size or _sha256(part) != f.sha256:
        part.unlink()
        raise PackError(f"{f.dest}: size or sha256 does not match voices.lock; removed, try again")
    part.replace(out)
    if cache:
        cache.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(out, cache / f.sha256)


# -- install -------------------------------------------------------------------------------
def _marks(home: Path) -> dict[str, dict[str, str]]:
    try:
        return json.loads((home / MARK).read_text())
    except (OSError, ValueError):
        return {}


def _save_marks(home: Path, marks: dict) -> None:
    tmp = home / (MARK + ".tmp")
    tmp.write_text(json.dumps(marks, indent=1, sort_keys=True) + "\n")
    tmp.replace(home / MARK)


def _inside(base: Path, name: str) -> Path:
    target = (base / name).resolve()
    if not target.is_relative_to(base.resolve()):
        raise PackError(f"archive entry escapes its folder: {name}")
    return target


def _unpack(archive: Path, kind: str, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    if kind == "tar":
        with tarfile.open(archive) as tar:
            for m in tar.getmembers():
                _inside(dest, m.name)
            tar.extractall(dest, filter="data")
    elif kind == "zip":
        with zipfile.ZipFile(archive) as z:
            for n in z.namelist():
                _inside(dest, n)
            z.extractall(dest)
    else:
        raise PackError(f"unknown unpack kind {kind}")


def _hf_refs(home: Path, files: list[File]) -> None:
    """A Hugging Face cache needs refs/main next to snapshots/<rev>/ to work offline."""
    for f in files:
        parts = Path(f.dest).parts
        if len(parts) > 4 and parts[0] == "hf" and parts[3] == "snapshots":
            ref = home.joinpath(*parts[:3], "refs", "main")
            ref.parent.mkdir(parents=True, exist_ok=True)
            ref.write_text(parts[4])


def add(lang: str, lock: Lock, home: Path, env: Mapping[str, str], log=print) -> int:
    """Install one pack into `home`; returns the bytes downloaded (0 when already there)."""
    files = lock.pack(lang)
    if not files:
        raise PackError(f"no voice pack for {lang} in voices.lock")
    home.mkdir(parents=True, exist_ok=True)
    marks = _marks(home)
    done = marks.get(lang, {})
    fetched = 0
    for f in files:
        target = home / f.dest
        if f.unpack:
            if done.get(f.dest) == f.sha256 and target.is_dir():
                continue
        elif target.is_file() and target.stat().st_size == f.size and _sha256(target) == f.sha256:
            done[f.dest] = f.sha256
            continue
        tmp = home / f".{Path(f.dest).name}.{f.sha256[:12]}.download"
        fetch(f, tmp, env, log)
        fetched += f.size
        if f.unpack:
            _unpack(tmp, f.unpack, target)
            tmp.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp.replace(target)
        done[f.dest] = f.sha256
        marks[lang] = done
        _save_marks(home, marks)
    _hf_refs(home, files)
    marks[lang] = done
    _save_marks(home, marks)
    return fetched


def verify(lang: str, lock: Lock, home: Path) -> list[str]:
    """Problems with an installed pack; empty when it is whole."""
    problems = []
    done = _marks(home).get(lang, {})
    for f in lock.pack(lang):
        target = home / f.dest
        if f.unpack:
            if done.get(f.dest) != f.sha256 or not target.is_dir():
                problems.append(f"{f.dest}: not unpacked from the pinned archive")
        elif not target.is_file():
            problems.append(f"{f.dest}: missing")
        elif target.stat().st_size != f.size or _sha256(target) != f.sha256:
            problems.append(f"{f.dest}: differs from voices.lock")
    return problems


def remove(lang: str, lock: Lock, home: Path) -> None:
    """Remove a pack's files; a folder another installed pack still lists is kept."""
    marks = _marks(home)
    others = {d for l, m in marks.items() if l != lang for d in m}
    for f in lock.pack(lang):
        if f.dest in others:
            continue
        target = home / f.dest
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)
    marks.pop(lang, None)
    _save_marks(home, marks)


# -- engines -------------------------------------------------------------------------------
def engine_ok(d: Path) -> bool:
    """An engine folder made by install_engine for this Python."""
    tag = f"{sys.version_info.major}.{sys.version_info.minor}"
    try:
        return (d / ".python").read_text().strip() == tag and (d / "piper").is_dir()
    except OSError:
        return False


def install_engine(engine: str, root: Path, log=print) -> Path:
    """Install an engine's pinned requirements into root/<engine> (pip --target), for the
    Python that runs Samvada. Engines live in separate folders: piper-tts and piper-plus both
    install a module called `piper`."""
    d = root / engine
    if engine_ok(d):
        return d
    reqs = root / f".{engine}.txt"
    root.mkdir(parents=True, exist_ok=True)
    reqs.write_text(engine_requirements(engine))
    staging = root / f".{engine}.staging"
    shutil.rmtree(staging, ignore_errors=True)
    uv = shutil.which("uv")
    if uv:
        cmd = [uv, "pip", "install", "--quiet", "--python", sys.executable,
               "--target", str(staging), "--no-deps", "-r", str(reqs)]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "--quiet", "--target", str(staging),
               "--no-deps", "-r", str(reqs)]
    log(f"  engine {engine}: installing its pinned requirements ({'uv' if uv else 'pip'})")
    r = subprocess.run(cmd)
    reqs.unlink(missing_ok=True)
    if r.returncode != 0:
        shutil.rmtree(staging, ignore_errors=True)
        raise PackError(f"engine {engine}: install failed (needs uv, or pip in this Python)")
    (staging / ".python").write_text(f"{sys.version_info.major}.{sys.version_info.minor}\n")
    shutil.rmtree(d, ignore_errors=True)
    staging.replace(d)
    return d


def sizes_line(lock: Lock, lang: str) -> str:
    return f"{len(lock.pack(lang))} files, {mb(lock.size(lang))}"


def default_env() -> Mapping[str, str]:
    return os.environ


def installed(home: Path) -> list[str]:
    """The packs Samvada installed in `home`."""
    return sorted(_marks(home))
