"""ClipEditor – a small local editor for game clips with multiple audio tracks.

Run:  python app.py           (opens a browser tab, console stays open)
      pythonw ClipEditor.pyw   (app mode: own window, no console, quits when the window is closed)

Listens on 127.0.0.1 only. Uses only the Python standard library plus FFmpeg/ffprobe
(taken from .\\ffmpeg\\bin if present, otherwise from PATH).
"""
import ctypes
import hashlib
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache"                   # thumbnails, metadata, extracted preview audio – safe to delete
THUMBS = CACHE / "thumbs"
AUDIO = CACHE / "audio"
EXPORT_TMP = CACHE / "export_tmp"
META_FILE = CACHE / "meta.json"
CONFIG_FILE = ROOT / "config.json"       # clip folder, export folder, mix track names
DRAFTS_FILE = ROOT / "drafts.json"       # saved edits per clip (your data, not cache)
LIBRARY_FILE = ROOT / "library.json"     # favorites, notes, which exports belong to which clip
DEFAULT_FOLDER = str(Path.home() / "Videos")
EXPORT_SUBFOLDER = "Edited"              # default export folder = <clip folder>\Edited
# Audio tracks with these names are treated as a pre-mixed "everything" track (e.g. OBS track 1).
DEFAULT_MIX_NAMES = ["Mix", "Mixed", "Total", "Master", "Combined", "All", "Gesamt"]
MIX_TITLE = "Mix"                        # name of the mix track in exports
HOST, PORT = "127.0.0.1", int(os.environ.get("CLIPEDITOR_PORT", 8765))

AUDIO_KEEP_CLIPS = 5                     # keep extracted preview audio for this many clips …
AUDIO_MAX_BYTES = 2 * 1024 ** 3          # … and at most this much disk space
EDIT_RE = re.compile(r"_edit\d*$", re.IGNORECASE)


def find_tool(name):
    local = ROOT / "ffmpeg" / "bin" / f"{name}.exe"
    return str(local) if local.exists() else shutil.which(name)


FFMPEG, FFPROBE = find_tool("ffmpeg"), find_tool("ffprobe")

# In app mode (pythonw, no console) every FFmpeg call would otherwise flash a console window.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run(args, timeout=120):
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, creationflags=NO_WINDOW)


