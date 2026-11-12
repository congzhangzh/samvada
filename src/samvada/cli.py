"""The `samvada` command."""

from __future__ import annotations

import os
import sys

from pathlib import Path

from . import __version__, engines, packs, paths
from .presence import where
from .speak import say, speak
from .state import ONLINE_MODES, State, session_key

USAGE = """\
usage: samvada speak [--save DIR] <lang> "<text>" [<lang> "<text>" ...]
                                       play in this order (--save: write the clips, play nothing)
       samvada <lang> "<text>" ...     the same
       samvada status                  where the voice goes now and why; languages; online mode
       samvada [--session] mute ["<why>"] | unmute
       samvada [--session] lang [--enable-<lang>]... [--disable-<lang>]... [--reset] [--list|-l]
                                       e.g. lang --enable-zh --disable-ja; --list: every
                                       language, on or off, where that was set, its voice pack
       samvada [--session] lang [on|off <lang>... | reset]   the older form, kept
       samvada [--session] online [off|fallback|always]     default off: local voices only
       samvada presence [phone]        phone: the owner just wrote from the phone
       samvada voices [list]           the voice packs: installed where, size, engine
       samvada voices add <lang>... [--engine auto|always|never]
                                       download, verify and install (network needed once)
       samvada voices verify [<lang>...] | remove <lang>...
       samvada setup [--agent claude|codex] [--native zh] [--langs en,zh] [--ipa|--no-ipa]
                     [--speak-alias[=replace]|--no-speak-alias] [--dry-run] [--uninstall]
                                       the tutor's rule, the skill and the hooks for an agent;
                                       --speak-alias adds a `speak` command for samvada speak
       samvada hook prompt|stop        run by the agent's hooks (JSON on stdin)
       samvada version

--session  the setting holds for this session only (Claude Code session, pys window or tty)
env        SAMVADA=on|off  force playing or silence for one call
           SAMVADA_ONLINE=off|fallback|always  the online mode for one call
           SAMVADA_DEBUG=1  show engine output;  SAMVADA_SESSION=<key>
           SAMVADA_VOICES=<dir>  SAMVADA_ENGINES=<dir>  the folders of the voices and engines
           HF_ENDPOINT, HF_TOKEN  a Hugging Face mirror; a token against rate limits (optional)
exit       0 played, or silent by decision; 1 a language failed; 2 bad usage"""


class Usage(Exception):
    pass


def _phone_marks(state: State) -> list:
    return [state.root / "presence" / "phone", paths.legacy_state_dir() / "remote"]


def _langs_line(state: State, voices: dict) -> str:
    dirs = paths.voices_dirs()
    parts = []
    for lang, voice in voices.items():
        s = state.lang(lang)
        pack = "" if engines.has_pack(voice, dirs) else ", no voice pack"
        parts.append(f"{lang} {s.value}({s.where}{pack})")
    return "languages: " + "  ".join(parts)


def _show(state: State, voices: dict, full: bool) -> None:
    print(_langs_line(state, voices))
    if full:
        mode = state.online()
        note = ""
        if mode.value != "off" and not engines.online_available():
            note = "  (edge-tts is not installed: pip install 'samvada[online]')"
        print(f"online: {mode.value} ({mode.where}){note}")
        print("voices: " + "  ".join(str(d) for d in paths.voices_dirs()))
    print(f"session: {state.key or 'none (no Claude session, pys window or tty)'}")


LANG_USAGE = ("usage: samvada [--session] lang [--enable-<lang>]... [--disable-<lang>]... "
              "[--reset] [--list|-l]\n"
              "       samvada [--session] lang [on|off <lang>... | reset]")


def _unknown(lang: str, voices: dict) -> Usage:
    return Usage(f"unknown language {lang} (one of: {' '.join(voices)})")


def _lang_flags(args: list[str], voices: dict) -> tuple[dict[str, str], bool, bool, bool]:
    """--enable-zh --disable-ja --reset --list -s → ({zh: on, ja: off}, reset, list, session)."""
    changes: dict[str, str] = {}
    reset = listing = session = False
    for a in args:
        if a in ("--list", "-l"):
            listing = True
        elif a == "--reset":
            reset = True
        elif a in ("--session", "-s"):
            session = True
        elif a.startswith(("--enable-", "--disable-")):
            value = "on" if a.startswith("--enable-") else "off"
            lang = a.split("-", 3)[3]
            if lang not in voices:
                raise _unknown(lang, voices)
            if changes.get(lang, value) != value:
                raise Usage(f"{lang} is both enabled and disabled")
            changes[lang] = value
        else:
            raise Usage(f"unknown lang option {a}\n{LANG_USAGE}")
    return changes, reset, listing, session


