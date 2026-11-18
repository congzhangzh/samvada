"""speak returns when its clips are made; a background player plays them in their turn,
one whole message at a time across sessions; --wait keeps the old blocking way."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def slow_player(env, seconds: float = 1.5) -> None:
    p = env.bin / "paplay"
    p.write_text(f'#!/bin/sh\necho "start $(basename "$1") $PPID $(date +%s.%N)" >> "$FAKE_LOG"\n'
                 f'sleep {seconds}\necho "end $(basename "$1") $PPID $(date +%s.%N)" >> "$FAKE_LOG"\n')
    p.chmod(0o755)


def wait_for(pred, seconds: float = 20) -> bool:
    end = time.time() + seconds
    while time.time() < end and not pred():
        time.sleep(0.05)
    return pred()


def log(env) -> list[str]:
    return env.log.read_text().splitlines() if env.log.exists() else []


def test_speak_returns_before_its_message_is_heard(env):
    slow_player(env)
    t0 = time.time()
    r = env.run("speak", "en", "a", "fr", "b", SAMVADA_WAIT="0")
    took = time.time() - t0
    assert r.returncode == 0, r.stderr
    assert took < 2.5, took                                    # two clips would take 3 s to hear
    assert wait_for(lambda: len([l for l in log(env) if l.startswith("end ")]) == 2)
    assert [l.split()[1] for l in log(env) if l.startswith("start ")] == ["1-en.wav", "2-fr.wav"]
    from samvada.warm import runtime_dir
    rt = runtime_dir(env.vars)
    assert wait_for(lambda: not list(rt.glob("msg.*")), 5)    # the clips are cleaned up


def test_wait_blocks_until_heard(env):
    slow_player(env, 1.0)
    t0 = time.time()
    r = env.run("speak", "--wait", "en", "a", SAMVADA_WAIT="0")
    assert r.returncode == 0 and time.time() - t0 >= 1.0
    assert [l.split()[0] for l in log(env)][-1] == "end"


def test_two_sessions_never_wait_and_are_heard_whole(env):
    slow_player(env, 1.0)
    e = {**env.vars, "PYTHONPATH": str(ROOT / "src"), "SAMVADA_WAIT": "0"}
    t0 = time.time()
    procs = [subprocess.Popen([sys.executable, "-m", "samvada", "speak", "en", "a", "fr", "b"], env=e)
             for _ in range(2)]
    assert [p.wait(60) for p in procs] == [0, 0]
    assert time.time() - t0 < 4, "a caller waited for the other's audio"   # 4 clips = 4 s heard
    assert wait_for(lambda: len([l for l in log(env) if l.startswith("end ")]) == 4)
    who = [l.split()[2] for l in log(env) if l.startswith("start ")]
    assert who[0] == who[1] and who[2] == who[3] and who[0] != who[2], who   # message by message


def test_nothing_to_play_leaves_nothing_behind(env):
    env.run("lang", "off", "en")
    r = env.run("speak", "en", "a", SAMVADA_WAIT="0")
    assert r.returncode == 0 and "silent" in r.stderr
    from samvada.warm import runtime_dir
    rt = runtime_dir(env.vars)
    assert not list(rt.glob("msg.*"))


def test_a_message_whose_turn_does_not_come_is_dropped(env):
    import fcntl, os
    from samvada.warm import runtime_dir
    rt = runtime_dir(env.vars)
    rt.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(rt / "play.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)          # another session is speaking and does not stop
    try:
        r = env.run("speak", "en", "late", SAMVADA_MAX_WAIT="1")      # --wait mode (conftest)
        assert r.returncode == 0 and "not played: its turn did not come within 1 s" in r.stderr
        assert not [l for l in log(env) if l.startswith(("play ", "start "))]
        r = env.run("speak", "en", "late", SAMVADA_MAX_WAIT="1", SAMVADA_WAIT="0")
        assert r.returncode == 0
        assert wait_for(lambda: "dropped, its turn did not come within 1 s" in
                        (rt / "player.log").read_text() if (rt / "player.log").exists() else False)
    finally:
        os.close(fd)
    assert not [l for l in log(env) if l.startswith(("play ", "start "))]   # never played late


def test_a_killed_callers_folder_is_swept(env):
    import os
    from samvada import playq
    from samvada.warm import runtime_dir
    rt = runtime_dir(env.vars)
    rt.mkdir(mode=0o700, parents=True, exist_ok=True)
    old, new = rt / "msg.old", rt / "msg.new"
    for q in (old, new):
        q.mkdir()
        (q / "1-en.wav").write_text("x")
    t = time.time() - playq.ORPHAN_AGE - 5
    os.utime(old, (t, t))
    playq.sweep(rt)
    assert not old.exists() and new.exists()
