# kinetic-captions

Documentary-style kinetic captions for vertical video. One emphasis word scaled
up, the rest small, every word fading in on the millisecond it is spoken.

Runs entirely on your machine: **whisper.cpp + ffmpeg/libass**. No API keys, no
cloud, no per-render fee. A 63-second clip takes **17 seconds** end to end, and
**24 seconds on a single CPU core** — so it fits on the cheapest VPS you have.

```bash
python3 kinetic.py input.mov
```

That is the whole interface. Layout, phrasing, emphasis, colour and loudness are
decided for you.

---

## Why this exists

Most caption tools draw every word at the same size. The look this imitates —
the one used in long-form documentary edits — works because **size carries
meaning**: the word the speaker leaned on is two or three times larger, the rest
stay quiet around it.

Doing that well needs three things most tools skip:

1. Word-level timing, so text appears with the voice rather than after it.
2. A reason to pick *this* word and not that one.
3. Typography that survives long words, narrow safe areas and bright footage.

## How emphasis is chosen, without a model

Speakers stretch the words that matter. The scorer divides each word's measured
duration by the duration its syllable count predicts:

```
score = duration / (0.085 + syllables × 0.135)
```

Then it damps the things that look like emphasis but are not — function words,
and the natural lengthening of whatever lands before a pause — and boosts
numbers. Checked against a hand-marked reference: **83% agreement**.

Optionally a single batched model call can pick the words instead
(`KIN_EMPHASIS=llm`), which reads semantics rather than prosody. It costs about
1500 input tokens per video and falls back to the scorer on any failure.

## Layout follows the footage

| source | result |
| --- | --- |
| landscape (≥ 1.5) | letterboxed into 9:16, text in the black bar above |
| portrait (≤ 0.7) | full bleed, text in the upper third |
| square | 4:5, text above |

Landscape is the easy case: the black bar is guaranteed empty, so nothing has to
dodge a face.

## Install

```bash
# macOS
brew install ffmpeg whisper-cpp

# Debian / Ubuntu
sudo apt install ffmpeg
# whisper.cpp: build from https://github.com/ggml-org/whisper.cpp

# a model (1.5 GB, best quality; ggml-small.bin works in 2 GB of RAM)
mkdir -p ~/.cache/whisper && curl -L -o ~/.cache/whisper/ggml-large-v3-turbo.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin
```

> **Check your ffmpeg has libass**: `ffmpeg -filters | grep ' ass '`
> Homebrew's slim `ffmpeg` formula ships without it and will silently render no
> text at all. Install `ffmpeg-full`, or on Linux the distro package is fine.

## Options

Everything is an environment variable; the defaults are the opinionated path.

| variable | default | what it does |
| --- | --- | --- |
| `KIN_LANG` | `ru` | `ru` or `en` — switches the whole language pack |
| `KIN_STYLE` | `white` | `white`, `yellow`, `sunny`, `orange` |
| `KIN_FONT` | `SF Pro Text` | caption face |
| `KIN_FONT_ACCENT` | — | a different face for the emphasis words |
| `KIN_GRADE` | `on` | the colour grade |
| `KIN_PROFANITY` | `mask` | `mask`, `drop` or `keep` |
| `KIN_EMPHASIS` | — | `llm` to let a model pick the words |
| `WHISPER_MODEL` | `~/.cache/whisper/…` | path to a ggml model |

```bash
KIN_LANG=en KIN_STYLE=yellow python3 kinetic.py talk.mp4
```

## Telegram bot

`bot/` contains a bot that accepts a video and returns the edit. Standard library
only — no dependencies beyond what the pipeline already needs.

```bash
sudo bash bot/install.sh <TELEGRAM_TOKEN>
```

It installs ffmpeg and whisper.cpp, picks a model that fits the available RAM,
and registers a systemd unit. **Nothing is retained**: the source is deleted the
moment rendering finishes, the result the moment it is sent, a janitor sweeps
anything older than 20 minutes, and the work directory is emptied on start.

## Things learned the hard way

Each of these cost a debugging round, and each is now handled:

- **`ffprobe` reports storage dimensions.** Phone footage carries a 90°/270°
  rotation flag; ignore it and a portrait clip is treated as landscape.
- **Long words overflow the frame.** Reels reserves ~12% on the right for its
  buttons. The emphasis size is solved from the longest word's real glyph width,
  not guessed.
- **A proper name must not be split.** "App Store" is bound into one unit — same
  size, same colour, joined with a hard space so it cannot break across lines.
- **iPhone records full-range colour.** `yuvj420p` fails platform validation;
  the range is converted explicitly.
- **Script faces render optically smaller** at the same point size, and narrower.
  Both are corrected per face, otherwise the emphasis stops reading as emphasis.
- **Automated contrast checks measure the wrong thing.** They compare text to its
  own background, not to the pixels underneath. Always look at a frame from the
  finished file.

## Output

1080×1920, H.264, −14 LUFS, −1.5 dBTP, BT.709 tagged. Passes platform validation
for Reels, Shorts and TikTok.

## License

MIT
