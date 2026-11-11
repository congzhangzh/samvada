---
name: samvada
description: Speak to the owner with Samvada (the `samvada` command) and act as their language tutor - mute or unmute the voice (e.g. "we are watching TV", "unmute"), turn a language on or off for this session or everywhere, allow or forbid online voices, handle a silent or failed `samvada speak`, install or check voice packs, and the conventions of the correction table and its IPA row. Use whenever the owner talks about the voice, a language, muting, or when `samvada` prints something unexpected.
---

# Samvada: the voice and the tutor

The always-on rule (in CLAUDE.md or AGENTS.md, between `samvada:begin` and `samvada:end`)
says what to do on every message. This skill is the how-to for everything else.

## Speaking

```bash
samvada speak en "<text>" zh "<text>" ja "<text>" fr "<text>"   # every language, in order
```

- **One call per opening or closing**, every language passed. Samvada skips the languages that
  are off or have no voice installed; you never need to know the settings.
- **Spoken text is plain language**: strip code, paths, formulas, symbols, URLs.
- **Silent is fine.** `samvada` decides where the voice plays (the owner at this desk, screen
  unlocked, not muted, not on the phone). When silent it prints the reason on stderr and
  exits 0: do not retry, do not fall back to another player.
- **Exit 1** means a language failed with every engine allowed; say so in one line and go on.
  `SAMVADA_DEBUG=1` shows the engine's own output. **Exit 2** is a usage error: fix the call.

## The owner's words → the command

| The owner says | Run |
|---|---|
| "we are watching TV", "watching with my son", "陪孩子看电视", "mute" | `samvada mute "watching TV"` (once), and say once that the voice is muted |
| "unmute", "TV is over", "speak again" | `samvada unmute` |
| "mute this session", "quiet here" | `samvada --session mute "<why>"` |
| "no Japanese anymore", "turn off French everywhere" | `samvada lang off ja` (global) |
| "turn off French here", "only English in this session" | `samvada --session lang off fr` |
| "bring French back here" | `samvada --session lang on fr`; `samvada --session lang reset` drops this session's choices |
| "online mode", "use edge tts" | `samvada online always`; "safe mode", "offline mode" → `samvada online off` |
| "allow online when a voice fails" | `samvada online fallback` |

Unclear whether they mean this session or everywhere: choose the session, and say so. A mute
in either scope silences; for languages and the online mode, the session wins over global.
While muted, keep the corrections and the table; only the voice stops. Do not play any other
sound (no test audio, no clients started with sound).

**Working from another machine over plain ssh**: this desk's input counter does not move, so
the voice may play to an empty room. Say so once, or run `samvada mute "on another machine"`
(and `samvada unmute` when the owner is back at the desk).

`samvada status` says where the voice goes now and why, which languages are on, the online
mode, and the session key (the Claude Code session id, kept across a resume; else the pys
window; else the tty; or `$SAMVADA_SESSION`).

## Online voices

Off by default: the owner's words never leave the machine. `fallback` retries only a language
that failed offline with edge-tts; `always` sends every language online. Both need the online
extra (`pip install 'samvada[online]'`); `samvada online <mode>` says when it is missing.
`SAMVADA_ONLINE=off|fallback|always` overrides for one call.

## Voice packs

```bash
samvada voices                 # installed where, size, engine
samvada voices add ja fr       # download (pinned, checksummed), install, and the engine if missing
samvada voices verify          # check the installed files against voices.lock
```

A language without a pack is skipped (offline) with "no voice pack". Suggest `samvada voices
add <lang>` once; do not install on your own: it downloads tens to hundreds of MB.

## The table and its IPA row

- One row per language the rule lists, natural phrasing in each, not word-for-word.
- The IPA row (when the rule asks for it) transcribes the whole English row, General American:
  slashes around it, stress marks (ˈ) on words of two or more syllables, the flap ɾ where
  Americans say it (little → ˈlɪɾəl), weak forms as spoken in a sentence (to → tə, and → ənd).
- In a correction about one word's sound or spelling, give that word's IPA too.

## Sessions that are not at a desk

- **Chat bridge** (the reply is shown in a chat on a phone; the system prompt says so):
  do not speak (the bridge runs with `SAMVADA=off`, so a call would be silent anyway). Keep
  the corrections and the table; if the chat cannot render a table, give one line per
  language: `EN: …`, `ZH: …`.
- **Headless runs** set `SAMVADA=off`. A chat bridge that hears from the owner on the phone
  runs `samvada presence phone`, so the desk stays silent until the owner types there again.

## Hooks (installed by `samvada setup`)

- Before a turn, a hook notes the turn's start, and adds one line of context when the voice
  is off ("the voice is off: …"), so you do not compose speech nobody will hear.
- At the end of a turn, if the voice is on and `samvada speak` was not called since the turn
  started, a hook asks you once to speak the closing. Speak it, then stop.