def _lang_list(state: State, voices: dict) -> None:
    """One row per supported language: on or off, where that was set, its voice pack."""
    dirs = paths.voices_dirs()
    rows = [("lang", "voice", "set in", "voice pack")]
    for lang, voice in voices.items():
        s = state.lang(lang)
        where_ = s.where
        scopes = state.lang_scopes(lang)
        if s.where == "session" and "global" in scopes:
            where_ += f" (global: {scopes['global']})"
        found = engines.voice_home(voice, dirs)
        pack = f"installed in {found}" if found else f"not installed: samvada voices add {lang}"
        rows.append((lang, s.value, where_, pack))
    widths = [max(len(r[i]) for r in rows) for i in range(3)]
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)) + "  " + r[3])
    print(f"session: {state.key or 'none (no Claude session, pys window or tty)'}")


def _lang(rest: list[str], session: bool, state: State, voices: dict) -> int:
    if rest[:1] in (["-h"], ["--help"]):
        print(LANG_USAGE)
        return 0
    listing = False
    if not rest:
        pass
    elif rest[0] == "reset" and len(rest) == 1:  # the older form
        state.reset_langs(session)
    elif rest[0] in ("on", "off") and len(rest) >= 2:
        unknown = [l for l in rest[1:] if l not in voices]
        if unknown:
            raise _unknown(unknown[0], voices)
        state.set_langs(session, rest[0], rest[1:])
    elif rest[0].startswith("-"):
        changes, reset, listing, in_session = _lang_flags(rest, voices)
        session = session or in_session
        if session and (reset or changes):
            state.scope_dir(True)  # no session here: say so before anything changes
        if reset:
            state.reset_langs(session)
        if changes:
            state.apply_langs(session, changes)
    else:
        raise Usage(LANG_USAGE)
    if listing:
        _lang_list(state, voices)
    else:
        _show(state, voices, full=False)
    return 0


def _pairs(args: list[str], voices: dict) -> list[tuple[str, str]]:
    if not args or len(args) % 2:
        raise Usage(f'usage: samvada speak <{"|".join(voices)}> "text" [<lang> "text" ...]')
    pairs = list(zip(args[::2], args[1::2]))
    for lang, _ in pairs:
        if lang not in voices:
            raise Usage(f"unknown language {lang} (one of: {' '.join(voices)})")
    return pairs


def run(argv: list[str]) -> int:
    env = os.environ
    voices = engines.load_voices()
    state = State(paths.state_dir(env), session_key(env), legacy=paths.legacy_state_dir(env))
    session = bool(argv) and argv[0] in ("--session", "-s")
    if session:
        argv = argv[1:]
    cmd, rest = (argv[0], argv[1:]) if argv else ("", [])

    if cmd in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if cmd == "version":
        print(f"samvada {__version__}")
        return 0
    if cmd == "status":
        print(where(state, _phone_marks(state)))
        _show(state, voices, full=True)
        return 0
    if cmd == "mute":
        state.mute(session, rest[0] if rest else "muted by hand")
        print(where(state, _phone_marks(state)))
        return 0
    if cmd == "unmute":
        state.unmute(session)
        print(where(state, _phone_marks(state)))
        return 0
    if cmd == "lang":
        return _lang(rest, session, state, voices)
    if cmd == "online":
        if rest:
            if len(rest) != 1 or rest[0] not in ONLINE_MODES:
                raise Usage(f"usage: samvada [--session] online [{'|'.join(ONLINE_MODES)}]")
            state.set_online(session, rest[0])
            if rest[0] != "off" and not engines.online_available():
                say("edge-tts is not installed; the online mode needs: pip install 'samvada[online]'")
        mode = state.online()
        print(f"online: {mode.value} ({mode.where})")
        return 0
    if cmd == "presence":
        if rest == ["phone"]:
            state.mark_phone()
        elif rest:
            raise Usage("usage: samvada presence [phone]")
        print(where(state, _phone_marks(state)))
        return 0
    if cmd == "voices":
        return _voices(rest, voices)
    if cmd == "setup":
        from .agents import setup
        return setup.main(rest)
    if cmd == "hook":
        from .agents import hooks
        if rest not in (["prompt"], ["stop"]):
            raise Usage("usage: samvada hook prompt|stop")
        return hooks.run(rest[0], state, lambda st: where(st, _phone_marks(st)))
    if session:
        raise Usage("--session goes with mute, unmute, lang or online")

    save = None
    if cmd == "speak":
        argv = rest
        if argv[:1] == ["--save"]:
            if len(argv) < 2:
                raise Usage("usage: samvada speak --save DIR <lang> \"text\" ...")
            save, argv = Path(argv[1]), argv[2:]
    pairs = _pairs(argv, voices)
    from .agents.hooks import mark_spoke
    mark_spoke(state)  # the agent spoke this turn, whatever is heard (the Stop hook reads it)
    if save is None:  # saving plays nothing, so where the owner is does not matter
        decision = where(state, _phone_marks(state))
        if not decision.on:
            say(f"silent, {decision.why}")
            return 0
    return speak(pairs, state=state, voices=voices, voices_dirs=paths.voices_dirs(env), env=env,
                 save=save)


