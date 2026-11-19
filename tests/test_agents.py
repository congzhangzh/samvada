"""The agent layer: setup (rule, skill, hooks) and the hooks themselves, in a fake home."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


@pytest.fixture
def claude(env, tmp_path):
    d = tmp_path / "claude"
    d.mkdir()
    (d / "CLAUDE.md").write_text("# My rules\n\nKeep this.\n", encoding="utf-8")
    (d / "settings.json").write_text(json.dumps({
        "model": "x",
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other-tool hook"}]}]},
    }, indent=1) + "\n", encoding="utf-8")
    env.vars |= {"CLAUDE_CONFIG_DIR": str(d), "XDG_CONFIG_HOME": str(tmp_path / "config")}
    return d


def test_dry_run_changes_nothing(env, claude):
    before = {p.name: p.read_text(encoding="utf-8") for p in claude.iterdir()}
    r = env.run("setup", "--dry-run")
    assert r.returncode == 0 and "dry run: nothing changed" in r.stdout
    assert "write " in r.stdout and "rules/samvada.md" in r.stdout and "install" in r.stdout
    assert {p.name: p.read_text(encoding="utf-8") for p in claude.iterdir()} == before


def test_setup_installs_rule_skill_hooks_and_keeps_the_rest(env, claude):
    r = env.run("setup", "--native", "zh", "--langs", "en,zh,ja,fr", "--table")
    assert r.returncode == 0, r.stderr
    assert (claude / "CLAUDE.md").read_text(encoding="utf-8") == "# My rules\n\nKeep this.\n"  # never touched
    md = (claude / "rules/samvada.md").read_text(encoding="utf-8")
    assert md.startswith("<!-- written by `samvada setup`") and "samvada:begin" not in md
    assert "native language is Chinese" in md and "English, Japanese, French" in md
    assert "🇺🇸 EN, 🔤 IPA, 🇨🇳 ZH" in md
    assert md.rstrip().endswith("where the two differ, it wins.") and "samvada.local.md" in md
    assert (claude / "skills/samvada/SKILL.md").read_text(encoding="utf-8").startswith("---\nname: samvada")
    s = json.loads((claude / "settings.json").read_text(encoding="utf-8"))
    assert s["model"] == "x"
    stop = [h["command"] for e in s["hooks"]["Stop"] for h in e["hooks"]]
    assert stop[0] == "other-tool hook" and stop[1].endswith("-m samvada hook stop")
    assert s["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"].endswith("hook prompt")
    assert (claude / "settings.json").read_text(encoding="utf-8").startswith('{\n "model"')  # indent kept
    assert not list(claude.glob("CLAUDE.md.samvada-backup-*")) and list(claude.glob("settings.json.samvada-backup-*"))
    # the settings are remembered, and a second run changes nothing
    r = env.run("setup")
    assert "nothing to change" in r.stdout and md == (claude / "rules/samvada.md").read_text(encoding="utf-8")
    assert len(json.loads((claude / "settings.json").read_text(encoding="utf-8"))["hooks"]["Stop"]) == 2


def test_setup_updates_the_rules_file_in_place(env, claude):
    env.run("setup", "--langs", "en,zh", "--table")
    r = env.run("setup", "--langs", "en,zh,fr", "--no-ipa")
    assert "update " in r.stdout
    md = (claude / "rules/samvada.md").read_text(encoding="utf-8")
    assert "🔤 IPA" not in md and "French" in md
    assert [p.name for p in (claude / "rules").iterdir()] == ["samvada.md"]  # no backup: the file is ours


def test_an_old_block_in_claude_md_moves_out(env, claude):
    """An older setup wrote the rule into CLAUDE.md: this one takes it out (a backup kept),
    keeps every line of the owner's around it, and writes rules/samvada.md instead."""
    from samvada.agents.setup import Tutor, render_rule
    old = "# My rules\n\nKeep this.\n\n" + render_rule(Tutor("zh", ("en", "zh"))) + "\n## Later section\nMine too.\n"
    (claude / "CLAUDE.md").write_text(old, encoding="utf-8")
    r = env.run("setup", "--native", "zh", "--langs", "en,zh")
    assert r.returncode == 0, r.stderr
    assert "move the old block out of" in r.stdout
    md = (claude / "CLAUDE.md").read_text(encoding="utf-8")
    assert "samvada:" not in md and md.startswith("# My rules\n\nKeep this.\n") and "## Later section\nMine too.\n" in md
    assert [b.read_text(encoding="utf-8") for b in claude.glob("CLAUDE.md.samvada-backup-*")] == [old]
    assert "native language is Chinese" in (claude / "rules/samvada.md").read_text(encoding="utf-8")
    assert "nothing to change" in env.run("setup").stdout


