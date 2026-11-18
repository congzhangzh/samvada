"""Warm engines: one load, many sentences; one server however many sessions start at once;
idle exit; the play lock keeps clips from overlapping."""

from __future__ import annotations

import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

FAKE_KOKORO = '''
import os, time
class Kokoro:
    def __init__(self, model, voices, vocab_config=None):
        time.sleep(0.5)                                   # loading takes a while
        with open(os.environ["FAKE_LOG"], "a") as f:
            f.write(f"load {os.getpid()}\\n")
    def create(self, text, voice, speed=1.0, lang="en-us", is_phonemes=False):
        with open(os.environ["FAKE_LOG"], "a") as f:
            f.write(f"say {os.getpid()} {text}\\n")
        class A(list):
            def clip(self, a, b): return self
            def __mul__(self, k): return self
            def astype(self, t): return self
            def tobytes(self): return b"\\0\\0" * 10
        return A([0.0] * 10), 24000
'''


@pytest.fixture
def warm(env):
    d = Path(env.vars["XDG_DATA_HOME"]) / "samvada" / "engines" / "kokoro" / "kokoro_onnx"
    d.mkdir(parents=True)
    (d / "__init__.py").write_text(FAKE_KOKORO)
    m = env.tmp / "k"
    m.mkdir()
    (m / "kokoro-v1.0.onnx").write_text("m")
    with zipfile.ZipFile(m / "voices-v1.0.bin", "w") as z:
        z.writestr("af_heart.npy", "x")
    cfg = env.tmp / ".config" / "samvada"
    cfg.mkdir(parents=True)
    (cfg / "voices.local.toml").write_text(
        f'[en]\nengine = "kokoro"\nmodel = "{m}/kokoro-v1.0.onnx"\nstyle = "af_heart"\n')
    env.vars |= {"SAMVADA_WARM": "on", "SAMVADA_WARM_IDLE": "20"}
    yield env
    env.run("engines", "stop")


def log(env) -> list[str]:
    return env.log.read_text().splitlines() if env.log.exists() else []


def test_one_load_many_sentences(warm):
    for t in ("one", "two", "three"):
        r = warm.run("speak", "en", t)
        assert r.returncode == 0, r.stderr
    lines = log(warm)
    loads = [l for l in lines if l.startswith("load ")]
    says = [l for l in lines if l.startswith("say ")]
    assert len(loads) == 1 and [l.split()[2] for l in says] == ["one", "two", "three"]
    assert {l.split()[1] for l in says} == {loads[0].split()[1]}      # all in the server
    r = warm.run("engines")
    assert "kokoro    en" in r.stdout and "served 3" in r.stdout