def write_json(path, data, indent=None):
    """Write atomically, so a crash never leaves a half-written file."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=indent), "utf-8")
    os.replace(tmp, path)


def read_json(path):
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------- configuration

def load_config():
    cfg = read_json(CONFIG_FILE)
    changed = False
    if not cfg.get("folder"):
        cfg["folder"], changed = DEFAULT_FOLDER, True
    if not cfg.get("export_folder"):
        cfg["export_folder"], changed = str(Path(cfg["folder"]) / EXPORT_SUBFOLDER), True
    if not isinstance(cfg.get("mix_track_names"), list):
        cfg["mix_track_names"], changed = DEFAULT_MIX_NAMES, True
    if changed:
        save_config(cfg)
    return cfg


def save_config(cfg):
    write_json(CONFIG_FILE, cfg, indent=2)


def folders():
    """"clips" = clip folder (read only), "edits" = export folder for the edited files."""
    cfg = load_config()
    return {"clips": Path(cfg["folder"]), "edits": Path(cfg["export_folder"])}


def is_mix_track(title):
    names = {n.strip().lower() for n in load_config()["mix_track_names"]}
    return title.strip().lower() in names


def is_in_edits(path):
    """Is the file inside the export folder?"""
    edits = folders()["edits"]
    return edits.is_dir() and path.parent.resolve() == edits.resolve()


def clip_path(name, where="clips"):
    """Only plain file names directly inside the clip or export folder are allowed."""
    dirs = folders()
    if where not in dirs:
        raise ValueError("Invalid folder")
    if not name or Path(name).name != name or not name.lower().endswith(".mp4"):
        raise ValueError("Invalid clip name")
    p = dirs[where] / name
    if not p.is_file():
        raise FileNotFoundError(f"Clip not found: {name}")
    return p


def migrate_legacy_files():
    """Early versions used German file names – rename them once."""
    for old, new in (("entwuerfe.json", DRAFTS_FILE), ("bibliothek.json", LIBRARY_FILE), ("appfenster", ROOT / "app-profile")):
        if (ROOT / old).exists() and not new.exists():
            try:
                os.replace(ROOT / old, new)
            except OSError:
                pass


# ---------------------------------------------------------------- ffprobe + metadata cache

def parse_rate(s):
    try:
        a, b = s.split("/")
        return float(a) / float(b) if float(b) else 0.0
    except (ValueError, AttributeError):
        return 0.0


def probe(path):
    r = run([FFPROBE, "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(path)], timeout=60)
    if r.returncode != 0:
        msg = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "ffprobe error"
        raise RuntimeError(msg.replace(f"{path}: ", ""))
    d = json.loads(r.stdout or "{}")
    streams = d.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    if v is None:
        raise RuntimeError("No video stream found")
    fps_str = v.get("avg_frame_rate") or v.get("r_frame_rate") or "60/1"
    fps = parse_rate(fps_str) or parse_rate(v.get("r_frame_rate")) or 60.0
    v_start = float(v.get("start_time") or 0)
    duration = float(v.get("duration") or d.get("format", {}).get("duration") or 0)
    tracks = []
    for i, s in enumerate(s for s in streams if s.get("codec_type") == "audio"):
        tags = {k.lower(): str(val) for k, val in (s.get("tags") or {}).items()}
        title = tags.get("title") or tags.get("name") or ""   # MP4 stores track names as "name"
        handler = tags.get("handler_name", "")
        if not title and handler and "handler" not in handler.lower():
            title = handler
        tracks.append({"index": i, "title": title or f"Track {i + 1}",
                       "channels": int(s.get("channels") or 2),
                       "offset": float(s.get("start_time") or 0) - v_start})
    return {"duration": duration, "fps": fps, "fps_str": fps_str,
            "width": v.get("width"), "height": v.get("height"),
            "v_start": v_start, "fmt_start": float(d.get("format", {}).get("start_time") or 0),
            "tracks": tracks}


def file_key(path, st):
    return hashlib.sha1(f"{str(path).lower()}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:16]


def make_thumb(path, duration, out):
    THUMBS.mkdir(parents=True, exist_ok=True)
    r = run([FFMPEG, "-v", "error", "-y", "-ss", f"{duration * 0.2:.3f}", "-i", str(path),
             "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "4", str(out)], timeout=60)
    return r.returncode == 0 and out.exists()


def analyze(path, st):
    entry = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "key": file_key(path, st), "error": None}
    try:
        entry.update(probe(path))
        thumb = THUMBS / f"{entry['key']}.jpg"   # thumbnail at 20 % (the first frame is often black)
        entry["thumb"] = thumb.name if make_thumb(path, entry["duration"], thumb) else None
    except Exception as e:                       # broken file, ffprobe error, timeout …
        entry.update(error=str(e) or type(e).__name__, tracks=[], duration=0, thumb=None)
    return entry


_meta_lock = threading.Lock()


def load_meta():
    return read_json(META_FILE)


def save_meta(meta):
    CACHE.mkdir(exist_ok=True)
    write_json(META_FILE, meta)


def scan_library():
    """Read clip + export folder; only new or changed files are analyzed again."""
    dirs = folders()
    folder, edits = dirs["clips"], dirs["edits"]
    base = {"folder": str(folder), "export_folder": str(edits), "error": None}
    if not folder.is_dir():
        return dict(base, error="Folder does not exist", clips=[])
    sources = [("clips", folder)]
    if edits.is_dir() and edits.resolve() != folder.resolve():
        sources.append(("edits", edits))
    with _meta_lock:
        meta = load_meta()
        files = [(where, p) for where, d in sources for p in d.iterdir()
                 if p.is_file() and p.suffix.lower() == ".mp4"]
        todo = []
        for _, p in files:
            st = p.stat()
            e = meta.get(str(p).lower())
            if not e or e.get("size") != st.st_size or e.get("mtime_ns") != st.st_mtime_ns:
                todo.append((p, st))
        if todo:
            with ThreadPoolExecutor(max_workers=4) as pool:
                for (p, _), entry in zip(todo, pool.map(lambda a: analyze(*a), todo)):
                    meta[str(p).lower()] = entry
        # forget files that no longer exist (only cache files are deleted)
        gone = [k for k in meta if not os.path.exists(k)]
        for k in gone:
            e = meta.pop(k)
            for f in (e.get("thumb"), e.get("sprite")):
                if f:
                    (THUMBS / f).unlink(missing_ok=True)
        if todo or gone:
            save_meta(meta)
        clips = []
        drafts, libmeta = load_drafts(), load_libmeta()
        is_edit = lambda where, p: where == "edits" or bool(EDIT_RE.search(p.stem))
        edit_names = {p.name for where, p in files if is_edit(where, p)}
        edit_bases = {EDIT_RE.sub("", Path(n).stem).lower() for n in edit_names}
        for where, p in files:
            key = str(p).lower()
            e, lm = meta[key], libmeta.get(key, {})
            clips.append({
                # A draft is an original you are working on. Clips in the export folder are never
                # drafts (their settings are still saved).
                "name": p.name, "dir": where,
                "has_draft": where == "clips" and key in drafts and not drafts[key].get("auto"),
                "mtime": e["mtime_ns"] / 1e9, "duration": e.get("duration", 0), "size": e["size"],
                "tracks": len(e.get("tracks", [])), "thumb": f"/thumb/{e['thumb']}" if e.get("thumb") else None,
                "sprite": f"/thumb/{e['sprite']}" if e.get("sprite") else None,
                "is_edit": is_edit(where, p), "error": e.get("error"),
                "fav": bool(lm.get("fav")), "note": lm.get("note", ""),
                "has_export": p.stem.lower() in edit_bases or any(n in edit_names for n in lm.get("exports", [])),
            })
    clips.sort(key=lambda c: c["mtime"], reverse=True)
    start_sprite_worker()
    return dict(base, clips=clips, processed=len(todo))


def clip_info(path):
    st = path.stat()
    with _meta_lock:
        e = load_meta().get(str(path).lower())
    if not e or e.get("size") != st.st_size or e.get("mtime_ns") != st.st_mtime_ns:
        scan_library()
        with _meta_lock:
            e = load_meta()[str(path).lower()]
    if e.get("error"):
        raise RuntimeError(e["error"])
    return e


# ---------------------------------------------------------------- hover preview (sprite)

SPRITE_FRAMES = 10
_sprite_lock = threading.Lock()
_sprite_thread = None


def make_sprite(path, key, duration):
    """10 small frames spread over the clip, side by side in one JPG."""
    out = THUMBS / f"{key}_s.jpg"
    if out.exists():
        return out.name
    tmp = THUMBS / f"{key}_s.tmp.jpg"
    args = [FFMPEG, "-v", "error", "-y"]
    for i in range(SPRITE_FRAMES):
        args += ["-ss", f"{duration * (i + 0.5) / SPRITE_FRAMES:.3f}", "-i", str(path)]
    graph = ";".join(f"[{i}:v:0]trim=end_frame=1,scale=240:-2,setsar=1[f{i}]" for i in range(SPRITE_FRAMES))
    graph += ";" + "".join(f"[f{i}]" for i in range(SPRITE_FRAMES)) + f"hstack=inputs={SPRITE_FRAMES}[out]"
    r = run(args + ["-filter_complex", graph, "-map", "[out]", "-frames:v", "1", "-q:v", "5", str(tmp)], timeout=120)
    if r.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        return None
    os.replace(tmp, out)
    return out.name


def sprite_for(path):
    """Return a clip's sprite, creating it if needed (one at a time to spare the CPU)."""
    key = str(path).lower()
    with _meta_lock:
        e = load_meta().get(key)
    if not e or e.get("error") or not e.get("duration"):
        return None
    if e.get("sprite"):
        return e["sprite"]
    with _sprite_lock:
        name = make_sprite(path, e["key"], e["duration"])
    if name:
        with _meta_lock:
            meta = load_meta()
            if key in meta:
                meta[key]["sprite"] = name
                save_meta(meta)
    return name


