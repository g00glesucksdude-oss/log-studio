#!/usr/bin/env python3
"""ADPCM Log Studio, cloud backend.

Drive layout (created automatically, in the Drive account):
  Logger workspace/
    uploads/<job>/   source audio, _background.*, _peaks.bin, _proxy.m4a (or _proxy_NNN.m4a pieces),
                     _meta.json
    outputs/audio/   exported .ogg (public link)
    outputs/video/   optional public copy of exported .mp4
    index/           the Audio Tree: one small file per job (job_*.json), export (exp_*.json) and
                     parallel-export plan (plan_*.json), so parallel jobs never overwrite each other
    _index.json      a combined copy of the index for other programs (rebuilt after every run)

  ingest : job folder -> merge (oldest->newest) -> waveform + playable copy (split into 1-hour
           pieces when >20 h) + metadata -> record the job in the Audio Tree -> wipe runner temp.
  export : job folder + segment queue -> .ogg / .mp4 parts
           -> video: YouTube (default) and optional public Drive copy; audio: Drive
           -> record in the Audio Tree -> wipe runner temp files.

Auth from env: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET,
GOOGLE_REFRESH_TOKEN (Drive account), YT_REFRESH_TOKEN (YouTube account, optional).
"""
import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

AUDIO_EXTS = {".wav", ".wave", ".bwf", ".adpcm", ".au", ".aif", ".aiff", ".flac",
              ".mp3", ".ogg", ".opus", ".m4a", ".aac", ".wma"}
FPS = 25
LITE_FPS = 12                         # half-rate visualizer, about 2x faster
STATIC_FPS = 2
GIF_FPS = 15
VIDEO_CRF = 26
WAVE_COLORS = ["0x00E5FF", "0xFF4081"]
CHUNK = 1 << 20
UPLOAD_CHUNK = 64 * 1024 * 1024        # multiple of 256 KB
PEAKS_PER_SEC = 200
YT_MAX_PART = 11 * 3600                # YouTube's limit is 12 h
SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/drive"]
DERIVED = ("_peaks.bin", "_proxy.m4a", "_meta.json")  # exact names; pieces are cleaned by prefix
PROXY_HOUR = 3600          # seconds per playable piece
PROXY_SPLIT_H = 20         # split into pieces when longer than this many hours
FOLDER = "application/vnd.google-apps.folder"
INDEX_NAME = "_index.json"


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def fmt_time(t):
    t = max(0, int(t))
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}"


def human_size(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.1f} {u}"
        n /= 1024


def natural_key(s):
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", s)]


def split_parts(segments, part_frames):
    """Split [(start,end),...] frame ranges into parts of at most part_frames."""
    parts, cur, room = [], [], part_frames
    for s, e in segments:
        while s < e:
            take = min(e - s, room)
            cur.append((s, s + take))
            s += take
            room -= take
            if room == 0:
                parts.append(cur)
                cur, room = [], part_frames
    if cur:
        parts.append(cur)
    return parts


# ------------------------------------------------------------------ Google
def _creds(refresh_token, scope):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    c = Credentials(None, refresh_token=refresh_token,
                    client_id=os.environ["GOOGLE_CLIENT_ID"],
                    client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
                    token_uri="https://oauth2.googleapis.com/token", scopes=[scope])
    c.refresh(Request())               # auto-refreshes again during multi-hour runs
    return c


def build_services(need_youtube):
    from googleapiclient.discovery import build
    drive = build("drive", "v3", cache_discovery=False,
                  credentials=_creds(os.environ["GOOGLE_REFRESH_TOKEN"], SCOPES[1]))
    yt = None
    if need_youtube:
        tok = os.environ.get("YT_REFRESH_TOKEN") or os.environ["GOOGLE_REFRESH_TOKEN"]
        yt = build("youtube", "v3", cache_discovery=False, credentials=_creds(tok, SCOPES[0]))
    return drive, yt


def list_folder(drive, folder_id):
    files, token = [], None
    while True:
        r = drive.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(id,name,mimeType,size,createdTime,modifiedTime)",
            pageSize=1000, pageToken=token, supportsAllDrives=True,
            includeItemsFromAllDrives=True).execute()
        files += r["files"]
        token = r.get("nextPageToken")
        if not token:
            break
    return files


