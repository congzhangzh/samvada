"""`samvada voices pick` and `samvada doctor`: the picker's own layer under the owner's file."""

from __future__ import annotations

from pathlib import Path

from samvada import engines, paths, pick


def cfg(env) -> Path:
    d = Path(env.vars["HOME"]) / ".config" / "samvada"
    d.mkdir(parents=True, exist_ok=True)
    return d


def full(env) -> dict:
    return {**env.vars, "XDG_CONFIG_HOME": str(Path(env.vars["HOME"]) / ".config")}


def trial(env) -> Path:
    d = env.tmp / "trial"
    d.mkdir()
    for n in ("en_GB-jenny_dioco-medium", "en_US-amy-medium"):
        (d / f"{n}.onnx").write_text("model")
        (d / f"{n}.onnx.json").write_text("{}")
    (d / "en_GB-jenny_dioco-medium.MODEL_CARD").write_text("* License: See URL\n")
    (d / "en_US-broken-medium.onnx").write_text("no json beside it")
    return d


def test_candidates_now_first_then_the_trial_folder(env):
    e = full(env)
    voices = engines.load_voices(env=e)
    got = pick.candidates("en", voices["en"], paths.voices_dirs(e), [trial(env)])
    assert [c.name for c in got] == ["en_US-lessac-high.onnx", "en_GB-jenny_dioco-medium.onnx",
                                     "en_US-amy-medium.onnx"]
    assert got[0].model == "en_US-lessac-high.onnx"           # in a voices folder: its name
    assert got[1].model == str(env.tmp / "trial" / "en_GB-jenny_dioco-medium.onnx")  # a path
    assert got[1].license == "See URL"


def test_a_pick_is_a_layer_and_the_owner_file_wins(env):
    e = full(env)
    voices = engines.load_voices(env=e)
    c = pick.candidates("en", voices["en"], paths.voices_dirs(e), [trial(env)])[1]
    f = pick.save("en", c, e)
    assert f == cfg(env) / "profiles" / "balanced.toml"  # the profile in force: balanced
    v = engines.load_voices(env=e)["en"]
    assert v.model == c.model and v.origin["model"] == "profile:balanced (yours)"
    assert v.license == "See URL" and v.origin["engine"] == "profile:balanced (yours)" and v.engine == "piper"
    assert v.origin["online"] == "default"
    assert pick.overridden("en", e) == ""
    (cfg(env) / "voices.local.toml").write_text('[en]\nmodel = "en_US-lessac-high.onnx"\n')
    v = engines.load_voices(env=e)["en"]
    assert v.model == "en_US-lessac-high.onnx" and v.origin["model"] == "yours"
    assert "voices.local.toml sets en.model" in pick.overridden("en", e)


def test_a_second_pick_keeps_the_other_languages(env):
    e = full(env)
    voices = engines.load_voices(env=e)
    d = trial(env)
    pick.save("fr", pick.Candidate("fr_FR-siwis-medium.onnx", d / "x", "CC-BY 4.0"), e)
    pick.save("en", pick.candidates("en", voices["en"], paths.voices_dirs(e), [d])[2], e)
    v = engines.load_voices(env=e)
    assert v["fr"].origin["model"] == "profile:balanced (yours)" and v["en"].model.endswith("en_US-amy-medium.onnx")


def test_pick_plain_plays_then_uses(env):
    d = trial(env)
    r = env.run("voices", "pick", "en", "--dir", str(d), "--plain", input="2\nu 2\nq\n")
    assert r.returncode == 0, r.stderr
    log = env.lines()
    assert any(l.startswith(f"piper {d}/en_GB-jenny_dioco-medium.onnx") for l in log)
    assert any(l.startswith("play sample.wav") for l in log)
    assert "en now uses en_GB-jenny_dioco-medium.onnx" in r.stdout
    assert "en_GB-jenny_dioco-medium.onnx" in (cfg(env) / "profiles" / "balanced.toml").read_text()
    r = env.run("speak", "en", "hi")
    assert any(l.startswith(f"piper {d}/en_GB-jenny_dioco-medium.onnx") for l in env.lines())


def jsons(env) -> None:
    for m in env.voices.glob("*.onnx"):
        Path(f"{m}.json").write_text("{}")


def test_doctor_all_well(env):
    jsons(env)
    r = env.run("doctor")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "✓  en  (on, default)  piper  en_US-lessac-high.onnx  [default]" in r.stdout
    assert "player: paplay" in r.stdout