def test_parallel_sessions_start_one_server(warm):
    env = {**warm.vars, "PYTHONPATH": str(ROOT / "src")}
    procs = [subprocess.Popen([sys.executable, "-m", "samvada", "speak", "en", f"s{i}"], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(4)]
    codes = [p.wait(60) for p in procs]
    assert codes == [0, 0, 0, 0], [p.stderr.read() for p in procs]
    lines = log(warm)
    assert len([l for l in lines if l.startswith("load ")]) == 1
    assert sorted(l.split()[2] for l in lines if l.startswith("say ")) == ["s0", "s1", "s2", "s3"]


def test_idle_exit_and_stop(warm):
    warm.vars["SAMVADA_WARM_IDLE"] = "1"
    assert warm.run("speak", "en", "hi").returncode == 0
    time.sleep(2.5)
    assert "no warm engine running" in warm.run("engines").stdout
    warm.vars["SAMVADA_WARM_IDLE"] = "30"
    warm.run("speak", "en", "again")
    assert "stopped 1 warm engine(s)" in warm.run("engines", "stop").stdout
    assert len([l for l in log(warm) if l.startswith("load ")]) == 2


def test_a_broken_engine_falls_back_to_one_process(warm):
    d = Path(warm.vars["XDG_DATA_HOME"]) / "samvada" / "engines" / "kokoro" / "kokoro_onnx"
    (d / "__init__.py").write_text(FAKE_KOKORO.replace("time.sleep(0.5)", "time.sleep(0.5)\n"
                                   "        if os.environ.get('SAMVADA_WARM_TEST_BREAK'): raise SystemExit(3)"))
    r = warm.run("speak", "en", "hi", SAMVADA_WARM_TEST_BREAK="1", SAMVADA_DEBUG="1")
    # the server dies while loading; the one-process path loads too and also dies: exit 1, not a hang
    assert r.returncode == 1 and "did not start" in r.stderr
    r = warm.run("speak", "en", "hi")
    assert r.returncode == 0


def test_the_play_lock_keeps_clips_apart(env):
    slow = env.bin / "paplay"
    slow.write_text('#!/bin/sh\necho "start $(date +%s.%N)" >> "$FAKE_LOG"\nsleep 0.4\n'
                    'echo "end $(date +%s.%N)" >> "$FAKE_LOG"\n')
    slow.chmod(0o755)
    e = {**env.vars, "PYTHONPATH": str(ROOT / "src")}
    procs = [subprocess.Popen([sys.executable, "-m", "samvada", "speak", "en", f"x{i}"], env=e)
             for i in range(3)]
    assert [p.wait(60) for p in procs] == [0, 0, 0]
    marks = [(l.split()[0], float(l.split()[1])) for l in env.log.read_text().splitlines()
             if l.startswith(("start ", "end "))]
    marks.sort(key=lambda m: m[1])
    assert [k for k, _ in marks] == ["start", "end"] * 3        # never two at once


def test_a_client_that_gives_up_never_ends_the_server(warm):
    import socket as so
    assert warm.run("speak", "en", "one").returncode == 0
    from samvada import warm as w
    d = w.runtime_dir({**warm.vars})
    sock = next(d.glob("*.sock"))
    for _ in range(3):  # connect and leave at once, and connect and send half a line
        c = so.socket(so.AF_UNIX, so.SOCK_STREAM); c.connect(str(sock)); c.close()
        c = so.socket(so.AF_UNIX, so.SOCK_STREAM); c.connect(str(sock)); c.sendall(b'{"te'); c.close()
    assert warm.run("speak", "en", "two").returncode == 0
    assert len([l for l in log(warm) if l.startswith("load ")]) == 1   # the same server


def test_a_slow_load_never_starts_a_second_server(warm):
    d = Path(warm.vars["XDG_DATA_HOME"]) / "samvada" / "engines" / "kokoro" / "kokoro_onnx"
    (d / "__init__.py").write_text(FAKE_KOKORO.replace("time.sleep(0.5)", "time.sleep(3)"))
    env = {**warm.vars, "PYTHONPATH": str(ROOT / "src"), "SAMVADA_WARM_START_WAIT": "0.5"}
    procs = [subprocess.Popen([sys.executable, "-m", "samvada", "speak", "en", f"s{i}"], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(3)]
    assert [p.wait(90) for p in procs] == [0, 0, 0]
    time.sleep(3.5)  # the one server finishes loading
    servers = [l for l in warm.run("engines").stdout.splitlines() if l.startswith("kokoro")]
    assert len(servers) == 1, servers


def test_a_server_that_grew_steps_down(warm):
    warm.vars["SAMVADA_WARM_GROWTH_MB"] = "-1"   # any size counts as grown
    assert warm.run("speak", "en", "one").returncode == 0
    time.sleep(0.5)
    assert warm.run("speak", "en", "two").returncode == 0
    assert len([l for l in log(warm) if l.startswith("load ")]) == 2   # a fresh one


def test_the_runtime_dir_must_be_the_owners_own(tmp_path):
    from samvada import warm as w
    d = tmp_path / "rt"
    d.mkdir(mode=0o777)
    d.chmod(0o777)
    assert w._own_private_dir(d) and (d.stat().st_mode & 0o777) == 0o700  # tightened
    link = tmp_path / "link"
    link.symlink_to(d)
    assert not w._own_private_dir(link)
    assert w.running({"SAMVADA_WARM": "off"}) == [] and w.stop_all({"SAMVADA_WARM": "off"}) == 0


def test_two_sessions_are_heard_one_whole_message_after_the_other(env):
    slow = env.bin / "paplay"
    slow.write_text('#!/bin/sh\necho "play $(basename "$1") $PPID" >> "$FAKE_LOG"\nsleep 0.3\n')
    slow.chmod(0o755)
    e = {**env.vars, "PYTHONPATH": str(ROOT / "src")}
    procs = [subprocess.Popen([sys.executable, "-m", "samvada", "speak", "en", "a", "fr", "b"],
                              env=e) for _ in range(2)]
    assert [p.wait(60) for p in procs] == [0, 0]
    who = [l.split()[2] for l in env.log.read_text().splitlines() if l.startswith("play ")]
    assert len(who) == 4 and who[0] == who[1] and who[2] == who[3] and who[0] != who[2], who
