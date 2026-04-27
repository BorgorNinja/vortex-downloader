import os, uuid, json, time, threading, re, shutil, zipfile, subprocess, sys
from pathlib import Path
from collections import defaultdict, deque
from flask import Flask, render_template, request, jsonify, Response, send_file, make_response
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)

_ALLOWED_ORIGINS = os.getenv(
    "VORTEX_ORIGINS", "http://localhost:5000,http://127.0.0.1:5000"
).split(",")
CORS(app, origins=_ALLOWED_ORIGINS)

DOWNLOAD_DIR = Path(__file__).parent / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

MAX_CONCURRENT     = int(os.getenv("VORTEX_MAX_CONCURRENT",    "3"))
FILE_TTL           = int(os.getenv("VORTEX_FILE_TTL",          "1800"))
SSE_TIMEOUT        = int(os.getenv("VORTEX_SSE_TIMEOUT",       "300"))
COOKIE_FILE        = os.getenv("VORTEX_COOKIE_FILE", "")
COOKIE_UPLOAD_PATH = Path(__file__).parent / "user_cookies.txt"
MAX_URL_LENGTH     = 2048
DISK_QUOTA_MB      = int(os.getenv("VORTEX_DISK_QUOTA_MB",     "2048"))
RATE_LIMIT_MAX     = int(os.getenv("VORTEX_RATE_LIMIT_MAX",    "10"))
RATE_LIMIT_WINDOW  = int(os.getenv("VORTEX_RATE_LIMIT_WINDOW", "60"))
SESSION_COOKIE     = "vortex_sid"

_download_sem       = threading.Semaphore(MAX_CONCURRENT)
tasks               = {}
tasks_lock          = threading.Lock()
_cancel_events      = {}
_cancel_lock        = threading.Lock()
_file_registry      = {}
_file_registry_lock = threading.Lock()

# ── Rate limiter ─────────────────────────────────────────────────────────────
_rate_data: dict = defaultdict(deque)
_rate_lock = threading.Lock()

def _check_rate_limit(ip: str) -> bool:
    now = time.time()
    with _rate_lock:
        dq = _rate_data[ip]
        while dq and now - dq[0] > RATE_LIMIT_WINDOW:
            dq.popleft()
        if len(dq) >= RATE_LIMIT_MAX:
            return False
        dq.append(now)
        return True

# ── Disk quota ───────────────────────────────────────────────────────────────
def _downloads_size_mb() -> float:
    try:
        return sum(f.stat().st_size for f in DOWNLOAD_DIR.rglob("*") if f.is_file()) / 1048576
    except Exception:
        return 0.0

def _under_quota() -> bool:
    return _downloads_size_mb() < DISK_QUOTA_MB

# ── Session ID ───────────────────────────────────────────────────────────────
def _get_sid() -> str:
    sid = request.cookies.get(SESSION_COOKIE, "")
    return sid if (sid and len(sid) == 36) else str(uuid.uuid4())

def _attach_sid(response, sid: str):
    response.set_cookie(SESSION_COOKIE, sid, max_age=86400 * 7, httponly=True, samesite="Lax")
    return response

# ── Helpers ──────────────────────────────────────────────────────────────────
def is_youtube_url(url: str) -> bool:
    return any(re.search(p, url) for p in [
        r"(https?://)?(www\.)?(youtube\.com|youtu\.be)/",
        r"(https?://)?music\.youtube\.com/",
    ])

def format_duration(seconds):
    if not seconds:
        return "Unknown"
    s = int(seconds); h = s // 3600; m = (s % 3600) // 60; s = s % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

def resolve_cookie_file() -> str:
    """Return the active cookie file path, preferring user-uploaded over env var."""
    if COOKIE_UPLOAD_PATH.is_file() and COOKIE_UPLOAD_PATH.stat().st_size > 0:
        return str(COOKIE_UPLOAD_PATH)
    if COOKIE_FILE and os.path.isfile(COOKIE_FILE):
        return COOKIE_FILE
    return ""