def audio_files(files):
    """Source audio only: skips folders, our own _derived files and the _background image."""
    return [f for f in files
            if not f["name"].startswith("_")
            and not f["mimeType"].startswith("application/vnd.google-apps")
            and (Path(f["name"]).suffix.lower() in AUDIO_EXTS or f["mimeType"].startswith("audio/"))]


def find_background(files):
    return next((f for f in files if f["name"].lower().startswith("_background.")), None)


def download(drive, file_id, dest):
    from googleapiclient.http import MediaIoBaseDownload
    req = drive.files().get_media(fileId=file_id, supportsAllDrives=True)
    with open(dest, "wb") as fh:
        dl = MediaIoBaseDownload(fh, req, chunksize=UPLOAD_CHUNK)
        done = False
        while not done:
            _, done = dl.next_chunk(num_retries=5)


def resumable_upload(request, label):
    from googleapiclient.errors import HttpError
    resp, retries, last_pct = None, 0, -10
    while resp is None:
        try:
            status, resp = request.next_chunk()
            if status and int(status.progress() * 100) >= last_pct + 10:
                last_pct = int(status.progress() * 100)
                log(f"{label}: {last_pct}%")
        except HttpError as e:
            if e.resp.status in (500, 502, 503, 504) and retries < 10:
                retries += 1
                time.sleep(2 ** min(retries, 6))
                continue
            raise
        except (ConnectionError, TimeoutError, OSError):
            if retries < 10:
                retries += 1
                time.sleep(2 ** min(retries, 6))
                continue
            raise
    return resp


