"""`samvada setup`: give an agent the tutor's rule, the skill and the hooks (DESIGN 7.4).

Prints the plan first (`--dry-run` stops there), then changes only what differs:
  1. rule   filled from the owner's settings. Claude Code: its own file, rules/samvada.md
            (Claude Code reads every rules/*.md at the start of a session); a block an older
            setup left in CLAUDE.md is moved out. Codex has no rules folder: the managed block
            between `samvada:begin` and `samvada:end` in AGENTS.md.
            The owner's own lines live beside it and win where they differ: rules/samvada.local.md
            (Claude Code), below `samvada:end` (Codex). Samvada never writes or removes them.
  2. skill  skills/samvada/SKILL.md, copied (marked as Samvada's; a folder that is not ours
            is never touched). The owner's LOCAL.md beside it is theirs: kept on --uninstall.
  3. hooks  Claude Code only: UserPromptSubmit and Stop in settings.json; other hooks kept
  4. voices whether each language of the rule has its voice pack (nothing is downloaded)
  5. alias  with --speak-alias: a `speak` command beside `samvada` (a two-line program, not a
            shell alias: an agent's shell never reads ~/.bashrc). A `speak` that is not ours
            (espeak-ng-espeak's /usr/bin/speak, an older wrapper) is left alone, unless
            --speak-alias=replace, which keeps it beside as a backup.
  6. path   a standalone bundle goes on PATH, so the agent runs `samvada` by name (--no-path:
            not): on Windows its bin folder in the user's PATH (a new terminal sees it), else a
            link ~/.local/bin/samvada ($XDG_BIN_HOME) to its launcher; a `samvada` there that is
            not ours is left alone. An install by pip or uv is on PATH already; a note if not.

Before a file is changed, a copy is kept beside it (<name>.samvada-backup-<time>).
`--uninstall` removes exactly the block, the skill, the hooks, the alias and the PATH entry it
added (and puts back a `speak` that --speak-alias=replace had set aside).
"""

from __future__ import annotations

import json
import locale
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

from .. import engines, packs, paths

NAMES = {"en": "English", "zh": "Chinese", "ja": "Japanese", "fr": "French"}
FLAGS = {"en": "🇺🇸 EN", "zh": "🇨🇳 ZH", "ja": "🇯🇵 JA", "fr": "🇫🇷 FR"}
BEGIN = "<!-- samvada:begin (managed by `samvada setup`; change the settings, not this block) -->"
END = "<!-- samvada:end -->"
BLOCK_RE = re.compile(r"\n*<!-- samvada:begin.*?<!-- samvada:end -->\n?", re.S)
RULES_MARK = "<!-- written by `samvada setup`"
RULES_HEADER = (f"{RULES_MARK} from the owner's settings; change the settings and run it again, "
                "never this file. The owner's own lines go in samvada.local.md beside it. -->")
LOCAL_RULE = "samvada.local.md"
LOCAL_SKILL = "LOCAL.md"
MARKER = ".samvada-managed"
HOOK_TAG = "-m samvada hook"
ALIAS_TAG = "managed by `samvada setup --speak-alias`"


@dataclass
class Tutor:
    native: str = "en"
    langs: tuple[str, ...] = ("en",)
    ipa: bool = True
    speak_alias: bool = False
    table: bool = False  # the table of the message in every language (and its IPA row): opt-in
    call: str | None = None  # the owner's name for the agent: None = never asked, "" = none
    path: bool = True  # a standalone bundle goes on PATH (step 6)


def locale_lang(env: Mapping[str, str] = os.environ) -> str:
    """The user's language from the locale, as POSIX orders it (LC_ALL, LC_MESSAGES, LANG),
    else what the system says (Windows); English when it is none of NAMES."""
    names = [env.get(k, "") for k in ("LC_ALL", "LC_MESSAGES", "LANG")]
    try:
        names.append(locale.getlocale()[0] or "")
    except ValueError:
        pass
    for name in names:
        if name and name not in ("C", "POSIX") and not name.startswith("C."):
            code = name.split("_")[0].split(".")[0].split("-")[0].lower()
            return code if code in NAMES else "en"
    return "en"


