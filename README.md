# Samvada

**A voice for AI agents, offline first.** The agent decides what to say; Samvada decides
whether, where and how it is heard. *Samvada* (संवाद, *saṃvāda*) means dialogue.

- **Offline first.** Local voices (Piper) for English, Chinese, Japanese and French, each a
  pack you add once. Online voices (edge-tts) are **off by default**, so your text never leaves
  the machine unless you turn them on.
- **Plays only where you are.** Silent when the screen is locked, when you are muted, or when
  your last input came from your phone instead of this desk. Silence is never an error.
- **One call, every language.** The caller passes all its languages in order; Samvada skips
  the ones that are off or have no voice installed.
- **No dependencies.** The package itself is pure Python; each voice engine runs in its own
  folder, with pinned versions.

Status: alpha. Linux (GNOME) is the tested platform.

## Install

```bash
uv tool install samvada          # or: pip install samvada
samvada voices add en zh ja fr   # once, the languages you want: each voice and its engine
samvada voices add en zh --kokoro   # optional: Kokoro, the fallback for en and zh (full models)
```

English and Chinese have a second voice, **Kokoro** (Apache-2.0, full fp32 models: en 354 MB,
zh 379 MB, and its engine). It is installed only with `--kokoro`, and speaks when the Piper
voice is missing or fails (`samvada status` shows each language's order). To make it your
default, name it for that language in `~/.config/samvada/voices.local.toml`
(`[en]` `engine = "kokoro"`, `model = "kokoro/v1.0/kokoro-v1.0.onnx"`, `style = "af_heart"`);
Piper then becomes its fallback.

Each language's voice is one wheel on samvada's GitHub release `voices-<version>` (en 106 MB,
zh 172, ja 38, fr 58; the Kokoro fallback, `--kokoro`: en 327 MB, zh 341 MB). `samvada voices add` downloads it, checks it against the sha256 Samvada
pins, and installs every file only after checking it against `voices.lock`; if GitHub fails, it
takes the same files from Hugging Face one by one. `SAMVADA_VOICES_URL` points it at a mirror
of the release. A wheel can also be installed with pip (`pip install <its URL>`); `samvada`
then takes the voice from it with no network.

## Use

```bash
samvada speak en "Hello" zh "你好"   # plays in this order, then exits
samvada status                      # where the voice goes now, and why
samvada mute "watching TV"          # silent until `samvada unmute`
samvada lang --disable-fr           # skip a language (the caller still passes it);
                                    #   several at once: --enable-zh --disable-ja
samvada lang --list                 # every language: on or off, set where, its voice pack
samvada --session mute              # the same, for this terminal session only
samvada online fallback             # allow edge-tts for a language that fails offline
                                    #   (needs: pip install 'samvada[online]')
samvada presence phone              # a chat bridge says: the owner just wrote from the phone
samvada voices verify               # check the installed voices against their checksums
samvada setup --speak-alias         # also answer to `speak en "Hello"` (opt-in: the name is
                                    #   espeak's on some systems; another `speak` is left alone)
```

Exit codes: `0` played, or silent by decision; `1` a language failed; `2` bad usage.

## Voice packs

Every file a pack needs is pinned in `src/samvada/voices.lock`: a commit on the
[Hugging Face Hub](https://huggingface.co) (or a URL at a commit), with its size and sha256.
`samvada voices add` takes the files from an installed `samvada-voice-<lang>` package, else
from the language's wheel on the GitHub release (pinned in `src/samvada/voice-packages.toml`),
else from the Hub's resolve URLs, resuming partial downloads, and checks every file against the
lock before putting it in place, wherever it came from (`SAMVADA_VOICE_PACKAGES=off` skips the
packages, installed or on GitHub). Nothing needs configuring; optionally
`HF_ENDPOINT` points at a mirror and `HF_TOKEN` lifts anonymous rate limits.

**Can't download, or want to fetch the files yourself?** `samvada voices add en zh --urls` (or
`SAMVADA_DOWNLOAD=urls samvada voices add en zh`) downloads nothing and prints, for each pack, the
wheel on the GitHub release and, as the other choice, each file at its pinned URL, with its size
and sha256, and how to install a missing engine. Put what you downloaded in one folder, any file
names, and run `samvada voices add en zh --from <folder>`: each file is found by its size, then its
sha256, and checked against the lock again; nothing goes to the network. A pack with a file
missing installs nothing and lists that file's URLs.

| Pack | Voice | Engine | Source |
|---|---|---|---|
| en | `en_US-lessac-high` | Piper | `rhasspy/piper-voices` |
| zh | `zh_CN-xiao_ya-medium` + g2pW + BERT tokenizer | Piper | `rhasspy/piper-voices`, `rhasspy/piper-checkpoints`, `google-bert/bert-base-chinese` |
| ja | Tsukuyomi-chan | piper-plus | `ayousanz/piper-plus-tsukuyomi-chan` + NLTK data |
| fr | `fr_FR-siwis-medium` | Piper | `rhasspy/piper-voices` |

Each voice keeps its own terms, which Samvada's MIT license does not cover: en, trained on the
Lessac (Blizzard 2013) data under its research licence; zh, trained on Databaker's BZNSYP,
non-commercial use; ja, the terms of Piper Plus Tsukuyomi-chan and of its corpus, with the
credit **■Piper Plus:つくよみちゃん**; fr, SIWIS, CC-BY 4.0. `src/samvada/voices.toml` links each
one; read them on the model cards before you use or redistribute a voice.

## Where things live

| What | Where |
|---|---|
| Settings (mute, languages, online mode, sessions) | `$XDG_STATE_HOME/samvada/` |
| Voices, engines | `$XDG_DATA_HOME/samvada/{voices,engines}/` (or `$SAMVADA_VOICES`, `$SAMVADA_ENGINES`) |

## Develop

```bash
uv run pytest -q                     # fake engines, player, desktop and Hub: no audio, no network
uv run python release/bundle.py <version> --test   # the standalone bundle and its offline test
```

## License

MIT, see [LICENSE](LICENSE). The voices are not part of this package; they are downloaded
from their own repositories under their own licenses.
