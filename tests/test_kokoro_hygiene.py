"""The Kokoro engine's housekeeping: jieba's cache in Samvada's cache folder, not the temp
folder; an engine installed with uv comes with its bytecode compiled."""

from __future__ import annotations

import subprocess
from pathlib import Path

from samvada import packs

from test_kokoro import FAKE_KOKORO, model, own

FAKE_JIEBA = "class _T:\n    tmp_dir = None\ndt = _T()\n"
# Says where jieba will keep its cache, at the first sentence (when jieba would load).
FAKE_MISAKI_ZH = '''
import jieba
class ZHG2P:
    def __init__(self, version=None): self.v = version
    def __call__(self, text): return (f"jieba-cache={jieba.dt.tmp_dir} " + text, None)
'''


def test_jiebas_cache_is_in_samvadas_cache_folder(env):
    d = Path(env.vars["XDG_DATA_HOME"]) / "samvada" / "engines" / "kokoro"
    for path, text in {"kokoro_onnx/__init__.py": FAKE_KOKORO, "jieba/__init__.py": FAKE_JIEBA,
                       "misaki/__init__.py": "", "misaki/zh.py": FAKE_MISAKI_ZH}.items():
        (d / path).parent.mkdir(parents=True, exist_ok=True)
        (d / path).write_text(text, encoding="utf-8")
    m11 = model(env.tmp / "kzh", "v1.1-zh", ["zf_001"], config=True)
    own(env, f'[zh]\nengine = "kokoro"\nmodel = "{m11}"\nstyle = "zf_001"\n')
    cache = Path(env.vars["HOME"]) / "c"
    r = env.run("speak", "zh", "你好", XDG_CACHE_HOME=str(cache))
    assert r.returncode == 0, r.stderr
    log = [l for l in env.lines() if l.startswith("kokoro ")]
    assert log == [f"kokoro kokoro-v1.1-zh.onnx zf_001 ph cfg=yes jieba-cache={cache / 'samvada'} 你好"]
    assert (cache / "samvada").is_dir()


def test_an_engine_installed_with_uv_has_its_bytecode(tmp_path, monkeypatch):
    cmds = []

    def run(cmd, *a, **k):
        cmds.append(cmd)
        Path(cmd[cmd.index("--target") + 1]).mkdir(parents=True)
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(packs.shutil, "which", lambda name, *a, **k: "/fake/uv" if name == "uv" else None)
    monkeypatch.setattr(packs.subprocess, "run", run)
    packs.install_engine("kokoro", tmp_path, log=lambda *a: None)
    assert cmds and cmds[0][:3] == ["/fake/uv", "pip", "install"]
    assert "--compile-bytecode" in cmds[0]
    assert (tmp_path / "kokoro" / ".python").is_file()
