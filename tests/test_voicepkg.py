"""Voice packages (samvada-voice-<lang>): laid out by release/voice_packages.py from the fake Hub's
files, put on PYTHONPATH as if pip had installed them, and used with the network closed."""

from __future__ import annotations

import importlib.util
import json
import tomllib
from pathlib import Path

from conftest import ROOT
from test_packs import REV, hub  # noqa: F401  (the fake Hub fixture)

from samvada import packs, voicepkg

spec = importlib.util.spec_from_file_location("voice_packages", ROOT / "release/voice_packages.py")
vp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vp)

CLOSED = "http://127.0.0.1:9"  # nothing listens: any download fails
VOICES = tomllib.loads((ROOT / "src/samvada/voices.toml").read_text(encoding="utf-8"))


def blob(hub, e: dict) -> Path:
    prefix = "datasets/" if e["repo_type"] == "dataset" else ""
    return hub.root / f"{prefix}{e['repo']}/resolve/{REV}/{e['path']}"


def install(env, hub, tmp_path, langs=("en", "zh")) -> dict[str, Path]:
    """Lay out the packages and put each one's src on PYTHONPATH."""
    out = {}
    for lang in langs:
        entries = [e for e in hub.entries if e["pack"] == lang]
        files = [packs.File(**e) for e in entries]
        blobs = {e["sha256"]: blob(hub, e) for e in entries}
        out[lang] = vp.layout(lang, files, blobs, "1.0", VOICES[lang], tmp_path / "pkgs" / lang)
    srcs = [str(d / "src") for d in out.values()]
    env.vars["PYTHONPATH"] = ":".join([str(ROOT / "src"), *srcs])
    env.vars["HF_ENDPOINT"] = CLOSED
    return out


def test_layout_is_one_project_with_the_files_as_pinned(env, hub, tmp_path):
    d = install(env, hub, tmp_path, langs=("zh",))["zh"]
    entries = [e for e in hub.entries if e["pack"] == "zh"]
    assert d.name == "samvada-voice-zh"
    main = tomllib.loads((d / "pyproject.toml").read_text())
    assert main["project"]["dependencies"] == []
    assert main["build-system"]["requires"] == [vp.HATCHLING]
    pkg = d / "src/samvada_voice_zh"
    index = json.loads((pkg / "index.json").read_text())
    assert index["format"] == voicepkg.FORMAT == 2
    assert [f["dest"] for f in index["files"]] == [e["dest"] for e in entries]
    for f, e in zip(index["files"], entries):
        assert f["path"] == f"data/{e['dest']}"
        assert (pkg / f["path"]).read_bytes() == blob(hub, e).read_bytes()
    readme = (d / "README.md").read_text()
    assert "non-commercial" in readme and index["digest"] in readme and "part" not in readme


def test_add_takes_the_pack_from_the_packages_without_network(env, hub, tmp_path):
    install(env, hub, tmp_path)
    r = env.run("voices", "add", "en", "zh", "--engine", "never")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "← samvada-voice-zh 1.0" in r.stdout
    assert hub.hits == []
    assert (hub.own / "en_US-lessac-high.onnx").read_bytes() == b"M" * 5000
    assert (hub.own / "g2pW" / "g2pw.onnx").is_file()  # the tar.gz from the package, unpacked
    assert env.run("voices", "verify", "en", "zh").returncode == 0
    r = env.run("voices", "list")
    assert "package samvada-voice-zh 1.0" in r.stdout and "incomplete" not in r.stdout


def test_a_file_missing_from_the_package_is_downloaded(env, hub, tmp_path):
    d = install(env, hub, tmp_path, langs=("zh",))["zh"]
    gone = next(e for e in hub.entries if e["pack"] == "zh")["dest"]
    (d / "src/samvada_voice_zh/data" / gone).unlink()
    assert f"incomplete: {gone} missing" in env.run("voices", "list").stdout
    env.vars["HF_ENDPOINT"] = hub.endpoint
    r = env.run("voices", "add", "zh", "--engine", "never")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"could not give {gone} (missing from the package)" in r.stdout and hub.hits


def test_a_package_that_lies_is_refused(env, hub, tmp_path):
    d = install(env, hub, tmp_path, langs=("en",))["en"]
    model = d / "src/samvada_voice_en/data/en_US-lessac-high.onnx"
    data = bytearray(model.read_bytes())
    data[10] ^= 1
    model.write_bytes(bytes(data))
    r = env.run("voices", "add", "en", "--engine", "never")
    assert r.returncode == 1 and "differs from voices.lock" in r.stdout
    assert not (hub.own / "en_US-lessac-high.onnx").exists()


def test_first_speak_installs_the_voice_from_its_package(env, hub, tmp_path):
    install(env, hub, tmp_path)
    r = env.run("en", "hello", "zh", "你好", SAMVADA="on")
    assert r.returncode == 0, r.stderr
    assert "en: voice installed from samvada-voice-en 1.0" in r.stderr
    assert env.played() == ["en", "zh"] and hub.hits == []
    r = env.run("en", "hello", SAMVADA="on")  # the second time it is simply there
    assert "installed from" not in r.stderr