def default_tutor(env: Mapping[str, str] = os.environ) -> Tutor:
    """No settings yet: native from the locale (English when unknown), learning English, or
    English alone for an English speaker."""
    native = locale_lang(env)
    return Tutor(native, tuple(l for l in NAMES if l in ("en", native)))


def config_path(env: Mapping[str, str] = os.environ) -> Path:
    base = Path(env["XDG_CONFIG_HOME"]) if env.get("XDG_CONFIG_HOME") else Path.home() / ".config"
    return base / "samvada" / "tutor.toml"


def load_tutor(env: Mapping[str, str] = os.environ) -> Tutor:
    t = default_tutor(env)
    try:
        d = tomllib.loads(config_path(env).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return t
    # a tutor.toml from before the table was opt-in (2026-11-19) had the table: an upgrade keeps it
    return Tutor(d.get("native", t.native), tuple(d.get("langs", t.langs)), bool(d.get("ipa", True)),
                 bool(d.get("speak_alias", False)), bool(d.get("table", True)),
                 str(d["call"]) if "call" in d else None, bool(d.get("path", True)))


def save_tutor(t: Tutor, env: Mapping[str, str] = os.environ) -> None:
    p = config_path(env)
    p.parent.mkdir(parents=True, exist_ok=True)
    langs = ", ".join(f'"{l}"' for l in t.langs)
    p.write_text(f'# Written by `samvada setup`; run it again after a change.\nnative = "{t.native}"\n'
                 f"langs = [{langs}]\nipa = {'true' if t.ipa else 'false'}\n"
                 f"speak_alias = {'true' if t.speak_alias else 'false'}\n"
                 f"table = {'true' if t.table else 'false'}\n"
                 f"path = {'true' if t.path else 'false'}\n"
                 + ("" if t.call is None else f"call = {json.dumps(t.call, ensure_ascii=False)}\n"),
                 encoding="utf-8")


def _call_line(t: Tutor) -> str:
    """What to call the owner. Their own rules (samvada.local.md, CLAUDE.md, AGENTS.md…) win."""
    if t.call:
        return (f"**Their name:** address the owner as **{t.call}**, on screen and in the voice, "
                "unless their own rules say otherwise (then theirs).\n\n")
    return ("**Their name:** when their own rules give none, address the owner as \"you\", with no "
            "name or title of your own (`samvada setup --call <name>` sets one).\n\n")


def _rule_body(t: Tutor) -> str:
    learning = [NAMES[l] for l in t.langs if l != t.native] or [NAMES[l] for l in t.langs]
    rows = []
    for l in t.langs:
        rows.append(FLAGS[l])
        if l == "en" and t.ipa:
            rows.append("🔤 IPA")
    ipa = (" The IPA row transcribes the whole English row (General American): slashes, stress "
           "marks, the flap ɾ, weak forms as spoken.") if t.ipa and "en" in t.langs else ""
    table = (f"2. **Show it in every language.** A table with one row per language: "
             f"{', '.join(rows)}.{ipa}\n") if t.table else ""
    body = resources.files("samvada").joinpath("agents", "rule.md").read_text(encoding="utf-8").format(
        native=NAMES[t.native], learning=", ".join(learning), table=table,
        n_speak=3 if t.table else 2, n_mid=4 if t.table else 3, and_table=" and table" if t.table else "",
        order=", ".join(NAMES[l] for l in t.langs) + " (each language once)", call=_call_line(t))
    return body.rstrip()


def render_rule(t: Tutor) -> str:
    """The block for AGENTS.md (Codex): the owner's own lines go below it."""
    return (f"{BEGIN}\n{_rule_body(t)}\n\nThe owner's own lines go below `samvada:end`, outside "
            f"this block (Samvada never writes there); where they differ from it, they win.\n{END}\n")


def render_rules_file(t: Tutor) -> str:
    """rules/samvada.md (Claude Code): the whole file is Samvada's; the owner's lines live in
    samvada.local.md beside it. Claude Code reads both and nothing merges them, so the last
    line says which one wins."""
    return (f"{RULES_HEADER}\n\n{_rule_body(t)}\n\n`{LOCAL_RULE}` beside this file is the owner's "
            "own (Samvada never writes it); where the two differ, it wins.\n")


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


def bin_dir(env: Mapping[str, str] = os.environ) -> Path:
    """Where `samvada` is run from: the alias goes beside it (SAMVADA_BIN_DIR overrides)."""
    if env.get("SAMVADA_BIN_DIR"):
        return Path(env["SAMVADA_BIN_DIR"])
    found = shutil.which("samvada", path=env.get("PATH"))
    if found:
        return Path(found).parent
    bundle = paths.bundle_root()  # its python/ holds no samvada; bin/ does
    return bundle / "bin" if bundle else Path(sys.executable).parent


def _on_path(env: Mapping[str, str]) -> bool:
    """Whether the agent can run `samvada` by name. On Windows the bare name must be samvada.exe
    or samvada itself: Git Bash, the shell Claude Code runs commands in, never tries .cmd."""
    names = ("samvada", "samvada.exe") if sys.platform == "win32" else ("samvada",)
    return any(d and any(Path(d, n).is_file() for n in names)
               for d in (env.get("PATH") or "").split(os.pathsep))


def _win_user_path() -> tuple[str, int]:
    """The user's own PATH (HKCU\\Environment), as written, and its registry type."""
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
        try:
            value, kind = winreg.QueryValueEx(k, "Path")
            return str(value), kind
        except FileNotFoundError:
            return "", winreg.REG_EXPAND_SZ


def _win_set_user_path(value: str, kind: int) -> None:
    import ctypes
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, "Path", 0, kind, value)
    # WM_SETTINGCHANGE "Environment" to every window: Explorer, then each new terminal, sees it
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x1A, 0, "Environment", 0x2, 5000, None)