def test_doctor_names_a_missing_model_and_a_broken_file(env):
    jsons(env)
    (env.voices / "fr_FR-siwis-medium.onnx").unlink()
    r = env.run("doctor")
    assert r.returncode == 1
    assert "✗  fr" in r.stdout and "samvada voices add fr" in r.stdout
    (cfg(env) / "voices.local.toml").write_text("[en]\nmodle = 1\n")
    r = env.run("doctor")
    assert r.returncode == 2 and "unknown key modle" in r.stdout


def test_notes_facts_and_the_owner_own(env):
    d = trial(env)
    r = env.run("voices", "pick", "en", "--dir", str(d), "--plain",
                input="n 2 so sweet, my favourite\nq\n")
    assert r.returncode == 0, r.stderr
    assert "about: Jenny, an Irish woman" in r.stdout
    assert 'must name the voice "Jenny (Dioco)"' in r.stdout
    notes = (cfg(env) / "voice-notes.toml").read_text()
    assert '["en_GB-jenny_dioco-medium.onnx"]' in notes and "so sweet, my favourite" in notes
    r = env.run("voices", "pick", "en", "--dir", str(d), "--plain", input="q\n")
    assert "yours: so sweet, my favourite" in r.stdout
    assert "yours: (none: n to write one)" in r.stdout


def test_every_shipped_voice_has_a_note():
    from samvada import pick as p
    shipped = {v.model for v in engines.load_voices(env={"XDG_CONFIG_HOME": "/nonexistent", "XDG_STATE_HOME": "/nonexistent", "SAMVADA_PROFILE": "balanced"}).values()}
    assert shipped <= set(p.facts())


def test_a_new_sample_stops_the_old_one(env, monkeypatch):
    """The screen's Listener: play returns at once; the next play stops the one playing."""
    import time
    slow = env.bin / "paplay"
    slow.write_text('#!/bin/sh\necho "play $(basename "$1")" >> "$FAKE_LOG"\nexec sleep 30\n')
    slow.chmod(0o755)
    for k in ("PATH", "FAKE_LOG", "SAMVADA_VOICES", "PIPER_DIR"):
        monkeypatch.setenv(k, env.vars[k])
    e = full(env)
    voices = engines.load_voices(env=e)
    cs = pick.candidates("en", voices["en"], paths.voices_dirs(e), [trial(env)])
    sound = pick.Listener()

    def wait_for(pred):
        end = time.time() + 10
        while time.time() < end and not pred():
            time.sleep(0.05)
        return pred()

    try:
        t0 = time.time()
        sound.play(voices["en"], cs[0])
        assert time.time() - t0 < 1                        # returns at once
        assert wait_for(lambda: sound._proc is not None and "playing" in sound.status)
        # the fake player has really started (it logs first): only then stop it
        assert wait_for(lambda: env.log.exists() and "play 1.wav" in env.log.read_text())
        first = sound._proc
        sound.play(voices["en"], cs[1])
        assert first.poll() is not None                    # the old one was stopped
        assert wait_for(lambda: sound._proc is not None and sound._proc is not first)
        assert sound.status.startswith(f"playing {cs[1].name}")
        assert wait_for(lambda: "play 2.wav" in env.log.read_text())
        second = sound._proc
        sound.stop()
        assert second.poll() is not None and sound.status == "stopped"
    finally:
        sound.close()
    assert [l for l in env.lines() if l.startswith("play ")] == ["play 1.wav", "play 2.wav"]


def test_a_folder_per_language(env):
    root = env.tmp / "trials"
    for lang, name in (("en", "my-sweet-voice.onnx"), ("fr", "ma-voix.onnx")):
        (root / lang).mkdir(parents=True)
        (root / lang / name).write_text("model")
        (root / lang / f"{name}.json").write_text("{}")
    (root / "en" / "no-json.onnx").write_text("model")
    e = full(env)
    voices = engines.load_voices(env=e)
    dirs = paths.voices_dirs(e)
    en = [c.name for c in pick.candidates("en", voices["en"], dirs, [root])]
    fr = [c.name for c in pick.candidates("fr", voices["fr"], dirs, [root])]
    assert en == ["en_US-lessac-high.onnx", "my-sweet-voice.onnx"]
    assert fr == ["fr_FR-siwis-medium.onnx", "ma-voix.onnx"]
    r = env.run("voices", "pick", "--dir", str(root), "--plain", input="\n\n\n\n")  # en zh ja fr
    assert r.returncode == 0 and "my-sweet-voice.onnx" in r.stdout and "ma-voix.onnx" in r.stdout