def test_voice_packages_can_be_turned_off(env, hub, tmp_path):
    install(env, hub, tmp_path, langs=("en",))
    r = env.run("voices", "add", "en", "--engine", "never", SAMVADA_VOICE_PACKAGES="off")
    assert r.returncode == 1 and "download failed" in r.stderr


def test_the_real_packs_fit_pypi():
    """With the real voices.lock every pack, stored whole (a zip of floats adds a little), fits
    its project's PyPI limit: en and zh need the raised one, ja and fr the default."""
    lock = packs.load_lock(ROOT / "src/samvada/voices.lock")
    for lang in ("en", "zh", "ja", "fr"):
        assert lock.size(lang) + 2**20 <= vp.limit(voicepkg.project(lang)), lang
    assert {l for l in ("en", "zh", "ja", "fr") if lock.size(l) > vp.PYPI_MAX} == {"en", "zh"}
    assert set(vp.LIMITS) == {"samvada-voice-en", "samvada-voice-zh"}


def test_every_voice_names_its_terms():
    for lang, v in VOICES.items():
        assert v.get("license") and v.get("license_url", "").startswith("https://"), lang
    assert VOICES["ja"]["credit"] == "■Piper Plus:つくよみちゃん"


def test_the_version_is_one_for_all_voice_packages():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    v = pyproject["tool"]["samvada"]["voice-packages"]
    assert v == vp.version()
    pins = pyproject["project"]["dependencies"] + [
        d for ds in pyproject["project"].get("optional-dependencies", {}).values() for d in ds]
    for d in pins:
        if d.startswith("samvada-voice-"):
            assert d.endswith(f"=={v}"), d


def test_pypi_plan_uploads_what_is_new_and_stops_on_a_changed_version(tmp_path):
    dest = tmp_path / "1.0"
    dest.mkdir()
    names = ["samvada_voice_en-1.0-py3-none-any.whl", "samvada_voice_zh-1.0-py3-none-any.whl"]
    for n in names:
        (dest / n).write_bytes(n.encode())
    (dest / "SHA256SUMS").write_text("".join(f"{vp.sha256(dest / n)}  {n}\n" for n in names))
    asked = []

    def pypi(theirs):
        def ask(project, v):
            asked.append((project, v))
            if project not in theirs:
                return None
            return {"urls": [{"filename": f"{project.replace('-', '_')}-{v}-py3-none-any.whl",
                              "digests": {"sha256": theirs[project]}}]}
        return ask

    same = vp.sha256(dest / names[0])
    todo = vp.pypi_plan(dest, tmp_path / "up", pypi({"samvada-voice-en": same}))
    assert todo == [names[1]] and [p.name for p in (tmp_path / "up").iterdir()] == [names[1]]
    assert asked == [("samvada-voice-en", "1.0"), ("samvada-voice-zh", "1.0")]
    import pytest
    with pytest.raises(SystemExit, match="bump"):
        vp.pypi_plan(dest, tmp_path / "up2", pypi({"samvada-voice-en": "0" * 64}))


def test_a_build_of_another_format_is_never_reused(tmp_path):
    dest = tmp_path / "out" / "1.0"
    dest.mkdir(parents=True)
    w = dest / "samvada_voice_en-1.0-py3-none-any.whl"
    w.write_bytes(b"old")
    (dest / "SHA256SUMS").write_text(f"{vp.sha256(w)}  {w.name}\n")
    (dest / "voice-packages.json").write_text('{"version": "1.0", "langs": []}\n')

    class A:
        out, work = tmp_path / "out", None
    import pytest
    with pytest.raises(SystemExit, match="format None, not 2"):
        vp.build_or_reuse(A, "1.0", ["en"], dest)


# -- the voice package wheels on samvada's GitHub release (voice-packages.toml) --------------
def release(env, hub, tmp_path, lang="en", *, sha=None, digest=None) -> str:
    """Zip the language's package as its wheel, serve it from the fake Hub as the GitHub
    release would, and pin it; returns the wheel's URL path."""
    import hashlib
    import zipfile
    d = install(env, hub, tmp_path, langs=(lang,))[lang]
    env.vars["PYTHONPATH"] = str(ROOT / "src")  # downloaded, not installed
    src = d / "src"
    name = f"samvada_voice_{lang}-1.0-py3-none-any.whl"
    whl = tmp_path / name
    with zipfile.ZipFile(whl, "w") as z:
        for p in sorted(src.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(src).as_posix())
    data = whl.read_bytes()
    hub.put(f"gh/voices-1.0/{name}", data)
    files = lock_files(env, lang)
    pins = tmp_path / "voice-packages.toml"
    pins.write_text(f'version = "1.0"\nurl = "{CLOSED}/nowhere/"\n[wheels.{lang}]\n'
                    f'file = "{name}"\nsize = {len(data)}\n'
                    f'sha256 = "{sha or hashlib.sha256(data).hexdigest()}"\n'
                    f'digest = "{digest or voicepkg.digest(files)}"\n')
    env.vars |= {"SAMVADA_VOICE_PINS": str(pins), "SAMVADA_VOICES_URL": f"{hub.endpoint}/gh/voices-1.0/"}
    return f"/gh/voices-1.0/{name}"


