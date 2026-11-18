"""The background player: one message's clips, played in their turn, while the caller goes on.

`samvada speak` makes its clips into a queue folder and starts this player
(`python -m samvada.playq <folder>`) as soon as the first clip is ready. The player waits for
the play turn (`play.lock`, flock: one whole message at a time across every session), plays
the clips in order as they appear (the caller is still making the later ones), and removes
the folder when the message is done. So a caller never waits for another session's audio;
`--wait` or `$SAMVADA_WAIT=1` keeps the old way (the caller waits for its message to finish).

The queue folder holds the clips and two small files:
  list   one clip name per line, in order, appended when a clip is complete
  done   written when the caller has made its last clip (the list is then complete)
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from collections.abc import Mapping
from pathlib import Path

from . import player

MAX_WAIT = 120     # seconds a message may wait for its turn; later it is dropped, never played
                   # late or over another
CLIP_WAIT = 60     # seconds to wait for the caller's next clip (a killed caller sends none)
ORPHAN_AGE = 600   # a message folder this old with no player is a killed caller's: swept


class Turn:
    """The play lock, held from a message's first clip to its last. `take` returns False when
    the turn did not come within `wait` seconds: the message is then dropped."""

    def __init__(self, env: Mapping[str, str], wait: float):
        self.env, self.wait, self.fd, self.free = env, wait, None, False

    def take(self) -> bool:
        if self.fd is not None or self.free:
            return True
        try:
            import fcntl
            from .warm import open_lock, runtime_dir
            d = runtime_dir(self.env)
            if d is None:
                self.free = True
                return True
            fd = open_lock(d / "play.lock")
        except (ImportError, OSError):
            self.free = True  # Windows, or no safe folder for the lock: play as before
            return True
        end = time.time() + self.wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.fd = fd
                return True
            except OSError:
                if time.time() > end:  # too old by now: drop it rather than play it late
                    os.close(fd)
                    return False
                time.sleep(0.05)

    def done(self) -> None:
        if self.fd is not None:
            os.close(self.fd)  # closing drops the lock
            self.fd = None


def mark_ready(qdir: Path, clip: Path) -> None:
    """The caller: a clip is complete; add it to the list."""
    with open(qdir / "list", "a", encoding="utf-8") as f:
        f.write(clip.name + "\n")


def mark_done(qdir: Path) -> None:
    (qdir / "done").write_text("", encoding="utf-8")


def max_wait(env: Mapping[str, str]) -> float:
    try:
        return float(env.get("SAMVADA_MAX_WAIT", MAX_WAIT))
    except ValueError:
        return MAX_WAIT


def sweep(d: Path, now: float | None = None) -> None:
    """Remove message folders a killed caller left behind (old, and no `done`/player)."""
    now = time.time() if now is None else now
    for q in d.glob("msg.*"):
        try:
            if now - q.stat().st_mtime > ORPHAN_AGE:
                shutil.rmtree(q, ignore_errors=True)
        except OSError:
            pass


def play_queue(qdir: Path, env: Mapping[str, str], turn_wait: float | None = None,
               log=lambda m: print(f"samvada: {m}", file=sys.stderr)) -> int:
    """Play the folder's clips in order in one turn: 0; 1 when a clip could not be played;
    3 when the turn did not come in time and the message was dropped."""
    wait = max_wait(env) if turn_wait is None else turn_wait
    status, n, turn = 0, 0, Turn(env, wait)
    try:
        last = time.time()
        while True:
            names = (qdir / "list").read_text(encoding="utf-8").split() if (qdir / "list").exists() else []
            if n < len(names):
                if not turn.take():  # the first clip waits for the turn; the rest are in it
                    log(f"{qdir.name}: dropped, its turn did not come within {wait:.0f} s")
                    return 3
                clip = qdir / names[n]
                n += 1
                if not player.play(clip):
                    log(f"{clip.name}: no player could play it (tried: {', '.join(player.PLAYERS)})")
                    status = 1
                last = time.time()
            elif (qdir / "done").exists() and n >= len(names):
                return status
            elif time.time() - last > CLIP_WAIT:
                log(f"no next clip after {CLIP_WAIT} s; the message ends here")
                return 1
            else:
                time.sleep(0.05)
    finally:
        turn.done()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m samvada.playq <queue folder>", file=sys.stderr)
        return 2
    qdir = Path(args[0])
    try:
        return play_queue(qdir, os.environ)
    finally:
        shutil.rmtree(qdir, ignore_errors=True)  # the message's temporary clips


if __name__ == "__main__":
    sys.exit(main())