def test_the_owner_files_are_never_touched(env, claude):
    """rules/samvada.local.md and the skill's LOCAL.md are the owner's: setup, uninstall,
    setup again, byte for byte the same; uninstall keeps the skill folder that holds LOCAL.md."""
    rules, skill = claude / "rules", claude / "skills/samvada"
    rules.mkdir()
    skill.mkdir(parents=True)  # the owner may write LOCAL.md before Samvada is ever set up
    mine = {rules / "samvada.local.md": "- The owner likes the IPA row in bold.\n",
            skill / "LOCAL.md": "Say the table's French row slowly.\n"}
    for p, body in mine.items():
        p.write_text(body, encoding="utf-8")
    r = env.run("setup")
    assert r.returncode == 0 and "kept as it is, and it wins" in r.stdout, r.stdout + r.stderr
    assert (skill / "SKILL.md").read_text(encoding="utf-8").rstrip().endswith("where it differs from this file, it wins.")
    r = env.run("setup", "--uninstall")
    assert r.returncode == 0 and "left in place" in r.stdout, r.stderr
    assert not (rules / "samvada.md").exists() and not (skill / "SKILL.md").exists() and not (skill / ".samvada-managed").exists()
    env.run("setup")
    assert {p: p.read_text(encoding="utf-8") for p in mine} == mine


def test_a_rules_file_that_is_not_ours_is_left_alone(env, claude):
    (claude / "rules").mkdir()
    (claude / "rules/samvada.md").write_text("my own samvada notes\n", encoding="utf-8")
    for args in (["setup"], ["setup", "--uninstall"]):
        r = env.run(*args)
        assert r.returncode == 0 and "is not Samvada's: left alone" in r.stdout
    assert (claude / "rules/samvada.md").read_text(encoding="utf-8") == "my own samvada notes\n"


def test_uninstall_removes_exactly_what_it_added(env, claude):
    before_md = (claude / "CLAUDE.md").read_text(encoding="utf-8")
    before_settings = json.loads((claude / "settings.json").read_text(encoding="utf-8"))
    env.run("setup")
    r = env.run("setup", "--uninstall")
    assert r.returncode == 0, r.stderr
    assert (claude / "CLAUDE.md").read_text(encoding="utf-8") == before_md
    assert json.loads((claude / "settings.json").read_text(encoding="utf-8")) == before_settings
    assert not (claude / "skills/samvada").exists()


def test_a_skill_folder_that_is_not_ours_is_left_alone(env, claude):
    mine = claude / "skills/samvada"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("my own", encoding="utf-8")
    r = env.run("setup")
    assert "is not Samvada's: left alone" in r.stdout and (mine / "SKILL.md").read_text(encoding="utf-8") == "my own"
    env.run("setup", "--uninstall")
    assert (mine / "SKILL.md").read_text(encoding="utf-8") == "my own"


def test_codex_gets_agents_md_and_skill_no_hooks(env, tmp_path):
    codex = tmp_path / "codex"
    env.vars |= {"CODEX_HOME": str(codex), "XDG_CONFIG_HOME": str(tmp_path / "config")}
    r = env.run("setup", "--agent", "codex")
    assert r.returncode == 0, r.stderr
    md = (codex / "AGENTS.md").read_text(encoding="utf-8")
    assert "samvada:begin" in md and "go below `samvada:end`" in md and not (codex / "rules").exists()
    assert (codex / "skills/samvada/SKILL.md").exists() and not (codex / "settings.json").exists()
    assert "warning" not in r.stdout


def test_codex_warns_when_agents_override_hides_the_block(env, tmp_path):
    """Codex reads AGENTS.override.md instead of AGENTS.md (it replaces, not adds): say so."""
    codex = tmp_path / "codex"
    codex.mkdir()
    (codex / "AGENTS.override.md").write_text("mine\n", encoding="utf-8")
    env.vars |= {"CODEX_HOME": str(codex), "XDG_CONFIG_HOME": str(tmp_path / "config")}
    r = env.run("setup", "--agent", "codex")
    assert r.returncode == 0 and "warning: Codex reads" in r.stdout, r.stdout + r.stderr
    assert (codex / "AGENTS.override.md").read_text(encoding="utf-8") == "mine\n"