def start_sprite_worker():
    """Create missing sprites in the background so hovering works right away."""
    global _sprite_thread
    if _sprite_thread and _sprite_thread.is_alive():
        return

    def work():
        with _meta_lock:
            todo = [k for k, e in load_meta().items() if not e.get("sprite") and not e.get("error")]
        for k in todo:
            if os.path.exists(k):
                sprite_for(Path(k))
    _sprite_thread = threading.Thread(target=work, daemon=True)
    _sprite_thread.start()


# ---------------------------------------------------------------- favorites + notes (library.json)

_lib_lock = threading.Lock()


def load_libmeta():
    return read_json(LIBRARY_FILE)


def update_libmeta(path, **changes):
    """Fields: fav (bool), note (str), exports (list of export names), last_export_sig.
    None / empty removes the field."""
    with _lib_lock:
        data = load_libmeta()
        key = str(path).lower()
        e = data.get(key, {})
        for k, v in changes.items():
            if v in (None, False, "", []):
                e.pop(k, None)
            else:
                e[k] = v
        if e:
            data[key] = e
        else:
            data.pop(key, None)
        write_json(LIBRARY_FILE, data, indent=1)
        return e


# ---------------------------------------------------------------- preview audio tracks

_audio_lock = threading.Lock()


def extract_audio(path, info):
    """Extract every audio track separately as FLAC to cache/audio/<key>/<n>.flac.
    (An HTML video element only plays the first track – the editor plays these via Web Audio.)"""
    n = len(info["tracks"])
    target = AUDIO / info["key"]
    with _audio_lock:
        if not (target / "done").exists() and n:
            tmp = AUDIO / f"{info['key']}.tmp"
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True)
            args = [FFMPEG, "-v", "error", "-y", "-i", str(path)]
            for i in range(n):
                args += ["-map", f"0:a:{i}", "-vn", "-c:a", "flac", str(tmp / f"{i}.flac")]
            r = run(args, timeout=600)
            if r.returncode != 0:
                shutil.rmtree(tmp, ignore_errors=True)
                raise RuntimeError("Could not extract the audio tracks: " + r.stderr.strip()[-300:])
            (tmp / "done").write_text("ok")
            shutil.rmtree(target, ignore_errors=True)
            os.replace(tmp, target)
        if target.exists():
            (target / "done").touch()           # remember "last used"
        cleanup_audio(keep=info["key"])
    return [f"/audio/{info['key']}/{i}.flac" for i in range(n)]


def cleanup_audio(keep):
    if not AUDIO.is_dir():
        return
    dirs = []
    for d in AUDIO.iterdir():
        if d.is_dir() and d.resolve().parent == AUDIO.resolve():
            marker = d / "done"
            used = marker.stat().st_mtime if marker.exists() else 0
            size = sum(f.stat().st_size for f in d.iterdir() if f.is_file())
            dirs.append((used, size, d))
    dirs.sort(key=lambda x: x[0], reverse=True)
    total = 0
    for i, (_, size, d) in enumerate(dirs):
        total += size
        if d.name != keep and (i >= AUDIO_KEEP_CLIPS or total > AUDIO_MAX_BYTES):
            shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- drafts (auto-saved edits)

_draft_lock = threading.Lock()


def load_drafts():
    return read_json(DRAFTS_FILE)


def clean_draft(s):
    """Keep only the expected fields with sane values."""
    f = lambda v: float(v) if isinstance(v, (int, float)) and math.isfinite(v) else 0.0
    return {
        "start": f(s.get("start")), "end": f(s.get("end")), "playhead": f(s.get("playhead")),
        "cuts": [[f(a), f(b)] for a, b in s.get("cuts", [])][:500],
        "tracks": [{"volume": max(0.0, min(2.0, f(t.get("volume")))), "muted": bool(t.get("muted"))}
                   for t in s.get("tracks", [])][:32],
        "saved_at": time.time(),
        **({"auto": True} if s.get("auto") else {}),   # created automatically after an export
    }


def save_draft(path, state):
    """state=None deletes the draft (e.g. when everything is back to default)."""
    with _draft_lock:
        drafts = load_drafts()
        key = str(path).lower()
        if state is None:
            if drafts.pop(key, None) is None:
                return None
        else:
            drafts[key] = clean_draft(state)
        write_json(DRAFTS_FILE, drafts, indent=1)
        return drafts.get(key)


def state_sig(start, end, cuts, tracks):
    """Fingerprint of an edit – tells whether a draft has already been exported exactly like this."""
    r = lambda v: round(float(v), 3)
    tr = [[r(t["volume"]), bool(t["muted"])] for t in sorted(tracks, key=lambda t: t["index"])]
    return json.dumps([r(start), r(end), sorted([r(a), r(b)] for a, b in cuts), tr])