def sanitize_filename(name: str) -> str:
    return re.sub(r'[\/*?:"<>|]', "_", name).strip().rstrip(".")[:200] or "playlist"

def register_file(path: Path) -> None:
    with _file_registry_lock:
        _file_registry[str(path)] = time.time()

# ── Cleanup worker ───────────────────────────────────────────────────────────
def _cleanup_worker():
    while True:
        time.sleep(60)
        now = time.time()

        expired = []
        with _file_registry_lock:
            for fp, ts in list(_file_registry.items()):
                if now - ts >= FILE_TTL:
                    expired.append(fp)
            for fp in expired:
                del _file_registry[fp]
        for fp in expired:
            p = Path(fp)
            try:
                shutil.rmtree(p, ignore_errors=True) if p.is_dir() else (p.unlink() if p.exists() else None)
            except Exception:
                pass

        try:
            for item in DOWNLOAD_DIR.iterdir():
                if now - item.stat().st_mtime < FILE_TTL:
                    continue
                with _file_registry_lock:
                    _file_registry.pop(str(item), None)
                try:
                    shutil.rmtree(item, ignore_errors=True) if item.is_dir() else item.unlink()
                except Exception:
                    pass
        except Exception:
            pass

        prune_cutoff = now - FILE_TTL * 2
        with tasks_lock:
            stale = [
                tid for tid, t in tasks.items()
                if t.get("status") in ("done", "error")
                and (t.get("_finished_at") or now) < prune_cutoff
            ]
            for tid in stale:
                del tasks[tid]

threading.Thread(target=_cleanup_worker, daemon=True).start()

# ─── Routes ──────────────────────────────────────────────────────────────────

@app.route("/guide")
def guide():
    return render_template("guide.html")


@app.route("/")
def index():
    sid = _get_sid()
    resp = make_response(render_template("index.html",
        disk_used_mb=round(_downloads_size_mb(), 1),
        disk_quota_mb=DISK_QUOTA_MB,
    ))
    _attach_sid(resp, sid)
    return resp


@app.route("/api/info", methods=["POST"])
def get_info():
    sid  = _get_sid()
    data = request.get_json()
    url  = (data or {}).get("url", "").strip()

    if not url:
        return jsonify({"error": "No URL provided"}), 400
    if len(url) > MAX_URL_LENGTH:
        return jsonify({"error": "URL too long"}), 400
    if not is_youtube_url(url):
        return jsonify({"error": "Only YouTube URLs are supported"}), 400

    ydl_opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "skip_download": True}
    _cf = resolve_cookie_file()
    if _cf:
        ydl_opts["cookiefile"] = _cf

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if info.get("_type") == "playlist":
            entries = info.get("entries") or []
            resp = make_response(jsonify({
                "is_playlist": True,
                "title":     info.get("title", "Unknown Playlist"),
                "channel":   info.get("uploader") or info.get("channel", "Unknown"),
                "count":     len(entries),
                "thumbnail": (info.get("thumbnails") or [{}])[-1].get("url"),
                "entries": [
                    {
                        "id":       e.get("id"),
                        "title":    e.get("title", f"Video {i+1}"),
                        "duration": format_duration(e.get("duration")),
                        "url":      e.get("url") or f"https://www.youtube.com/watch?v={e.get('id')}",
                    }
                    for i, e in enumerate(entries[:50])
                ],
            }))
        else:
            resp = make_response(jsonify({
                "is_playlist": False,
                "id":          info.get("id"),
                "title":       info.get("title", "Unknown"),
                "channel":     info.get("uploader") or info.get("channel", "Unknown"),
                "duration":    format_duration(info.get("duration")),
                "thumbnail":   info.get("thumbnail"),
                "view_count":  info.get("view_count"),
                "upload_date": info.get("upload_date"),
                "chapters":    bool(info.get("chapters")),
            }))
        _attach_sid(resp, sid)
        return resp
    except yt_dlp.utils.DownloadError as e:
        return jsonify({"error": str(e).replace("ERROR: ", "")[:200]}), 422
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500