def lock_files(env, lang):
    return packs.load_lock(Path(env.vars["SAMVADA_VOICE_LOCK"])).pack(lang)


def test_add_takes_the_pack_from_the_release_wheel_not_the_hub(env, hub, tmp_path):
    path = release(env, hub, tmp_path, "zh")
    env.vars["HF_ENDPOINT"] = CLOSED
    r = env.run("voices", "add", "zh", "--engine", "never")
    assert r.returncode == 0, r.stdout + r.stderr
    assert hub.hits == [path]
    assert "← samvada-voice-zh 1.0" in r.stdout
    assert (hub.own / "g2pW" / "g2pw.onnx").is_file()
    assert env.run("voices", "verify", "zh").returncode == 0
    assert not list(hub.own.glob(".*.whl.download*"))  # the wheel is not kept


def test_the_release_down_falls_back_to_the_hub(env, hub, tmp_path):
    release(env, hub, tmp_path, "en")
    env.vars["SAMVADA_VOICES_URL"] = CLOSED
    env.vars["HF_ENDPOINT"] = hub.endpoint
    r = env.run("voices", "add", "en", "--engine", "never")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "the GitHub release failed" in r.stdout and "file by file from the Hub" in r.stdout
    assert all("/resolve/" in h for h in hub.hits)


def test_a_wheel_unlike_its_pin_is_refused_and_the_hub_used(env, hub, tmp_path):
    path = release(env, hub, tmp_path, "en", sha="0" * 64)
    env.vars["HF_ENDPOINT"] = hub.endpoint
    r = env.run("voices", "add", "en", "--engine", "never")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "does not match voice-packages.toml" in r.stdout
    assert hub.hits[0] == path and len(hub.hits) == 3


def test_a_wheel_of_another_voices_lock_is_never_downloaded(env, hub, tmp_path):
    release(env, hub, tmp_path, "en", digest="f" * 64)
    env.vars["HF_ENDPOINT"] = hub.endpoint
    r = env.run("voices", "add", "en", "--engine", "never")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not any(h.startswith("/gh/") for h in hub.hits)


def test_a_partial_wheel_resumes(env, hub, tmp_path):
    path = release(env, hub, tmp_path, "en")
    data = (hub.root / path.lstrip("/")).read_bytes()
    hub.own.mkdir(parents=True, exist_ok=True)
    name = path.rsplit("/", 1)[1]
    (hub.own / f".{name}.download.part").write_bytes(data[:1000])
    env.vars["HF_ENDPOINT"] = CLOSED
    r = env.run("voices", "add", "en", "--engine", "never")
    assert r.returncode == 0, r.stdout + r.stderr
    assert env.run("voices", "verify", "en").returncode == 0


def test_packages_off_also_skips_the_release(env, hub, tmp_path):
    release(env, hub, tmp_path, "en")
    env.vars["HF_ENDPOINT"] = hub.endpoint
    r = env.run("voices", "add", "en", "--engine", "never", SAMVADA_VOICE_PACKAGES="off")
    assert r.returncode == 0 and not any(h.startswith("/gh/") for h in hub.hits)


def test_the_shipped_pins_name_every_voice_of_this_voices_lock():
    """voice-packages.toml pins all four wheels of the voice-packages version, each made from
    the pack voices.lock pins now: a voices.lock change without new wheels fails here."""
    pins = tomllib.loads((ROOT / "src/samvada/voice-packages.toml").read_text())
    lock = packs.load_lock(ROOT / "src/samvada/voices.lock")
    assert pins["version"] == vp.version()
    assert pins["url"] == vp.release_url(vp.version())
    assert set(pins["wheels"]) == set(vp.ALL) == {"en", "zh", "ja", "fr", "en-kokoro", "zh-kokoro"}
    for lang, w in pins["wheels"].items():
        assert w["digest"] == voicepkg.digest(lock.pack(lang)), lang
        assert w["file"] == f"{voicepkg.module(lang)}-{pins['version']}-py3-none-any.whl"
        assert 0.8 * lock.size(lang) < w["size"] < vp.limit(voicepkg.project(lang)), lang


def test_the_kokoro_packs_are_github_only(tmp_path):
    """Their wheels (the full models) are over any PyPI limit: the PyPI plan leaves them out."""
    assert voicepkg.module("en-kokoro") == "samvada_voice_en_kokoro"
    assert vp.voice_of("zh-kokoro", VOICES)["engine"] == "kokoro"
    d = tmp_path / "built"
    d.mkdir()
    (d / "SHA256SUMS").write_text("ab  samvada_voice_en_kokoro-1.0-py3-none-any.whl\n")
    asked = []
    assert vp.pypi_plan(d, tmp_path / "up", ask=lambda p, v: asked.append(p)) == []
    assert asked == []