def youtube_upload(yt, path, title, description):
    from googleapiclient.http import MediaFileUpload
    body = {"snippet": {"title": title[:100], "description": description[:4900], "categoryId": "22"},
            "status": {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": False}}
    media = MediaFileUpload(path, mimetype="video/mp4", chunksize=UPLOAD_CHUNK, resumable=True)
    resp = resumable_upload(yt.videos().insert(part="snippet,status", body=body, media_body=media), "YouTube")
    privacy = resp.get("status", {}).get("privacyStatus")
    if privacy != "unlisted":
        log(f"WARNING: YouTube set privacy to '{privacy}'. The API project probably hasn't "
            f"passed the YouTube API audit, so uploads are locked private.")
    return f"https://youtu.be/{resp['id']}", privacy


def drive_upload(drive, path, folder_id, name, mime, public, description=""):
    from googleapiclient.http import MediaFileUpload
    media = MediaFileUpload(path, mimetype=mime, chunksize=UPLOAD_CHUNK, resumable=True)
    body = {"name": name, "parents": [folder_id]}
    if description:
        body["description"] = description
    req = drive.files().create(body=body, media_body=media,
                               fields="id,webViewLink", supportsAllDrives=True)
    resp = resumable_upload(req, "Drive")
    if public:
        drive.permissions().create(fileId=resp["id"], body={"type": "anyone", "role": "reader"},
                                   supportsAllDrives=True).execute()
    return resp["id"], resp["webViewLink"]


# ------------------------------------------------------------------ workspace + Audio Tree
def parents_of(drive, fid):
    r = drive.files().get(fileId=fid, fields="id,parents", supportsAllDrives=True).execute()
    return r.get("parents", [])


def get_or_create_folder(drive, name, parent):
    q = (f"name='{name}' and mimeType='{FOLDER}' and '{parent}' in parents and trashed=false")
    r = drive.files().list(q=q, fields="files(id)", supportsAllDrives=True,
                           includeItemsFromAllDrives=True).execute()
    if r["files"]:
        return r["files"][0]["id"]
    return drive.files().create(body={"name": name, "mimeType": FOLDER, "parents": [parent]},
                                fields="id", supportsAllDrives=True).execute()["id"]


def workspace_of(drive, job_id):
    """job -> uploads -> Logger workspace. Falls back to the job's parent for old layouts."""
    up = parents_of(drive, job_id)
    if not up:
        sys.exit("Could not find the job folder's parent in Drive.")
    gp = parents_of(drive, up[0])
    return gp[0] if gp else up[0]


def drive_exists(drive, fid):
    from googleapiclient.errors import HttpError
    try:
        r = drive.files().get(fileId=fid, fields="id,trashed", supportsAllDrives=True).execute()
        return not r.get("trashed")
    except HttpError as e:
        if e.resp.status == 404:
            return False
        raise


def index_dir(drive, ws):
    return get_or_create_folder(drive, "index", ws)


def _children(drive, parent):
    out, token = [], None
    while True:
        r = drive.files().list(q=f"'{parent}' in parents and trashed=false",
                               fields="nextPageToken, files(id,name)", pageSize=1000, pageToken=token,
                               supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
        out += r["files"]
        token = r.get("nextPageToken")
        if not token:
            return out


def _json_media(obj):
    from googleapiclient.http import MediaIoBaseUpload
    return MediaIoBaseUpload(io.BytesIO(json.dumps(obj, indent=1).encode()),
                             mimetype="application/json", resumable=False)


def put_entry(drive, ws, name, obj):
    """Create or replace one small index file. Each run writes only its own file."""
    d = index_dir(drive, ws)
    existing = next((f["id"] for f in _children(drive, d) if f["name"] == name), None)
    if existing:
        drive.files().update(fileId=existing, media_body=_json_media(obj), supportsAllDrives=True).execute()
    else:
        drive.files().create(body={"name": name, "parents": [d]}, media_body=_json_media(obj),
                             fields="id", supportsAllDrives=True).execute()


def load_index(drive, ws):
    """Returns {"jobs": {id: entry}, "exports": [entry], "plans": [entry], "files": {name: file_id}}."""
    data = {"version": 1, "jobs": {}, "exports": [], "plans": [], "files": {}}
    for f in _children(drive, index_dir(drive, ws)):
        try:
            obj = json.loads(drive.files().get_media(fileId=f["id"], supportsAllDrives=True).execute())
        except Exception:
            continue
        data["files"][f["name"]] = f["id"]
        if f["name"].startswith("job_"):
            data["jobs"][obj.get("id") or f["name"][4:-5]] = obj
        elif f["name"].startswith("exp_"):
            data["exports"].append(obj)
        elif f["name"].startswith("plan_"):
            data["plans"].append(obj)
    return data


def prune_index(drive, ws, data):
    """Remove entries whose Drive job folder or export file was deleted or trashed."""
    dead = []
    for jid in list(data["jobs"]):
        if not drive_exists(drive, jid):
            dead.append(f"job_{jid}.json")
            del data["jobs"][jid]
    keep = []
    for e in data["exports"]:
        if e.get("driveFileId") and not drive_exists(drive, e["driveFileId"]):
            dead.append(f"exp_{e['id']}.json")
        else:
            keep.append(e)
    data["exports"] = keep
    for name in dead:
        fid = data["files"].pop(name, None)
        if fid:
            drive.files().delete(fileId=fid, supportsAllDrives=True).execute()


def compile_snapshot(drive, ws, data):
    """Combined _index.json for other programs. Derived data: rebuilt whenever a run finishes."""
    from googleapiclient.http import MediaIoBaseUpload
    snap = {"version": 1, "updated": datetime.now(timezone.utc).isoformat(),
            "jobs": data["jobs"], "exports": data["exports"]}
    media = _json_media(snap)
    r = drive.files().list(q=f"name='{INDEX_NAME}' and '{ws}' in parents and trashed=false",
                           fields="files(id)", supportsAllDrives=True,
                           includeItemsFromAllDrives=True).execute()
    if r["files"]:
        drive.files().update(fileId=r["files"][0]["id"], media_body=media, supportsAllDrives=True).execute()
    else:
        drive.files().create(body={"name": INDEX_NAME, "parents": [ws]}, media_body=media,
                             fields="id", supportsAllDrives=True).execute()


def refresh_index(drive, ws, extra=None):
    """After a run: add this run's entry, drop dead entries, rebuild the combined copy."""
    if extra:
        put_entry(drive, ws, extra[0], extra[1])
    data = load_index(drive, ws)
    prune_index(drive, ws, data)
    compile_snapshot(drive, ws, data)


# ------------------------------------------------------------------ audio
def probe_format(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=sample_rate,channels", "-of", "csv=p=0", path],
            capture_output=True, text=True, check=True).stdout.strip()
        rate, ch = out.split(",")[:2]
        return int(rate), max(1, min(int(ch), 2))
    except Exception:
        return 44100, 2


def decode_append(src, out, rate, ch):
    """Decode src to raw s16le appended to `out`. Returns frames written (0 on failure)."""
    fb = 2 * ch
    start = out.tell()
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", src,
           "-vn", "-sn", "-dn", "-map", "0:a:0", "-f", "s16le", "-acodec", "pcm_s16le",
           "-ar", str(rate), "-ac", str(ch), "pipe:1"]
    with tempfile.TemporaryFile() as err:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err)
        got = 0
        while True:
            buf = p.stdout.read(CHUNK)
            if not buf:
                break
            out.write(buf)
            got += len(buf)
        rc = p.wait()
        if rc != 0 or got < fb:
            err.seek(0)
            log(f"  skipped (decode failed): {err.read().decode(errors='replace')[-200:]}")
            out.flush()
            out.truncate(start)
            out.seek(start)
            return 0
    extra = got % fb
    if extra:
        out.flush()
        out.truncate(start + got - extra)
        out.seek(start + got - extra)
    return (got - extra) // fb


def build_master(sources, master_path, fetch, rate=None, ch=None):
    """sources: [(name, handle)]. Returns rate, ch, total_frames, [(name, start_frame)], [skipped].
    Pass rate/ch to force the format used by an earlier ingest."""
    boundaries, skipped = [], []
    work = Path(master_path).parent
    with open(master_path, "wb") as out:
        total = 0
        for i, (name, handle) in enumerate(sources, 1):
            local = fetch(handle, work / f"src_{i}{Path(name).suffix.lower() or '.bin'}")
            if rate is None:
                rate, ch = probe_format(str(local))
                log(f"Master format: {rate} Hz, {ch} ch")
            log(f"Decoding {i}/{len(sources)}: {name}")
            frames = decode_append(str(local), out, rate, ch)
            if str(local).startswith(str(work)):
                os.remove(local)                  # free disk as we go
            if frames:
                boundaries.append((name, total))
                total += frames
            else:
                skipped.append(name)
    return rate, ch, total, boundaries, skipped


def write_peaks(master, rate, ch, total, out_path):
    """Min/max envelope of channel 0 as int8 pairs, PEAKS_PER_SEC bins per second."""
    import numpy as np
    bin_f = max(1, rate // PEAKS_PER_SEC)
    nbins = -(-total // bin_f)
    out = np.zeros(nbins * 2, np.int8)
    mm = np.memmap(master, dtype="<i2", mode="r")
    block = 50000
    for b0 in range(0, nbins, block):
        b1 = min(nbins, b0 + block)
        seg = np.asarray(mm[b0 * bin_f * ch: min(total, b1 * bin_f) * ch]).reshape(-1, ch)[:, 0]
        n, full = len(seg), len(seg) // bin_f
        mn = np.zeros(b1 - b0, np.int32)
        mx = np.zeros(b1 - b0, np.int32)
        if full:
            r = seg[:full * bin_f].reshape(full, bin_f)
            mn[:full], mx[:full] = r.min(1), r.max(1)
        if n > full * bin_f:
            tail = seg[full * bin_f:]
            mn[full], mx[full] = tail.min(), tail.max()
        out[2 * b0:2 * b1:2] = np.clip(np.floor(mn / 256.0), -128, 127)
        out[2 * b0 + 1:2 * b1:2] = np.clip(np.ceil(mx / 256.0), -128, 127)
    del mm
    out.tofile(out_path)
    return bin_f, nbins


def write_proxy(master, rate, ch, total, out_path):
    """Small mono AAC copy the browser can play (single file)."""
    prate = min(rate, 22050)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "s16le", "-ar", str(rate),
           "-ac", str(ch), "-i", "pipe:0", "-ac", "1", "-ar", str(prate),
           "-c:a", "aac", "-b:a", "24k", "-movflags", "+faststart", out_path]
    feed_ffmpeg(cmd, master, [(0, total)], ch)


def write_proxy_pieces(master, rate, ch, total, work):
    """Write 1-hour AAC pieces when the log is long; otherwise a single _proxy.m4a.
    Returns list of dicts for _meta.json: [{"name", "start", "dur"}, ...]."""
    dur = total / rate
    if dur <= PROXY_SPLIT_H * 3600:
        path = work / "_proxy.m4a"
        write_proxy(master, rate, ch, total, str(path))
        return [{"name": "_proxy.m4a", "start": 0.0, "dur": dur}]

    pieces = []
    hour_frames = int(PROXY_HOUR * rate)
    idx = 0
    start = 0
    while start < total:
        end = min(start + hour_frames, total)
        name = f"_proxy_{idx:03d}.m4a"
        path = work / name
        prate = min(rate, 22050)
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "s16le", "-ar", str(rate),
               "-ac", str(ch), "-i", "pipe:0", "-ac", "1", "-ar", str(prate),
               "-c:a", "aac", "-b:a", "24k", "-movflags", "+faststart", str(path)]
        feed_ffmpeg(cmd, master, [(start, end)], ch, label=f"proxy {idx}")
        pieces.append({"name": name, "start": start / rate, "dur": (end - start) / rate})
        start = end
        idx += 1
    return pieces


def feed_ffmpeg(cmd, master, ranges, ch, label="encode"):
    fb = 2 * ch
    total = sum((b - a) * fb for a, b in ranges)
    errbuf = tempfile.TemporaryFile()
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errbuf)
    sent, last, rc = 0, time.monotonic(), None
    try:
        with open(master, "rb") as src:
            for a, b in ranges:
                src.seek(a * fb)
                rem = (b - a) * fb
                while rem > 0:
                    buf = src.read(min(CHUNK, rem))
                    if not buf:
                        break
                    try:
                        proc.stdin.write(buf)
                    except (BrokenPipeError, OSError):
                        rem = 0
                        break
                    rem -= len(buf)
                    sent += len(buf)
                    if time.monotonic() - last > 60:
                        last = time.monotonic()
                        log(f"  {label} {sent / max(total, 1) * 100:.0f}%")
        try:
            proc.stdin.close()
        except Exception:
            pass
        rc = proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
    if rc != 0:
        errbuf.seek(0)
        raise RuntimeError("ffmpeg failed: " + errbuf.read().decode(errors="replace")[-600:])
    errbuf.close()


