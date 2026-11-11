"""Where the owner is, so the voice plays only there (DESIGN 5).

First match wins:
  1. SAMVADA=on|off (or SPEAK=on|off) on the call;
  2. a mute, global or for this session;
  3. the screen is locked;
  4. the newer of the desktop's last input and the owner's last phone message: the phone
     means silence;
  5. no desktop idle time at all: a phone message in the last 30 minutes means silence.

The desktop signals come from backends. Each gives `locked(env)` (True, False, or None:
can't tell here) and `idle(env)` (seconds since the last keyboard or mouse input, or None).
For each question the first backend that answers wins, and the decision names it
("at the desktop (idle 3s, gnome)").
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .state import State

PHONE_RECENT = 1800  # seconds


@dataclass
class Decision:
    on: bool
    why: str

    def __str__(self) -> str:
        return ("on " if self.on else "off ") + self.why


def _run(argv: list[str], env: Mapping[str, str]) -> str | None:
    """A helper's stdout, or None when it is missing, fails or hangs."""
    try:
        r = subprocess.run(argv, env=dict(env), capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


class Backend:
    """A source of desktop signals. None means "can't tell here": the next backend is asked."""

    name = "?"

    def locked(self, env: Mapping[str, str]) -> bool | None:
        return None

    def idle(self, env: Mapping[str, str]) -> int | None:
        return None


# -- gnome: Mutter's idle monitor and the ScreenSaver lock, over the session bus ----------

def _gdbus(env: Mapping[str, str], *args: str) -> str | None:
    """Call the desktop session's bus, also from screen, ssh or an agent's daemon."""
    run_env = dict(env)
    if not run_env.get("DBUS_SESSION_BUS_ADDRESS"):
        runtime = run_env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        run_env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={runtime}/bus"
    return _run(["gdbus", "call", "--session", *args], run_env)


def _unix_desktop() -> bool:
    return os.name == "posix" and sys.platform != "darwin"


class Gnome(Backend):
    name = "gnome"

    def locked(self, env: Mapping[str, str]) -> bool | None:
        if not _unix_desktop():
            return None
        out = _gdbus(env, "--dest", "org.gnome.ScreenSaver", "--object-path",
                     "/org/gnome/ScreenSaver", "--method", "org.gnome.ScreenSaver.GetActive")
        return None if out is None else out == "(true,)"

    def idle(self, env: Mapping[str, str]) -> int | None:
        if not _unix_desktop():
            return None
        out = _gdbus(env, "--dest", "org.gnome.Mutter.IdleMonitor", "--object-path",
                     "/org/gnome/Mutter/IdleMonitor/Core",
                     "--method", "org.gnome.Mutter.IdleMonitor.GetIdletime")
        m = re.fullmatch(r"\(uint64 (\d+),\)", out or "")
        return int(m.group(1)) // 1000 if m else None


# -- x11: the XScreenSaver extension through ctypes (libX11 + libXss) -----------------------

def local_display(display: str) -> bool:
    """":0", ":1.0", "unix:0" are this machine's screen. "localhost:10.0" (ssh -X) or
    "host:0" is another machine's: its idle time says nothing about this room."""
    host, sep, _ = display.rpartition(":")
    return bool(sep) and host in ("", "unix")


def _x11_idle_ms(display: str) -> int | None:
    import ctypes
    import ctypes.util

    x11_name, xss_name = ctypes.util.find_library("X11"), ctypes.util.find_library("Xss")
    if not (x11_name and xss_name):
        return None
    try:
        x11, xss = ctypes.CDLL(x11_name), ctypes.CDLL(xss_name)
    except OSError:
        return None

    class Info(ctypes.Structure):  # XScreenSaverInfo, X11/extensions/scrnsaver.h
        _fields_ = [("window", ctypes.c_ulong), ("state", ctypes.c_int),
                    ("kind", ctypes.c_int), ("til_or_since", ctypes.c_ulong),
                    ("idle", ctypes.c_ulong), ("eventMask", ctypes.c_ulong)]

    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XDefaultRootWindow.restype = ctypes.c_ulong
    x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    x11.XFree.argtypes = [ctypes.c_void_p]
    xss.XScreenSaverQueryExtension.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int),
                                               ctypes.POINTER(ctypes.c_int)]
    xss.XScreenSaverAllocInfo.restype = ctypes.POINTER(Info)
    xss.XScreenSaverQueryInfo.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(Info)]

    dpy = x11.XOpenDisplay(display.encode())
    if not dpy:
        return None
    try:
        event, error = ctypes.c_int(), ctypes.c_int()
        if not xss.XScreenSaverQueryExtension(dpy, ctypes.byref(event), ctypes.byref(error)):
            return None
        info = xss.XScreenSaverAllocInfo()
        if not info:
            return None
        try:
            if not xss.XScreenSaverQueryInfo(dpy, x11.XDefaultRootWindow(dpy), info):
                return None
            return int(info.contents.idle)
        finally:
            x11.XFree(info)
    finally:
        x11.XCloseDisplay(dpy)


class X11(Backend):
    """Other Linux desktops, when $DISPLAY is this machine's. No lock: X has no common way
    to ask for one."""

    name = "x11"

    def idle(self, env: Mapping[str, str]) -> int | None:
        display = env.get("DISPLAY", "")
        if not _unix_desktop() or not local_display(display):
            return None
        ms = _x11_idle_ms(display)
        return None if ms is None else ms // 1000


# -- macos: IOKit's HIDIdleTime, and the console session's lock flag, through ioreg --------

def parse_hid_idle(text: str | None) -> int | None:
    """`ioreg -c IOHIDSystem` → seconds. HIDIdleTime is in nanoseconds."""
    m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', text or "")
    return int(m.group(1)) // 1_000_000_000 if m else None


