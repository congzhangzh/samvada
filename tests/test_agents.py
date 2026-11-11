"""The agent layer: setup (rule, skill, hooks) and the hooks themselves, in a fake home."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def claude(env, tmp_path):
    d = tmp_path / "claude"
    d.mkdir()
    (d / "CLAUDE.md").write_text("# My rules\n\nKeep this.\n")
    (d / "settings.json").write_text(json.dumps({
        "model": "x",
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other-tool hook"}]}]},
    }, indent=1) + "\n")
    env.vars |= {"CLAUDE_CONFIG_DIR": str(d), "XDG_CONFIG_HOME": str(tmp_path / "config")}
    return d


def test_dry_run_changes_nothing(env, claude):
    before = {p.name: p.read_text() for p in claude.iterdir()}
    r = env.run("setup", "--dry-run")
    assert r.returncode == 0 and "dry run: nothing changed" in r.stdout
    assert "add the block to" in r.stdout and "install" in r.stdout
    assert {p.name: p.read_text() for p in claude.iterdir()} == before


def test_setup_installs_rule_skill_hooks_and_keeps_the_rest(env, claude):
    r = env.run("setup", "--native", "zh", "--langs", "en,zh,ja,fr")
    assert r.returncode == 0, r.stderr
    md = (claude / "CLAUDE.md").read_text()
    assert md.startswith("# My rules\n\nKeep this.\n")
    assert "samvada:begin" in md and "samvada:end" in md
    assert "native language is Chinese" in md and "English, Japanese, French" in md
    assert "🇺🇸 EN, 🔤 IPA, 🇨🇳 ZH" in md
    assert (claude / "skills/samvada/SKILL.md").read_text().startswith("---\nname: samvada")
    s = json.loads((claude / "settings.json").read_text())
    assert s["model"] == "x"
    stop = [h["command"] for e in s["hooks"]["Stop"] for h in e["hooks"]]
    assert stop[0] == "other-tool hook" and stop[1].endswith("-m samvada hook stop")
    assert s["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"].endswith("hook prompt")
    assert (claude / "settings.json").read_text().startswith('{\n "model"')  # indent kept
    assert list(claude.glob("CLAUDE.md.samvada-backup-*")) and list(claude.glob("settings.json.samvada-backup-*"))
    # the settings are remembered, and a second run changes nothing
    r = env.run("setup")
    assert "nothing to change" in r.stdout and md == (claude / "CLAUDE.md").read_text()
    assert len(json.loads((claude / "settings.json").read_text())["hooks"]["Stop"]) == 2


def test_setup_updates_the_block_in_place(env, claude):
    env.run("setup", "--langs", "en,zh")
    (claude / "CLAUDE.md").write_text((claude / "CLAUDE.md").read_text() + "\n## Later section\n")
    env.run("setup", "--langs", "en,zh,fr", "--no-ipa")
    md = (claude / "CLAUDE.md").read_text()
    assert md.count("samvada:begin") == 1 and "## Later section" in md and "🔤 IPA" not in md and "French" in md


def test_uninstall_removes_exactly_what_it_added(env, claude):
    before_md = (claude / "CLAUDE.md").read_text()
    before_settings = json.loads((claude / "settings.json").read_text())
    env.run("setup")
    r = env.run("setup", "--uninstall")
    assert r.returncode == 0, r.stderr
    assert (claude / "CLAUDE.md").read_text() == before_md
    assert json.loads((claude / "settings.json").read_text()) == before_settings
    assert not (claude / "skills/samvada").exists()


def test_a_skill_folder_that_is_not_ours_is_left_alone(env, claude):
    mine = claude / "skills/samvada"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("my own")
    r = env.run("setup")
    assert "is not Samvada's: left alone" in r.stdout and (mine / "SKILL.md").read_text() == "my own"
    env.run("setup", "--uninstall")
    assert (mine / "SKILL.md").read_text() == "my own"


def test_codex_gets_agents_md_and_skill_no_hooks(env, tmp_path):
    codex = tmp_path / "codex"
    env.vars |= {"CODEX_HOME": str(codex), "XDG_CONFIG_HOME": str(tmp_path / "config")}
    r = env.run("setup", "--agent", "codex")
    assert r.returncode == 0, r.stderr
    assert "samvada:begin" in (codex / "AGENTS.md").read_text()
    assert (codex / "skills/samvada/SKILL.md").exists() and not (codex / "settings.json").exists()


def test_bad_arguments(env, claude):
    assert env.run("setup", "--langs", "en,de").returncode == 2
    assert env.run("setup", "--agent", "vim").returncode == 2
    assert env.run("hook", "nope").returncode == 2


# -- hooks ----------------------------------------------------------------------------------
def hook(env, kind, event, **extra):
    import subprocess, sys
    return subprocess.run([sys.executable, "-m", "samvada", "hook", kind], input=json.dumps(event),
                          env={**env.vars, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"), **extra},
                          capture_output=True, text=True)


SID = "abc-123"


def test_stop_asks_once_for_the_closing_when_nothing_was_spoken(env):
    assert hook(env, "prompt", {"session_id": SID, "hook_event_name": "UserPromptSubmit"}).stdout == ""
    r = hook(env, "stop", {"session_id": SID, "hook_event_name": "Stop"})
    out = json.loads(r.stdout)
    assert out["decision"] == "block" and "samvada speak" in out["reason"]
    # asked once per turn, and never when the harness says the hook already continued
    assert hook(env, "stop", {"session_id": SID}).stdout == ""
    hook(env, "prompt", {"session_id": SID})
    assert hook(env, "stop", {"session_id": SID, "stop_hook_active": True}).stdout == ""


def test_stop_is_quiet_after_speaking(env):
    hook(env, "prompt", {"session_id": SID})
    r = env.run("en", "hi", SAMVADA="on", CLAUDE_CODE_SESSION_ID=SID, SAMVADA_SESSION="")
    assert r.returncode == 0, r.stderr
    assert hook(env, "stop", {"session_id": SID}).stdout == ""


def test_voice_off_adds_context_and_stop_does_not_insist(env):
    env.run("mute", "watching TV")
    r = hook(env, "prompt", {"session_id": SID, "hook_event_name": "UserPromptSubmit"})
    ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "voice is off now (muted: watching TV)" in ctx
    assert hook(env, "stop", {"session_id": SID}).stdout == ""


def test_hooks_never_break_a_session(env):
    r = hook(env, "stop", "not json at all")
    assert r.returncode == 0 and r.stdout == ""
