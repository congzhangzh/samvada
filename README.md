# Samvada

**A voice for AI agents, offline first.** The agent decides what to say; Samvada decides
whether, where and how it is heard. *Samvada* (संवाद, *saṃvāda*) means dialogue.

- **Offline first.** Local voices (Piper) for English and Chinese, French and Japanese as
  optional packs. Online voices (edge-tts) are **off by default**, so your text never leaves
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
uv tool install samvada          # or: pipx install samvada, pip install samvada
samvada voices add en zh         # once: about 290 MB, downloaded and verified
```

## Use

```bash
samvada speak en "Hello" zh "你好"   # plays in this order, then exits
samvada status                      # where the voice goes now, and why
samvada mute "watching TV"          # silent until `samvada unmute`
samvada lang off fr                 # skip a language (the caller still passes it)
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
`samvada voices add` downloads from the Hub's resolve URLs, resumes partial downloads, and
checks every file before putting it in place. Nothing needs configuring; optionally
`HF_ENDPOINT` points at a mirror and `HF_TOKEN` lifts anonymous rate limits.

| Pack | Voice | Engine | Source |
|---|---|---|---|
| en | `en_US-lessac-high` | Piper | `rhasspy/piper-voices` |
| zh | `zh_CN-xiao_ya-medium` + g2pW + BERT tokenizer | Piper | `rhasspy/piper-voices`, `rhasspy/piper-checkpoints`, `google-bert/bert-base-chinese` |
| ja | Tsukuyomi-chan | piper-plus | `ayousanz/piper-plus-tsukuyomi-chan` + NLTK data |
| fr | `fr_FR-siwis-medium` | Piper | `rhasspy/piper-voices` |

Each voice keeps its own license; see its model card on the Hub before you redistribute it.

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
