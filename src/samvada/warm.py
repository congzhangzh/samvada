"""Warm engines: load a model and its text reader once, then speak many sentences.

Kokoro and VOICEVOX spend most of a clip getting ready (loading 300 MB of weights, and for
Chinese building pypinyin's dictionary: 0.8 to 2.7 s, every clip). A warm engine is a small
server of Samvada's own, `python -m samvada.warm --engine E --model M --lang L --socket S`,
started the first time a voice is needed and kept while it is used:

- one server per (engine, model, language, Samvada's version, the engine's environment), on
  a Unix socket in `$XDG_RUNTIME_DIR/samvada/`, a folder that must be the owner's own (mode
  0700, not a link); a folder that is not turns warm engines off;
- **one server, however many sessions**: the server holds `<name>.pid` locked (flock) for its
  whole life, and a client starts one only under the start lock `<name>.lock` and only when no
  server holds the pid lock, so parallel sessions and a slow load never start a second one;
- **one sentence at a time**: connections are answered in turn; a client that gives up never
  ends the server;
- **it frees its memory**: freed heap goes back to the system after each sentence (malloc_trim);
  it exits after `$SAMVADA_WARM_IDLE` seconds with no sentence (600), and when it has grown
  `$SAMVADA_WARM_GROWTH_MB` (768) past its size after loading; the next sentence starts a fresh one;
- `$SAMVADA_WARM=off` turns it off; any failure falls back to one process per clip.

The protocol is one JSON line each way: {"text", "voice", "out"} → {"ok": true} or
{"ok": false, "error": "..."}; {"ping"}, {"info"}, {"stop"} for the tools.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path

try:
    import fcntl  # POSIX; on Windows there are no warm engines (one process per clip)
except ImportError:  # pragma: no cover
    fcntl = None

ENGINES = {"kokoro": "samvada.kokoro", "voicevox": "samvada.voicevox"}
IDLE = 600          # seconds without a sentence before a server exits
GROWTH_MB = 768     # growth past the loaded size that makes a server step down (measured:
                    # VOICEVOX's arena levels off ~370 MB above its loaded size)
START_WAIT = 60     # seconds a client waits for a new server to load
TIMEOUT = 180       # seconds for one sentence


def _own_private_dir(d: Path) -> bool:
    """A folder of the owner's own: not a link, owned by this user, no access for others."""
    try:
        st = os.lstat(d)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        return False
    if st.st_mode & 0o077:
        try:
            os.chmod(d, 0o700)
        except OSError:
            return False
    return not (os.lstat(d).st_mode & 0o077)


def runtime_dir(env: Mapping[str, str] = os.environ) -> Path | None:
    """Where the sockets and locks live: $XDG_RUNTIME_DIR/samvada, else a folder in /tmp; a
    Unix socket's path must stay under about 100 bytes. None: no safe folder (and on Windows)."""
    if not hasattr(os, "getuid") or not hasattr(socket, "AF_UNIX"):
        return None
    base = env.get("XDG_RUNTIME_DIR")
    d = Path(base) / "samvada" if base and os.path.isdir(base) else None
    if d is None or len(str(d)) > 70:
        d = Path(tempfile.gettempdir()) / f"samvada-{os.getuid()}"
    try:
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        return None
    return d if _own_private_dir(d) else None


def enabled(env: Mapping[str, str] = os.environ) -> bool:
    return (fcntl is not None and env.get("SAMVADA_WARM", "on").lower() not in ("off", "0", "no")
            and runtime_dir(env) is not None)


def _version() -> str:
    """Samvada's version and the newest change to its code: an editable install keeps its
    version while the code changes, and a server must not outlive the code it runs."""
    try:
        from . import __version__
    except ImportError:  # pragma: no cover
        __version__ = "?"
    here = Path(__file__).parent
    newest = max((f.stat().st_mtime_ns for f in here.glob("*.py")), default=0)
    return f"{__version__}+{newest}"


def server_name(engine: str, model: str, lang: str, base: list[str],
                env: Mapping[str, str]) -> str:
    """One name per engine, model, language, code version and engine environment: a server
    started under other settings is never asked."""
    keys = sorted((k, v) for k, v in env.items()
                  if k in ("PYTHONPATH", "HF_HOME", "NLTK_DATA", "VOICEVOX_ROOT")
                  or k.startswith(("HF_", "PIPER_")))
    h = hashlib.sha1(json.dumps([engine, str(Path(model).resolve()), lang, base, keys,
                                 _version()]).encode()).hexdigest()[:12]
    return f"{engine}-{h}"


