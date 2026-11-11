"""The presence backends: the order of the decision, and each backend's parsing.

macOS and Windows can't run here: their parsing and arithmetic are tested with fakes; the
calls into IOKit and user32 are not verified on real hardware by these tests.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

from samvada import presence
from samvada.presence import Backend, where
from samvada.state import State


class Fake(Backend):
    def __init__(self, name, locked=None, idle=None):
        self.name, self._locked, self._idle = name, locked, idle
        self.asked: list[str] = []

    def locked(self, env):
        self.asked.append("locked")
        return self._locked

    def idle(self, env):
        self.asked.append("idle")
        return self._idle


@pytest.fixture
def state(tmp_path):
    return State(tmp_path / "state", "s")


def test_first_backend_that_answers_wins(state):
    a, b, c = Fake("a"), Fake("b", locked=False, idle=7), Fake("c", locked=True, idle=1)
    d = where(state, [], env={}, now=1000, backends=[a, b, c])
    assert d.on and d.why == "at the desktop (idle 7s, b)"
    assert c.asked == []  # never asked once b answered


def test_locked_names_its_backend(state):
    d = where(state, [], env={}, backends=[Fake("a"), Fake("b", locked=True, idle=1)])
    assert not d.on and d.why == "the screen is locked (b)"


def test_unlocked_answer_stops_the_lock_question(state):
    # gnome says "not locked": a later backend's "locked" is not consulted
    d = where(state, [], env={}, now=1000,
              backends=[Fake("g", locked=False), Fake("x", locked=True, idle=5)])
    assert d.on and d.why == "at the desktop (idle 5s, x)"


def test_order_forced_and_mute_before_backends(state):
    b = Fake("b", locked=True, idle=1)
    assert where(state, [], env={"SAMVADA": "on"}, backends=[b]).on
    state.mute(False, "tv")
    d = where(state, [], env={}, backends=[b])
    assert d.why == "muted: tv" and b.asked == []


def test_phone_newer_than_desktop_input(state, tmp_path):
    mark = tmp_path / "phone"
    mark.write_text("")
    os.utime(mark, (990, 990))
    d = where(state, [mark], env={}, now=1000, backends=[Fake("b", idle=60)])
    assert not d.on and d.why == "the phone was last (10s ago; desktop idle 60s, b)"
    d = where(state, [mark], env={}, now=1000, backends=[Fake("b", idle=5)])
    assert d.on


def test_no_backend_answers_falls_to_the_phone(state, tmp_path):
    mark = tmp_path / "phone"
    mark.write_text("")
    now = int(time.time())
    none = [Fake("a"), presence.Pys()]
    d = where(state, [mark], env={}, now=now, backends=none)
    assert not d.on and "no desktop idle time" in d.why
    d = where(state, [], env={}, now=now, backends=none)
    assert d.on and d.why == "no desktop idle time, no recent phone message"


def test_default_backends_order():
    assert [b.name for b in presence.BACKENDS] == ["gnome", "x11", "macos", "windows", "pys"]


# -- x11 --------------------------------------------------------------------------------

@pytest.mark.parametrize("display,local", [
    (":0", True), (":1.0", True), ("unix:0", True),
    ("localhost:10.0", False), ("host:0", False), ("", False), ("garbage", False)])
def test_x11_only_a_local_display(display, local):
    assert presence.local_display(display) is local


def test_x11_needs_display(monkeypatch):
    called = []
    monkeypatch.setattr(presence, "_x11_idle_ms", lambda d: called.append(d) or 4200)
    x = presence.X11()
    assert x.idle({}) is None and called == []
    assert x.idle({"DISPLAY": "localhost:10.0"}) is None and called == []  # ssh -X
    if presence._unix_desktop():
        assert x.idle({"DISPLAY": ":0"}) == 4 and called == [":0"]
    assert x.locked({"DISPLAY": ":0"}) is None


def test_x11_no_server_is_none():
    # a display number nobody serves: XOpenDisplay fails, no crash, no answer
    if not presence._unix_desktop():
        pytest.skip("X11 is a Unix backend")
    assert presence._x11_idle_ms(":987") is None


# -- macos ------------------------------------------------------------------------------

IOREG_HID = """\
+-o IOHIDSystem  <class IOHIDSystem, id 0x100000215, registered, matched, active, busy 0>
    {
      "HIDIdleTime" = 12345678901
      "HIDParameters" = {"HIDClickTime"=500000000}
    }
"""

IOREG_ROOT_LOCKED = """\
+-o Root  <class IORegistryEntry, id 0x100000100, retain 26>
    {
      "IOConsoleUsers" = ({"kCGSSessionOnConsoleKey"=Yes,"CGSSessionScreenIsLocked"=Yes,"kCGSSessionUserNameKey"="owner"})
    }
"""


def test_macos_parsing():
    assert presence.parse_hid_idle(IOREG_HID) == 12
    assert presence.parse_hid_idle("") is None and presence.parse_hid_idle(None) is None
    assert presence.parse_mac_locked(IOREG_ROOT_LOCKED) is True
    assert presence.parse_mac_locked(IOREG_ROOT_LOCKED.replace(
        ',"CGSSessionScreenIsLocked"=Yes', "")) is None  # not locked, or can't tell


def test_macos_backend_through_a_fake_ioreg(monkeypatch, tmp_path):
    fake = tmp_path / "ioreg"
    (tmp_path / "hid").write_text(IOREG_HID)
    (tmp_path / "root").write_text(IOREG_ROOT_LOCKED)
    fake.write_text(f'#!/bin/sh\ncase "$*" in *IOHIDSystem*) cat {tmp_path}/hid ;; '
                    f'*Root*) cat {tmp_path}/root ;; esac\n')
    fake.chmod(0o755)
    env = {"PATH": f"{tmp_path}:/usr/bin:/bin"}
    m = presence.MacOS()
    if sys.platform != "darwin":
        assert m.idle(env) is None and m.locked(env) is None  # not asked off macOS
    monkeypatch.setattr(sys, "platform", "darwin")
    assert m.idle(env) == 12
    assert m.locked(env) is True
    assert m.idle({"PATH": str(tmp_path / "nowhere")}) is None  # no ioreg


# -- windows ----------------------------------------------------------------------------

def test_windows_idle_arithmetic_and_wrap():
    assert presence.win_idle_ms(10_000, 7_000) == 3_000
    # the 64-bit tick passed 2**32: the 32-bit dwTime wrapped before it
    assert presence.win_idle_ms(2**32 + 500, 2**32 - 1500) == 2_000
    assert presence.win_idle_ms(5 * 2**32 + 100, 50) == 50


@pytest.mark.skipif(sys.platform == "win32", reason="checks the off-Windows path")
def test_windows_backend_off_windows():
    w = presence.Windows()
    assert w.idle({}) is None and w.locked({}) is None


# -- pys --------------------------------------------------------------------------------

def test_pys_does_not_answer_yet():
    p = presence.Pys()
    env = {"PYS_WINDOW": "3"}
    assert p.idle(env) is None and p.locked(env) is None