@app.route("/api/formats", methods=["POST"])
def get_formats():
    data = request.get_json()
    url  = (data or {}).get("url", "").strip()
    if not url or not is_youtube_url(url):
        return jsonify({"error": "Invalid URL"}), 400

    ydl_opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    _cf = resolve_cookie_file()
    if _cf:
        ydl_opts["cookiefile"] = _cf

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        formats = []
        seen = set()
        for f in (info.get("formats") or []):
            res = f.get("format_note") or f.get("height")
            ext = f.get("ext", "")
            vcodec = f.get("vcodec", "none")
            acodec = f.get("acodec", "none")
            key    = (res, ext, vcodec != "none", acodec != "none")
            if key in seen:
                continue
            seen.add(key)
            formats.append({
                "id":       f.get("format_id"),
                "ext":      ext,
                "res":      str(res) if res else "audio",
                "note":     f.get("format_note", ""),
                "fps":      f.get("fps"),
                "filesize": f.get("filesize") or f.get("filesize_approx"),
                "has_video": vcodec != "none",
                "has_audio": acodec != "none",
                "tbr":       f.get("tbr"),
            })
        return jsonify({"formats": formats[-40:]})
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500


VALID_FORMATS = {"mp3", "mp4", "m4a", "flac", "ogg", "gif"}

@app.route("/api/download", methods=["POST"])
def start_download():
    sid  = _get_sid()
    ip   = request.remote_addr or "unknown"

    if not _check_rate_limit(ip):
        return jsonify({"error": f"Rate limit hit — max {RATE_LIMIT_MAX} downloads per {RATE_LIMIT_WINDOW}s"}), 429

    if not _under_quota():
        return jsonify({"error": f"Server disk quota reached ({DISK_QUOTA_MB} MB). Try again later."}), 507

    data           = request.get_json()
    url            = (data or {}).get("url", "").strip()
    fmt            = (data or {}).get("format", "mp4").lower()
    quality        = (data or {}).get("quality", "best")
    playlist_title = (data or {}).get("playlist_title", "").strip()
    is_playlist    = bool((data or {}).get("is_playlist", False))
    selected_urls  = (data or {}).get("selected_urls", [])
    if not isinstance(selected_urls, list):
        selected_urls = []

    start_time_raw  = str((data or {}).get("start_time",  "")).strip()
    end_time_raw    = str((data or {}).get("end_time",    "")).strip()
    subtitles       = bool((data or {}).get("subtitles",       False))
    burn_subtitles  = bool((data or {}).get("burn_subtitles",  False))
    split_chapters  = bool((data or {}).get("split_chapters",  False))
    age_bypass      = bool((data or {}).get("age_bypass",      False))

    if not url or len(url) > MAX_URL_LENGTH or not is_youtube_url(url):
        return jsonify({"error": "Invalid URL"}), 400
    if fmt not in VALID_FORMATS:
        return jsonify({"error": f"Format must be one of: {', '.join(sorted(VALID_FORMATS))}"}), 400

    def _parse_time(raw):
        if not raw:
            return None
        try:
            v = float(raw)
            return v if v >= 0 else False
        except ValueError:
            return False

    ts = _parse_time(start_time_raw)
    te = _parse_time(end_time_raw)
    if ts is False or te is False:
        return jsonify({"error": "start_time / end_time must be non-negative numbers"}), 400
    if ts is not None and te is not None and ts >= te:
        return jsonify({"error": "start_time must be less than end_time"}), 400

    task_id = str(uuid.uuid4())
    with tasks_lock:
        tasks[task_id] = {
            "status":      "queued",
            "progress":    0,
            "speed":       "",
            "eta":         "",
            "filename":    None,
            "title":       "",
            "error":       None,
            "format":      fmt,
            "is_playlist": is_playlist,
            "_finished_at": None,
            "_session":    sid,
        }

    with _cancel_lock:
        _cancel_events[task_id] = threading.Event()

    threading.Thread(
        target=run_download,
        args=(task_id, url, fmt, quality, is_playlist, playlist_title,
              selected_urls, ts, te, subtitles, burn_subtitles, split_chapters, age_bypass),
        daemon=True,
    ).start()

    resp = make_response(jsonify({"task_id": task_id}))
    _attach_sid(resp, sid)
    return resp