def draft_request(src, where, d):
    """Build an export request from a saved draft."""
    return {"name": src.name, "dir": where, "start": d["start"], "end": d["end"], "cuts": d.get("cuts", []),
            "tracks": [{"index": i, **t} for i, t in enumerate(d.get("tracks", []))]}


def list_drafts():
    """All your drafts (not the automatic ones), with an 'already exported?' flag."""
    drafts, libmeta, lib = load_drafts(), load_libmeta(), scan_library()
    with _meta_lock:
        meta = load_meta()
    out = []
    for c in lib["clips"]:
        src = folders()[c["dir"]] / c["name"]
        key = str(src).lower()
        d = drafts.get(key)
        if not d or d.get("auto") or c.get("error") or c["dir"] == "edits":
            continue
        req = draft_request(src, c["dir"], d)
        fps = meta.get(key, {}).get("fps") or 60
        length = sum(b - a for a, b in kept_ranges(req["start"], req["end"], req["cuts"], fps))
        sig = state_sig(req["start"], req["end"], req["cuts"], req["tracks"])
        out.append({"name": c["name"], "dir": c["dir"], "saved_at": d.get("saved_at"), "length": length,
                    "exported": libmeta.get(key, {}).get("last_export_sig") == sig, "mtime": c["mtime"]})
    return out


# ---------------------------------------------------------------- delete (recycle bin)

class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", ctypes.c_void_p), ("wFunc", ctypes.c_uint), ("pFrom", ctypes.c_wchar_p),
                ("pTo", ctypes.c_wchar_p), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", ctypes.c_int),
                ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", ctypes.c_wchar_p)]


def to_recycle_bin(path):
    """Move a file to the Windows recycle bin (restorable) instead of deleting it for good."""
    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 0x4, 0x10, 0x40, 0x400
    op = _SHFILEOPSTRUCTW(wFunc=FO_DELETE, pFrom=str(path.resolve()) + "\0",   # double null-terminated
                          fFlags=FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI)
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if rc or op.fAnyOperationsAborted or path.exists():
        raise RuntimeError(f"Could not move it to the recycle bin (code {rc}). "
                           "Is the file still open in another program?")


def delete_clip(name, where):
    src = clip_path(name, where)
    with _meta_lock:
        entry = load_meta().get(str(src).lower(), {})
    to_recycle_bin(src)
    save_draft(src, None)
    update_libmeta(src, fav=None, note=None, exports=None, last_export_sig=None)
    if entry.get("key"):                         # drop this clip's preview audio from the cache too
        shutil.rmtree(AUDIO / entry["key"], ignore_errors=True)


def delete_clips(items):
    """Move several clips to the recycle bin; collect errors instead of stopping at the first one."""
    errors = []
    for it in items[:500]:
        try:
            delete_clip(it.get("name"), it.get("dir", "clips"))
        except Exception as e:
            errors.append(f"{it.get('name')}: {e}")
    return dict(scan_library(), delete_errors=errors)   # the scan removes metadata + thumbnails


# ---------------------------------------------------------------- export

# Video encoders, in order of preference. To add one (e.g. AMD AMF):
#   1. add an entry to VIDEO_ENCODERS, e.g.
#      "h264_amf": ["-c:v", "h264_amf", "-quality", "quality", "-rc", "cqp", "-qp_i", "18", "-qp_p", "18"],
#   2. put its name into ENCODER_ORDER before "libx264".
VIDEO_ENCODERS = {
    "h264_nvenc": ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "19", "-b:v", "0"],
    "libx264": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"],
}
ENCODER_ORDER = ["h264_nvenc", "libx264"]
_encoder = None


def pick_encoder():
    """Use the first encoder that actually works (test-encodes a tiny frame)."""
    global _encoder
    if _encoder is None:
        _encoder = "libx264"
        for name in ENCODER_ORDER:
            r = run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "color=s=256x256:d=0.1",
                     "-frames:v", "1", *VIDEO_ENCODERS[name], "-f", "null", "-"], timeout=30)
            if r.returncode == 0:
                _encoder = name
                break
    return _encoder


def kept_ranges(start, end, cuts, fps):
    """Trim [start, end] minus the cut segments -> list of kept ranges (snapped to frames)."""
    snap = lambda t: round(t * fps) / fps
    start, end = snap(start), snap(end)
    cuts = sorted((max(snap(a), start), min(snap(b), end)) for a, b in cuts)
    ranges, pos = [], start
    for a, b in cuts:
        if a > pos:
            ranges.append((pos, a))
        pos = max(pos, b)
    if pos < end:
        ranges.append((pos, end))
    return [(a, b) for a, b in ranges if b - a > 0.5 / fps]


def clean_filename(name):
    """Custom export name: strip forbidden characters and .mp4 (it is added automatically)."""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", str(name or "")).strip()
    name = re.sub(r"\.mp4$", "", name, flags=re.IGNORECASE).strip().rstrip(".")
    return name[:150]


def output_path(src, custom=None):
    out_dir = folders()["edits"]
    out_dir.mkdir(exist_ok=True)                 # only the export folder itself, never missing parents
    base = EDIT_RE.sub("", src.stem)
    n = 1
    while True:
        if custom:                               # "Name.mp4", on conflict "Name (2).mp4"
            out = out_dir / (f"{custom}.mp4" if n == 1 else f"{custom} ({n}).mp4")
        else:
            out = out_dir / f"{base}_edit{'' if n == 1 else n}.mp4"
        if not out.exists() and out.resolve() != src.resolve():
            return out
        n += 1