def _same_dir(a: str, b: Path) -> bool:
    return os.path.normcase(a.strip().rstrip("\\/")) == os.path.normcase(str(b).rstrip("\\/"))


def _win_path_steps(bin_: Path, t: Tutor, uninstall: bool, env: Mapping[str, str]) -> list[Step]:
    value, kind = _win_user_path()
    parts = [x for x in value.split(";") if x.strip()]
    has = any(_same_dir(x, bin_) for x in parts)
    if uninstall or not t.path:
        if not has:
            return []
        keep = ";".join(x for x in parts if not _same_dir(x, bin_))
        return [Step("path", f"take {bin_} out of your user PATH", lambda: _win_set_user_path(keep, kind))]
    if has:
        return [Step("path", f"{bin_} is on your user PATH")]
    new = ";".join([*parts, str(bin_)])
    return [Step("path", f"add {bin_} to your user PATH, so the agent runs `samvada` by name "
                         "(a new terminal sees it: restart the agent)", lambda: _win_set_user_path(new, kind))]


def link_dir(env: Mapping[str, str] = os.environ) -> Path:
    return Path(env["XDG_BIN_HOME"]) if env.get("XDG_BIN_HOME") else Path.home() / ".local" / "bin"


def _posix_path_steps(launcher: Path, t: Tutor, uninstall: bool, env: Mapping[str, str]) -> list[Step]:
    link = link_dir(env) / "samvada"
    target = Path(os.readlink(link)) if link.is_symlink() else None
    ours = target == launcher
    # a link to another bundle's launcher (an older version), or to one that is gone: ours to move
    old_bundle = target is not None and not ours and target.name == "samvada" \
        and target.parent.name == "bin" and ((target.parents[1] / "BUNDLE.json").is_file()
                                             or not target.exists())
    if uninstall or not t.path:
        return [Step("path", f"remove the link {link}", link.unlink)] if ours else []
    if ours:
        steps = [Step("path", f"{link} runs this bundle")]
    elif (link.exists() or link.is_symlink()) and not old_bundle:
        return [Step("path", f"{link} is another samvada: left alone (the agent runs that one)")]
    else:
        def make():
            link.parent.mkdir(parents=True, exist_ok=True)
            if link.is_symlink():
                link.unlink()
            link.symlink_to(launcher)
        verb = f"move the link {link} from {target}" if old_bundle else f"link {link}"
        steps = [Step("path", f"{verb} to {launcher}, so the agent runs `samvada` by name", make)]
    if not any(d and _same_dir(d, link.parent) for d in (env.get("PATH") or "").split(os.pathsep)):
        steps.append(Step("path", f"note: {link.parent} is not on PATH: add it"))
    return steps


