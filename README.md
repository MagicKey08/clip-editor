<p align="center">
  <img src="icon.png" width="96" alt="ClipEditor icon">
</p>

<h1 align="center">ClipEditor</h1>

<p align="center">
  A small, fast, local editor for game clips with <b>multiple audio tracks</b>.<br>
  Trim, cut, mix game / voice chat / mic separately – and export for Discord or YouTube.
</p>

---

## Why?

Recording software like OBS can save game audio, voice chat and your microphone on **separate audio
tracks**. Most editors then only play the first track, or they are far too heavy for "trim this
2‑minute replay and turn Discord down a bit".

ClipEditor does exactly that and nothing more: it shows your clip folder as a library, lets you
**hear and adjust every track on its own**, cut frame-accurately, and exports a clean MP4.
Everything runs locally on your PC – nothing is uploaded anywhere.

## Features

**Library**
- Grid of all MP4 clips with thumbnail, date, length, size and number of audio tracks
- Hover a thumbnail to scrub through the clip
- Tabs for **Originals** and **Edited**, search (name or note), sort by date / name / size / length
- Filters: favorites, with draft, unedited · ★ favorites and 📝 notes per clip
- Select several clips to export or delete them (deleted clips go to the **recycle bin**)
- Thumbnails and metadata are cached, so even large folders open instantly

**Editor**
- Every audio track is played separately (Web Audio) with its own **volume (0–200 %)** and **mute**
- Waveform lane per track – see at a glance when someone was talking
- Trim start/end, cut out any number of segments, drag edges on the timeline – all frame-accurate
- The preview plays exactly what the export will sound like (same mix, same limiter, cuts skipped)
- Frame stepping, 0.25×–2× preview speed, undo/redo
- A pre-mixed track (e.g. OBS track 1 "Mix") is muted automatically when you start mixing the separate tracks
- Every change is saved automatically as a **draft** – close the app and continue later

**Export**
- **Best quality**: original resolution and frame rate, H.264 CRF 18 (NVENC if available)
- **Keep tracks separate**: track 1 is your mix (so every player plays it), followed by the untouched
  separate tracks – reopen the export later and keep mixing
- **Target size** for sharing: *Discord without Nitro (20 MB)*, *Nitro Basic (50 MB)* or any size.
  Two-pass encoding with a calculated bitrate, automatically 720p when that looks better.
  The panel tells you the resulting resolution, bitrate and expected quality before you export.
- Custom file names, export all drafts in one go, sound + notification when done
- **YouTube (unlisted)**: one click opens the upload page and selects the file in Explorer
- Originals are never modified

**App**
- Runs in its own window like a desktop app (no browser tabs, no console window)
- Closing the window quits ClipEditor – but never in the middle of an export

## Requirements

- **Windows 10/11** (the editor itself runs in any Chromium browser; recycle bin, Explorer and app
  window integration are Windows-only)
- **Python 3.9+** – standard library only, no `pip install` needed
- **FFmpeg + ffprobe**
- Google Chrome, Microsoft Edge or Brave for the app window (otherwise your default browser is used)

## Installation

1. Download or clone this repository.
2. Get FFmpeg – either install it so `ffmpeg` and `ffprobe` are on your `PATH`, or download the
   [release essentials build from gyan.dev](https://www.gyan.dev/ffmpeg/builds/) and extract it so that
   `ffmpeg\bin\ffmpeg.exe` and `ffmpeg\bin\ffprobe.exe` are inside the ClipEditor folder.
3. Optional: right-click `create-shortcut.ps1` → **Run with PowerShell** to get a ClipEditor shortcut
   (with icon) on your desktop and in the Start menu.

## Usage

| Start | What happens |
|---|---|
| Shortcut or double-click **`ClipEditor.pyw`** | App mode: own window, no console. Closing the window quits it. |
| **`start.bat`** | Opens a browser tab and keeps a console window open – handy for troubleshooting. |

On first start, click **⚙ Folders** and choose your clip folder (default: `~\Videos`). Exports go to
`<clip folder>\Edited` unless you choose another export folder.

### Keyboard shortcuts

| Key | Action |
|---|---|
| `Space` / click the video | Play / pause |
| `Home` | Jump to the trim start |
| `←` / `→` | One frame back / forward |
| `I` / `O` | Set trim start / end at the playhead |
| `M`, then `X` | Set a mark, then cut out everything between mark and playhead |
| `Ctrl+Z` / `Ctrl+Y` | Undo / redo |
| Drag on the timeline | Seek; drag the green trim edges or red cut edges to adjust them |

### Drafts

A **draft** is an original clip you are working on. Trim, cuts, volumes and the playhead are saved
automatically and restored when you open the clip again. Once you export a draft it is done – the
draft badge disappears. Clips in the *Edited* tab keep their settings too, they are just not drafts.

### Tip: recording separate tracks with OBS

In OBS, go to *Settings → Output → Recording* (use *Advanced* output mode) and enable several audio
tracks. Then assign your sources in *Edit → Advanced Audio Properties*, for example:

| Track | Content |
|---|---|
| 1 | Everything (name it "Mix") |
| 2 | Voice chat |
| 3 | Microphone |
| 4 | Game |

The replay buffer records the same tracks. ClipEditor recognises track 1 as the pre-mixed track by its
name (`mix_track_names` in `config.json`).

## Configuration

`config.json` is created on first start:

| Key | Meaning |
|---|---|
| `folder` | Clip folder (read only – originals are never changed) |
| `export_folder` | Where exports are written |
| `mix_track_names` | Track names treated as a pre-mixed "everything" track |

Video encoders are listed in `VIDEO_ENCODERS` / `ENCODER_ORDER` in `app.py`; NVENC is used when it
works, otherwise libx264. Adding another encoder (e.g. AMD AMF) takes two lines – see the comment there.

## Files

| Path | Purpose |
|---|---|
| `app.py` | Local web server, FFmpeg/ffprobe handling, cache, export |
| `index.html` | The whole user interface |
| `ClipEditor.pyw` | Starts the app without a console |
| `start.bat` | Starts with a console (troubleshooting) |
| `create-shortcut.ps1` | Creates desktop / Start menu shortcuts |
| `app.ico` | App icon |
| `config.json`, `drafts.json`, `library.json` | Your settings, drafts, favorites and notes (created at runtime) |
| `cache\` | Thumbnails, metadata and extracted preview audio – safe to delete at any time |

## How it works

- `app.py` is a small HTTP server on `127.0.0.1:8765` (local only) built on the Python standard library.
- A browser's `<video>` element only plays the first audio track. ClipEditor therefore extracts every
  track once as FLAC and plays them in sync with the muted video via the Web Audio API – one gain node
  per track, scheduled sample-accurately so cuts are skipped exactly like in the export.
- The export cuts video and all tracks in one FFmpeg filter graph (`trim`/`atrim` + `concat`), mixes with
  `amix` (`normalize=0`, so your volumes stay as set) and protects against clipping with `alimiter`.
  The preview uses an equivalent lookahead limiter, so what you hear is what you get.

## License

MIT – see [LICENSE](LICENSE).