def open_lock(path: Path) -> int:
    """A lock file opened without following a link (O_NOFOLLOW), mode 0600."""
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    return os.open(path, flags, 0o600)


def _held(path: Path) -> bool:
    """Does a live process hold this lock?"""
    try:
        fd = open_lock(path)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    finally:
        os.close(fd)  # closing drops a lock we took
    return False


# ---- the client ----------------------------------------------------------------------------

def _ask(sock: Path, req: dict, timeout: float = TIMEOUT) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(str(sock))
        s.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    try:
        r = json.loads(buf or b"{}")
    except ValueError:
        return {}
    return r if isinstance(r, dict) else {}


def _alive(sock: Path, timeout: float = 5) -> bool:
    try:
        return bool(_ask(sock, {"ping": True}, timeout=timeout).get("ok"))
    except OSError:
        return False


def _start(cmd: list[str], env: Mapping[str, str], d: Path, name: str) -> bool:
    """Make sure a server runs; start one under the start lock if none does. True when one
    answers on its socket."""
    sock, pidf = d / f"{name}.sock", d / f"{name}.pid"
    lf = open_lock(d / f"{name}.lock")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX)          # parallel clients queue here
        if _alive(sock):                        # another session started it while we waited
            return True
        proc = None
        if not _held(pidf):                     # no server, not even one still loading
            with open(d / f"{name}.log", "ab") as out:
                proc = subprocess.Popen(cmd, env=dict(env), stdin=subprocess.DEVNULL,
                                        stdout=out, stderr=out, start_new_session=True,
                                        close_fds=True)
        end = time.time() + float(env.get("SAMVADA_WARM_START_WAIT", START_WAIT))
        while time.time() < end:
            if sock.exists() and _alive(sock):
                return True
            if proc is not None and proc.poll() is not None:
                return False                    # it died while loading: no use waiting
            if proc is None and not _held(pidf):
                return False                    # the one that was loading has gone
            time.sleep(0.05)
        return False
    finally:
        os.close(lf)


def speak(engine: str, base: list[str], extra_env: Mapping[str, str], env: Mapping[str, str],
          model: str, lang: str, voice: str, text: str, out: Path, debug: bool = False) -> bool:
    """Speak one sentence through the warm server of this voice, starting it if need be.
    `base` is the engine's own command ([python, -s, -m, samvada.kokoro]): the server runs
    with the same Python and paths. False: the caller falls back to one process per clip."""
    if engine not in ENGINES or not enabled(env):
        return False
    d = runtime_dir(env)
    full = {**env, **extra_env}
    name = server_name(engine, model, lang, base, full)
    sock = d / f"{name}.sock"
    req = {"text": text, "voice": voice, "out": str(out)}
    try:
        try:
            r = _ask(sock, req)
        except (FileNotFoundError, ConnectionRefusedError):
            cmd = [base[0], *base[1:-2], "-m", "samvada.warm", "--engine", engine,
                   "--model", model, "--lang", lang, "--socket", str(sock)]
            if not _start(cmd, full, d, name):
                if debug:
                    print(f"samvada: the warm {engine} did not start (see {d / (name + '.log')})",
                          file=sys.stderr)
                return False
            r = _ask(sock, req)
    except OSError as e:
        if debug:
            print(f"samvada: warm {engine}: {e}", file=sys.stderr)
        return False
    if not r.get("ok") and debug:
        print(f"samvada: warm {engine}: {r.get('error')}", file=sys.stderr)
    return bool(r.get("ok")) and out.is_file() and out.stat().st_size > 0


def _pid_of(pidf: Path) -> int | None:
    try:
        pid = int(pidf.read_text().split()[0])
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes() if Path("/proc").is_dir() else b""
    except (OSError, ValueError, IndexError):
        return None
    if Path("/proc").is_dir() and b"samvada.warm" not in cmdline:
        return None                              # that pid is someone else's by now
    return pid if _held(pidf) else None


def running(env: Mapping[str, str] = os.environ) -> list[dict]:
    """Every server: what it holds and how it does, or `busy` when it does not answer now."""
    if not enabled(env):
        return []
    out = []
    for pidf in sorted(runtime_dir(env).glob("*.pid")):
        pid = _pid_of(pidf)
        if pid is None:
            continue
        sock = pidf.with_suffix(".sock")
        try:
            info = _ask(sock, {"info": True}, timeout=2)
        except OSError:
            info = {}
        out.append({**info, "pid": pid, "socket": str(sock), "busy": not info.get("ok")})
    return out


