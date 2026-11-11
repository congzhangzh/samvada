"""`samvada setup`: give an agent the tutor's rule, the skill and the hooks (DESIGN 7.4).

Prints the plan first (`--dry-run` stops there), then changes only what differs:
  1. rule   the managed block between `samvada:begin` and `samvada:end` in CLAUDE.md
            (Claude Code) or AGENTS.md (Codex), filled from the owner's settings
  2. skill  skills/samvada/SKILL.md, copied (marked as Samvada's; a folder that is not ours
            is never touched)
  3. hooks  Claude Code only: UserPromptSubmit and Stop in settings.json; other hooks kept
  4. voices whether each language of the rule has its voice pack (nothing is downloaded)

Before a file is changed, a copy is kept beside it (<name>.samvada-backup-<time>).
`--uninstall` removes exactly the block, the skill and the hooks it added.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from .. import engines, paths

NAMES = {"en": "English", "zh": "Chinese", "ja": "Japanese", "fr": "French"}
FLAGS = {"en": "🇺🇸 EN", "zh": "🇨🇳 ZH", "ja": "🇯🇵 JA", "fr": "🇫🇷 FR"}
BEGIN = "<!-- samvada:begin (managed by `samvada setup`; change the settings, not this block) -->"
END = "<!-- samvada:end -->"
BLOCK_RE = re.compile(r"\n*<!-- samvada:begin.*?<!-- samvada:end -->\n?", re.S)
MARKER = ".samvada-managed"
HOOK_TAG = "-m samvada hook"


@dataclass
class Tutor:
    native: str = "zh"
    langs: tuple[str, ...] = ("en", "zh")
    ipa: bool = True


def config_path(env: Mapping[str, str] = os.environ) -> Path:
    base = Path(env["XDG_CONFIG_HOME"]) if env.get("XDG_CONFIG_HOME") else Path.home() / ".config"
    return base / "samvada" / "tutor.toml"


def load_tutor(env: Mapping[str, str] = os.environ) -> Tutor:
    try:
        d = tomllib.loads(config_path(env).read_text())
    except (OSError, ValueError):
        return Tutor()
    return Tutor(d.get("native", "zh"), tuple(d.get("langs", ("en", "zh"))), bool(d.get("ipa", True)))


def save_tutor(t: Tutor, env: Mapping[str, str] = os.environ) -> None:
    p = config_path(env)
    p.parent.mkdir(parents=True, exist_ok=True)
    langs = ", ".join(f'"{l}"' for l in t.langs)
    p.write_text(f'# Written by `samvada setup`; run it again after a change.\nnative = "{t.native}"\n'
                 f"langs = [{langs}]\nipa = {'true' if t.ipa else 'false'}\n")


def render_rule(t: Tutor) -> str:
    learning = [NAMES[l] for l in t.langs if l != t.native] or [NAMES[l] for l in t.langs]
    rows = []
    for l in t.langs:
        rows.append(FLAGS[l])
        if l == "en" and t.ipa:
            rows.append("🔤 IPA")
    ipa = (" The IPA row transcribes the whole English row (General American): slashes, stress "
           "marks, the flap ɾ, weak forms as spoken.") if t.ipa and "en" in t.langs else ""
    body = resources.files("samvada").joinpath("agents", "rule.md").read_text().format(
        native=NAMES[t.native], learning=", ".join(learning), rows=", ".join(rows), ipa=ipa,
        order=", ".join(NAMES[l] for l in t.langs) + " (each language once)")
    return f"{BEGIN}\n{body.rstrip()}\n{END}\n"


def agent_dir(agent: str, env: Mapping[str, str] = os.environ) -> Path:
    if agent == "claude":
        return Path(env["CLAUDE_CONFIG_DIR"]) if env.get("CLAUDE_CONFIG_DIR") else Path.home() / ".claude"
    return Path(env["CODEX_HOME"]) if env.get("CODEX_HOME") else Path.home() / ".codex"


def _backup(p: Path) -> None:
    if p.exists():
        shutil.copy2(p, p.with_name(f"{p.name}.samvada-backup-{time.strftime('%Y%m%d-%H%M%S')}"))


def _hook_command(kind: str) -> str:
    return f'"{sys.executable}" {HOOK_TAG} {kind}'


def _is_ours(entry: dict) -> bool:
    return any(HOOK_TAG in h.get("command", "") for h in entry.get("hooks", []))


def _indent(text: str) -> int:
    m = re.search(r"\n( +)\S", text)
    return len(m.group(1)) if m else 2


class Step:
    def __init__(self, name: str, desc: str, apply=None):
        self.name, self.desc, self.apply = name, desc, apply


def plan(agent: str, t: Tutor, uninstall: bool, env: Mapping[str, str] = os.environ) -> list[Step]:
    home = agent_dir(agent, env)
    steps: list[Step] = []

    # 1. rule
    rule_file = home / ("CLAUDE.md" if agent == "claude" else "AGENTS.md")
    old = rule_file.read_text() if rule_file.exists() else ""
    stripped = BLOCK_RE.sub("\n", old).rstrip("\n")
    new = (stripped + "\n") if uninstall else ((stripped + "\n\n" if stripped else "") + render_rule(t))
    if uninstall and new.strip() == stripped.strip() and BLOCK_RE.search(old) is None:
        steps.append(Step("rule", f"no Samvada block in {rule_file}"))
    elif new == old:
        steps.append(Step("rule", f"unchanged in {rule_file}"))
    else:
        def write_rule(new=new):
            rule_file.parent.mkdir(parents=True, exist_ok=True)
            _backup(rule_file)
            rule_file.write_text(new)
        verb = "remove the block from" if uninstall else ("update the block in" if BLOCK_RE.search(old) else "add the block to")
        steps.append(Step("rule", f"{verb} {rule_file}", write_rule))

    # 2. skill
    dest = home / "skills" / "samvada"
    src = resources.files("samvada").joinpath("agents", "skills", "samvada", "SKILL.md").read_text()
    ours = (dest / MARKER).exists()
    if dest.exists() and not ours:
        steps.append(Step("skill", f"{dest} exists and is not Samvada's: left alone"))
    elif uninstall:
        if ours:
            steps.append(Step("skill", f"remove {dest}", lambda: shutil.rmtree(dest)))
        else:
            steps.append(Step("skill", f"not installed in {dest}"))
    elif ours and (dest / "SKILL.md").exists() and (dest / "SKILL.md").read_text() == src:
        steps.append(Step("skill", f"unchanged in {dest}"))
    else:
        def write_skill():
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "SKILL.md").write_text(src)
            (dest / MARKER).write_text("installed by `samvada setup`; `samvada setup --uninstall` removes it\n")
        steps.append(Step("skill", f"{'update' if ours else 'install'} {dest / 'SKILL.md'}", write_skill))

    # 3. hooks (Claude Code)
    if agent == "claude":
        sfile = home / "settings.json"
        text = sfile.read_text() if sfile.exists() else "{}"
        try:
            settings = json.loads(text)
        except ValueError:
            steps.append(Step("hooks", f"{sfile} is not valid JSON: left alone"))
            settings = None
        if settings is not None:
            hooks = settings.setdefault("hooks", {})
            for event in ("UserPromptSubmit", "Stop"):
                hooks[event] = [e for e in hooks.get(event, []) if not _is_ours(e)]
                if not uninstall:
                    kind = "prompt" if event == "UserPromptSubmit" else "stop"
                    hooks[event].append({"hooks": [{"type": "command", "command": _hook_command(kind),
                                                    "timeout": 10}]})
                if not hooks[event]:
                    del hooks[event]
            if not hooks:
                del settings["hooks"]
            new_text = json.dumps(settings, indent=_indent(text), ensure_ascii=False) + "\n"
            if json.loads(new_text) == json.loads(text):
                steps.append(Step("hooks", f"unchanged in {sfile}"))
            else:
                def write_hooks(new_text=new_text):
                    sfile.parent.mkdir(parents=True, exist_ok=True)
                    _backup(sfile)
                    sfile.write_text(new_text)
                verb = "remove the UserPromptSubmit and Stop hooks from" if uninstall else \
                    "set the UserPromptSubmit and Stop hooks in"
                steps.append(Step("hooks", f"{verb} {sfile}", write_hooks))
    else:
        steps.append(Step("hooks", "Codex: no hooks; the rule and the skill carry the behaviour"))

    # 4. voices (report only)
    if not uninstall:
        voices = engines.load_voices()
        dirs = paths.voices_dirs(env)
        missing = [l for l in t.langs if l in voices and not engines.has_pack(voices[l], dirs)]
        steps.append(Step("voices", "every language has its voice pack" if not missing else
                          f"no voice pack for {' '.join(missing)}: samvada voices add {' '.join(missing)}"))
    return steps


def main(args: list[str], env: Mapping[str, str] = os.environ) -> int:
    agent, dry, uninstall = "claude", False, False
    t = load_tutor(env)
    it = iter(args)
    for a in it:
        if a == "--agent":
            agent = next(it, "")
        elif a == "--dry-run":
            dry = True
        elif a == "--uninstall":
            uninstall = True
        elif a == "--native":
            t.native = next(it, "")
        elif a == "--langs":
            t.langs = tuple(l for l in next(it, "").split(",") if l)
        elif a in ("--ipa", "--no-ipa"):
            t.ipa = a == "--ipa"
        else:
            print(f"samvada setup: unknown argument {a}", file=sys.stderr)
            return 2
    if agent not in ("claude", "codex"):
        print("samvada setup: --agent claude|codex", file=sys.stderr)
        return 2
    bad = [l for l in (*t.langs, t.native) if l not in NAMES]
    if bad or not t.langs:
        print(f"samvada setup: languages are {' '.join(NAMES)} (got {' '.join(bad) or 'none'})", file=sys.stderr)
        return 2
    steps = plan(agent, t, uninstall, env)
    print(f"samvada setup ({agent}{', uninstall' if uninstall else ''}): native {t.native}, "
          f"languages {','.join(t.langs)}, IPA {'on' if t.ipa else 'off'}")
    for s in steps:
        print(f"  {s.name:7} {s.desc}")
    if dry:
        print("dry run: nothing changed")
        return 0
    for s in steps:
        if s.apply:
            s.apply()
    if not uninstall:
        save_tutor(t, env)
    print("done" + ("" if any(s.apply for s in steps) else ": nothing to change"))
    return 0