# ------------------------------------------------------------------ export
def build_cmd(opts, rate, ch, out):
    base = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "s16le", "-ar", str(rate), "-ac", str(ch), "-i", "pipe:0"]
    if opts["kind"] == "audio":
        return base + ["-c:a", "libvorbis", "-q:a", str(opts["quality"]), out]
    W, H = opts["res"].split("x")
    bg = opts.get("bg")
    is_gif = bool(bg) and bg.lower().endswith(".gif")
    if opts["mode"] == "still":
        fps = GIF_FPS if is_gif else STATIC_FPS
        if is_gif:
            src = ["-stream_loop", "-1", "-i", bg]
        elif bg:
            src = ["-loop", "1", "-framerate", str(fps), "-i", bg]
        else:
            src = ["-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:r={fps}"]
        fc = (f"[1:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
              f"setsar=1,fps={fps},format=yuv420p[v]")
        return (base + src + ["-filter_complex", fc, "-map", "[v]", "-map", "0:a",
                              "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26", "-r", str(fps)]
                + ([] if is_gif else ["-tune", "stillimage"])
                + ["-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", out])
    fps = LITE_FPS if opts.get("lite") else FPS
    colors = "|".join(WAVE_COLORS[i % len(WAVE_COLORS)] for i in range(ch))
    if is_gif:
        bg_in = ["-stream_loop", "-1", "-i", bg]
    elif bg:
        bg_in = ["-loop", "1", "-framerate", str(fps), "-i", bg]
    else:
        bg_in = ["-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:r={fps}"]
    fc = (f"[0:a]showwaves=s={W}x{H}:mode=cline:rate={fps}:colors={colors},format=rgba[wv];"
          f"[1:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,format=rgba[bg];"
          f"[bg][wv]overlay=shortest=1:format=auto,format=yuv420p[v]")
    return base + bg_in + ["-filter_complex", fc, "-map", "[v]", "-map", "0:a",
                           "-c:v", "libx264", "-preset", "veryfast", "-crf", str(VIDEO_CRF), "-r", str(fps),
                           "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", out]