def run_download(task_id, url, fmt, quality, is_playlist, playlist_title,
                 selected_urls, start_time, end_time, subtitles,
                 burn_subtitles, split_chapters, age_bypass):
    _download_sem.acquire()
    try:
        _run_download_inner(
            task_id, url, fmt, quality, is_playlist, playlist_title,
            selected_urls or [], start_time, end_time, subtitles,
            burn_subtitles, split_chapters, age_bypass,
        )
    finally:
        _download_sem.release()
        with _cancel_lock:
            _cancel_events.pop(task_id, None)


def _run_download_inner(task_id, url, fmt, quality, is_playlist, playlist_title,
                        selected_urls, start_time, end_time, subtitles,
                        burn_subtitles, split_chapters, age_bypass):
    task_dir        = DOWNLOAD_DIR / task_id
    task_dir.mkdir(exist_ok=True)
    output_template = str(task_dir / "%(playlist_index)s - %(title)s.%(ext)s")

    download_targets   = selected_urls if selected_urls else [url]
    effective_playlist = is_playlist or len(download_targets) > 1

    current_index = [0]
    total_count   = [len(download_targets)]
    cancel_ev     = _cancel_events.get(task_id)

    def progress_hook(d):
        if cancel_ev and cancel_ev.is_set():
            raise Exception("Download cancelled by user")
        with tasks_lock:
            task = tasks.get(task_id)
            if not task:
                return
            if d["status"] == "downloading":
                raw = d.get("_percent_str", "0%").strip().replace("%", "")
                try:
                    pct = float(raw)
                except ValueError:
                    pct = 0
                info = d.get("info_dict", {})
                idx  = info.get("playlist_index") or info.get("playlist_autonumber") or current_index[0]
                n    = info.get("playlist_count") or total_count[0] or 1
                task["progress"] = round(((idx - 1) * 100 + pct) / n, 1) if (is_playlist and n > 1) else round(pct, 1)
                task["status"]   = "downloading"
                task["speed"]    = d.get("_speed_str", "").strip()
                task["eta"]      = d.get("_eta_str",   "").strip()
                task["title"]    = info.get("title", "")
                task["speed_bytes"] = d.get("speed") or 0
                current_index[0] = idx
                total_count[0]   = n
            elif d["status"] == "finished":
                task["status"]   = "processing"
                task["progress"] = 99

    AUDIO_QUALITY_MAP = {"128k": "5", "192k": "2", "320k": "0"}

    common_opts = {
        "outtmpl":        output_template,
        "progress_hooks": [progress_hook],
        "quiet":          True,
        "no_warnings":    True,
        "ignoreerrors":   True,
        "prefer_ffmpeg":  True,
    }
    _cf = resolve_cookie_file()
    if _cf:
        common_opts["cookiefile"] = _cf
    if age_bypass:
        common_opts["age_limit"]   = 99
        common_opts["user_agent"]  = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    if start_time is not None or end_time is not None:
        r_start = start_time if start_time is not None else 0
        r_end   = end_time   if end_time   is not None else float("inf")
        common_opts["download_ranges"]         = yt_dlp.utils.download_range_func(None, [(r_start, r_end)])
        common_opts["force_keyframes_at_cuts"] = True

    is_audio = fmt in ("mp3", "m4a", "flac", "ogg")
    is_gif   = fmt == "gif"

    if is_audio:
        aq = AUDIO_QUALITY_MAP.get(quality, "2")
        codec_map = {"mp3": "mp3", "m4a": "m4a", "flac": "flac", "ogg": "vorbis"}
        pp = [{"key": "FFmpegExtractAudio", "preferredcodec": codec_map[fmt],
               **({"preferredquality": aq} if fmt != "flac" else {})}]
        pp += [{"key": "FFmpegMetadata", "add_metadata": True}, {"key": "EmbedThumbnail"}]
        ydl_opts = {**common_opts, "format": "bestaudio/best", "writethumbnail": True, "postprocessors": pp}

    elif is_gif:
        # Download as mp4 first then convert via ffmpeg
        ydl_opts = {**common_opts, "format": "bestvideo[height<=480]+bestaudio/best",
                    "merge_output_format": "mp4",
                    "postprocessors": [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}]}

    else:  # mp4
        quality_map = {
            "360p":  "bestvideo[height<=360]+bestaudio/best[height<=360]",
            "480p":  "bestvideo[height<=480]+bestaudio/best[height<=480]",
            "720p":  "bestvideo[height<=720]+bestaudio/best[height<=720]",
            "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
            "best":  "bestvideo+bestaudio/best",
        }
        pp = [
            {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"},
            {"key": "FFmpegMetadata", "add_metadata": True},
        ]
        if split_chapters:
            pp.append({"key": "FFmpegSplitChapters", "force_keyframes": True})
        if subtitles and not burn_subtitles:
            pp.append({"key": "FFmpegEmbedSubtitle", "already_have_subtitle": False})
        ydl_opts = {**common_opts, "format": quality_map.get(quality, "bestvideo+bestaudio/best"),
                    "merge_output_format": "mp4", "postprocessors": pp}
        if subtitles or burn_subtitles:
            ydl_opts.update({"writesubtitles": True, "writeautomaticsub": True, "subtitleslangs": ["en", "en-orig"]})

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download(download_targets)

        # ── GIF conversion ──────────────────────────────────────────────────
        if is_gif:
            mp4_files = list(task_dir.glob("*.mp4"))
            if mp4_files:
                src_mp4  = mp4_files[0]
                gif_path = src_mp4.with_suffix(".gif")
                palette  = src_mp4.with_suffix(".png")
                try:
                    # Generate palette for better quality
                    subprocess.run([
                        "ffmpeg", "-i", str(src_mp4),
                        "-vf", "fps=10,scale=480:-1:flags=lanczos,palettegen",
                        str(palette), "-y"
                    ], capture_output=True, timeout=120)
                    subprocess.run([
                        "ffmpeg", "-i", str(src_mp4), "-i", str(palette),
                        "-filter_complex", "fps=10,scale=480:-1:flags=lanczos[x];[x][1:v]paletteuse",
                        str(gif_path), "-y"
                    ], capture_output=True, timeout=120)
                    src_mp4.unlink(missing_ok=True)
                    palette.unlink(missing_ok=True)
                except Exception as gif_err:
                    print(f"[gif] conversion error: {gif_err}")

        # ── Burn subtitles ──────────────────────────────────────────────────
        if burn_subtitles and not is_audio and not is_gif:
            mp4_files = list(task_dir.glob("*.mp4"))
            sub_files = list(task_dir.glob("*.vtt")) + list(task_dir.glob("*.srt"))
            if mp4_files and sub_files:
                src_mp4   = mp4_files[0]
                sub_file  = sub_files[0]
                burned    = src_mp4.with_name(src_mp4.stem + "_burned.mp4")
                try:
                    subprocess.run([
                        "ffmpeg", "-i", str(src_mp4),
                        "-vf", f"subtitles={str(sub_file)}",
                        "-c:a", "copy", str(burned), "-y"
                    ], capture_output=True, timeout=600)
                    if burned.exists() and burned.stat().st_size > 0:
                        src_mp4.unlink(missing_ok=True)
                        burned.rename(src_mp4)
                    for sf in sub_files:
                        sf.unlink(missing_ok=True)
                except Exception as burn_err:
                    print(f"[burn_subs] error: {burn_err}")

        downloaded_files = sorted([
            f for f in task_dir.glob("*")
            if f.is_file() and f.suffix not in (".part", ".ytdl", ".png")
        ])

        if not downloaded_files:
            with tasks_lock:
                tasks[task_id]["status"]       = "error"
                tasks[task_id]["error"]        = "No files downloaded"
                tasks[task_id]["_finished_at"] = time.time()
            shutil.rmtree(task_dir, ignore_errors=True)
            return

        if len(downloaded_files) > 1 or effective_playlist:
            safe_title = sanitize_filename(playlist_title) if playlist_title else f"playlist_{task_id[:8]}"
            zip_path   = DOWNLOAD_DIR / f"{safe_title}.zip"
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for f in downloaded_files:
                    zf.write(f, arcname=f.name)
            shutil.rmtree(task_dir, ignore_errors=True)
            with tasks_lock:
                tasks[task_id].update({"status": "done", "progress": 100,
                                       "filename": zip_path.name, "_finished_at": time.time()})
            register_file(zip_path)
        else:
            src  = downloaded_files[0]
            dest = DOWNLOAD_DIR / f"{task_id}_{src.name}"
            shutil.move(str(src), str(dest))
            shutil.rmtree(task_dir, ignore_errors=True)
            with tasks_lock:
                tasks[task_id].update({"status": "done", "progress": 100,
                                       "filename": dest.name, "_finished_at": time.time()})
            register_file(dest)

    except Exception as e:
        msg = str(e).replace("ERROR: ", "")[:300]
        with tasks_lock:
            tasks[task_id].update({
                "status": "error",
                "error":  "Cancelled" if "cancelled by user" in msg.lower() else msg,
                "_finished_at": time.time(),
            })
        shutil.rmtree(task_dir, ignore_errors=True)


@app.route("/api/progress/<task_id>")
def sse_progress(task_id):
    def generate():
        deadline = time.time() + SSE_TIMEOUT
        while True:
            if time.time() > deadline:
                yield f"data: {json.dumps({'status':'error','error':'Stream timeout'})}\n\n"
                break
            with tasks_lock:
                task = tasks.get(task_id)
            if not task:
                yield f"data: {json.dumps({'status':'error','error':'Task not found'})}\n\n"
                break
            yield f"data: {json.dumps({k: v for k, v in task.items() if not k.startswith('_')})}\n\n"
            if task["status"] in ("done", "error"):
                break
            time.sleep(0.4)
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no","Connection":"keep-alive"})


@app.route("/api/cancel/<task_id>", methods=["POST"])
def cancel_task(task_id):
    with _cancel_lock:
        ev = _cancel_events.get(task_id)
    if ev:
        ev.set()
        return jsonify({"ok": True})
    with tasks_lock:
        if task_id not in tasks:
            return jsonify({"error": "Task not found"}), 404
    return jsonify({"ok": True, "note": "Task already finished"})


@app.route("/api/tasks")
def list_tasks():
    sid = _get_sid()
    with tasks_lock:
        result = {
            tid: {k: v for k, v in t.items() if not k.startswith("_")}
            for tid, t in tasks.items()
            if t.get("_session") == sid
        }
    return jsonify(result)


@app.route("/api/file/<task_id>")
def serve_file(task_id):
    with tasks_lock:
        task = tasks.get(task_id)
    if not task or task["status"] != "done" or not task["filename"]:
        return jsonify({"error": "File not ready or not found"}), 404
    file_path = DOWNLOAD_DIR / task["filename"]
    if not file_path.exists():
        return jsonify({"error": "File has expired or been deleted"}), 410
    fname = task["filename"]
    download_name = fname if fname.endswith(".zip") else fname.split("_", 1)[-1]
    return send_file(file_path, as_attachment=True, download_name=download_name)


@app.route("/api/status/<task_id>")
def task_status(task_id):
    with tasks_lock:
        task = tasks.get(task_id)
    if not task:
        return jsonify({"error": "Not found"}), 404
    return jsonify({k: v for k, v in task.items() if not k.startswith("_")})


@app.route("/api/cookies/upload", methods=["POST"])
def upload_cookies():
    f = request.files.get("cookies")
    if not f or not f.filename:
        return jsonify({"error": "No file provided"}), 400

    # Basic Netscape cookie file validation
    raw = f.read(4096).decode("utf-8", errors="replace")
    if not any(line.startswith("# Netscape") or ("	" in line and len(line.split("	")) >= 7)
               for line in raw.splitlines()[:10]):
        return jsonify({"error": "File does not look like a Netscape cookie file. Export cookies as .txt from your browser extension."}), 422

    # Write the full file
    f.seek(0)
    COOKIE_UPLOAD_PATH.write_bytes(f.read())
    size = COOKIE_UPLOAD_PATH.stat().st_size
    print(f"[cookies] Uploaded cookie file — {size} bytes")
    return jsonify({"ok": True, "size": size})


@app.route("/api/cookies/status")
def cookie_status():
    if COOKIE_UPLOAD_PATH.is_file() and COOKIE_UPLOAD_PATH.stat().st_size > 0:
        stat = COOKIE_UPLOAD_PATH.stat()
        return jsonify({
            "active": True,
            "source": "uploaded",
            "size":   stat.st_size,
            "mtime":  stat.st_mtime,
        })
    if COOKIE_FILE and os.path.isfile(COOKIE_FILE):
        return jsonify({"active": True, "source": "env", "path": COOKIE_FILE})
    return jsonify({"active": False})


@app.route("/api/cookies/clear", methods=["POST"])
def clear_cookies():
    if COOKIE_UPLOAD_PATH.is_file():
        COOKIE_UPLOAD_PATH.unlink()
        print("[cookies] Uploaded cookie file cleared")
    return jsonify({"ok": True})


@app.route("/api/health")
def health():
    with tasks_lock:
        active = sum(1 for t in tasks.values() if t["status"] in ("queued", "downloading", "processing"))
    return jsonify({
        "status":       "ok",
        "disk_used_mb": round(_downloads_size_mb(), 1),
        "disk_quota_mb": DISK_QUOTA_MB,
        "active_tasks": active,
        "max_concurrent": MAX_CONCURRENT,
    })


@app.route("/api/update-ytdlp", methods=["POST"])
def update_ytdlp():
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-U", "yt-dlp", "--break-system-packages"],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0:
            import importlib
            import yt_dlp as _yt
            importlib.reload(_yt)
            return jsonify({"ok": True, "output": result.stdout[-500:]})
        return jsonify({"error": result.stderr[-300:]}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Update timed out"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    debug_mode = os.getenv("VORTEX_DEBUG", "0").lower() in ("1", "true", "yes")
    print("─" * 50)
    print("  VORTEX Downloader  →  http://localhost:5000")
    print(f"  Concurrency cap    →  {MAX_CONCURRENT}")
    print(f"  File TTL           →  {FILE_TTL // 60} min")
    print(f"  Disk quota         →  {DISK_QUOTA_MB} MB")
    print(f"  Rate limit         →  {RATE_LIMIT_MAX} req / {RATE_LIMIT_WINDOW}s per IP")
    if debug_mode:
        print("  [WARN] Debug mode ON")
    print("─" * 50)
    app.run(debug=debug_mode, host="0.0.0.0", port=5000, threaded=True)