def _voices(rest: list[str], voices: dict) -> int:
    lock = packs.load_lock()
    home = paths.install_voices_dir()
    sub, args = (rest[0], rest[1:]) if rest else ("list", [])
    engine_mode = "auto"
    if "--engine" in args:
        i = args.index("--engine")
        if i + 1 >= len(args) or args[i + 1] not in ("auto", "always", "never"):
            raise Usage("--engine auto|always|never")
        engine_mode = args[i + 1]
        del args[i:i + 2]
    unknown = [l for l in args if l not in voices]
    if unknown:
        raise Usage(f"unknown language {unknown[0]} (one of: {' '.join(voices)})")

    if sub == "list" and not args:
        dirs = paths.voices_dirs()
        for lang, voice in voices.items():
            found = engines.voice_home(voice, dirs)
            where_ = f"installed in {found}" if found else "not installed"
            print(f"{lang}  {where_}  ({packs.sizes_line(lock, lang)})  "
                  f"engine {voice.engine}: {engines.engine_where(voice.engine)}")
        print(f"install folder: {home}")
        return 0
    if sub == "add" and args:
        total = sum(lock.size(l) for l in args)
        print(f"samvada: installing {' '.join(args)} into {home} (up to {packs.mb(total)})")
        status = 0
        for lang in args:
            try:
                got = packs.add(lang, lock, home, os.environ)
                print(f"{lang}: {'installed' if got else 'already installed'}, verified")
                engine = voices[lang].engine
                where_ = engines.engine_where(engine)
                if where_ != "samvada" and (engine_mode == "always"
                                            or (engine_mode == "auto" and where_ == "missing")):
                    packs.install_engine(engine, paths.engines_dir())
                    print(f"{lang}: engine {engine} installed in {paths.engines_dir() / engine}")
            except packs.PackError as e:
                say(f"{lang}: {e}")
                status = 1
        return status
    if sub == "verify":
        status = 0
        for lang in args or list(voices):
            if lang not in packs.installed(home):
                print(f"{lang}: not installed by samvada in {home}; not verified")
                continue
            problems = packs.verify(lang, lock, home)
            print(f"{lang}: " + ("verified" if not problems else "; ".join(problems)))
            status |= bool(problems)
        return status
    if sub == "remove" and args:
        for lang in args:
            packs.remove(lang, lock, home)
            print(f"{lang}: removed from {home}")
        return 0
    raise Usage("usage: samvada voices [list | add <lang>... | verify [<lang>...] | remove <lang>...]")


def _safe_streams() -> None:
    """Never die on our own output: a Windows console or pipe is often cp1252, which has no
    "←" or Chinese. What it cannot show is replaced; the work goes on."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _safe_streams()
    try:
        return run(list(sys.argv[1:] if argv is None else argv))
    except Usage as e:
        print(f"samvada: {e}", file=sys.stderr)
        return 2
    except LookupError as e:
        say(str(e))
        return 2


if __name__ == "__main__":
    sys.exit(main())