# ------------------------------------------------------------------ common
def gather(a, drive):
    """Returns sources [(name, handle)], fetch(handle, dest)->path, and the Drive file list (or None)."""
    if a.local_dir:
        paths = sorted((p for p in Path(a.local_dir).iterdir()
                        if p.suffix.lower() in AUDIO_EXTS and not p.name.startswith("_")),
                       key=lambda p: natural_key(p.name))
        return [(p.name, p) for p in paths], (lambda h, dest: h), None
    all_files = list_folder(drive, a.source_folder)
    files = sorted(audio_files(all_files), key=lambda f: natural_key(f["name"]))
    return ([(f["name"], f["id"]) for f in files],
            (lambda h, dest: (download(drive, h, str(dest)), dest)[1]), all_files)


def select_ranged(sources, meta, segs_sec):
    """Pick only the source files that overlap the segments, using the ingest metadata.
    Returns (selected_sources, offset_frames) or None when the files don't match the metadata."""
    skipped = list(meta.get("skipped", []))
    mf, rate, chosen, k = meta["files"], meta["rate"], [], 0
    for src in sources:
        disp = display_name(src[0])
        if k < len(mf) and mf[k]["name"] == disp:
            end = mf[k + 1]["start"] if k + 1 < len(mf) else meta["duration"]
            chosen.append((src, mf[k]["start"], end))
            k += 1
        elif disp in skipped:
            skipped.remove(disp)
        else:
            return None
    if k != len(mf):
        return None
    lo, hi = min(s for s, _ in segs_sec), max(e for _, e in segs_sec)
    sel = [c for c in chosen if c[2] > lo and c[1] < hi]
    if not sel:
        return None
    return [c[0] for c in sel], int(round(sel[0][1] * rate))