# Target-size export (e.g. Discord): libx264 in two passes with a calculated bitrate.
SIZE_PRESET = "medium"       # slower than veryfast, but more quality per MB
SIZE_AUDIO_KBPS = 128
HD_MIN_KBPS = 4500           # below this, 720p looks better than 1080p
SIZE_SAFETY = 0.95           # headroom for container overhead and rate variance


def export_plan(info, total, has_audio, target_mb):
    """None = best quality (CRF). Otherwise bitrate/resolution so the file stays below target_mb."""
    if not target_mb:
        return None
    target_bytes = int(target_mb * 1_000_000)    # decimal MB: fits whether a site means MB or MiB
    audio = SIZE_AUDIO_KBPS if has_audio else 0
    video = int(target_bytes * 8 * SIZE_SAFETY / 1000 / total - audio)
    if video < 250:
        raise ValueError(f"At {total:.0f} s the clip is too long for {target_mb:g} MB – please shorten it.")
    height = 720 if video < HD_MIN_KBPS and (info.get("height") or 0) > 720 else None
    quality = ("very good" if video >= 8000 else "good" if video >= HD_MIN_KBPS else
               "okay" if video >= 2500 else "noticeably worse" if video >= 1200 else "much worse")
    return {"target_mb": target_mb, "target_bytes": target_bytes, "video_kbps": video, "audio_kbps": audio,
            "height": height or info.get("height"), "scale": bool(height), "fps": round(info["fps"]),
            "quality": quality}


def build_ffmpeg_args(src, info, ranges, tracks, tmp_out, plan=None, pass_no=None, passlog=None, raw=()):
    """Cut video + audio identically, mix the active tracks (volume -> amix normalize=0 -> alimiter).
    raw = indices of tracks that are additionally kept as separate, unchanged tracks."""
    fps = info["fps"]
    shift = info["v_start"] - info["fmt_start"]       # UI time (0 = first frame) -> filter time
    half = 0.5 / fps                                   # half a frame: exactly (b-a)*fps frames per range
    active = [t for t in tracks if not t["muted"] and t["volume"] > 0]
    act = {t["index"] for t in active}
    raw = list(raw)
    use = sorted(act | set(raw))                       # all tracks that have to be cut
    parts, concat_in = [], ""
    for k, (a, b) in enumerate(ranges):
        parts.append(f"[0:v:0]trim=start={max(0, a - half + shift):.6f}:end={b - half + shift:.6f},"
                     f"setpts=PTS-STARTPTS[v{k}]")
        concat_in += f"[v{k}]"
        for j in use:
            parts.append(f"[0:a:{j}]atrim=start={a + shift:.6f}:end={b + shift:.6f},"
                         f"asetpts=PTS-STARTPTS[a{j}_{k}]")
            concat_in += f"[a{j}_{k}]"
    scale = plan and plan["scale"]
    outs = ("[vcat]" if scale else "[vout]") + "".join(f"[c{j}]" for j in use)
    parts.append(f"{concat_in}concat=n={len(ranges)}:v=1:a={len(use)}{outs}")
    if scale:
        parts.append(f"[vcat]scale=-2:{plan['height']}:flags=lanczos[vout]")
    for j in use:                                      # track goes into the mix AND stays separate -> split
        if j in act and j in raw:
            parts.append(f"[c{j}]asplit=2[x{j}][r{j}]")
    to_mix = lambda j: f"[x{j}]" if j in raw else f"[c{j}]"
    to_raw = lambda j: f"[r{j}]" if j in act else f"[c{j}]"
    if active:
        mixed = ""
        for t in active:
            j = t["index"]
            ch = info["tracks"][j]["channels"]
            layout = "pan=stereo|c0=c0|c1=c0" if ch == 1 else "aformat=channel_layouts=stereo"
            parts.append(f"{to_mix(j)}volume={t['volume']:.4f},{layout},aresample=48000[m{j}]")
            mixed += f"[m{j}]"
        mix = f"amix=inputs={len(active)}:normalize=0:duration=longest," if len(active) > 1 else ""
        parts.append(f"{mixed}{mix}alimiter=limit=0.891:level=0[aout]")
    elif raw:                                          # everything muted but separate tracks wanted: silent mix
        total = sum(b - a for a, b in ranges)
        parts.append(f"anullsrc=channel_layout=stereo:sample_rate=48000,atrim=duration={total:.6f}[aout]")
    args = [FFMPEG, "-hide_banner", "-v", "error", "-y", "-i", str(src),
            "-filter_complex", ";".join(parts), "-map", "[vout]"]
    if plan:
        args += ["-map", "[aout]", "-c:a", "aac", "-b:a", f"{plan['audio_kbps']}k", "-ac", "2"] if active else ["-an"]
        args += ["-c:v", "libx264", "-preset", SIZE_PRESET, "-b:v", f"{plan['video_kbps']}k",
                 "-pass", str(pass_no), "-passlogfile", str(passlog)]
    else:
        if active or raw:
            # track 1 = the mix, followed by the separate tracks with their names (like OBS)
            args += ["-map", "[aout]"] + [x for j in raw for x in ("-map", to_raw(j))]
            args += ["-c:a", "aac", "-b:a", "192k", "-ac:a:0", "2"]
            names = [MIX_TITLE] + [info["tracks"][j]["title"] for j in raw]
            for n, title in enumerate(names):
                args += [f"-metadata:s:a:{n}", f"title={title}", f"-metadata:s:a:{n}", f"handler_name={title}"]
        else:
            args += ["-an"]
        args += VIDEO_ENCODERS[pick_encoder()]
    args += ["-pix_fmt", "yuv420p", "-r", info["fps_str"], "-progress", "pipe:1", "-nostats"]
    if plan and pass_no == 1:
        return args + ["-f", "null", os.devnull]       # pass 1 only analyzes
    return args + ["-movflags", "+faststart", str(tmp_out)]


