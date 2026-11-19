"""By hand: the URL mode (`voices add --urls`, SAMVADA_DOWNLOAD=urls) prints where each file
is and downloads nothing; `voices add --from DIR` installs from downloaded files, whatever
their names, matched by size then sha256, with no network. Against the fake Hub and the fake
GitHub release of the other pack tests; the Hub counts every request."""

from __future__ import annotations

import shutil
from pathlib import Path

from test_packs import REV, hub  # noqa: F401  (the fake Hub fixture)
from test_voicepkg import CLOSED, blob, release


def folder(tmp_path: Path, files: dict[str, bytes | Path]) -> Path:
    d = tmp_path / "downloads"
    d.mkdir(exist_ok=True)
    for name, data in files.items():
        if isinstance(data, Path):
            shutil.copyfile(data, d / name)
        else:
            (d / name).write_bytes(data)
    return d


def entries(hub, lang):
    return [e for e in hub.entries if e["pack"] == lang]


def closed(env) -> None:
    """Every network address shut: a download would fail."""
    env.vars |= {"HF_ENDPOINT": CLOSED, "SAMVADA_VOICES_URL": CLOSED}


def test_the_url_mode_prints_the_wheel_and_the_files_and_downloads_nothing(env, hub, tmp_path):
    path = release(env, hub, tmp_path, "en")
    env.vars["HF_ENDPOINT"] = hub.endpoint
    for how in (["--urls"], []):
        extra = {} if how else {"SAMVADA_DOWNLOAD": "urls"}
        r = env.run("voices", "add", "en", "zh", *how, "--engine", "never", **extra)
        assert r.returncode == 0, r.stdout + r.stderr
        out = r.stdout
        assert f"{hub.endpoint}{path}" in out  # the wheel, the first choice
        assert f"{hub.endpoint}/org/voices/resolve/{REV}/en/a.onnx" in out
        assert f"{hub.endpoint}/datasets/org/ckpt/resolve/{REV}/zh/_resources/g2pw.tar.gz" in out
        for e in entries(hub, "en") + entries(hub, "zh"):
            assert f"{e['size']} bytes  sha256 {e['sha256']}" in out
        assert "samvada voices add en zh --from <folder>" in out
    assert hub.hits == []
    assert not hub.own.exists() or not any(hub.own.iterdir())


def test_the_url_mode_says_what_is_already_there(env, hub, tmp_path):
    assert env.run("voices", "add", "en", "--engine", "never").returncode == 0
    n = len(hub.hits)
    r = env.run("voices", "add", "en", "--urls")
    assert r.returncode == 0 and "en: already installed" in r.stdout and len(hub.hits) == n


def test_from_only_the_wheel_renamed(env, hub, tmp_path):
    path = release(env, hub, tmp_path, "en")
    d = folder(tmp_path, {"downloaded.zip": hub.root / path.lstrip("/")})
    closed(env)
    r = env.run("voices", "add", "en", "--from", str(d), "--engine", "never")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "← downloaded.zip" in r.stdout
    assert env.run("voices", "verify", "en").returncode == 0
    assert hub.hits == [] and (d / "downloaded.zip").is_file()  # copied, never moved


def test_from_only_the_loose_files_renamed(env, hub, tmp_path):
    d = folder(tmp_path, {f"file{i}": blob(hub, e) for i, e in enumerate(entries(hub, "zh"))})
    closed(env)
    r = env.run("voices", "add", "zh", "--from", str(d), "--engine", "never")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (hub.own / "g2pW" / "g2pw.onnx").is_file()  # the tar, matched and unpacked
    assert (hub.own / "hf/hub/models--bert-base-chinese/refs/main").read_text() == REV
    assert env.run("voices", "verify", "zh").returncode == 0
    assert hub.hits == []


def test_from_a_wheel_and_loose_files_mixed(env, hub, tmp_path):
    path = release(env, hub, tmp_path, "en")
    files = {"en.whl": hub.root / path.lstrip("/")}
    files |= {f"zh-{i}": blob(hub, e) for i, e in enumerate(entries(hub, "zh"))}
    d = folder(tmp_path, files)
    closed(env)
    r = env.run("voices", "add", "en", "zh", "--from", str(d), "--engine", "never")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "← en.whl" in r.stdout and "← zh-0" in r.stdout
    assert env.run("voices", "verify", "en", "zh").returncode == 0
    assert hub.hits == []


def test_from_a_wrong_file_is_refused(env, hub, tmp_path):
    model, cfg = entries(hub, "en")
    d = folder(tmp_path, {"model.onnx": b"X" * model["size"], "cfg.json": blob(hub, cfg)})
    closed(env)
    r = env.run("voices", "add", "en", "--from", str(d), "--engine", "never")
    assert r.returncode == 1
    assert "ignored model.onnx" in r.stderr
    assert f"/org/voices/resolve/{REV}/en/a.onnx" in r.stdout  # what to get instead
    assert not (hub.own / "en_US-lessac-high.onnx").exists()
    assert not (hub.own / "en_US-lessac-high.onnx.json").exists()  # nothing of a pack with gaps
    assert hub.hits == []


def test_from_with_something_missing_fails_with_its_urls_and_no_network(env, hub, tmp_path):
    release(env, hub, tmp_path, "en")
    _, cfg = entries(hub, "en")
    d = folder(tmp_path, {"cfg.json": blob(hub, cfg)})
    env.vars["HF_ENDPOINT"] = hub.endpoint  # open, to see that nothing reaches it
    r = env.run("voices", "add", "en", "--from", str(d), "--engine", "never")
    assert r.returncode == 1
    assert "1 file(s) not in" in r.stderr and "nothing installed for en" in r.stderr
    assert "samvada_voice_en-1.0-py3-none-any.whl" in r.stdout and "en/a.onnx" in r.stdout
    assert hub.hits == []
    r = env.run("voices", "add", "en", "--from", str(tmp_path / "nowhere"))
    assert r.returncode == 1 and "no such folder" in r.stderr


def test_a_failed_download_ends_with_the_hint(env, hub, tmp_path):
    closed(env)
    r = env.run("voices", "add", "en", "zh", "--engine", "never")
    assert r.returncode == 1
    assert ("can't download? samvada voices add en zh --urls shows where to get the files; "
            "then samvada voices add en zh --from <folder>") in r.stderr


def test_the_hint_is_in_the_help_and_the_doctor(env, hub):
    for args in (["--help"], ["voices", "--help"]):
        r = env.run(*args)
        assert r.returncode == 0 and "Can't download, or want to fetch the files yourself?" in r.stdout
        assert "--urls" in r.stdout and "--from DIR" in r.stdout and "SAMVADA_DOWNLOAD=urls" in r.stdout
    r = env.run("doctor")
    assert "samvada voices add en --urls, then --from <folder>" in r.stdout


def test_bad_uses(env, hub, tmp_path):
    assert env.run("voices", "add", "en", "--urls", "--from", str(tmp_path)).returncode == 2
    assert env.run("voices", "add", "en", SAMVADA_DOWNLOAD="maybe").returncode == 2
    assert env.run("voices", "list", SAMVADA_DOWNLOAD="maybe").returncode == 0  # list downloads nothing
    assert env.run("voices", "add", "en", "--from").returncode == 2