def display_name(name):
    return re.sub(r"^\d{4}_", "", name)


def cmd_ingest(a, drive):
    work = Path(a.workdir)
    sources, fetch, all_files = gather(a, drive)
    if not sources:
        sys.exit("No audio files found.")
    log(f"{len(sources)} files")
    master = str(work / "master.pcm")
    rate, ch, total, bounds, skipped = build_master(sources, master, fetch)
    if total == 0:
        sys.exit("Nothing could be decoded.")
    log(f"Master: {fmt_time(total / rate)} ({human_size(os.path.getsize(master))})")
    log("Computing waveform")
    bin_f, nbins = write_peaks(master, rate, ch, total, str(work / "_peaks.bin"))
    log("Encoding playable copy")
    proxy_pieces = write_proxy_pieces(master, rate, ch, total, work)
    meta = {"version": 2, "rate": rate, "channels": ch, "frames": total, "duration": total / rate,
            "bin_frames": bin_f, "bins": nbins,
            "files": [{"name": display_name(n), "start": s / rate} for n, s in bounds],
            "skipped": [display_name(n) for n in skipped],
            "proxy": proxy_pieces,
            "made": datetime.now(timezone.utc).isoformat()}
    (work / "_meta.json").write_text(json.dumps(meta))
    if a.local_dir:
        log(f"Local run: results are in {work}")
        return
    for f in all_files:                                  # replace results of an earlier ingest
        n = f["name"]
        if n in DERIVED or n.startswith("_proxy_"):
            drive.files().delete(fileId=f["id"], supportsAllDrives=True).execute()
    drive_upload(drive, str(work / "_peaks.bin"), a.source_folder, "_peaks.bin",
                 "application/octet-stream", public=False)
    for p in proxy_pieces:
        drive_upload(drive, str(work / p["name"]), a.source_folder, p["name"],
                     "audio/mp4", public=False)
    drive_upload(drive, str(work / "_meta.json"), a.source_folder, "_meta.json",
                 "application/json", public=False)
    jname = drive.files().get(fileId=a.source_folder, fields="name", supportsAllDrives=True).execute()["name"]
    ws = workspace_of(drive, a.source_folder)

    refresh_index(drive, ws, (f"job_{a.source_folder}.json", {
        "id": a.source_folder, "name": jname, "created": meta["made"], "duration": meta["duration"],
        "files": len(meta["files"]), "rate": rate, "channels": ch,
        "driveUrl": f"https://drive.google.com/drive/folders/{a.source_folder}"}))
    log("Ingest finished. The page can load the waveform now.")