JOBS = {}


def prepare_export(req):
    src = clip_path(req["name"], req.get("dir", "clips"))
    info = clip_info(src)
    ranges = kept_ranges(float(req["start"]), float(req["end"]), req.get("cuts", []), info["fps"])
    if not ranges:
        raise ValueError("Nothing is left after trimming and cutting")
    tracks = [{"index": int(t["index"]), "volume": max(0.0, min(2.0, float(t["volume"]))),
               "muted": bool(t["muted"])} for t in req.get("tracks", [])
              if 0 <= int(t["index"]) < len(info["tracks"])]
    total = sum(b - a for a, b in ranges)
    target = req.get("target_mb")
    target = max(1.0, min(4000.0, float(target))) if target else None
    has_audio = any(not t["muted"] and t["volume"] > 0 for t in tracks)
    plan = export_plan(info, total, has_audio, target)
    # Separate tracks only for "best quality" (with a target size every MB goes to the picture).
    # Old mix tracks are not kept separately – the new mix replaces them.
    raw = []
    if req.get("multitrack") and not plan:
        raw = [i for i, t in enumerate(info["tracks"]) if not is_mix_track(t["title"])]
    return src, info, ranges, tracks, total, plan, raw


def draft_for_export(info, tracks, raw, total):
    """Settings for the new file: mix track muted, separate tracks at the volumes just used.
    If the old mix track was part of the mix, it is (being the sum of the separate tracks)
    added to every separate track's volume."""
    by = {t["index"]: t for t in tracks}
    on = lambda j: j in by and not by[j]["muted"] and by[j]["volume"] > 0
    g = sum(by[j]["volume"] for j in range(len(info["tracks"]))
            if on(j) and is_mix_track(info["tracks"][j]["title"]))
    out = [{"volume": 1.0, "muted": True}]
    for j in raw:
        v = (by[j]["volume"] if on(j) else 0.0) + g
        out.append({"volume": min(2.0, v) if v > 0 else 1.0, "muted": v <= 0})
    return {"start": 0, "end": total, "cuts": [], "playhead": 0, "tracks": out, "auto": True}


def new_export_job(req):
    """Prepare an export and register it as a job (not started yet)."""
    src, info, ranges, tracks, total, plan, raw = prepare_export(req)
    custom = None
    if str(req.get("output_name") or "").strip():
        custom = clean_filename(req["output_name"])
        if not custom:
            raise ValueError("The file name contains no valid characters")
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "running", "progress": 0.0, "output": None, "error": None, "plan": plan,
                    "encoder": "libx264 2-pass" if plan else pick_encoder(), "expected_duration": total,
                    "audio_tracks": (1 + len(raw)) if raw else int(any(not t["muted"] and t["volume"] > 0 for t in tracks))}
    sig = state_sig(req["start"], req["end"], req.get("cuts", []), tracks)
    return job_id, (job_id, src, info, ranges, tracks, total, plan, custom, raw, sig)


def start_export(req):
    job_id, args = new_export_job(req)
    threading.Thread(target=run_export, args=args, daemon=True).start()
    return job_id


def start_batch(req):
    """Export several drafts one after another (sequentially, so the PC isn't overloaded)."""
    drafts = load_drafts()
    todo = []
    for it in req.get("items", [])[:200]:
        if it.get("dir", "clips") != "clips":         # only originals are drafts
            continue
        src = clip_path(it.get("name"), "clips")
        d = drafts.get(str(src).lower())
        if d:
            todo.append(dict(draft_request(src, "clips", d),
                             multitrack=bool(req.get("multitrack")), target_mb=req.get("target_mb")))
    if not todo:
        raise ValueError("No drafts selected for export")
    batch_id = uuid.uuid4().hex[:12]
    JOBS[batch_id] = {"status": "running", "progress": 0.0, "batch": True, "count": len(todo), "index": 0,
                      "current": None, "phase": None, "results": [], "error": None}

    def work():
        job = JOBS[batch_id]
        for i, r in enumerate(todo):
            job.update(index=i, current=r["name"], phase=None)
            try:
                sub_id, args = new_export_job(r)
                sub = JOBS[sub_id]
                t = threading.Thread(target=run_export, args=args, daemon=True)
                t.start()
                while t.is_alive():
                    job["progress"] = (i + sub["progress"]) / len(todo)
                    job["phase"] = sub.get("phase")
                    t.join(0.3)
                job["results"].append({"name": r["name"], "output": sub.get("output"), "error": sub.get("error"),
                                       "size_mb": sub.get("size_mb")})
            except Exception as e:
                job["results"].append({"name": r["name"], "output": None, "error": str(e)})
        failed = sum(1 for x in job["results"] if x["error"])
        job.update(status="done", progress=1.0, current=None, failed=failed)
    threading.Thread(target=work, daemon=True).start()
    return batch_id


def run_ffmpeg(args, job, total, lo, hi):
    """Run FFmpeg and map its progress onto the range lo..hi."""
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    err = []
    threading.Thread(target=lambda: err.extend(p.stderr), daemon=True).start()
    for line in p.stdout:
        if line.startswith("out_time_us="):
            try:
                job["progress"] = min(0.99, lo + (hi - lo) * int(line.split("=")[1]) / 1e6 / total)
            except ValueError:
                pass
    if p.wait() != 0:
        raise RuntimeError("".join(err).strip()[-600:] or "FFmpeg error")


