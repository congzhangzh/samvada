from __future__ import annotations

import os
import time

FOUR = ["en", "hello", "zh", "你好", "ja", "こんにちは", "fr", "bonjour"]


def test_plays_in_order(env):
    r = env.run("speak", *FOUR, SAMVADA="on")
    assert r.returncode == 0, r.stderr
    assert env.played() == ["en", "zh", "ja", "fr"]


def test_bare_language_form_and_engines(env):
    r = env.run("ja", "こんにちは", "zh", "你好", SAMVADA="on")
    assert r.returncode == 0
    lines = env.lines()
    assert lines[0].startswith("piper-plus tsukuyomi")
    assert lines[2].startswith("piper zh_CN")
    assert f"cwd={env.voices}" in lines[0]  # engines run in the voices folder (g2pW)


def test_usage_errors(env):
    assert env.run().returncode == 2
    assert env.run("speak", "en").returncode == 2
    assert env.run("speak", "de", "hallo").returncode == 2
    assert env.run("lang", "off", "de").returncode == 2
    assert env.run("online", "maybe").returncode == 2
    assert env.run("--session", "speak", "en", "x").returncode == 2


def test_lang_off_skips_global_and_session(env):
    env.run("lang", "off", "ja", "fr")
    r = env.run("speak", *FOUR, SAMVADA="on")
    assert env.played() == ["en", "zh"]
    assert "skipped ja(global) fr(global)" in r.stderr
    env.run("--session", "lang", "on", "fr")  # the session wins over the global setting
    env.run("speak", *FOUR, SAMVADA="on")
    assert env.played() == ["en", "zh", "fr"]
    env.run("--session", "lang", "reset")
    env.run("speak", *FOUR, SAMVADA="on")
    assert env.played() == ["en", "zh"]


def test_all_off_is_silent_and_ok(env):
    env.run("lang", "off", "en")
    r = env.run("en", "hi", SAMVADA="on")
    assert r.returncode == 0 and "silent" in r.stderr and env.played() == []


def test_mute_either_scope_silences(env):
    env.run("mute", "watching TV")
    r = env.run("en", "hi")
    assert r.returncode == 0 and "muted: watching TV" in r.stderr and env.played() == []
    env.run("unmute")
    env.run("--session", "mute", "meeting")
    r = env.run("en", "hi")
    assert "muted for this session: meeting" in r.stderr and env.played() == []
    other = env.run("en", "hi", SAMVADA_SESSION="another")
    assert other.returncode == 0 and env.played() == ["en"]


def test_presence_lock_idle_phone(env):
    r = env.run("en", "hi", FAKE_LOCKED="1")
    assert "the screen is locked" in r.stderr and env.played() == []
    env.run("presence", "phone")  # the phone is newer than the desktop's last input
    r = env.run("en", "hi", FAKE_IDLE_MS="60000")
    assert "the phone was last" in r.stderr and env.played() == []
    phone = env.state / "samvada" / "presence" / "phone"
    old = time.time() - 120
    os.utime(phone, (old, old))  # typed at the desk after the phone: play again
    r = env.run("en", "hi", FAKE_IDLE_MS="5000")
    assert env.played() == ["en"]
    r = env.run("en", "hi", SAMVADA="off")  # forced silence wins over everything
    assert "SAMVADA=off" in r.stderr and env.played() == []


def test_legacy_phone_mark_is_read(env):
    (env.state / "speak").mkdir()
    (env.state / "speak" / "remote").write_text("")  # a chat bridge may still touch speak's mark
    r = env.run("en", "hi", FAKE_IDLE_MS="60000")
    assert "the phone was last" in r.stderr and env.played() == []


def test_no_desktop_trusts_a_recent_phone_message(env):
    env.run("presence", "phone")
    r = env.run("en", "hi", FAKE_IDLE_MS="")
    assert "no desktop idle time" in r.stderr and env.played() == []


def test_online_off_by_default_never_calls_edge(env):
    r = env.run("speak", "en", "hi", "zh", "你好", SAMVADA="on", FAKE_FAIL="piper:zh")
    assert r.returncode == 1
    lines = env.lines()
    assert not any(l.startswith("edge") for l in lines)
    assert ["play 1-en.wav"] == [l for l in lines if l.startswith("play")]
    assert "online fallback is off" in r.stderr and "samvada online fallback" in r.stderr


def test_online_fallback_retries_only_the_failed_language(env):
    env.run("online", "fallback")
    r = env.run("speak", "en", "hi", "zh", "你好", SAMVADA="on", FAKE_FAIL="piper:zh")
    assert r.returncode == 0, r.stderr
    lines = env.lines()
    assert "edge zh-CN-XiaoxiaoNeural" in lines
    assert [l for l in lines if l.startswith("play")] == ["play 1-en.wav", "play 2-zh.mp3"]
    assert "zh offline failed, used edge-tts" in r.stderr


def test_online_always_and_env_override(env):
    r = env.run("en", "hi", SAMVADA="on", SAMVADA_ONLINE="always")
    assert env.lines() == ["edge en-US-AvaMultilingualNeural", "play 1-en.mp3"]
    r = env.run("en", "hi", SAMVADA="on", SPEAK_ONLINE="1")  # the old switch still works
    assert env.lines()[0].startswith("edge")
    env.run("online", "always")
    r = env.run("en", "hi", SAMVADA="on", SAMVADA_ONLINE="off")  # one call can turn it off
    assert env.lines()[0].startswith("piper")


def test_failed_both_ways_exits_1(env):
    r = env.run("en", "hi", SAMVADA="on", SAMVADA_ONLINE="fallback", FAKE_FAIL="piper edge")
    assert r.returncode == 1 and "failed (offline and online)" in r.stderr


def test_missing_pack_skips_offline_speaks_online_when_allowed(env):
    (env.voices / "fr_FR-siwis-medium.onnx").unlink()
    r = env.run("en", "hi", "fr", "salut", SAMVADA="on")
    assert r.returncode == 0 and "fr(no voice pack)" in r.stderr and env.played() == ["en"]
    r = env.run("en", "hi", "fr", "salut", SAMVADA="on", SAMVADA_ONLINE="fallback")
    assert env.played() == ["en", "fr"] and "fr has no voice pack, used edge-tts" in r.stderr
    st = env.run("status")
    assert "fr on(default, no voice pack)" in st.stdout


def test_status_lines(env):
    st = env.run("status", FAKE_IDLE_MS="3000")
    out = st.stdout.splitlines()
    assert out[0] == "on at the desktop (idle 3s, gnome)"
    assert out[1].startswith("languages: en on(default)")
    assert out[2] == "online: off (default)"
    assert out[4] == "session: test-session"


def test_migration_copies_speak_state_once(env):
    legacy = env.state / "speak"
    (legacy / "sessions" / "test-session").mkdir(parents=True)
    (legacy / "langs").write_text("ja off\nfr off\n")
    (legacy / "sessions" / "test-session" / "langs").write_text("fr on\n")
    env.run("speak", *FOUR, SAMVADA="on")
    assert env.played() == ["en", "zh", "fr"]
    assert (legacy / "langs").exists()  # copied, never moved
    (legacy / "langs").write_text("en off\n")  # later changes to speak are not followed
    env.run("speak", *FOUR, SAMVADA="on")
    assert env.played() == ["en", "zh", "fr"]