def cmd_export(a, drive, yt):
    work = Path(a.workdir)
    kind = a.kind
    if kind == "video" and not a.no_upload and not a.local_dir and a.no_youtube and not a.drive_copy:
        sys.exit("No upload target selected: tick YouTube and/or the public Drive copy.")
    sources, fetch, all_files = gather(a, drive)
    if not sources:
        sys.exit("No audio files found.")
    bg = None
    if all_files and find_background(all_files):
        bgf = find_background(all_files)
        bg = str(work / ("background" + Path(bgf["name"]).suffix))
        download(drive, bgf["id"], bg)
    master = str(work / "master.pcm")
    seg_list = json.loads(a.segments) if a.segments.strip() else None
    offset, forced = 0, {}
    if a.ranged and seg_list:                       # decode only the files this part needs
        try:
            if a.meta:
                meta = json.loads(Path(a.meta).read_text())
            else:
                mf = next(f for f in all_files if f["name"] == "_meta.json")
                meta = json.loads(drive.files().get_media(fileId=mf["id"], supportsAllDrives=True).execute())
            picked = select_ranged(sources, meta, seg_list)
            if picked:
                sources, offset = picked[0], picked[1]
                forced = {"rate": meta["rate"], "ch": meta["channels"]}
                log(f"Range-aware decode: {len(sources)} source file(s) for this part")
            else:
                log("Range-aware decode skipped (files don't match the analysis); decoding everything.")
        except Exception as ex:
            log(f"Range-aware decode skipped ({ex}); decoding everything.")
    rate, ch, total, bounds, _ = build_master(sources, master, fetch, **forced)
    if total == 0:
        sys.exit("Nothing could be decoded.")
    log(f"Master: {fmt_time(total / rate)}" + (" (partial)" if offset else ""))

    segs = []
    for s_, e_ in (seg_list if seg_list is not None else [[0, total / rate]]):
        f0 = max(0, int(round(s_ * rate)) - offset)
        f1 = min(total, int(round(e_ * rate)) - offset)
        if f1 > f0:
            segs.append((f0, f1))
    if not segs:
        sys.exit("The segment queue is empty.")

    part_sec = a.part_min * 60 if a.part_min > 0 else 0
    if kind == "video" and yt and (part_sec == 0 or part_sec > YT_MAX_PART):
        part_sec = YT_MAX_PART
        log("Part length limited to 11 h for YouTube.")
    grand = sum(b - x for x, b in segs)
    part_frames = max(1, int(part_sec * rate)) if part_sec > 0 else grand
    parts = split_parts(segs, part_frames)
    log(f"Export {kind}: {fmt_time(grand / rate)} in {len(parts)} part(s)")

    # Drive layout + job info (not used in local test runs)
    ws = out_folder = job_name = None
    if drive and not a.no_upload:
        ws = workspace_of(drive, a.source_folder)
        out_root = get_or_create_folder(drive, "outputs", ws)
        out_folder = get_or_create_folder(drive, "audio" if kind == "audio" else "video", out_root)
        job_name = drive.files().get(fileId=a.source_folder, fields="name",
                                     supportsAllDrives=True).execute()["name"]

    now = datetime.now(timezone.utc)
    stamp_h = now.strftime("%Y-%m-%d %H:%M UTC")
    stamp_f = now.strftime("%Y-%m-%d_%H%M")
    ext = "ogg" if kind == "audio" else "mp4"
    opts = {"kind": kind, "mode": a.mode, "res": a.res, "bg": bg, "quality": a.quality, "lite": a.lite}
    results, failed = [], False
    for i, ranges in enumerate(parts, 1):
        n = len(parts)
        pi, pn = (a.part_idx, a.part_total) if a.part_total > 1 else (i, n)   # parallel run vs one job
        label = a.prefix if pn == 1 else f"{a.prefix}_part{pi}"
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", label)
        out = str(work / f"{safe}.{ext}")
        dur = sum(b - x for x, b in ranges) / rate
        log(f"Rendering {label}: {fmt_time(dur)}")
        feed_ffmpeg(build_cmd(opts, rate, ch, out), master, ranges, ch, "render")
        log(f"Rendered {human_size(os.path.getsize(out))}")
        row = {"title": label, "youtube": None, "drive": None}
        entry = {"id": uuid.uuid4().hex[:8], "job": a.source_folder, "jobName": job_name, "kind": kind,
                 "created": now.isoformat(), "duration": dur}
        if a.group:
            entry.update(group=a.group, part=pi, partTotal=pn)
        desc = f"Duration {fmt_time(dur)}." + (f" Uploaded {stamp_h}." if a.stamp else "")
        base_title = (a.title or a.prefix) + ("" if pn == 1 else f" (part {pi}/{pn})")
        suffix = f" ({stamp_h})" if a.stamp else ""
        title = base_title[:100 - len(suffix)] + suffix
        entry["title"] = title
        if not a.no_upload:
            if kind == "video" and yt and not a.no_youtube:            # default video target
                try:
                    row["youtube"], privacy = youtube_upload(yt, out, title, desc)
                    row["privacy"] = privacy
                    entry.update(youtubeUrl=row["youtube"], youtubeId=row["youtube"].rsplit("/", 1)[-1],
                                 privacy=privacy)
                    log(f"YouTube: {row['youtube']} ({privacy})")
                except Exception as ex:                                # keep going so a Drive copy isn't lost
                    failed = True
                    log(f"YouTube upload FAILED: {ex}")
            if kind == "audio" or a.drive_copy:                        # audio always; video only if asked
                try:
                    dname = f"{safe}{'_' + stamp_f if a.stamp else ''}.{ext}"
                    fid, link = drive_upload(drive, out, out_folder, dname,
                                             "video/mp4" if kind == "video" else "audio/ogg",
                                             public=True, description=desc)
                    row["drive"] = link
                    entry.update(driveFileId=fid, driveUrl=link, driveName=dname)
                    log(f"Drive: {link}")
                except Exception as ex:
                    failed = True
                    log(f"Drive upload FAILED: {ex}")
            if row["youtube"] or row["drive"]:                         # index whatever succeeded
                try:
                    refresh_index(drive, ws, (f"exp_{entry['id']}.json", entry))
                    log("Audio Tree updated.")
                except Exception as ex:
                    log(f"WARNING: could not update the Audio Tree: {ex}")
            os.remove(out)
        results.append(row)

    if a.delete_source and not a.local_dir and not failed and not a.no_upload:
        drive.files().update(fileId=a.source_folder, body={"trashed": True},
                             supportsAllDrives=True).execute()
        refresh_index(drive, ws)                                      # prune the deleted job
        log("Source folder moved to Drive trash.")

    lines = ["## Log Studio export finished", ""]
    for r in results:
        lines.append(f"**{r['title']}**")
        if kind == "video":
            lines.append(f"- YouTube: {r['youtube'] or 'skipped / failed'}"
                         + (f" ({r.get('privacy')})" if r.get("privacy") else ""))
        lines.append(f"- Drive: {r['drive'] or 'not requested / failed'}")
        lines.append("")
    text = "\n".join(lines)
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write(text + "\n")
    if failed:
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["ingest", "export"])
    ap.add_argument("--source-folder", help="Drive job folder ID")
    ap.add_argument("--local-dir", help="(testing) use a local folder instead of Drive")
    ap.add_argument("--output-folder", help="Drive folder ID for the public copy")
    ap.add_argument("--segments", default="", help='JSON [[start_s,end_s],...]; empty = entire')
    ap.add_argument("--kind", choices=["audio", "video"], default="video")
    ap.add_argument("--prefix", default="log")
    ap.add_argument("--title", default="")
    ap.add_argument("--part-min", type=float, default=600)
    ap.add_argument("--quality", type=int, default=10)
    ap.add_argument("--mode", choices=["visualizer", "still"], default="visualizer")
    ap.add_argument("--res", default="1280x720")
    ap.add_argument("--no-youtube", action="store_true")
    ap.add_argument("--drive-copy", action="store_true", help="video: also save a public copy in Drive")
    ap.add_argument("--stamp", action="store_true", help="append the upload date/time to titles and descriptions")
    ap.add_argument("--keep-work", action="store_true", help="(testing) keep the temp folder")
    ap.add_argument("--ranged", action="store_true", help="decode only the source files this export needs")
    ap.add_argument("--lite", action="store_true", help="half-frame-rate visualizer (faster)")
    ap.add_argument("--group", default="", help="id shared by the parallel parts of one export")
    ap.add_argument("--part-idx", type=int, default=0)
    ap.add_argument("--part-total", type=int, default=1)
    ap.add_argument("--meta", default=None, help="(testing) path to a _meta.json for local ranged runs")
    ap.add_argument("--delete-source", action="store_true")
    ap.add_argument("--no-upload", action="store_true")
    ap.add_argument("--workdir", default=None)
    a = ap.parse_args()
    a.quality = min(10, max(0, a.quality))
    a.workdir = a.workdir or tempfile.mkdtemp(prefix="logstudio_")
    Path(a.workdir).mkdir(parents=True, exist_ok=True)

    drive = yt = None
    try:
        if not a.local_dir:
            drive, yt = build_services(need_youtube=(a.command == "export" and a.kind == "video"
                                                     and not a.no_youtube and not a.no_upload))
        if a.command == "ingest":
            cmd_ingest(a, drive)
        else:
            cmd_export(a, drive, yt)
    finally:
        if not a.keep_work:                      # cleanup rule: nothing is left on the runner
            shutil.rmtree(a.workdir, ignore_errors=True)
            log("Temporary files removed.")


if __name__ == "__main__":
    main()
