"""The owner's voice overrides (DESIGN 6.1): `~/.config/samvada/voices.local.toml`, merged
over the package's voices.toml by language, then by key; never written by Samvada."""

from __future__ import annotations

from pathlib import Path

from samvada import engines


def own_file(env) -> Path:
    p = Path(env.vars["HOME"]) / ".config" / "samvada" / "voices.local.toml"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def load(env, **extra):
    # in-process: paths falls back to Path.home(), the real one, so name the folder outright
    xdg = str(Path(env.vars["HOME"]) / ".config")
    return engines.load_voices(env={**env.vars, "XDG_CONFIG_HOME": xdg, **extra})


def test_no_owner_file_is_the_package_map(env):
    voices = load(env)
    assert list(voices) == ["en", "zh", "ja", "fr"]
    assert voices["en"].model == "en_US-lessac-high.onnx"
    assert set(voices["en"].origin.values()) == {"default"}


def test_one_key_changes_only_that_key(env):
    own_file(env).write_text('[en]\nmodel = "en_GB-jenny_dioco-medium.onnx"\n')
    v = load(env)["en"]
    assert v.model == "en_GB-jenny_dioco-medium.onnx"
    assert v.engine == "piper" and v.online == "en-US-AvaMultilingualNeural"
    assert v.origin["model"] == "yours" and v.origin["engine"] == "default"


def test_env_tables_merge_by_key(env):
    own_file(env).write_text('[zh.env]\nEXTRA = "1"\n')
    v = load(env)["zh"]
    assert v.env == {"HF_HUB_OFFLINE": "1", "EXTRA": "1"}
    assert v.origin["env.EXTRA"] == "yours" and v.origin["env.HF_HUB_OFFLINE"] == "default"


def test_a_new_language_comes_after_the_package_ones(env):
    own_file(env).write_text('[de]\nengine = "piper"\nmodel = "de.onnx"\nonline = "de-DE-KatjaNeural"\n')
    assert list(load(env)) == ["en", "zh", "ja", "fr", "de"]


def test_a_new_language_needs_all_three_keys(env):
    own_file(env).write_text('[de]\nmodel = "de.onnx"\n')
    r = env.run("status")
    assert r.returncode == 2
    assert "voices.local.toml: [de] is a new language: it needs engine, online" in r.stderr


def test_a_typo_stops_with_the_file_and_the_key(env):
    own_file(env).write_text('[en]\nmodle = "x.onnx"\n')
    r = env.run("speak", "en", "hi")
    assert r.returncode == 2
    assert "voices.local.toml: [en] unknown key modle" in r.stderr
    own_file(env).write_text('[en\n')
    r = env.run("status")
    assert r.returncode == 2 and "not TOML" in r.stderr


def test_a_typo_never_breaks_the_hooks(env):
    own_file(env).write_text('[en]\nmodle = "x.onnx"\n')
    r = env.run("hook", "prompt")
    assert r.returncode == 0, r.stderr


def test_the_voice_map_variable_stands_alone(env, tmp_path):
    own_file(env).write_text('[en]\nmodel = "mine.onnx"\n')
    other = tmp_path / "map.toml"
    other.write_text('[en]\nengine = "piper"\nmodel = "other.onnx"\nonline = "x"\n')
    voices = load(env, SAMVADA_VOICE_MAP=str(other))
    assert list(voices) == ["en"] and voices["en"].model == "other.onnx"
    assert voices["en"].origin["model"] == "map"


def test_a_model_path_with_tilde_is_spoken(env):
    home = Path(env.vars["HOME"])
    (home / "trial").mkdir()
    (home / "trial" / "jenny.onnx").write_text("model")
    own_file(env).write_text('[en]\nmodel = "~/trial/jenny.onnx"\n')
    r = env.run("speak", "en", "hello")
    assert r.returncode == 0, r.stderr
    assert any(l.startswith(f"piper {home}/trial/jenny.onnx") for l in env.lines())


def test_config_list_shows_each_layer(env):
    own_file(env).write_text('[fr]\nmodel = "fr_FR-upmc-medium.onnx"\n')
    r = env.run("config", "--list")
    assert r.returncode == 0, r.stderr
    rows = [l.split() for l in r.stdout.splitlines()]
    assert ["fr", "model", "fr_FR-upmc-medium.onnx", "yours", f"({own_file(env)})"] in rows
    assert ["fr", "engine", "piper", "default"] in rows
    assert f"{own_file(env)} (yours, by hand; read)" in r.stdout


def test_config_init_writes_once_and_never_again(env):
    p = own_file(env)
    p.unlink(missing_ok=True)
    r = env.run("config", "--init")
    assert r.returncode == 0 and p.is_file()
    assert set(load(env)["en"].origin.values()) == {"default"}
    p.write_text('[en]\nmodel = "mine.onnx"\n')
    assert "left as it is" in env.run("config", "--init").stdout
    assert p.read_text() == '[en]\nmodel = "mine.onnx"\n'


def test_setup_never_touches_the_owner_file(env):
    p = own_file(env)
    body = '# mine\n[en]\nmodel = "en_US-lessac-high.onnx"\n'
    p.write_text(body)
    extra = {"CLAUDE_CONFIG_DIR": str(env.tmp / "claude")}
    for args in (["setup"], ["setup", "--uninstall"], ["setup"]):
        r = env.run(*args, **extra)
        assert r.returncode == 0, r.stderr
    assert p.read_text() == body


def test_a_new_model_drops_the_old_terms(env):
    own_file(env).write_text('[ja]\nengine = "piper"\nmodel = "ja_x.onnx"\n')
    v = load(env)["ja"]
    assert v.credit == "" and v.license == "" and "credit" not in v.origin
    own_file(env).write_text('[ja]\nmodel = "ja_x.onnx"\ncredit = "mine"\n')
    assert load(env)["ja"].credit == "mine"
    own_file(env).write_text('[zh]\nengine = "piper-plus"\nmodel = "zh_x.onnx"\n')
    assert load(env)["zh"].env == {}                    # engine settings went with the engine
    own_file(env).write_text('[zh]\nmodel = "zh_x.onnx"\n')
    assert load(env)["zh"].env == {"HF_HUB_OFFLINE": "1"}   # same engine: kept


def test_a_model_path_works_when_the_first_voices_folder_is_missing(env):
    home = Path(env.vars["HOME"])
    (home / "trial").mkdir()
    (home / "trial" / "jenny.onnx").write_text("model")
    own_file(env).write_text(f'[en]\nmodel = "{home}/trial/jenny.onnx"\n')
    r = env.run("speak", "en", "hello", SAMVADA_VOICES=str(home / "missing"))
    assert r.returncode == 0, r.stderr
    assert any(f"cwd={home}/trial" in l for l in env.lines())
