"""A standalone bundle reached the way an agent reaches it: the hooks run the bundle's python with
no launcher (nothing sets SAMVADA_VOICES or SAMVADA_ENGINES), and setup says when the agent
cannot run `samvada` by name. Pure functions only: nothing here writes the real home."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from samvada import paths
from samvada.agents import setup


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    root = tmp_path / "samvada-1-windows-arm64"
    (root / "python").mkdir(parents=True)
    (root / "bin").mkdir()
    (root / "BUNDLE.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(sys, "prefix", str(root / "python"))
    return root.resolve()


def test_no_bundle_outside_one(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "venv"))
    assert paths.bundle_root() is None
    env = {"XDG_DATA_HOME": str(tmp_path / "data")}
    assert paths.voices_dirs(env)[0] == tmp_path / "data/samvada/voices"
    assert paths.engines_dir(env) == tmp_path / "data/samvada/engines"


def test_a_bundles_python_finds_its_own_folders_without_the_launcher(bundle, tmp_path):
    env = {"XDG_DATA_HOME": str(tmp_path / "data")}  # no SAMVADA_VOICES: a hook's environment
    assert paths.bundle_root() == bundle
    assert paths.voices_dirs(env) == [bundle / "voices"]
    assert paths.install_voices_dir(env) == bundle / "voices"
    assert paths.engines_dir(env) == bundle / "engines"


def test_the_folders_set_by_hand_still_win(bundle, tmp_path):
    env = {"SAMVADA_VOICES": str(tmp_path / "v"), "SAMVADA_ENGINES": str(tmp_path / "e")}
    assert paths.voices_dirs(env) == [tmp_path / "v"]
    assert paths.install_voices_dir(env) == tmp_path / "v"
    assert paths.engines_dir(env) == tmp_path / "e"


def test_an_install_not_on_path_gets_a_note(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "venv"))
    [step] = setup._path_steps(setup.Tutor(), False, {"PATH": str(tmp_path / "empty")})
    assert step.name == "path" and "not on PATH" in step.desc and step.apply is None
    assert setup._path_steps(setup.Tutor(), True, {"PATH": ""}) == []


def test_no_note_when_the_agent_finds_samvada(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "venv"))
    (tmp_path / "samvada").write_text("", encoding="utf-8")
    assert setup._path_steps(setup.Tutor(), False, {"PATH": str(tmp_path)}) == []


def test_a_cmd_alone_is_not_enough_on_windows(tmp_path, monkeypatch):
    """Git Bash runs `samvada` only as samvada.exe or samvada; samvada.cmd answers in cmd and
    PowerShell alone (seen on Windows 11 ARM64: exit 127)."""
    monkeypatch.setattr(sys, "platform", "win32")
    (tmp_path / "samvada.cmd").write_text("", encoding="utf-8")
    assert not setup._on_path({"PATH": str(tmp_path)})
    (tmp_path / "samvada.exe").write_text("", encoding="utf-8")
    assert setup._on_path({"PATH": str(tmp_path)})


@pytest.fixture
def links(tmp_path):
    d = tmp_path / "localbin"
    return d, {"XDG_BIN_HOME": str(d), "PATH": os.pathsep.join([str(d), "/usr/bin"])}


@pytest.mark.skipif(sys.platform == "win32", reason="a link on Linux and macOS")
def test_a_bundle_links_itself_into_local_bin_and_uninstall_takes_it_out(bundle, links):
    d, env = links
    launcher = bundle / "bin" / "samvada"
    [step] = setup._path_steps(setup.Tutor(), False, env)
    assert step.apply and "link" in step.desc
    step.apply()
    assert os.readlink(d / "samvada") == str(launcher)
    [again] = setup._path_steps(setup.Tutor(), False, env)
    assert again.apply is None and "runs this bundle" in again.desc
    [off] = setup._path_steps(setup.Tutor(), True, env)
    off.apply()
    assert not (d / "samvada").is_symlink()


@pytest.mark.skipif(sys.platform == "win32", reason="a link on Linux and macOS")
def test_another_samvada_in_local_bin_is_left_alone(bundle, links):
    d, env = links
    d.mkdir()
    (d / "samvada").write_text("#!/bin/sh\n# uv's\n", encoding="utf-8")
    [step] = setup._path_steps(setup.Tutor(), False, env)
    assert step.apply is None and "left alone" in step.desc
    assert setup._path_steps(setup.Tutor(), True, env) == []
    assert (d / "samvada").read_text(encoding="utf-8") == "#!/bin/sh\n# uv's\n"


@pytest.mark.skipif(sys.platform == "win32", reason="a link on Linux and macOS")
def test_a_link_to_an_older_bundle_moves_to_this_one(bundle, links, tmp_path):
    d, env = links
    old = tmp_path / "samvada-0-linux-x86_64"
    (old / "bin").mkdir(parents=True)
    (old / "BUNDLE.json").write_text("{}\n", encoding="utf-8")
    d.mkdir()
    (d / "samvada").symlink_to(old / "bin" / "samvada")
    [step] = setup._path_steps(setup.Tutor(), False, env)
    assert "move the link" in step.desc
    step.apply()
    assert os.readlink(d / "samvada") == str(bundle / "bin" / "samvada")


@pytest.mark.skipif(sys.platform == "win32", reason="a link on Linux and macOS")
def test_no_path_and_a_link_dir_not_on_path(bundle, links):
    d, env = links
    assert setup._path_steps(setup.Tutor(path=False), False, env) == []
    steps = setup._path_steps(setup.Tutor(), False, {**env, "PATH": "/usr/bin"})
    assert [s.desc for s in steps][-1] == f"note: {d} is not on PATH: add it"


def test_windows_user_path_gains_the_bundles_bin_once_and_loses_it_on_uninstall(bundle, monkeypatch):
    reg = {"value": r"C:\Tools;C:\Users\me\.local\bin", "kind": 2}
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(setup, "_win_user_path", lambda: (reg["value"], reg["kind"]))
    monkeypatch.setattr(setup, "_win_set_user_path", lambda v, k: reg.update(value=v, kind=k))
    [step] = setup._path_steps(setup.Tutor(), False, {})
    step.apply()
    assert reg["value"] == rf"C:\Tools;C:\Users\me\.local\bin;{bundle / 'bin'}" and reg["kind"] == 2
    [again] = setup._path_steps(setup.Tutor(), False, {})
    assert again.apply is None and "is on your user PATH" in again.desc
    [off] = setup._path_steps(setup.Tutor(), True, {})
    off.apply()
    assert reg["value"] == r"C:\Tools;C:\Users\me\.local\bin"


def test_the_path_choice_is_kept(tmp_path):
    env = {"XDG_CONFIG_HOME": str(tmp_path)}
    setup.save_tutor(setup.Tutor(path=False), env)
    assert setup.load_tutor(env).path is False
    assert "path = false" in setup.config_path(env).read_text(encoding="utf-8")


def test_the_git_bash_launcher_is_lf_and_uses_the_bundles_python():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "release"))
    try:
        import bundle as b
    finally:
        sys.path.pop(0)
    text = b.LAUNCHER_SH_WINDOWS
    assert text.startswith("#!/bin/sh\n") and "\r" not in text
    assert '"$here/python/python.exe" -s -m samvada "$@"' in text