def _path_steps(t: Tutor, uninstall: bool, env: Mapping[str, str]) -> list[Step]:
    """Step 6: a standalone bundle goes on PATH (the rule has the agent run `samvada speak` by
    name); an install by pip or uv is there already, and gets a note if it is not."""
    bundle = paths.bundle_root()
    if bundle is None:
        if uninstall or _on_path(env):
            return []
        return [Step("path", f"note: `samvada` is not on PATH, and the agent runs `samvada speak`: "
                             f"add {bin_dir(env)} to PATH")]
    if sys.platform == "win32":
        return _win_path_steps(bundle / "bin", t, uninstall, env)
    return _posix_path_steps(bundle / "bin" / "samvada", t, uninstall, env)


def alias_text() -> str:
    if sys.platform == "win32":
        return (f"@echo off\r\nrem {ALIAS_TAG}; `samvada setup --uninstall` removes it\r\n"
                f'"{sys.executable}" -m samvada speak %*\r\n')
    return (f"#!/bin/sh\n# {ALIAS_TAG}; `samvada setup --uninstall` removes it\n"
            f'exec "{sys.executable}" -m samvada speak "$@"\n')


def _alias_is_ours(p: Path) -> bool:
    try:
        return ALIAS_TAG.encode() in p.read_bytes()[:400]
    except OSError:
        return False


def _alias_steps(t: Tutor, uninstall: bool, replace: bool, env: Mapping[str, str]) -> list[Step]:
    target = bin_dir(env) / ("speak.cmd" if sys.platform == "win32" else "speak")
    exists = target.exists() or target.is_symlink()
    ours = exists and _alias_is_ours(target)
    backups = sorted(target.parent.glob(f"{target.name}.samvada-backup-*"))
    if uninstall or not t.speak_alias:
        if not ours:
            return [] if not uninstall else [Step("alias", f"no Samvada alias at {target}")]

        def remove():
            target.unlink()
            if backups:  # the speak that --speak-alias=replace set aside comes back
                backups[-1].rename(target)
        back = f", put back {backups[-1].name}" if backups else ""
        return [Step("alias", f"remove {target}{back}", remove)]
    text = alias_text()
    if ours and target.read_text(encoding="utf-8") == text:
        return [Step("alias", f"unchanged at {target}")]
    if exists and not ours and not replace:
        return [Step("alias", f"{target} exists and is not Samvada's: left alone "
                              "(--speak-alias=replace sets it aside as a backup)")]

    def write():
        target.parent.mkdir(parents=True, exist_ok=True)
        if exists and not ours:
            target.rename(target.with_name(f"{target.name}.samvada-backup-{time.strftime('%Y%m%d-%H%M%S')}"))
        elif target.is_symlink():
            target.unlink()
        target.write_text(text, newline="", encoding="utf-8")
        target.chmod(0o755)
    verb = "replace (the old one kept as a backup)" if exists and not ours else ("update" if ours else "add")
    steps = [Step("alias", f"{verb} {target}: speak runs samvada speak", write)]
    first = shutil.which("speak", path=env.get("PATH"))
    if first and Path(first).parent != target.parent:
        steps.append(Step("alias", f"note: {first} comes first on PATH and will answer to speak"))
    return steps


