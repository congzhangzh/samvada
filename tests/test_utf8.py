"""UTF-8 everywhere: Samvada never relies on the locale's encoding (cp936/GBK on a Chinese
Windows cannot hold the rule block's 🇺🇸, nor read a UTF-8 CLAUDE.md with Chinese in it).

Every child a test starts runs with -X warn_default_encoding and EncodingWarning as an error
(tests/conftest.py); here: that holds, and setup in a real GBK locale writes UTF-8."""

from __future__ import annotations

import subprocess
import sys

import pytest

from test_agents import claude  # noqa: F401  (the fake agent home)

OURS = "# 我的规则\n中文的说明，留着不动。\n"


def test_every_child_is_held_to_utf8(env, claude):
    r = env.run(cmd=[sys.executable, "-c", "import sys, warnings; print(sys.flags.warn_default_encoding)"])
    assert r.stdout.strip() == "1"
    r = env.run("setup", "--native", "zh", "--langs", "en,zh,ja,fr")
    assert r.returncode == 0 and "EncodingWarning" not in r.stderr, r.stderr
    out = env.tmp / "clips"
    r = env.run("speak", "--save", str(out), "en", "hi", "zh", "你好")
    assert r.returncode == 0 and "EncodingWarning" not in r.stderr, r.stderr
    assert sorted(p.name for p in out.iterdir()) == ["1-en.wav", "2-zh.wav"]


def _locale(prefer=("zh_CN.gbk", "zh_CN.gb18030", "en_US.iso885915", "en_US")) -> tuple[str, str]:
    """A locale whose encoding is not UTF-8 (and cannot hold an emoji), with its encoding."""
    for name in prefer:
        r = subprocess.run([sys.executable, "-c", "import locale; print(locale.getencoding())"],
                           env={"LC_ALL": name, "PYTHONUTF8": "0", "PATH": "/usr/bin:/bin"},
                           capture_output=True, text=True, encoding="utf-8")
        enc = r.stdout.strip()
        if r.returncode == 0 and enc and enc.lower().replace("-", "") not in ("utf8", "ascii",
                                                                              "ansix3.41968"):
            return name, enc
    pytest.skip("no non-UTF-8 locale installed here (zh_CN.gbk, en_US.iso885915)")


def test_setup_writes_utf8_in_a_gbk_locale(env, claude):
    name, enc = _locale()
    md = claude / "CLAUDE.md"
    md.write_bytes((md.read_text(encoding="utf-8") + OURS).encode("utf-8"))
    loc = {"LC_ALL": name, "LANG": name, "PYTHONUTF8": "0", "PYTHONIOENCODING": ""}
    r = env.run("setup", "--native", "zh", "--langs", "en,zh,ja,fr", **loc)
    assert r.returncode == 0, f"{name} ({enc}): {r.stderr}"
    raw = md.read_bytes()
    text = raw.decode("utf-8")  # strict: every byte is UTF-8
    assert "🇺🇸 EN".encode("utf-8") in raw and "🔤 IPA".encode("utf-8") in raw
    assert OURS in text  # the owner's own Chinese, read and written back unchanged
    skill = (claude / "skills/samvada/SKILL.md").read_bytes().decode("utf-8")
    assert skill.startswith("---\nname: samvada")
    r = env.run("setup", **loc)  # read back in the same locale: nothing to change
    assert r.returncode == 0 and "nothing to change" in r.stdout, r.stderr
    assert md.read_bytes() == raw


def test_a_pipe_gets_utf8_whatever_the_locale(env, claude):
    name, _ = _locale()
    r = subprocess.run([sys.executable, "-m", "samvada", "lang", "--list"],
                       env={**env.vars, "PYTHONPATH": env.vars.get("PYTHONPATH", "") or
                            str(__import__("conftest").ROOT / "src"),
                            "LC_ALL": name, "PYTHONUTF8": "0", "PYTHONIOENCODING": ""},
                       capture_output=True)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    r.stdout.decode("utf-8")  # strict: what an agent reads from the pipe is UTF-8
