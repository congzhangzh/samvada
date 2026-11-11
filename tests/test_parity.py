"""The same cases through the bash `speak` wrapper and through Samvada.

Runs only where the wrapper is (point $SAMVADA_PARITY_SPEAK at it); the release tree has no
wrapper, so there it is skipped. Every case runs with the online mode set to `fallback`, the
behaviour `speak` always had; the default differs on purpose (DESIGN 11).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

SPEAK = os.environ.get("SAMVADA_PARITY_SPEAK", "")
pytestmark = pytest.mark.skipif(not (SPEAK and Path(SPEAK).is_file()),
                                reason="set SAMVADA_PARITY_SPEAK to the bash speak wrapper")

FOUR = ["en", "hello", "zh", "你好", "ja", "こんにちは", "fr", "bonjour"]

CASES = [
    # (setup commands, speak args, extra env)
    ([], FOUR, {"SPEAK": "on", "SAMVADA": "on"}),
    ([["lang", "off", "ja", "fr"]], FOUR, {"SPEAK": "on", "SAMVADA": "on"}),
    ([["lang", "off", "zh"], ["--session", "lang", "on", "zh"]], FOUR,
     {"SPEAK": "on", "SAMVADA": "on"}),
    ([["mute", "tv"]], FOUR, {}),
    ([["--session", "mute", "tv"]], FOUR, {}),
    ([], FOUR, {"FAKE_LOCKED": "1"}),
    ([], ["en", "hi"], {"SPEAK": "off", "SAMVADA": "off"}),
    ([], FOUR, {"SPEAK": "on", "SAMVADA": "on", "FAKE_FAIL": "piper:zh"}),
    ([], FOUR, {"SPEAK": "on", "SAMVADA": "on", "FAKE_FAIL": "piper:zh edge"}),
    ([], ["en", "hi", "zh"], {"SPEAK": "on", "SAMVADA": "on"}),
]


@pytest.mark.parametrize("setup,args,extra", CASES)
def test_same_as_speak(env, setup, args, extra):
    results = []
    for cmd in (["bash", SPEAK], None):
        for s in setup:
            env.run(*s, cmd=cmd)
        r = env.run(*args, cmd=cmd, SAMVADA_ONLINE="fallback", **extra)
        results.append((r.returncode, env.lines()))
        for s in setup:  # undo, so the second tool starts from the same state
            if s[-2:] == ["mute", "tv"]:
                env.run(*s[:-2], "unmute", cmd=cmd)
            elif "lang" in s:
                env.run(*s[: s.index("lang") + 1], "reset", cmd=cmd)
    (code_a, lines_a), (code_b, lines_b) = results
    assert code_a == code_b
    assert lines_a == lines_b