class Step:
    def __init__(self, name: str, desc: str, apply=None):
        self.name, self.desc, self.apply = name, desc, apply


def _block_steps(rule_file: Path, t: Tutor, uninstall: bool, *, migrate: bool = False) -> list[Step]:
    """The managed block in an instruction file the owner also writes (AGENTS.md); with
    migrate, only take out a block an older setup left there (CLAUDE.md)."""
    old = rule_file.read_text(encoding="utf-8") if rule_file.exists() else ""
    has = BLOCK_RE.search(old) is not None
    stripped = BLOCK_RE.sub("\n", old).rstrip("\n")
    if uninstall or migrate:
        if not has:
            return [] if migrate else [Step("rule", f"no Samvada block in {rule_file}")]
        new = stripped + "\n"
        verb, why = ("remove the block from", "") if uninstall else \
            ("move the old block out of", " (the rule has its own file now)")
    else:
        new = (stripped + "\n\n" if stripped else "") + render_rule(t)
        if new == old:
            return [Step("rule", f"unchanged in {rule_file}")]
        verb, why = ("update the block in" if has else "add the block to"), ""

    def write_rule(new=new):
        rule_file.parent.mkdir(parents=True, exist_ok=True)
        _backup(rule_file)
        rule_file.write_text(new, encoding="utf-8")
    return [Step("rule", f"{verb} {rule_file}{why}", write_rule)]


def _rules_file_steps(home: Path, t: Tutor, uninstall: bool) -> list[Step]:
    """Claude Code: rules/samvada.md is Samvada's whole file; rules/samvada.local.md is the
    owner's and is never written or removed."""
    rules = home / "rules"
    f, local = rules / "samvada.md", rules / LOCAL_RULE
    cur = f.read_text(encoding="utf-8") if f.exists() else None
    steps: list[Step] = []
    if cur is not None and not cur.startswith(RULES_MARK):
        steps.append(Step("rule", f"{f} exists and is not Samvada's: left alone"))
    elif uninstall:
        steps.append(Step("rule", f"remove {f}", f.unlink) if cur is not None
                     else Step("rule", f"no Samvada rules file in {rules}"))
    else:
        want = render_rules_file(t)
        if cur == want:
            steps.append(Step("rule", f"unchanged in {f}"))
        else:
            def write_file(want=want):
                rules.mkdir(parents=True, exist_ok=True)
                f.write_text(want, encoding="utf-8")
            steps.append(Step("rule", f"{'update' if cur is not None else 'write'} {f}", write_file))
    steps += _block_steps(home / "CLAUDE.md", t, uninstall, migrate=True)
    if local.exists():
        steps.append(Step("rule", f"{local} is yours: " +
                          ("left in place" if uninstall else "kept as it is, and it wins")))
    return steps