def parse_mac_locked(text: str | None) -> bool | None:
    """`ioreg -n Root -d1` → True when the console session says it is locked. The flag is
    left out when the screen is not locked, so its absence is not taken as an answer."""
    return True if re.search(r'"CGSSessionScreenIsLocked"\s*=\s*Yes', text or "") else None


class MacOS(Backend):
    name = "macos"

    def locked(self, env: Mapping[str, str]) -> bool | None:
        if sys.platform != "darwin":
            return None
        return parse_mac_locked(_run(["ioreg", "-n", "Root", "-d1"], env))

    def idle(self, env: Mapping[str, str]) -> int | None:
        if sys.platform != "darwin":
            return None
        return parse_hid_idle(_run(["ioreg", "-c", "IOHIDSystem"], env))


# -- windows: GetLastInputInfo, and the name of the input desktop -------------------------

def win_idle_ms(now_tick64: int, last_input32: int) -> int:
    """GetLastInputInfo's dwTime is a 32-bit tick count; it wraps after 49.7 days."""
    return ((now_tick64 & 0xFFFFFFFF) - last_input32) & 0xFFFFFFFF


class Windows(Backend):
    name = "windows"

    def idle(self, env: Mapping[str, str]) -> int | None:
        if sys.platform != "win32":
            return None
        import ctypes
        from ctypes import wintypes

        class LastInputInfo(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        info = LastInputInfo(cbSize=ctypes.sizeof(LastInputInfo))
        try:
            if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
                return None
            kernel32 = ctypes.windll.kernel32
            kernel32.GetTickCount64.restype = ctypes.c_ulonglong
            return win_idle_ms(kernel32.GetTickCount64(), info.dwTime) // 1000
        except (AttributeError, OSError):
            return None

    def locked(self, env: Mapping[str, str]) -> bool | None:
        """Best effort: while locked, the input desktop is Winlogon's, not "Default". When
        it can't be opened (a service or an ssh session, or a lock that refuses access),
        can't tell."""
        if sys.platform != "win32":
            return None
        import ctypes
        from ctypes import wintypes

        try:
            user32 = ctypes.windll.user32
            user32.OpenInputDesktop.restype = wintypes.HANDLE
            desk = user32.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
            if not desk:
                return None
            try:
                name = ctypes.create_unicode_buffer(256)
                need = wintypes.DWORD()
                if not user32.GetUserObjectInformationW(desk, 2, name, ctypes.sizeof(name),
                                                        ctypes.byref(need)):  # 2: UOI_NAME
                    return None
                return name.value.lower() != "default"
            finally:
                user32.CloseDesktop(desk)
        except (AttributeError, OSError):
            return None


# -- pys: the terminal multiplexer's attached client --------------------------------------

class Pys(Backend):
    """The case the desktop can't see: the owner types into a pys window over ssh from
    another machine; this desktop's idle counter never moves, and the voice would play to
    an empty room.

    Not answerable today. A program in a pys window ($PYS_WINDOW) can ask the server
    (`pys ls --json`, read-only) only how many clients are attached and their terminal
    sizes. The server keeps when each client last attached, resized or typed, but does not
    report it, and a client's attach message does not say whether its terminal is local or
    an ssh session. What pys would need to expose, read-only, e.g. in `pys ls --json`:

        "client_info": [{"typed_ago": <seconds since this client's last key>,
                         "remote": <true when the client's terminal is an ssh session>}]

    (remote: the client would send bool($SSH_CONNECTION or $SSH_TTY) in its attach message.)
    With that, a remote client that typed more recently than the desktop's last input would
    mean "the owner is elsewhere", as a phone message does, and a local client's last key
    would be desktop input. Until then this backend answers None to both questions.
    """

    name = "pys"


BACKENDS: tuple[Backend, ...] = (Gnome(), X11(), MacOS(), Windows(), Pys())


def _first(backends: Sequence[Backend], ask: str, env: Mapping[str, str]):
    """(answer, backend name) from the first backend that answers; (None, "") if none does."""
    for b in backends:
        answer = getattr(b, ask)(env)
        if answer is not None:
            return answer, b.name
    return None, ""


def last_phone(paths: list[Path]) -> int:
    """The newest phone mark: Samvada's own, and the `speak` wrapper's, which a chat bridge
    may still touch."""
    newest = 0
    for p in paths:
        try:
            newest = max(newest, int(p.stat().st_mtime))
        except OSError:
            pass
    return newest


def where(state: State, phone_marks: list[Path], env: Mapping[str, str] = os.environ,
          now: int | None = None, backends: Sequence[Backend] = BACKENDS) -> Decision:
    forced = env.get("SAMVADA") or env.get("SPEAK")
    if forced in ("on", "off"):
        var = "SAMVADA" if env.get("SAMVADA") else "SPEAK"
        return Decision(forced == "on", f"{var}={forced}")
    muted = state.muted()
    if muted:
        scope, why = muted
        return Decision(False, ("muted: " if scope == "global" else "muted for this session: ") + why)
    locked, by = _first(backends, "locked", env)
    if locked:
        return Decision(False, f"the screen is locked ({by})")
    now = int(time.time()) if now is None else now
    phone = last_phone(phone_marks)
    idle, by = _first(backends, "idle", env)
    if idle is not None:
        if phone > now - idle:
            return Decision(False, f"the phone was last ({now - phone}s ago; "
                                   f"desktop idle {idle}s, {by})")
        return Decision(True, f"at the desktop (idle {idle}s, {by})")
    if now - phone < PHONE_RECENT:
        return Decision(False, f"the phone was last ({now - phone}s ago; no desktop idle time)")
    return Decision(True, "no desktop idle time, no recent phone message")