def run_export(job_id, src, info, ranges, tracks, total, plan=None, custom=None, raw=(), sig=None):
    job = JOBS[job_id]
    EXPORT_TMP.mkdir(parents=True, exist_ok=True)
    tmp_out = EXPORT_TMP / f"{job_id}.mp4"
    passlog = EXPORT_TMP / f"{job_id}-pass"
    try:
        if not plan:
            run_ffmpeg(build_ffmpeg_args(src, info, ranges, tracks, tmp_out, raw=raw), job, total, 0, 1)
        else:
            job["phase"] = "pass 1/2"
            run_ffmpeg(build_ffmpeg_args(src, info, ranges, tracks, tmp_out, plan, 1, passlog), job, total, 0, 0.5)
            job["phase"] = "pass 2/2"
            run_ffmpeg(build_ffmpeg_args(src, info, ranges, tracks, tmp_out, plan, 2, passlog), job, total, 0.5, 1)
            size = tmp_out.stat().st_size
            if size > plan["target_bytes"]:       # rare: one correction pass with a matching lower bitrate
                job["phase"] = "correction pass"
                plan = dict(plan, video_kbps=int(plan["video_kbps"] * plan["target_bytes"] / size * 0.95))
                run_ffmpeg(build_ffmpeg_args(src, info, ranges, tracks, tmp_out, plan, 2, passlog), job, total, 0.5, 1)
            if tmp_out.stat().st_size > plan["target_bytes"]:
                raise RuntimeError("The file is still above the target size – please shorten the clip.")
        size_mb = tmp_out.stat().st_size / 1_000_000
        out = output_path(src, custom)            # pick the name at the very end; never touch the original
        shutil.move(str(tmp_out), str(out))
        exports = load_libmeta().get(str(src).lower(), {}).get("exports", [])
        update_libmeta(src, exports=(exports + [out.name])[-50:],   # for the "Unedited" filter
                       last_export_sig=sig)                         # "this draft was exported like this"
        if raw:                                   # reopening the new file starts with the same mix
            save_draft(out, draft_for_export(info, tracks, raw, total))
        # Once an original has been exported, it is no longer a draft.
        draft_removed = not is_in_edits(src)
        if draft_removed:
            save_draft(src, None)
        job.update(status="done", progress=1.0, output=out.name, output_dir=str(out.parent), size_mb=round(size_mb, 2),
                   draft_removed=draft_removed)
    except Exception as e:
        job.update(status="error", error=str(e))
    finally:
        tmp_out.unlink(missing_ok=True)
        for f in EXPORT_TMP.glob(f"{job_id}-pass*"):     # x264 analysis files (own cache only)
            f.unlink(missing_ok=True)


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, ctype):
        """Static file with HTTP range support (needed for seeking in the video)."""
        size = path.stat().st_size
        start, end = 0, size - 1
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def handle_safe(self, fn):
        try:
            fn()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass                                  # browsers cancel range requests all the time while seeking
        except (ValueError, FileNotFoundError) as e:
            self.send_json({"error": str(e)}, 400)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)

    def do_GET(self):
        self.handle_safe(self.route_get)

    def do_POST(self):
        self.handle_safe(self.route_post)

    def route_get(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        path = u.path
        if path == "/":
            self.send_file(ROOT / "index.html", "text/html; charset=utf-8")
        elif path == "/favicon.ico":
            self.send_file(ROOT / "app.ico", "image/x-icon")
        elif path == "/api/ping":                     # heartbeat from the window (app mode)
            note_ping()
            self.send_json({"ok": True})
        elif path == "/api/library":
            if not (FFMPEG and FFPROBE):
                return self.send_json({"error": "FFmpeg/ffprobe not found", "clips": [],
                                       "folder": str(folders()["clips"])})
            self.send_json(scan_library())
        elif path == "/api/open":
            where = q.get("dir", "clips")
            src = clip_path(q.get("name"), where)
            info = clip_info(src)
            urls = extract_audio(src, info)
            self.send_json({"name": src.name, "dir": where, "draft": load_drafts().get(str(src).lower()),
                            "video": f"/video?dir={where}&name=" + quote(src.name),
                            "duration": info["duration"], "fps": info["fps"],
                            "width": info["width"], "height": info["height"],
                            "tracks": [dict(t, url=u, is_mix=is_mix_track(t["title"]))
                                       for t, u in zip(info["tracks"], urls)]})
        elif path == "/api/drafts":                   # all your drafts, for "Export drafts"
            self.send_json({"drafts": list_drafts()})
        elif path == "/api/sprite":                   # create a hover sprite on demand
            name = sprite_for(clip_path(q.get("name"), q.get("dir", "clips")))
            self.send_json({"sprite": f"/thumb/{name}" if name else None})
        elif path == "/video":
            self.send_file(clip_path(q.get("name"), q.get("dir", "clips")), "video/mp4")
        elif m := re.fullmatch(r"/thumb/([0-9a-f]{16}(?:_s)?\.jpg)", path):
            f = THUMBS / m.group(1)
            if not f.exists():
                raise FileNotFoundError("No thumbnail")
            self.send_file(f, "image/jpeg")
        elif m := re.fullmatch(r"/audio/([0-9a-f]{16})/(\d+)\.flac", path):
            f = AUDIO / m.group(1) / f"{m.group(2)}.flac"
            if not f.exists():
                raise FileNotFoundError("Audio track not in cache")
            self.send_file(f, "audio/flac")
        elif m := re.fullmatch(r"/api/export/([0-9a-f]{12})", path):
            job = JOBS.get(m.group(1))
            if not job:
                raise FileNotFoundError("Unknown export")
            self.send_json(job)
        else:
            self.send_json({"error": "Not found"}, 404)

    def route_post(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/api/config":
            clean = lambda s: str(s or "").strip().strip('"')
            folder = Path(clean(body.get("folder")))
            if not clean(body.get("folder")) or not folder.is_dir():
                raise ValueError(f"Clip folder does not exist: {folder}")
            export = Path(clean(body.get("export_folder")) or folder / EXPORT_SUBFOLDER)
            if not export.is_dir():
                if not export.parent.is_dir():
                    raise ValueError(f"Parent folder is missing: {export.parent}")
                export.mkdir()
            cfg = load_config()
            cfg["folder"], cfg["export_folder"] = str(folder), str(export)
            save_config(cfg)
            self.send_json(scan_library())
        elif self.path == "/api/export":
            self.send_json({"job": start_export(body)})
        elif self.path == "/api/export_batch":        # export several drafts one after another
            self.send_json({"job": start_batch(body)})
        elif self.path == "/api/plan":                # preview: what the chosen size results in
            *_, total, plan, raw = prepare_export(body)
            self.send_json({"duration": total, "plan": plan, "raw_tracks": len(raw)})
        elif self.path == "/api/draft":
            src = clip_path(body.get("name"), body.get("dir", "clips"))
            state = body.get("state")
            saved = save_draft(src, state if isinstance(state, dict) else None)
            self.send_json({"ok": True, "saved_at": saved and saved["saved_at"]})
        elif self.path == "/api/youtube":             # select the file in Explorer (to drag it into YouTube)
            src = clip_path(body.get("name"), body.get("dir", "clips"))
            subprocess.Popen(f'explorer.exe /select,"{src}"')
            self.send_json({"path": str(src)})
        elif self.path == "/api/delete":              # {items: [{name, dir}, …]} or a single clip
            items = body.get("items") or [{"name": body.get("name"), "dir": body.get("dir", "clips")}]
            self.send_json(delete_clips(items))
        elif self.path == "/api/clipmeta":            # set favorite / note
            src = clip_path(body.get("name"), body.get("dir", "clips"))
            changes = {}
            if "fav" in body:
                changes["fav"] = bool(body["fav"])
            if "note" in body:
                changes["note"] = str(body["note"] or "").strip()[:200]
            e = update_libmeta(src, **changes)
            self.send_json({"fav": bool(e.get("fav")), "note": e.get("note", "")})
        elif self.path == "/api/bye":                 # the window is being closed
            note_ping(closing=True)
            self.send_json({"ok": True})
        else:
            self.send_json({"error": "Not found"}, 404)


class Server(ThreadingHTTPServer):
    # On Windows, SO_REUSEADDR lets a second server bind the same port. Bind exclusively,
    # so a second start notices "port in use" and only opens another window.
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


# ---------------------------------------------------------------- app mode (own window, like a desktop app)
# Started via ClipEditor.pyw: no console window, the UI runs in its own Chromium app window
# (no tabs or address bar). Closing the window quits ClipEditor – but never during a running export.

APP_PROFILE = ROOT / "app-profile"       # separate browser profile just for the app window
APP_IDLE_SECONDS = 120                   # quit after this long without a heartbeat from a window
APP_BROWSERS = [
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe",
]
_last_ping = None


def note_ping(closing=False):
    """Heartbeat from a window. closing=True: quit in ~12 s unless another window reports in
    (a page reload reports in again right away)."""
    global _last_ping
    _last_ping = time.time() - (APP_IDLE_SECONDS - 12 if closing else 0)


def open_app_window(url):
    for p in APP_BROWSERS:
        exe = os.path.expandvars(p)
        if os.path.isfile(exe):
            subprocess.Popen([exe, f"--app={url}", f"--user-data-dir={APP_PROFILE}", "--no-first-run",
                              "--no-default-browser-check", "--window-size=1600,950",
                              "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
                              "--disable-backgrounding-occluded-windows"])
            return
    webbrowser.open(url)                 # no Chromium browser found: use a normal browser tab


def app_watchdog(server):
    started = time.time()
    while True:
        time.sleep(3)
        if any(j.get("status") == "running" for j in JOBS.values()):
            continue                     # an export is running: never quit
        ref = _last_ping or started
        if time.time() - ref > (APP_IDLE_SECONDS if _last_ping else 180):
            server.shutdown()
            return


def main(app_mode=None):
    if app_mode is None:
        app_mode = "--app" in sys.argv
    if app_mode and sys.stdout is None:  # pythonw: write output + errors to a log file
        CACHE.mkdir(exist_ok=True)
        log = open(CACHE / "clipeditor.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = log
        print(f"\n--- start {time.strftime('%Y-%m-%d %H:%M:%S')}")
    migrate_legacy_files()
    for d in (THUMBS, AUDIO):
        d.mkdir(parents=True, exist_ok=True)
    load_config()
    url = f"http://{HOST}:{PORT}/"
    show = open_app_window if app_mode else webbrowser.open
    try:
        server = Server((HOST, PORT), Handler)
    except OSError:
        print(f"Port {PORT} is in use – ClipEditor is probably already running. Opening {url}")
        show(url)
        return
    shutil.rmtree(EXPORT_TMP, ignore_errors=True)   # leftovers of aborted exports (safe: no second server)
    print(f"ClipEditor running at {url}" + ("" if app_mode else "  (close this window or press Ctrl+C to quit)"))
    if not (FFMPEG and FFPROBE):
        print("WARNING: ffmpeg/ffprobe not found (expected in .\\ffmpeg\\bin or on PATH)")
    if os.environ.get("CLIPEDITOR_NO_BROWSER") != "1":
        threading.Timer(0.3, lambda: show(url)).start()
    if app_mode:
        threading.Thread(target=app_watchdog, args=(server,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    print("ClipEditor stopped")


if __name__ == "__main__":
    main()