def plan(agent: str, t: Tutor, uninstall: bool, env: Mapping[str, str] = os.environ,
         replace: bool = False) -> list[Step]:
    home = agent_dir(agent, env)
    steps: list[Step] = []

    # 1. rule
    if agent == "claude":
        steps += _rules_file_steps(home, t, uninstall)
    else:
        steps += _block_steps(home / "AGENTS.md", t, uninstall)
        if not uninstall and (home / "AGENTS.override.md").exists():
            # Codex reads the first of AGENTS.override.md, AGENTS.md: the override replaces
            # AGENTS.md, it is not added to it (unlike Claude Code's .local files)
            steps.append(Step("rule", f"warning: Codex reads {home / 'AGENTS.override.md'} instead of "
                              "AGENTS.md, so it will not see this block; copy the block there, "
                              "or move your lines below `samvada:end` in AGENTS.md"))

    # 2. skill
    dest = home / "skills" / "samvada"
    src = resources.files("samvada").joinpath("agents", "skills", "samvada", "SKILL.md").read_text(encoding="utf-8")
    ours = (dest / MARKER).exists()
    mine = dest / LOCAL_SKILL  # the owner's, beside ours: never written, never removed
    foreign = dest.exists() and not ours and any(p.name != LOCAL_SKILL for p in dest.iterdir())
    if foreign:
        steps.append(Step("skill", f"{dest} exists and is not Samvada's: left alone"))
    elif uninstall:
        if ours and mine.exists():
            def remove_ours():
                for name in ("SKILL.md", MARKER):
                    (dest / name).unlink(missing_ok=True)
            steps.append(Step("skill", f"remove {dest / 'SKILL.md'}; {mine} is yours: left in place",
                              remove_ours))
        elif ours:
            steps.append(Step("skill", f"remove {dest}", lambda: shutil.rmtree(dest)))
        else:
            steps.append(Step("skill", f"not installed in {dest}"))
    elif ours and (dest / "SKILL.md").exists() and (dest / "SKILL.md").read_text(encoding="utf-8") == src:
        steps.append(Step("skill", f"unchanged in {dest}"))
    else:
        def write_skill():
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "SKILL.md").write_text(src, encoding="utf-8")
            (dest / MARKER).write_text("installed by `samvada setup`; `samvada setup --uninstall` removes it\n", encoding="utf-8")
        steps.append(Step("skill", f"{'update' if ours else 'install'} {dest / 'SKILL.md'}", write_skill))

    # 3. hooks (Claude Code)
    if agent == "claude":
        sfile = home / "settings.json"
        text = sfile.read_text(encoding="utf-8") if sfile.exists() else "{}"
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
                    sfile.write_text(new_text, encoding="utf-8")
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
        lock = packs.load_lock()
        boxed = [l for l in missing if packs.from_package(l, lock, env)]
        rest = [l for l in missing if l not in boxed]
        if boxed:
            target = paths.install_voices_dir(env)

            def install_boxed(boxed=boxed, target=target):
                for l in boxed:
                    packs.add(l, lock, target, env, log=lambda *a: None, offline=True)
            steps.append(Step("voices", f"install {' '.join(boxed)} from the installed voice "
                              f"packages into {target} (no network)", install_boxed))
        steps.append(Step("voices", "every language has its voice pack" if not missing else
                          (f"no voice pack for {' '.join(rest)}: samvada voices add {' '.join(rest)}"
                           if rest else "the other languages have their voice packs")))

    # 5. the speak alias
    steps += _alias_steps(t, uninstall, replace, env)

    # 6. the agent can find samvada
    steps += _path_steps(t, uninstall, env)
    return steps


def _ask_call() -> str | None:
    """Ask once, at a terminal, what the agent should call the owner (Enter: no name)."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None  # asked at the next setup run at a terminal
    try:
        return input("What should the agent call you? A name, or Enter for none: ").strip()
    except EOFError:
        return None


def main(args: list[str], env: Mapping[str, str] = os.environ, ask=_ask_call) -> int:
    agent, dry, uninstall, replace = "claude", False, False, False
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
        elif a in ("--table", "--no-table"):
            t.table = a == "--table"
        elif a == "--call":
            t.call = next(it, "").strip()
        elif a == "--no-call":
            t.call = ""
        elif a in ("--path", "--no-path"):
            t.path = a == "--path"
        elif a in ("--speak-alias", "--speak-alias=replace", "--no-speak-alias"):
            t.speak_alias = a != "--no-speak-alias"
            replace = a == "--speak-alias=replace"
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
    if t.call is None and not dry and not uninstall:
        try:
            t.call = ask()
        except KeyboardInterrupt:
            print("\nsamvada setup: stopped, nothing changed", file=sys.stderr)
            return 130
    steps = plan(agent, t, uninstall, env, replace)
    print(f"samvada setup ({agent}{', uninstall' if uninstall else ''}): native {t.native}, "
          f"languages {','.join(t.langs)}, table {'on' if t.table else 'off'}"
          f"{(', IPA ' + ('on' if t.ipa else 'off')) if t.table else ''}"
          f", name {t.call or 'none'}")
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
