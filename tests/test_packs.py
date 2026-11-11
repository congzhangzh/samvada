"""Voice packs against a fake Hub (a local HTTP server): no network, nothing real downloaded."""

from __future__ import annotations

import hashlib
import http.server
import io
import json
import sys
import tarfile
import threading
import zipfile
from functools import partial
from pathlib import Path

import pytest

REV = "0123456789abcdef0123456789abcdef01234567"


class Hub:
    """Serves <root>/<url path>; counts requests; honours Range like the Hub does."""

    def __init__(self, root: Path):
        self.root = root
        self.hits: list[str] = []
        hub = self

        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                hub.hits.append(self.path)
                f = Path(self.translate_path(self.path.split("?")[0]))
                if not f.is_file():
                    self.send_error(404)
                    return
                data = f.read_bytes()
                rng = self.headers.get("Range")
                start = int(rng.split("=")[1].rstrip("-")) if rng else 0
                self.send_response(206 if rng else 200)
                self.send_header("Content-Length", str(len(data) - start))
                self.end_headers()
                self.wfile.write(data[start:])

        self.server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), partial(Handler, directory=str(root)))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_address[1]}"

    def put(self, url_path: str, data: bytes) -> None:
        p = self.root / url_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def entry(pack, dest, data, *, path, repo="org/voices", repo_type="model", unpack=""):
    return {"pack": pack, "repo": repo, "repo_type": repo_type, "revision": REV, "path": path,
            "dest": dest, "unpack": unpack, "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest()}


def tgz(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def zipped(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def write_lock(path: Path, entries: list[dict]) -> None:
    out = []
    for e in entries:
        out.append("[[file]]")
        out += [f"{k} = {v if isinstance(v, int) else json.dumps(v)}" for k, v in e.items()]
    out += ["[engines]", 'piper = "piper-tts==0"']
    path.write_text("\n".join(out) + "\n")


@pytest.fixture
def hub(env, tmp_path):
    h = Hub(tmp_path / "hub")
    model, cfg = b"M" * 5000, b'{"x": 1}'
    g2pw = tgz({"g2pw.onnx": b"G" * 300, "config.py": b"model_source = 'bert-base-chinese'"})
    vocab = b"[PAD]\n"
    nltk = zipped({"cmudict/README": b"cmu"})
    zh_model = b"Z" * 4000
    entries = [
        entry("en", "en_US-lessac-high.onnx", model, path="en/a.onnx"),
        entry("en", "en_US-lessac-high.onnx.json", cfg, path="en/a.onnx.json"),
        entry("zh", "zh_CN-xiao_ya-medium.onnx", zh_model, path="zh/b.onnx"),
        entry("zh", "g2pW", g2pw, path="zh/_resources/g2pw.tar.gz", repo="org/ckpt",
              repo_type="dataset", unpack="tar"),
        entry("zh", f"hf/hub/models--bert-base-chinese/snapshots/{REV}/vocab.txt", vocab,
              path="vocab.txt", repo="org/bert"),
        entry("ja", "nltk_data/corpora", nltk, path="cmudict.zip", repo="org/nltk", unpack="zip"),
    ]
    h.put(f"org/voices/resolve/{REV}/en/a.onnx", model)
    h.put(f"org/voices/resolve/{REV}/en/a.onnx.json", cfg)
    h.put(f"org/voices/resolve/{REV}/zh/b.onnx", zh_model)
    h.put(f"datasets/org/ckpt/resolve/{REV}/zh/_resources/g2pw.tar.gz", g2pw)
    h.put(f"org/bert/resolve/{REV}/vocab.txt", vocab)
    h.put(f"org/nltk/resolve/{REV}/cmudict.zip", nltk)
    lock = tmp_path / "voices.lock"
    write_lock(lock, entries)
    own = tmp_path / "own-voices"
    env.vars |= {"SAMVADA_VOICE_LOCK": str(lock), "HF_ENDPOINT": h.endpoint,
                 "SAMVADA_VOICES": str(own)}
    for m in env.voices.iterdir():  # start with no voice anywhere
        m.unlink()
    h.own = own
    h.entries = entries
    return h


def test_add_downloads_verifies_and_lays_out(env, hub):
    r = env.run("voices", "add", "en", "zh", "--engine", "never")
    assert r.returncode == 0, r.stderr
    own = hub.own
    assert (own / "en_US-lessac-high.onnx").read_bytes() == b"M" * 5000
    assert (own / "g2pW" / "g2pw.onnx").is_file()  # the tar is unpacked into g2pW/
    snap = own / "hf/hub/models--bert-base-chinese"
    assert (snap / "refs" / "main").read_text() == REV  # from_pretrained works offline
    assert (snap / "snapshots" / REV / "vocab.txt").is_file()
    assert not list(own.glob(".*.download")) and not list(own.glob("*.part"))
    assert "/datasets/org/ckpt/resolve/" in " ".join(hub.hits)
    # a second add downloads nothing
    n = len(hub.hits)
    r = env.run("voices", "add", "en", "zh", "--engine", "never")
    assert "already installed" in r.stdout and len(hub.hits) == n
    assert env.run("voices", "verify", "en", "zh").returncode == 0


def test_installed_pack_speaks_with_its_own_hf_cache(env, hub):
    env.run("voices", "add", "zh", "--engine", "never")
    r = env.run("zh", "你好", SAMVADA="on")
    assert r.returncode == 0, r.stderr
    line = env.lines()[0]
    assert line.startswith("piper zh_CN") and f"cwd={hub.own}" in line
    assert f"hf={hub.own}/hf" in line


def test_nltk_zip_is_unpacked(env, hub):
    env.run("voices", "add", "ja", "--engine", "never")
    assert (hub.own / "nltk_data/corpora/cmudict/README").is_file()


def test_bad_checksum_is_refused_and_nothing_installed(env, hub):
    hub.put(f"org/voices/resolve/{REV}/en/a.onnx", b"X" * 5000)  # same size, other bytes
    r = env.run("voices", "add", "en", "--engine", "never")
    assert r.returncode == 1 and "does not match voices.lock" in r.stderr
    assert not (hub.own / "en_US-lessac-high.onnx").exists()


def test_partial_download_resumes(env, hub):
    hub.own.mkdir(parents=True)
    e = hub.entries[0]
    part = hub.own / f".en_US-lessac-high.onnx.{e['sha256'][:12]}.download.part"
    part.write_bytes(b"M" * 1234)
    r = env.run("voices", "add", "en", "--engine", "never")
    assert r.returncode == 0, r.stderr
    assert (hub.own / "en_US-lessac-high.onnx").read_bytes() == b"M" * 5000


def test_download_cache_is_used_and_filled(env, hub, tmp_path):
    cache = tmp_path / "dlcache"
    env.run("voices", "add", "en", "--engine", "never", SAMVADA_DOWNLOAD_CACHE=str(cache))
    assert (cache / hub.entries[0]["sha256"]).is_file()
    env.run("voices", "remove", "en")
    n = len(hub.hits)
    env.run("voices", "add", "en", "--engine", "never", SAMVADA_DOWNLOAD_CACHE=str(cache))
    assert len(hub.hits) == n and (hub.own / "en_US-lessac-high.onnx").is_file()


def test_verify_finds_tampering_and_remove_cleans(env, hub):
    env.run("voices", "add", "en", "--engine", "never")
    (hub.own / "en_US-lessac-high.onnx").write_bytes(b"M" * 4999 + b"!")
    r = env.run("voices", "verify", "en")
    assert r.returncode == 1 and "differs from voices.lock" in r.stdout
    env.run("voices", "remove", "en")
    assert not (hub.own / "en_US-lessac-high.onnx").exists()
    assert "not installed" in env.run("voices").stdout.splitlines()[0]


def test_unknown_language_and_list(env, hub):
    assert env.run("voices", "add", "de").returncode == 2
    out = env.run("voices").stdout
    assert out.splitlines()[0].startswith("en  not installed  (2 files")
    assert "engine piper: PATH" in out


def test_tar_escape_is_refused(env, hub):
    bad = tgz({"../evil": b"x"})
    hub.put(f"datasets/org/ckpt/resolve/{REV}/zh/_resources/g2pw.tar.gz", bad)
    e = hub.entries[3] | {"size": len(bad), "sha256": hashlib.sha256(bad).hexdigest()}
    write_lock(Path(env.vars["SAMVADA_VOICE_LOCK"]), hub.entries[:3] + [e] + hub.entries[4:])
    r = env.run("voices", "add", "zh", "--engine", "never")
    assert r.returncode == 1 and "escapes" in r.stderr
    assert not (hub.own.parent / "evil").exists()


def test_managed_engine_is_preferred_over_path(env, hub, tmp_path):
    """An engine folder made for this Python runs as `python -m piper` with PYTHONPATH."""
    eng = tmp_path / "engines"
    mod = eng / "piper" / "piper"
    mod.mkdir(parents=True)
    (mod / "__init__.py").write_text("")
    (mod / "__main__.py").write_text(
        "import os, sys\n"
        "a = sys.argv; out = a[a.index('-f') + 1]\n"
        "open(out, 'wb').write(b'RIFF')\n"
        "open(os.environ['FAKE_LOG'], 'a').write('managed ' + a[a.index('-m') + 1] + '\\n')\n")
    (eng / "piper" / ".python").write_text(f"{sys.version_info.major}.{sys.version_info.minor}\n")
    env.run("voices", "add", "en", "--engine", "never")
    r = env.run("en", "hi", SAMVADA="on", SAMVADA_ENGINES=str(eng))
    assert r.returncode == 0, r.stderr
    assert env.lines()[0] == "managed en_US-lessac-high.onnx"
    assert "engine piper: samvada" in env.run("voices", SAMVADA_ENGINES=str(eng)).stdout


def test_save_writes_clips_and_skips_presence(env, tmp_path):
    out = tmp_path / "clips"
    r = env.run("speak", "--save", str(out), "en", "hi", "zh", "你好", FAKE_LOCKED="1")
    assert r.returncode == 0, r.stderr
    assert sorted(p.name for p in out.iterdir()) == ["1-en.wav", "2-zh.wav"]
    assert not [l for l in env.lines() if l.startswith("play")]


def test_a_narrow_console_does_not_crash_the_install(env, hub):
    """Windows pipes are often cp1252: no "←", no Chinese. Seen on the windows-2025 runner."""
    r = env.run("voices", "add", "en", "--engine", "never", PYTHONIOENCODING="cp1252")
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stderr and (hub.own / "en_US-lessac-high.onnx").is_file()