def test_bad_arguments(env, claude):
    assert env.run("setup", "--langs", "en,de").returncode == 2
    assert env.run("setup", "--agent", "vim").returncode == 2
    assert env.run("hook", "nope").returncode == 2


# -- hooks ----------------------------------------------------------------------------------
def hook(env, kind, event, **extra):
    import subprocess, sys
    return subprocess.run([sys.executable, "-m", "samvada", "hook", kind], input=json.dumps(event),
                          env={**env.vars, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"), **extra},
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


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


@pytest.fixture
def bindir(env, tmp_path):
    d = tmp_path / "tool-bin"
    d.mkdir()
    env.vars["SAMVADA_BIN_DIR"] = str(d)
    return d


def test_speak_alias_is_opt_in_and_runs_samvada_speak(env, claude, bindir):
    env.run("setup")
    assert not (bindir / "speak").exists()  # not asked for: nothing
    r = env.run("setup", "--speak-alias")
    assert r.returncode == 0, r.stderr
    text = (bindir / "speak").read_text(encoding="utf-8")
    assert "managed by `samvada setup --speak-alias`" in text and '-m samvada speak "$@"' in text
    assert (bindir / "speak").stat().st_mode & 0o111
    assert "unchanged at" in env.run("setup").stdout  # remembered in tutor.toml
    r = env.run("setup", "--uninstall")
    assert "remove" in r.stdout and not (bindir / "speak").exists()


def test_speak_alias_leaves_a_foreign_speak_alone_unless_replace(env, claude, bindir):
    foreign = bindir / "speak"
    foreign.write_text("#!/bin/sh\necho the old wrapper\n", encoding="utf-8")
    r = env.run("setup", "--speak-alias")
    assert "is not Samvada's: left alone" in r.stdout and foreign.read_text(encoding="utf-8").endswith("old wrapper\n")
    r = env.run("setup", "--speak-alias=replace")
    assert "replace" in r.stdout and "managed by `samvada setup" in foreign.read_text(encoding="utf-8")
    backups = list(bindir.glob("speak.samvada-backup-*"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8").endswith("old wrapper\n")
    env.run("setup", "--uninstall")  # removes ours and puts the old one back
    assert foreign.read_text(encoding="utf-8").endswith("old wrapper\n") and not list(bindir.glob("speak.samvada-backup-*"))


def test_speak_alias_actually_speaks(env, claude, bindir):
    env.run("setup", "--speak-alias")
    env.lines()
    r = env.run("en", "hello", "zh", "你好", cmd=[str(bindir / "speak")])
    assert r.returncode == 0, r.stderr
    assert env.played() == ["en", "zh"]


def test_the_default_rule_corrects_and_speaks_the_summary_with_no_table(env, claude, tmp_path):
    """The table (the message in every language, the IPA row) is for a learner: opt-in."""
    r = env.run("setup", "--native", "zh", "--langs", "en,zh")
    assert r.returncode == 0 and "table off" in r.stdout and "IPA" not in r.stdout, r.stdout + r.stderr
    md = (claude / "rules/samvada.md").read_text(encoding="utf-8")
    assert "1. **Correct it.**" in md and "2. **Speak with `samvada speak`**" in md
    assert "3. **A message that arrives while you work** gets the same: its corrections are" in md
    assert "Show it in every language" not in md and "🔤 IPA" not in md and "and table" not in md
    assert "table = false" in (tmp_path / "config/samvada/tutor.toml").read_text(encoding="utf-8")
    r = env.run("setup", "--table", "--ipa")
    md = (claude / "rules/samvada.md").read_text(encoding="utf-8")
    assert "2. **Show it in every language.**" in md and "🇺🇸 EN, 🔤 IPA, 🇨🇳 ZH" in md
    assert "4. **A message that arrives while you work** gets the same: its corrections and table are" in md
    assert "nothing to change" in env.run("setup").stdout  # --table is remembered


def test_an_upgrade_keeps_the_table_of_an_older_setup(env, claude, tmp_path):
    """A tutor.toml from before the table was opt-in has no `table` key: that user had the table."""
    cfg = tmp_path / "config/samvada"
    cfg.mkdir(parents=True)
    (cfg / "tutor.toml").write_text('native = "zh"\nlangs = ["en", "zh"]\nipa = true\nspeak_alias = false\n',
                                    encoding="utf-8")
    r = env.run("setup")
    assert r.returncode == 0 and "table on" in r.stdout, r.stdout + r.stderr
    assert "Show it in every language" in (claude / "rules/samvada.md").read_text(encoding="utf-8")
    assert "table = true" in (cfg / "tutor.toml").read_text(encoding="utf-8")


def test_setup_defaults_to_the_locale_and_english_when_unknown():
    from samvada.agents import setup
    assert setup.locale_lang({"LANG": "ja_JP.UTF-8"}) == "ja"
    assert setup.locale_lang({"LC_ALL": "fr_CA.UTF-8", "LANG": "zh_CN.UTF-8"}) == "fr"
    assert setup.locale_lang({"LC_MESSAGES": "zh_TW.UTF-8"}) == "zh"
    assert setup.locale_lang({"LANG": "de_DE.UTF-8"}) == "en"
    t = setup.default_tutor({"LANG": "zh_CN.UTF-8"})
    assert (t.native, t.langs) == ("zh", ("en", "zh"))
    t = setup.default_tutor({"LANG": "en_GB.UTF-8"})
    assert (t.native, t.langs) == ("en", ("en",))


def test_setup_with_no_settings_takes_the_locale(env, claude):
    r = env.run("setup", "--dry-run", LANG="fr_FR.UTF-8", LC_ALL="", LC_MESSAGES="")
    assert r.returncode == 0, r.stderr
    assert "native fr, languages en,fr" in r.stdout


def _setup_in_process(tmp_path, monkeypatch, args, ask):
    """setup.main in this process, its home a test folder: the rules, skill and hooks paths come
    from Path.home(), so HOME itself must move (a dict passed as env is not enough)."""
    from samvada.agents import setup
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    for k, v in {"HOME": str(home), "XDG_CONFIG_HOME": str(tmp_path / "config"),
                 "XDG_DATA_HOME": str(tmp_path / "data"), "SAMVADA_BIN_DIR": str(tmp_path / "bin"),
                 "LANG": "en_US.UTF-8"}.items():
        monkeypatch.setenv(k, v)
    assert Path.home() == home
    rc = setup.main(args, dict(os.environ), ask=ask)
    rule = home / ".claude" / "rules" / "samvada.md"
    toml = tmp_path / "config" / "samvada" / "tutor.toml"
    return (rc, rule.read_text(encoding="utf-8") if rule.exists() else "",
            toml.read_text(encoding="utf-8") if toml.exists() else "")


def test_setup_asks_the_name_once_and_the_rule_uses_it(tmp_path, monkeypatch):
    """Idea C: the first setup at a terminal asks; the answer is kept, never asked again."""
    asked = []
    rc, md, toml = _setup_in_process(tmp_path, monkeypatch, [], lambda: asked.append(1) or "Alex Lee")
    assert rc == 0 and asked == [1]
    assert "address the owner as **Alex Lee**" in md and "unless their own rules say otherwise" in md
    assert 'call = "Alex Lee"' in toml
    rc, md, _ = _setup_in_process(tmp_path, monkeypatch, ["--table"], lambda: asked.append(2) or "Other")
    assert rc == 0 and asked == [1] and "**Alex Lee**" in md


def test_no_name_means_you_and_their_own_rules_win(tmp_path, monkeypatch):
    """Idea B: Enter (or no terminal) gives no name: "you", never a title the agent picks."""
    rc, md, toml = _setup_in_process(tmp_path, monkeypatch, [], lambda: "")
    assert rc == 0 and 'call = ""' in toml
    assert 'when their own rules give none, address the owner as "you"' in md
    assert "master" not in md.lower()
    rc, md, toml = _setup_in_process(tmp_path, monkeypatch, ["--call", "聪"], lambda: "never asked")
    assert 'call = "聪"' in toml and "**聪**" in md
    rc, md, toml = _setup_in_process(tmp_path, monkeypatch, ["--no-call"], lambda: "never asked")
    assert 'call = ""' in toml and '"you"' in md


def test_no_terminal_asks_at_the_next_setup(tmp_path, monkeypatch):
    rc, md, toml = _setup_in_process(tmp_path, monkeypatch, [], lambda: None)
    assert rc == 0 and "call" not in toml and '"you"' in md
    rc, md, toml = _setup_in_process(tmp_path, monkeypatch, [], lambda: "Alex")
    assert 'call = "Alex"' in toml and "**Alex**" in md


def test_a_dry_run_never_asks(tmp_path, monkeypatch):
    def boom():
        raise AssertionError("asked")
    rc, md, toml = _setup_in_process(tmp_path, monkeypatch, ["--dry-run"], boom)
    assert rc == 0 and md == "" and toml == ""
