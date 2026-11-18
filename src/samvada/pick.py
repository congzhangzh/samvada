"""`samvada voices pick`: list the voice files for a language, listen, choose (DESIGN 6.1).

The choice goes into the profile in force, `~/.config/samvada/profiles/<name>.toml`; the
owner's `voices.local.toml` stays theirs and wins over every profile. The screen is curses (Python's own); where
there is no curses or no terminal (Windows, a pipe), a numbered prompt does the same.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
import tomllib
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from importlib import resources

from . import engines, kokoro, paths, player, voicevox

SAMPLES = {
    "en": "Hello. This is the voice that reads your corrections each day. "
          "The rain stopped at dusk, and the city grew quiet and warm.",
    "zh": "你好。这是每天为你读改错的声音。黄昏时雨停了，城市变得安静而温暖。",
    "ja": "こんにちは。毎日あなたの添削を読む声です。夕暮れに雨がやみ、街は静かで温かくなりました。",
    "fr": "Bonjour. Voici la voix qui lit tes corrections chaque jour. "
          "La pluie s'est arrêtée au crépuscule, et la ville est devenue calme et douce.",
}
DEFAULT_SAMPLE = "Hello. This is a sample of this voice."


@dataclass
class Candidate:
    model: str          # what goes into the map: a bare name in a voices folder, or a path
    path: Path          # the file
    license: str = ""   # from a MODEL_CARD beside it, when there is one
    note: str = ""      # facts about the voice (voice_notes.toml)
    terms: str = ""
    mine: str = ""      # the owner's own note (voice-notes.toml)
    engine: str = ""    # "" : the language's engine
    style: str = ""     # VOICEVOX: the style id
    label: str = ""     # VOICEVOX: speaker and style; else the file name
    key: str = ""       # what notes are kept under; else the file name
    credit: str = ""

    @property
    def name(self) -> str:
        return self.label or self.path.name

    @property
    def note_key(self) -> str:
        return self.key or self.path.name

    def same(self, voice: engines.Voice) -> bool:
        """Is this the language's voice now?"""
        return ((self.model == voice.model or str(self.path) == voice.model)
                and (self.style or "") == (voice.style if voice.engine in engines.PY_ENGINES else ""))


def facts() -> dict[str, dict[str, str]]:
    return tomllib.loads(resources.files("samvada").joinpath("voice_notes.toml")
                         .read_text(encoding="utf-8"))


def owner_notes_file(env: Mapping[str, str] = os.environ) -> Path:
    """The owner's notes on how each voice sounds, written by the picker (`n`)."""
    return paths.config_dir(env) / "voice-notes.toml"