def stop_all(env: Mapping[str, str] = os.environ) -> int:
    """Stop every server: asked to, and a busy or stuck one with SIGTERM."""
    n = 0
    for s in running(env):
        try:
            if s["busy"]:
                raise OSError("busy")
            _ask(Path(s["socket"]), {"stop": True}, timeout=2)
        except OSError:
            try:
                os.kill(s["pid"], signal.SIGTERM)
            except OSError:
                continue
        n += 1
    return n


# ---- the server ----------------------------------------------------------------------------

def _rss_mb() -> float:
    """This process's memory now (Linux: /proc/self/statm), else its peak."""
    try:
        pages = int(Path("/proc/self/statm").read_text().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE") / 2**20
    except (OSError, ValueError, IndexError):
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return peak / (2**20 if sys.platform == "darwin" else 2**10)


def _trim() -> None:
    """Give freed heap back to the system (glibc keeps it otherwise): Kokoro's "growth" after a
    long sentence is mostly that, and goes after this (measured: 841 MB -> 384 MB)."""
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass  # not glibc


def _read_line(conn: socket.socket) -> bytes:
    buf = b""
    while not buf.endswith(b"\n") and len(buf) < 1 << 20:
        chunk = conn.recv(65536)
        if not chunk:
            break
        buf += chunk
    return buf


def serve(engine_name: str, model: str, lang: str, sock: Path, idle: float,
          growth_mb: float) -> int:
    import importlib

    pidf = sock.with_suffix(".pid")
    pfd = open_lock(pidf)
    try:
        fcntl.flock(pfd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # one server per name, for life
    except OSError:
        return 0                                          # another one is already here
    os.ftruncate(pfd, 0)
    os.write(pfd, f"{os.getpid()}\n".encode())
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # so `finally` cleans up

    mod = importlib.import_module(ENGINES[engine_name])
    t0 = time.time()
    engine = mod.Engine(model, lang)                      # the slow part, once
    loaded, base_mb = time.time() - t0, _rss_mb()
    sock.unlink(missing_ok=True)                          # we hold the pid lock: not a live one
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o077)
    try:
        srv.bind(str(sock))
    finally:
        os.umask(old)
    inode = os.stat(sock).st_ino
    srv.listen(16)
    srv.settimeout(idle)
    served, started, why = 0, time.time(), "idle"
    try:
        while True:
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                return 0                                  # idle: free the memory
            stop = False
            try:
                with conn:
                    conn.settimeout(TIMEOUT)
                    try:
                        req = json.loads(_read_line(conn) or b"{}")
                        if not isinstance(req, dict):
                            raise ValueError("not a JSON object")
                        if req.get("ping"):
                            resp = {"ok": True}
                        elif req.get("info"):
                            resp = {"ok": True, "pid": os.getpid(), "engine": engine_name,
                                    "model": model, "lang": lang, "loaded_s": round(loaded, 2),
                                    "served": served, "up_s": round(time.time() - started),
                                    "mb": round(_rss_mb()), "base_mb": round(base_mb)}
                        elif req.get("stop"):
                            resp, stop, why = {"ok": True}, True, "asked to stop"
                        else:
                            engine.speak(req["text"], req["voice"], req["out"])
                            served += 1
                            resp = {"ok": True}
                            _trim()
                            if _rss_mb() > base_mb + growth_mb:
                                stop, why = True, "grown"  # step down; the next one is fresh
                    except Exception as e:  # one bad request must not end the server
                        resp = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode())
            except OSError:
                pass                                      # a client that gave up
            if stop:
                print(f"samvada.warm: exit ({why}) after {served} sentences, "
                      f"{_rss_mb():.0f} MB (loaded {base_mb:.0f} MB)", file=sys.stderr)
                return 0
    finally:
        srv.close()
        try:
            if os.stat(sock).st_ino == inode:             # only our own socket
                sock.unlink()
        except OSError:
            pass
        os.close(pfd)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="samvada.warm")
    p.add_argument("--engine", required=True, choices=sorted(ENGINES))
    p.add_argument("--model", required=True)
    p.add_argument("--lang", required=True)
    p.add_argument("--socket", required=True)
    a = p.parse_args(argv)
    idle = float(os.environ.get("SAMVADA_WARM_IDLE", IDLE))
    growth = float(os.environ.get("SAMVADA_WARM_GROWTH_MB", GROWTH_MB))
    return serve(a.engine, a.model, a.lang, Path(a.socket), idle, growth)


if __name__ == "__main__":
    sys.exit(main())
