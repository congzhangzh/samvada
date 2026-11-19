"""Voice packs: download, verify, install and remove what a language needs (DESIGN 4.2, 8.4).

A pack is every file of one language in voices.lock, plus the engine environment it runs in.
Files come from an installed voice package (samvada-voice-<lang>, voicepkg.py), else from the
language's voice package wheel on samvada's GitHub release (pinned in voice-packages.toml),
else from the Hugging Face Hub at a pinned commit (or a URL at a commit), file by file. Each is
checked against its size and sha256 before it is put in place, wherever it came from. No
dependency: plain HTTP, which honours HF_ENDPOINT (a mirror) and HF_TOKEN (rate limits) for the
Hub, SAMVADA_VOICES_URL for the GitHub release. By hand: the URL mode prints where each file
comes from and downloads nothing; `--from` installs from a folder of downloaded files.
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

from . import __version__, voicepkg

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


class DownloadError(PackError):
    """A download failed or came wrong: the network, not the pack (the hint names --urls)."""


def mb(n: int) -> str:
    return f"{n / 1e6:.0f} MB"


# -- download ------------------------------------------------------------------------------
def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _download(url: str, part: Path, size: int, headers: dict[str, str], log, label: str) -> None:
    """Download `url` into `part` up to `size` bytes, resuming what `part` already holds.
    The caller checks the result."""
    have = part.stat().st_size if part.exists() else 0
    if have > size:
        part.unlink()
        have = 0
    if have >= size:
        return
    headers = {"User-Agent": f"samvada/{__version__}", **headers}
    if have:
        headers["Range"] = f"bytes={have}-"
    log(f"  {label} ← {url.split('?')[0]}")
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            mode = "ab" if have and r.status == 206 else "wb"
            with part.open(mode) as fh:
                while chunk := r.read(CHUNK):
                    fh.write(chunk)
    except OSError as e:
        raise PackError(f"{label}: download failed: {e}") from e


def fetch(f: File, out: Path, env: Mapping[str, str], log=print,
          pkg: voicepkg.Package | None = None) -> None:
    """Download one file to `out`, resuming a partial download, and verify it.

    A voice package is used first (`pkg`, a downloaded wheel; else the installed one), then
    $SAMVADA_DOWNLOAD_CACHE (a folder of files named by sha256, filled by downloads, so
    building bundles again never downloads twice), then the Hub.
    """
    pkg = pkg or voicepkg.find(f.pack, env)
    if pkg and pkg.has(f.sha256):
        if pkg.extract(f.sha256, out) and out.stat().st_size == f.size \
                and _sha256(out) == f.sha256:
            log(f"  {f.pack}: {f.dest} ({mb(f.size)}) ← {pkg.name} {pkg.version}")
            return
        out.unlink(missing_ok=True)
        missing = f.dest in pkg.missing()
        log(f"  {f.pack}: {pkg.name} {pkg.version} could not give {f.dest}"
            + (" (missing from the package)" if missing else " (differs from voices.lock)")
            + "; downloading it")
    cache = Path(env["SAMVADA_DOWNLOAD_CACHE"]) if env.get("SAMVADA_DOWNLOAD_CACHE") else None
    if cache and (cache / f.sha256).is_file():
        shutil.copyfile(cache / f.sha256, out)
        if out.stat().st_size == f.size and _sha256(out) == f.sha256:
            return
        out.unlink()
    part = out.with_name(out.name + ".part")
    headers = {"Authorization": f"Bearer {env['HF_TOKEN']}"} if f.repo and env.get("HF_TOKEN") else {}
    try:
        _download(f.source_url(env), part, f.size, headers, log, f"{f.pack}: {f.dest} ({mb(f.size)})")
    except PackError as e:
        raise DownloadError(f"{f.dest}: download failed: {e.__cause__}") from e
    if part.stat().st_size != f.size or _sha256(part) != f.sha256:
        part.unlink()
        raise DownloadError(f"{f.dest}: size or sha256 does not match voices.lock; removed, try again")
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


def from_package(lang: str, lock: Lock, env: Mapping[str, str]) -> voicepkg.Package | None:
    """The installed voice package that holds every file of this pack, or None."""
    pkg = voicepkg.find(lang, env)
    files = lock.pack(lang)
    return pkg if pkg and files and pkg.complete(files) else None


def add(lang: str, lock: Lock, home: Path, env: Mapping[str, str], log=print,
        offline: bool = False) -> int:
    """Install one pack into `home`; returns the bytes put in place (0 when already there).
    offline: only from an installed voice package, never the network."""
    files = lock.pack(lang)
    if not files:
        raise PackError(f"no voice pack for {lang} in voices.lock")
    if offline and from_package(lang, lock, env) is None:
        raise PackError(f"{lang}: no complete samvada-voice-{lang} package installed")
    home.mkdir(parents=True, exist_ok=True)
    marks = _marks(home)
    done = marks.get(lang, {})
    todo = _todo(files, home, done)
    wheel = None
    if todo and not offline and from_package(lang, lock, env) is None:
        wheel = release_wheel(lang, lock, home, env, log, need=sum(f.size for f in todo))
    try:
        fetched = _put(lang, todo, home, lambda f, out: fetch(f, out, env, log, pkg=wheel),
                       marks, done)
    finally:
        if wheel:
            wheel.close()
            if not env.get("SAMVADA_DOWNLOAD_CACHE"):
                wheel.folder.unlink(missing_ok=True)
    _hf_refs(home, files)
    marks[lang] = done
    _save_marks(home, marks)
    return fetched


def _todo(files: list[File], home: Path, done: dict) -> list[File]:
    """The files of a pack not yet in `home` as pinned (a file found whole is noted in `done`)."""
    todo = []
    for f in files:
        target = home / f.dest
        if f.unpack:
            if done.get(f.dest) == f.sha256 and target.is_dir():
                continue
        elif target.is_file() and target.stat().st_size == f.size and _sha256(target) == f.sha256:
            done[f.dest] = f.sha256
            continue
        todo.append(f)
    return todo


def release_wheel(lang: str, lock: Lock, home: Path, env: Mapping[str, str], log=print,
                  need: int = 0) -> voicepkg.Wheel | None:
    """Download `lang`'s voice package wheel from samvada's GitHub release and open it; None
    (and a line why) when there is none, it is much bigger than what is missing, or it fails:
    the caller then goes to the Hub file by file. A partial download resumes next time."""
    w = voicepkg.release_wheel(lang, lock.pack(lang), env)
    if w is None or need * 2 < w["size"]:
        return None
    cache = Path(env["SAMVADA_DOWNLOAD_CACHE"]) if env.get("SAMVADA_DOWNLOAD_CACHE") else None
    path = cache / w["sha256"] if cache else home / f".{w['file']}.download"
    if not (path.is_file() and path.stat().st_size == w["size"] and _sha256(path) == w["sha256"]):
        part = path.with_name(path.name + ".part")
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _download(w["url"], part, w["size"], {}, log, f"{lang}: {w['file']} ({mb(w['size'])})")
        except PackError as e:
            log(f"  {lang}: the GitHub release failed ({e.__cause__}); file by file from the Hub")
            return None
        if part.stat().st_size != w["size"] or _sha256(part) != w["sha256"]:
            part.unlink()
            log(f"  {lang}: {w['file']} does not match voice-packages.toml; file by file from the Hub")
            return None
        part.replace(path)
    wheel = voicepkg.open_wheel(path, lang)
    if wheel is None:
        log(f"  {lang}: {w['file']} is not a voice package of this format; file by file from the Hub")
    return wheel


def _put(lang: str, todo: list[File], home: Path, get, marks: dict, done: dict) -> int:
    """Put each file in place; `get(f, out)` writes it to `out`, verified against voices.lock."""
    fetched = 0
    for f in todo:
        target = home / f.dest
        tmp = home / f".{Path(f.dest).name}.{f.sha256[:12]}.download"
        get(f, tmp)
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
    return fetched


# -- by hand: the URL mode and --from (DESIGN 8.4) -------------------------------------------
def _size(n: int) -> str:
    return f"{mb(n)} ({n} bytes)" if n >= 1_000_000 else f"{n} bytes"


def url_lines(lang: str, lock: Lock, todo: list[File], env: Mapping[str, str]) -> list[str]:
    """Where to download a pack's missing files by hand: its wheel on samvada's GitHub release
    (one file: the first choice), else each file from where it is published (the Hub at its
    pinned commit, or its URL). Each with its size and sha256, to check what came."""
    files = lock.pack(lang)
    out = [f"{lang} ({len(todo)} of {len(files)} files, {mb(sum(f.size for f in todo))}):"]
    w = voicepkg.release_wheel(lang, files, env)
    if w:
        out += ["  either its voice package, one file from samvada's GitHub release:",
                f"    {w['url']}", f"      {_size(w['size'])}  sha256 {w['sha256']}",
                "  or each file from where it is published:"]
    else:
        out.append("  each file from where it is published:")
    for f in todo:
        out += [f"    {f.source_url(env)}",
                f"      {_size(f.size)}  sha256 {f.sha256}  (installed as {f.dest})"]
    return out


def urls(lang: str, lock: Lock, home: Path, env: Mapping[str, str]) -> list[str]:
    """The URL mode for one pack: what to download by hand; nothing is downloaded."""
    files = lock.pack(lang)
    if not files:
        raise PackError(f"no voice pack for {lang} in voices.lock")
    todo = _todo(files, home, dict(_marks(home).get(lang, {})))
    if not todo:
        return [f"{lang}: already installed in {home}, nothing to download"]
    if from_package(lang, lock, env) is not None:
        return [f"{lang}: in the installed {voicepkg.project(lang)} package, nothing to download "
                f"(samvada voices add {lang})"]
    return url_lines(lang, lock, todo, env)


def engine_lines(engine: str, again: str) -> list[str]:
    """How to install a missing engine: its pinned requirements, from PyPI."""
    reqs = resources.files("samvada").joinpath("locks", f"{engine}.txt")
    try:
        pins = [l for l in reqs.read_text().splitlines() if l and not l[0].isspace() and l[0] != "#"]
    except OSError:
        pins = []
    return [f"engine {engine}: not installed; it comes from PyPI, {len(pins)} pinned packages "
            f"({reqs}):", f"  {again} --engine always"]


class Missing(PackError):
    """Files of a pack that the folder does not hold."""

    def __init__(self, lang: str, files: list[File]):
        super().__init__(f"{len(files)} file(s) missing")
        self.lang, self.files = lang, files


class Downloads:
    """A folder of files downloaded by hand, whatever their names: a file is found by its
    size, then its sha256; a file is hashed only when its size is one we look for, and once."""

    def __init__(self, folder: Path):
        if not folder.is_dir():
            raise PackError(f"--from {folder}: no such folder")
        self.folder = folder
        self.by_size: dict[int, list[Path]] = {}
        for p in sorted(folder.iterdir()):
            if p.name.endswith(".part") or not p.is_file():
                continue
            try:
                self.by_size.setdefault(p.stat().st_size, []).append(p)
            except OSError:
                continue
        self.sha: dict[Path, str] = {}
        self.used: set[Path] = set()

    def find(self, size: int, sha256: str) -> Path | None:
        for p in self.by_size.get(size, []):
            if p not in self.sha:
                self.sha[p] = _sha256(p)
            if self.sha[p] == sha256:
                self.used.add(p)
                return p
        return None

    def wrong(self) -> list[Path]:
        """Files of a size we looked for whose sha256 matched nothing: not what was pinned."""
        return [p for p in self.sha if p not in self.used]


def add_from(lang: str, lock: Lock, home: Path, dl: Downloads, env: Mapping[str, str],
             log=print) -> int:
    """Install one pack from a folder of downloaded files, with no network: the pack's wheel
    from the GitHub release and/or its files, mixed. Nothing is installed unless the folder
    holds everything missing (else Missing). Every file is checked against voices.lock."""
    files = lock.pack(lang)
    if not files:
        raise PackError(f"no voice pack for {lang} in voices.lock")
    home.mkdir(parents=True, exist_ok=True)
    marks = _marks(home)
    done = marks.get(lang, {})
    todo = _todo(files, home, done)
    wheel = None
    w = voicepkg.release_wheel(lang, files, env) if todo else None
    if w and (p := dl.find(w["size"], w["sha256"])):
        wheel = voicepkg.open_wheel(p, lang)
        if wheel is None:
            log(f"  {lang}: {p.name} matches its pin but is not a voice package of this format")
    try:
        gaps = set(wheel.missing()) if wheel else set()
        loose: dict[str, Path] = {}
        missing = []
        for f in todo:
            p = dl.find(f.size, f.sha256)
            if p:
                loose[f.sha256] = p
            elif not (wheel and wheel.has(f.sha256) and f.dest not in gaps):
                missing.append(f)
        if missing:
            raise Missing(lang, missing)

        def get(f: File, out: Path) -> None:
            src = loose.get(f.sha256)
            if src is not None:
                shutil.copyfile(src, out)  # copied: the downloaded file stays where it is
            else:
                wheel.extract(f.sha256, out)
            label = src.name if src is not None else wheel.folder.name
            if out.stat().st_size != f.size or _sha256(out) != f.sha256:
                out.unlink()
                raise PackError(f"{f.dest}: {label} does not match voices.lock")
            log(f"  {lang}: {f.dest} ({mb(f.size)}) ← {label}")
        fetched = _put(lang, todo, home, get, marks, done)
    finally:
        if wheel:
            wheel.close()
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
        package = {"kokoro": "kokoro_onnx", "voicevox": "voicevox_core"}.get(d.name, "piper")
        return (d / ".python").read_text().strip() == tag and (d / package).is_dir()
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
        # --compile-bytecode: the first sentence would otherwise compile it (pypinyin's
        # dictionaries made Kokoro's first Chinese load 12 s instead of 3.7 s); pip does by default
        cmd = [uv, "pip", "install", "--quiet", "--python", sys.executable, "--compile-bytecode",
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