def owner_notes(env: Mapping[str, str] = os.environ) -> dict[str, str]:
    try:
        d = tomllib.loads(owner_notes_file(env).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return {k: v.get("note", "") for k, v in d.items() if isinstance(v, dict)}


def save_note(c: Candidate, text: str, env: Mapping[str, str] = os.environ) -> Path:
    notes = owner_notes(env)
    if text.strip():
        notes[c.note_key] = text.strip()
    else:
        notes.pop(c.note_key, None)
    f = owner_notes_file(env)
    lines = ["# Your notes on how each voice sounds, by file name (VOICEVOX: voicevox:<speaker>/<style>);",
             "# `n` in `samvada voices pick`",
             "# writes them (you may edit this file too).", ""]
    for name, note in notes.items():
        lines += [f"[{_toml_value(name)}]", f"note = {_toml_value(note)}", ""]
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("\n".join(lines), encoding="utf-8")
    c.mine = text.strip()
    return f


def _annotate(cs: list[Candidate], env: Mapping[str, str]) -> list[Candidate]:
    known, mine = facts(), owner_notes(env)
    for c in cs:
        f = known.get(c.note_key, {})
        c.note = c.note or f.get("note", "")
        c.terms = c.terms or f.get("terms", "")
        c.mine = mine.get(c.note_key, "")
    return cs


def describe(c: Candidate) -> list[str]:
    """The lines shown under the list for the voice at the cursor."""
    out = [f"about: {c.note or 'no notes on this file yet'}"]
    terms = c.terms or c.license
    if terms:
        out.append(f"terms: {terms}")
    if c.credit:
        out.append(f"credit: {c.credit}")
    out.append(f"yours: {c.mine or '(none: n to write one)'}")
    out.append(f"file:  {c.path}" + (f"  style {c.style}" if c.style else ""))
    return out


def _card_license(model: Path) -> str:
    """The `License:` line of a Piper MODEL_CARD beside the model (<name>.MODEL_CARD or
    MODEL_CARD), else ""."""
    for card in (model.with_suffix(".MODEL_CARD"), model.parent / "MODEL_CARD"):
        try:
            m = re.search(r"License:\s*(.+)", card.read_text(encoding="utf-8"))
        except OSError:
            continue
        if m:
            return m.group(1).strip()
    return ""


def candidates(lang: str, voice: engines.Voice, dirs: list[Path],
               extra: list[Path] = ()) -> list[Candidate]:
    """The language's voice now first, then each `<lang>_*.onnx` with its `.onnx.json` in the
    voices folders and the extra folders (Piper's naming), and any `.onnx` with its `.onnx.json`
    in an extra folder's `<lang>/` subfolder (the folder names the language), each file once."""
    out: list[Candidate] = []
    seen: set = set()
    # an .onnx file here is a Piper (or Piper Plus) voice, whatever speaks the language now
    onnx_engine = voice.engine if voice.engine in ("piper", "piper-plus") else engines.default_engine(lang)
    if onnx_engine not in ("piper", "piper-plus"):
        onnx_engine = "piper"
    now = engines.voice_home(voice, dirs)
    if now is not None:
        p = (now / voice.model).resolve()
        if voice.engine == "voicevox":
            out += [c for c in _vv(lang, [p], voice.model) if c.style == voice.style]
            seen |= {(p, c.style) for c in out}
        elif voice.engine == "kokoro":
            out += [c for c in _kk(lang, [p], voice.model) if c.style == voice.style]
            seen |= {(p, c.style) for c in out}
        else:
            out.append(Candidate(voice.model, p, voice.license or _card_license(p),
                                 engine=voice.engine, credit=voice.credit))
            seen.add(p)
    base = engines.default_voice(lang)  # the package's own voice: always a way back to it
    home = engines.voice_home(base, dirs) if base and base.engine != "voicevox" else None
    if home is not None and (home / base.model).resolve() not in seen:
        p = (home / base.model).resolve()
        out.append(Candidate(base.model, p, base.license or _card_license(p), engine=base.engine,
                             credit=base.credit))
        seen.add(p)
    places = [(d, f"{lang}_*.onnx") for d in [*dirs, *extra]]
    places += [(d / lang, "*.onnx") for d in extra]  # a folder per language: <dir>/en/, <dir>/ja/
    for d, pattern in places:
        if not d.is_dir():
            continue
        for f in sorted(d.glob(pattern)):
            r = f.resolve()
            if r in seen or f.name.endswith(".opt.onnx") or not Path(f"{f}.json").is_file():
                continue
            seen.add(r)
            model = f.name if d in dirs else str(r)
            out.append(Candidate(model, r, _card_license(r), engine=onnx_engine))
    if lang in voicevox.LANGS:  # VOICEVOX model files: in <voices>/voicevox/, <dir>/<lang>/, <dir>
        vvms: list[Path] = []
        for d in [*(x / "voicevox" for x in dirs), *(x / lang for x in extra)]:
            if d.is_dir():
                vvms += sorted(d.rglob("*.vvm"))
        for d in extra:
            if d.is_dir():
                vvms += sorted(d.glob("*.vvm"))
        for c in _vv(lang, vvms):
            if (c.path, c.style) not in seen:
                seen.add((c.path, c.style))
                out.append(c)
    if lang in kokoro.PREFIXES:  # Kokoro model files: kokoro*.onnx with a voices*.bin beside
        models: list[Path] = []
        for d in dirs:
            if (d / "kokoro").is_dir():
                models += sorted((d / "kokoro").rglob("kokoro*.onnx"))
        for d in extra:
            for place in (d, d / lang):
                if place.is_dir():
                    models += sorted(place.glob("kokoro*.onnx"))
                    models += sorted(place.glob("kokoro*/kokoro*.onnx"))
        for c in _kk(lang, models):
            if (c.path, c.style) not in seen:
                seen.add((c.path, c.style))
                out.append(c)
    return out


def _kk(lang: str, models: list[Path], model: str | None = None) -> list[Candidate]:
    """One candidate per Kokoro voice of this language in the model files."""
    out = []
    for f in models:
        f = f.resolve()
        for v in kokoro.voices(f, lang):
            out.append(Candidate(model or str(f), f, engine="kokoro", style=v.name,
                                 label=f"{v.name}  (Kokoro {v.version})", key=v.key,
                                 terms=kokoro.TERMS, note=v.note))
    return out


def _vv(lang: str, vvms: list[Path], model: str | None = None) -> list[Candidate]:
    """One candidate per speaking style in the model files, with its terms and credit."""
    out, terms_of = [], {}
    for f in vvms:
        f = f.resolve()
        if f.parent not in terms_of:
            terms_of[f.parent] = voicevox.terms(f)
        known = terms_of[f.parent]
        for st in voicevox.styles(f):
            t, credit = known.get(st.speaker, ("", ""))
            if not t and f.name.startswith("n") and "VOICEVOX Nemo" in known:
                t, credit = known["VOICEVOX Nemo"]  # n*.vvm: the VOICEVOX Nemo voices
            note = f"VOICEVOX voice {st.speaker}, style {st.style}"
            if not t:
                note += "; its terms are not in the downloader's README: see voicevox.hiroshiba.jp"
            out.append(Candidate(model or str(f), f, engine="voicevox", style=str(st.id),
                                 label=st.label, key=st.key, terms=t, credit=credit, note=note))
    return out


def _tmpdir() -> tempfile.TemporaryDirectory:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    return tempfile.TemporaryDirectory(
        prefix="samvada-pick.", dir=runtime if runtime and os.path.isdir(runtime) else None)


def _synth(voice: engines.Voice, c: Candidate, out: Path, text: str | None) -> bool:
    trial = replace(voice, model=str(c.path), engine=c.engine or voice.engine, style=c.style)
    home = engines.voice_home(voice, paths.voices_dirs()) or c.path.parent
    return engines.offline(trial, text or SAMPLES.get(voice.lang, DEFAULT_SAMPLE), out, home)


def listen(voice: engines.Voice, c: Candidate, text: str | None = None) -> bool:
    """Speak the language's sample with this file and play it to the end; False when either
    fails (the numbered prompt)."""
    with _tmpdir() as t:
        out = Path(t, "sample.wav")
        return _synth(voice, c, out, text) and player.play(out)


class Listener:
    """Plays samples in the background for the screen: `play` returns at once, and a new
    sample stops the one before it (made or playing). `status` says what it is doing."""

    def __init__(self) -> None:
        self._tmp = _tmpdir()
        self._lock = threading.Lock()
        self._gen = 0
        self._proc = None
        self.status = ""

    def play(self, voice: engines.Voice, c: Candidate, text: str | None = None) -> None:
        with self._lock:
            self._gen += 1
            gen = self._gen
            self._stop_locked()
            self.status = f"making {c.name} …"
        threading.Thread(target=self._run, args=(gen, voice, c, text), daemon=True).start()

    def _run(self, gen: int, voice: engines.Voice, c: Candidate, text: str | None) -> None:
        out = Path(self._tmp.name, f"{gen}.wav")
        ok = _synth(voice, c, out, text)
        with self._lock:
            if gen != self._gen:
                return  # a newer sample was asked for while this one was being made
            if not ok:
                self.status = f"{c.name}: could not speak it"
                return
            self._proc = player.start(out)
            proc = self._proc
            if proc is None:  # Windows, or no player that can be stopped: play to the end
                self.status = f"playing {c.name} (cannot be stopped here)"
            else:  # set with the process, under the lock, so no one reads a stale status
                self.status = f"playing {c.name}   (space again: replay)"
        if proc is None:
            done = player.play(out)
        else:
            done = proc.wait() == 0
        with self._lock:
            if gen == self._gen:
                self._proc = None
                self.status = f"played {c.name}" if done else f"{c.name}: no player could play it"

    def _stop_locked(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except Exception:
                self._proc.kill()
        self._proc = None

    def stop(self) -> None:
        with self._lock:
            self._gen += 1
            self._stop_locked()
            self.status = "stopped"

    def close(self) -> None:
        self.stop()
        self._tmp.cleanup()


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return json.dumps(str(v), ensure_ascii=False)  # a JSON string is a TOML basic string


def dump_map(data: dict) -> list[str]:
    """A voice map as TOML lines: each language's keys, then its [lang.env] table; nothing
    of what tomllib read is dropped."""
    lines: list[str] = []
    for lang, t in data.items():
        lines.append(f"[{lang}]")
        subs = {k: v for k, v in t.items() if isinstance(v, dict)}
        lines += [f"{k} = {_toml_value(v)}" for k, v in t.items() if not isinstance(v, dict)]
        lines.append("")
        for k, sub in subs.items():
            lines.append(f"[{lang}.{k}]")
            lines += [f"{kk} = {_toml_value(vv)}" for kk, vv in sub.items()]
            lines.append("")
    return lines


def save(lang: str, c: Candidate, env: Mapping[str, str] = os.environ) -> Path:
    """Record the choice in the owner's file of the profile in force (its other languages
    stay as they were)."""
    name, _ = engines.active_profile(env)
    src = paths.profile_file(name, env)  # for `like`, maybe the older voices.toml
    f = paths.profiles_dir(env) / f"{name}.toml"
    try:
        data = tomllib.loads(src.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        data = {}
    old = data.get(lang, {})
    keep = {"env": old["env"]} if "env" in old and old.get("engine") in (None, c.engine) else {}
    data[lang] = {"engine": c.engine, "model": c.model} if c.engine else {"model": c.model}
    if c.style:
        data[lang]["style"] = c.style
    data[lang]["license"] = (c.license or c.terms
                             or "unknown: chosen with samvada voices pick; see its model card")
    data[lang]["credit"] = c.credit  # "" clears the credit of the voice it replaces
    data[lang].update(keep)  # the owner's [lang.env] stays while the engine does
    lines = [f"# Profile {name}: written by `samvada voices pick` while {name} is in force; it",
             "# rewrites this file. Your own changes go in ../voices.local.toml, which wins over",
             "# every profile.", ""] + dump_map(data)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("\n".join(lines), encoding="utf-8")
    return f


def overridden(lang: str, env: Mapping[str, str] = os.environ) -> str:
    """A line saying the owner's own file sets this language's model, else ""."""
    own = paths.owner_voices_file(env)
    try:
        t = tomllib.loads(own.read_text(encoding="utf-8")).get(lang, {})
    except (OSError, tomllib.TOMLDecodeError):
        return ""
    return f"note: {own} sets {lang}.model, and it wins over a pick" if "model" in t else ""


def terms_short(c: Candidate) -> str:
    """The terms in a few words, for a table (the full text stays in `describe`). A summary
    by rule of thumb; the voice's own terms decide."""
    t = c.terms or c.license
    low = t.lower()
    if not t:
        return "unknown"
    if "非商用（" in t or "non-commercial" in low or "-nc-" in low or " nc " in low:
        return "non-commercial only"
    if "除いて" in t:
        return "free, with exceptions; companies ask first" if "企業" in t else "free, with exceptions"
    if "商用利用の場合は" in t and "問い合わせ" in t:
        return "non-commercial; commercial: ask first"
    if "企業" in t or "法人" in t:
        return "free for people; companies ask first"
    if "by-sa" in low:
        return "free, credit, share alike"
    if "public domain" in low or "cc0" in low:
        return "free, no conditions"
    if "商用・非商用" in t or "commercial use allowed" in low or "cc by 4.0" in low or "cc-by 4.0" in low:
        return "free, with credit"
    if "research" in low:
        return "research licence"
    if "apache" in low:
        return "free (Apache-2.0)"
    return t[:40]


def about_short(c: Candidate) -> str:
    """The first part of the facts: who speaks and the accent, cut at a word; for a VOICEVOX
    style (whose name already says who) its credit."""
    if c.engine == "voicevox":
        return c.credit or "credit: see its terms"
    if c.engine == "kokoro":  # "Kokoro v1.0, Mandarin Chinese, female; … grade …: C | MM minutes | D"
        sex = c.note.split(", ")[2].split(";")[0] if c.note.count(", ") >= 2 else ""
        grade = c.note.rsplit(": ", 1)[1].rsplit("| ", 1)[-1] if "grade" in c.note else ""
        extra = f"grade {grade}" if grade else ("pro data, >100 h" if "LongMaoData" in c.note
                                                 else "synthetic data" if "synthetic" in c.note else "")
        return f"Kokoro, {sex}" + (f", {extra}" if extra else "")
    text = (c.note or "").split(", trained")[0].split(" (")[0].split(";")[0]
    if len(text) <= 60:
        return text
    return text[:60].rsplit(" ", 1)[0].rstrip(",:") + " …"


def table(langs: list[str], voices: dict[str, engines.Voice],
          lists: dict[str, list[Candidate]]) -> str:
    """Every tab's voices as Markdown tables; VOICEVOX grouped by speaker (styles in a row)."""
    out = []
    for lang in langs:
        items = lists[lang]
        out += [f"## {lang} ({len(items)} voices)", ""]
        files = [c for c in items if c.engine != "voicevox"]
        if files:
            out += ["| now | voice | engine | about | terms | yours |", "|---|---|---|---|---|---|"]
            out += [f"| {'●' if c.same(voices[lang]) else ''} | {c.name} | {c.engine or voices[lang].engine} "
                    f"| {about_short(c)} | {terms_short(c)} | {c.mine} |" for c in files]
            out.append("")
        vv: dict[str, list[Candidate]] = {}
        for c in items:
            if c.engine == "voicevox":  # key: voicevox:<speaker>/<style>
                vv.setdefault(c.key.split(":", 1)[1].rsplit("/", 1)[0], []).append(c)
        if vv:
            out += [f"VOICEVOX: {len(vv)} speakers, {sum(map(len, vv.values()))} styles", "",
                    "| now | speaker | styles (id) | terms | credit |", "|---|---|---|---|---|"]
            for speaker, cs in vv.items():
                now = "●" if any(c.same(voices[lang]) for c in cs) else ""
                st = ", ".join(f"{c.key.rsplit('/', 1)[1]} ({c.style})" for c in cs)
                out.append(f"| {now} | {speaker} | {st} | {terms_short(cs[0])} | {cs[0].credit} |")
            out.append("")
    return "\n".join(out)


def cells(text: str) -> int:
    """How many terminal columns the text takes: CJK characters take two."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def pad(text: str, width: int) -> str:
    """Cut or pad the text to exactly `width` terminal columns."""
    out, used = [], 0
    for ch in text:
        n = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if used + n > width:
            break
        out.append(ch)
        used += n
    return "".join(out) + " " * (width - used)


def columns(w: int) -> list[tuple[str, int]]:
    """The screen's table columns for a terminal `w` wide: (title, width); the last takes
    the rest."""
    cols = [("now", 4), ("voice", 40), ("engine", 11)]
    if w >= 130:
        cols += [("about", 40), ("terms", 28)]
    else:
        cols += [("terms", 28)]
    used = sum(n for _, n in cols)
    return cols + [("yours" if w >= 130 else "yours / about", max(0, w - used - 1))]


def row(c: Candidate, voice: engines.Voice, cols: list[tuple[str, int]]) -> str:
    cell = {"now": " ●" if c.same(voice) else "", "voice": c.name, "engine": c.engine or voice.engine,
            "about": about_short(c),
            "terms": terms_short(c), "yours": c.mine,
            "yours / about": c.mine or about_short(c)}
    return "".join(pad(cell[t], n - 1) + " " if n else "" for t, n in cols)  # a space between cells


# ---- the screens -------------------------------------------------------------------------

BUTTONS = (("listen", "▶ listen"), ("stop", "■ stop"), ("use", "✓ use"), ("note", "✎ note"),
           ("prev", "◀"), ("next", "▶"), ("quit", "✕ quit"))


def _tui(voices: dict[str, engines.Voice], langs: list[str], lists: dict[str, list[Candidate]],
         env: Mapping[str, str]) -> int:
    import curses

    keys = "keys: ↑↓ PgUp PgDn  space listen  s stop  enter use  n note  ←→ language  q quit"
    mouse = "mouse: click listens, double-click uses, wheel scrolls (Shift+drag selects text)"
    sound = Listener()

    def main(scr) -> int:
        curses.curs_set(0)
        scr.timeout(150)  # redraw while a sample is made or played, so its state shows
        wheel_down = getattr(curses, "BUTTON5_PRESSED", 0)
        curses.mousemask(curses.BUTTON1_CLICKED | curses.BUTTON1_DOUBLE_CLICKED
                         | curses.BUTTON4_PRESSED | wheel_down)
        curses.mouseinterval(250)
        li, pos, top = 0, {l: 0 for l in langs}, {l: 0 for l in langs}
        msg = ""
        while True:
            lang = langs[li]
            items = lists[lang]
            scr.erase()
            h, w = scr.getmaxyx()
            rows = max(1, h - 12)
            if h < 14 or w < 60:  # too small for the table: say so instead of crashing
                scr.erase()
                scr.addnstr(0, 0, f"terminal too small ({w}x{h}); 60x14 at least; q quits", max(1, w - 1))
                scr.refresh()
                k = scr.getch()
                if k in (ord("q"), 27):
                    return 0
                continue
            # the title and the language tabs, each tab's columns kept for a click
            title = f"samvada voices pick · profile {engines.active_profile(env)[0]}   "
            scr.addnstr(0, 0, title, w - 1, curses.A_BOLD)
            tabs, x = {}, len(title)
            for l in langs:
                label = f"[{l}]" if l == lang else f" {l} "
                if x + len(label) < w:
                    scr.addstr(0, x, label, curses.A_BOLD | (curses.A_REVERSE if l == lang else 0))
                tabs[l] = (x, x + len(label))
                x += len(label) + 2
            if pos[lang] < top[lang]:  # scroll so the cursor stays on the screen
                top[lang] = pos[lang]
            elif pos[lang] >= top[lang] + rows:
                top[lang] = pos[lang] - rows + 1
            shown = items[top[lang]: top[lang] + rows]
            cols = columns(w)
            head = "".join(pad(t, n - 1) + " " if n else "" for t, n in cols)
            if len(items) > rows:
                where_ = f" {pos[lang] + 1}/{len(items)}"
                head = pad(head, max(0, w - 1 - len(where_))) + where_
            scr.addnstr(1, 0, head, w - 1, curses.A_UNDERLINE | curses.A_BOLD)
            for i, c in enumerate(shown):
                scr.addnstr(2 + i, 0, row(c, voices[lang], cols), w - 1,
                            curses.A_REVERSE if top[lang] + i == pos[lang] else curses.A_NORMAL)
            if not items:
                scr.addnstr(2, 0, f"no voice files for {lang}", w - 1)
            else:
                for j, d in enumerate(describe(items[pos[lang]])):
                    scr.addnstr(3 + len(shown) + j, 2, d, w - 3)
            note = overridden(lang, env)
            if note:
                scr.addnstr(h - 5, 0, note, w - 1)
            scr.addnstr(h - 4, 0, "● the voice now", w - 1, curses.A_DIM)
            scr.addnstr(h - 3, 0, msg or sound.status, w - 1)
            # the buttons, each one's columns kept for a click
            buttons, x = {}, 0
            for name, label in BUTTONS:
                text = f"[ {label} ]"
                if x + len(text) < w:
                    scr.addstr(h - 2, x, text, curses.A_REVERSE)
                buttons[name] = (x, x + len(text))
                x += len(text) + 1
            scr.addnstr(h - 1, 0, keys if w < len(keys) + len(mouse) + 3 else f"{keys}   {mouse}",
                        w - 1, curses.A_DIM)
            scr.refresh()
            k = scr.getch()
            if k == -1:
                continue
            msg = ""
            action = None
            if k == curses.KEY_MOUSE:
                try:
                    _, mx, my, _, b = curses.getmouse()
                except curses.error:
                    continue
                if b & curses.BUTTON4_PRESSED:
                    action = "up3"
                elif wheel_down and b & wheel_down:
                    action = "down3"
                elif my == 0:
                    for l, (a, z) in tabs.items():
                        if a <= mx < z:
                            li = langs.index(l)
                elif my == h - 2:
                    for name, (a, z) in buttons.items():
                        if a <= mx < z:
                            action = name
                elif 2 <= my < 2 + len(shown):
                    pos[lang] = top[lang] + my - 2
                    action = "use" if b & curses.BUTTON1_DOUBLE_CLICKED else "listen"
                if action is None:
                    continue
            else:
                action = {ord("q"): "quit", 27: "quit", ord("s"): "stop", ord(" "): "listen",
                          ord("n"): "note", 10: "use", 13: "use", curses.KEY_ENTER: "use",
                          curses.KEY_UP: "up", ord("k"): "up", curses.KEY_DOWN: "down",
                          ord("j"): "down", curses.KEY_NPAGE: "pgdn", curses.KEY_PPAGE: "pgup",
                          curses.KEY_LEFT: "prev", ord("h"): "prev", curses.KEY_RIGHT: "next",
                          ord("l"): "next", 9: "next"}.get(k)
            if action == "quit":
                return 0
            if action == "stop":
                sound.stop()
            elif action == "prev":
                li = (li - 1) % len(langs)
            elif action == "next":
                li = (li + 1) % len(langs)
            elif not items or action is None:
                continue
            elif action == "up":
                pos[lang] = (pos[lang] - 1) % len(items)
            elif action == "down":
                pos[lang] = (pos[lang] + 1) % len(items)
            elif action in ("up3", "down3"):  # the wheel scrolls the list, the cursor follows
                step = -3 if action == "up3" else 3
                pos[lang] = min(len(items) - 1, max(0, pos[lang] + step))
            elif action == "pgdn":
                pos[lang] = min(len(items) - 1, pos[lang] + rows)
            elif action == "pgup":
                pos[lang] = max(0, pos[lang] - rows)
            elif action == "listen":
                sound.play(voices[lang], items[pos[lang]])
            elif action == "note":
                c = items[pos[lang]]
                scr.addnstr(h - 3, 0, f"your note on {c.name}: ".ljust(w - 1), w - 1)
                curses.echo()
                curses.curs_set(1)
                scr.timeout(-1)
                text = scr.getstr(h - 3, min(w - 2, len(c.name) + 15), 200).decode("utf-8", "replace")
                scr.timeout(150)
                curses.noecho()
                curses.curs_set(0)
                msg = f"note saved in {save_note(c, text, env)}"
            elif action == "use":
                c = items[pos[lang]]
                f = save(lang, c, env)
                voices[lang] = replace(voices[lang], model=c.model, engine=c.engine or voices[lang].engine,
                                       style=c.style)
                msg = f"{lang} now uses {c.name} (saved in {f})"

    try:
        return curses.wrapper(main)
    finally:
        sound.close()


def _prompt(voices: dict[str, engines.Voice], langs: list[str],
            lists: dict[str, list[Candidate]], env: Mapping[str, str]) -> int:
    for lang in langs:
        items = lists[lang]
        while True:
            print(f"\n{lang}:")
            for i, c in enumerate(items, 1):
                mark = "●" if c.same(voices[lang]) else " "
                print(f"  {i} {mark} {c.name}")
                for d in describe(c)[:-1]:
                    print(f"        {d}")
            note = overridden(lang, env)
            if note:
                print(f"  {note}")
            try:
                a = input("number to listen, u <number> to use, n <number> <note>, "
                          "enter: next language, q quit: ").split()
            except EOFError:
                return 0
            if not a:
                break
            if a[0] == "q":
                return 0
            if a[0] == "n" and len(a) >= 2:
                try:
                    c = items[int(a[1]) - 1]
                except (ValueError, IndexError):
                    continue
                print(f"note saved in {save_note(c, ' '.join(a[2:]), env)}")
                continue
            use = a[0] == "u"
            try:
                c = items[int(a[-1]) - 1]
            except (ValueError, IndexError):
                continue
            if use:
                print(f"{lang} now uses {c.name} (saved in {save(lang, c, env)})")
                voices[lang] = replace(voices[lang], model=c.model, engine=c.engine or voices[lang].engine,
                                       style=c.style)
            elif not listen(voices[lang], c):
                print(f"{c.name}: could not speak or play it")
    return 0


def dirs_file(env: Mapping[str, str] = os.environ) -> Path:
    """The folders `--dir` named last time: the picker looks there when none is given."""
    return paths.state_dir(env) / "pick-dirs"


def _check_dir(d: Path) -> None:
    if d.is_dir():
        return
    import difflib
    near = difflib.get_close_matches(d.name, [x.name for x in d.parent.iterdir() if x.is_dir()]
                                     if d.parent.is_dir() else [], n=1)
    hint = f" (did you mean {d.parent / near[0]}?)" if near else ""
    raise LookupError(f"voices pick: no folder {d}{hint}")


def main(args: list[str], voices: dict[str, engines.Voice],
         env: Mapping[str, str] = os.environ) -> int:
    """`voices pick [<lang>...] [--dir DIR]... [--plain | --table]`."""
    extra, langs, plain, as_table = [], [], False, False
    it = iter(args)
    for a in it:
        if a == "--dir":
            d = next(it, None)
            if d is None:
                raise LookupError("--dir needs a folder")
            extra.append(Path(d).expanduser())
        elif a == "--plain":
            plain = True
        elif a == "--table":
            as_table = True
        elif a in voices:
            langs.append(a)
        else:
            raise LookupError(f"voices pick: unknown {a} (a language: {' '.join(voices)})")
    langs = langs or list(voices)
    for d in extra:  # a folder that is not there is a typo, never silence
        _check_dir(d)
    remembered = dirs_file(env)
    if extra:
        remembered.parent.mkdir(parents=True, exist_ok=True)
        remembered.write_text("".join(f"{d.resolve()}\n" for d in extra), encoding="utf-8")
    else:
        try:
            extra = [Path(l) for l in remembered.read_text(encoding="utf-8").splitlines() if l]
        except OSError:
            extra = []
        gone = [d for d in extra if not d.is_dir()]
        extra = [d for d in extra if d.is_dir()]
        if gone:
            print(f"samvada: the folders of last time are gone, not looked in: "
                  f"{' '.join(map(str, gone))}", file=sys.stderr)
    dirs = paths.voices_dirs(env)
    lists = {l: _annotate(candidates(l, voices[l], dirs, extra), env) for l in langs}
    if as_table:
        print(table(langs, voices, lists))
        return 0
    tty = sys.stdin.isatty() and sys.stdout.isatty()
    if not plain and tty:
        try:
            import curses  # noqa: F401
            return _tui(voices, langs, lists, env)
        except ImportError:
            pass
    return _prompt(voices, langs, lists, env)