def test_table_shows_every_voice_with_terms(env):
    d = trial(env)
    r = env.run("voices", "pick", "en", "fr", "--dir", str(d), "--table")
    assert r.returncode == 0, r.stderr
    assert "## en (3 voices)" in r.stdout and "## fr (1 voices)" in r.stdout
    assert "| ● | en_US-lessac-high.onnx | piper |" in r.stdout
    assert "| en_GB-jenny_dioco-medium.onnx | piper | Jenny" in r.stdout
    assert env.lines() == []  # nothing synthesised or played


def test_terms_in_a_few_words():
    c = pick.Candidate
    assert pick.terms_short(c("m", Path("x"), terms="CC BY-NC-SA 4.0: non-commercial")) == "non-commercial only"
    assert pick.terms_short(c("m", Path("x"), terms="public domain")) == "free, no conditions"
    assert pick.terms_short(c("m", Path("x"), terms="「VOICEVOX:冥鳴ひまり」とクレジットを記載すれば、商用・非商用で利用可能です。")) == "free, with credit"
    assert pick.terms_short(c("m", Path("x"), terms="個人が…商用・非商用で利用可能です。ただし企業が携わる形で利用する場合は…")) == "free for people; companies ask first"
    assert pick.terms_short(c("m", Path("x"), terms="非商用（同人利用や配信による収入は OK）で利用可能です。")) == "non-commercial only"


def test_screen_rows_line_up_in_columns_with_wide_characters():
    c = pick.Candidate("m", Path("x.vvm"), label="冥鳴ひまり ノーマル  (1.vvm #14)",
                       terms="商用・非商用で利用可能", mine="sweet")
    v = engines.Voice("ja", "voicevox", "m", "x", style="")
    for w in (100, 160):
        cols = pick.columns(w)
        line = pick.row(c, v, cols)
        assert pick.cells(line) == sum(n for _, n in cols)      # every row the same width
        head = "".join(pick.pad(t, n - 1) + " " for t, n in cols)
        assert head.index("terms") == pick.cells(line[: line.index("free")])  # terms under its title
    assert pick.pad("冥鳴ひまり", 5) == "冥鳴 "                    # never half a wide character


def test_a_missing_dir_is_an_error_with_a_hint(env):
    d = trial(env)
    r = env.run("voices", "pick", "--dir", str(d)[:-1], "--table")
    assert r.returncode == 2
    assert f"no folder {str(d)[:-1]} (did you mean {d}?)" in r.stderr


def test_the_dirs_are_remembered(env):
    d = trial(env)
    r = env.run("voices", "pick", "en", "--dir", str(d), "--table")
    assert "jenny_dioco" in r.stdout
    r = env.run("voices", "pick", "en", "--table")          # no --dir: last time's folders
    assert r.returncode == 0 and "jenny_dioco" in r.stdout
    import shutil
    shutil.rmtree(d)
    r = env.run("voices", "pick", "en", "--table")
    assert r.returncode == 0 and "folders of last time are gone" in r.stderr


def test_a_pick_keeps_every_other_language_whole(env):
    e = full(env)
    prof = cfg(env) / "profiles"
    prof.mkdir()
    (prof / "balanced.toml").write_text('[ja]\nengine = "voicevox"\nmodel = "x.vvm"\nstyle = 30\n'
                                        '[zh]\nmodel = "zh_CN-xiao_ya-medium.onnx"\n[zh.env]\nFOO = "1"\n')
    voices = engines.load_voices(env=e)
    c = pick.candidates("en", voices["en"], paths.voices_dirs(e), [trial(env)])[1]
    pick.save("en", c, e)
    import tomllib
    data = tomllib.loads((prof / "balanced.toml").read_text())
    assert data["ja"]["style"] == 30 and data["zh"]["env"] == {"FOO": "1"}
    assert data["en"]["engine"] == "piper"


def test_piper_files_stay_piper_when_kokoro_speaks(env):
    e = full(env)
    v = engines.Voice("en", "kokoro", "/x/kokoro-v1.0.onnx", "x", style="af_heart")
    cs = pick.candidates("en", v, paths.voices_dirs(e), [trial(env)])
    assert {c.engine for c in cs if c.name.endswith(".onnx") and "kokoro" not in c.name} == {"piper"}


def test_a_broken_map_still_lets_you_mute_and_pick(env):
    (cfg(env) / "voices.local.toml").write_text('[en]\nmodle = "x"\n')
    assert env.run("mute", "tv").returncode == 0 and env.run("unmute").returncode == 0
    assert env.run("online").returncode == 0
    r = env.run("voices", "pick", "--table")
    assert r.returncode == 0 and "unknown key modle" in r.stderr and "## en" in r.stdout
    assert env.run("speak", "en", "hi").returncode == 2
