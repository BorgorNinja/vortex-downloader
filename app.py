import os
import uuid
import json
import time
import threading
import re
import shutil
import zipfile
from pathlib import Path
from flask import Flask, render_template, request, jsonify, Response, send_file
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR = Path(__file__).parent / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

# In-memory task store
tasks = {}
tasks_lock = threading.Lock()

# ─── Helpers ────────────────────────────────────────────────────────────────

def is_youtube_url(url: str) -> bool:
    patterns = [
        r"(https?://)?(www\.)?(youtube\.com|youtu\.be)/",
        r"(https?://)?music\.youtube\.com/",
    ]
    return any(re.search(p, url) for p in patterns)

def format_duration(seconds):
    if not seconds:
        return "Unknown"
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"

FILE_TTL = 1800  # 30 minutes per file

# Registry: { "/abs/path/to/file": created_at_float }
# Each finished download (single file or playlist zip) gets its own entry.
_file_registry: dict = {}
_file_registry_lock = threading.Lock()


def register_file(path: Path) -> None:
    """Record a completed output file so the sweeper can expire it after FILE_TTL."""
    with _file_registry_lock:
        _file_registry[str(path)] = time.time()
    print(f"[cleanup] Registered {path.name!r} — expires in {FILE_TTL // 60} min")


def _cleanup_worker() -> None:
    """
    Background daemon thread.
    Wakes every 60 s and deletes any registered file whose age >= FILE_TTL.
    Also sweeps the downloads directory for orphaned files (e.g. after a
    server restart) whose mtime is older than FILE_TTL.
    """
    while True:
        time.sleep(60)
        now = time.time()

        # ── Expire registered files ──────────────────────────────────────────
        expired_paths = []
        with _file_registry_lock:
            for fpath, created_at in list(_file_registry.items()):
                if now - created_at >= FILE_TTL:
                    expired_paths.append(fpath)
            for fpath in expired_paths:
                del _file_registry[fpath]

        for fpath in expired_paths:
            p = Path(fpath)
            try:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                    print(f"[cleanup] TTL expired (dir):  {p.name}")
                elif p.exists():
                    p.unlink()
                    print(f"[cleanup] TTL expired (file): {p.name}")
            except Exception as exc:
                print(f"[cleanup] Error deleting {fpath}: {exc}")

        # ── Sweep orphaned files left on disk (server restarts, etc.) ────────
        try:
            for item in DOWNLOAD_DIR.iterdir():
                if not item.is_file():
                    continue
                if item.suffix in (".part", ".ytdl"):
                    continue
                age = now - item.stat().st_mtime
                if age >= FILE_TTL:
                    # Remove from registry too if it somehow ended up there
                    with _file_registry_lock:
                        _file_registry.pop(str(item), None)
                    try:
                        item.unlink()
                        print(f"[cleanup] Orphan swept:        {item.name}")
                    except Exception as exc:
                        print(f"[cleanup] Error sweeping {item.name}: {exc}")
        except Exception as exc:
            print(f"[cleanup] Directory sweep error: {exc}")


# Start the sweeper once at import time (daemon=True means it never blocks shutdown)
threading.Thread(target=_cleanup_worker, name="cleanup-sweeper", daemon=True).start()


def sanitize_filename(name: str) -> str:
    """Strip characters that are unsafe in filenames/zip names."""
    return re.sub(r'[\/*?:"<>|]', "_", name).strip().rstrip(".")[:200] or "playlist"

# ─── Routes ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.get_json()
    url = (data or {}).get("url", "").strip()

    if not url:
        return jsonify({"error": "No URL provided"}), 400
    if not is_youtube_url(url):
        return jsonify({"error": "Only YouTube URLs are supported"}), 400

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if info.get("_type") == "playlist":
            entries = info.get("entries") or []
            return jsonify({
                "is_playlist": True,
                "title": info.get("title", "Unknown Playlist"),
                "channel": info.get("uploader") or info.get("channel", "Unknown"),
                "count": len(entries),
                "thumbnail": info.get("thumbnails", [{}])[-1].get("url") if info.get("thumbnails") else None,
                "entries": [
                    {
                        "id": e.get("id"),
                        "title": e.get("title", f"Video {i+1}"),
                        "duration": format_duration(e.get("duration")),
                        "url": e.get("url") or f"https://www.youtube.com/watch?v={e.get('id')}",
                    }
                    for i, e in enumerate(entries[:50])  # cap at 50 for display
                ],
            })
        else:
            return jsonify({
                "is_playlist": False,
                "id": info.get("id"),
                "title": info.get("title", "Unknown"),
                "channel": info.get("uploader") or info.get("channel", "Unknown"),
                "duration": format_duration(info.get("duration")),
                "thumbnail": info.get("thumbnail"),
                "view_count": info.get("view_count"),
                "upload_date": info.get("upload_date"),
            })
    except yt_dlp.utils.DownloadError as e:
        msg = str(e).replace("ERROR: ", "")
        return jsonify({"error": msg[:200]}), 422
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.get_json()
    url = (data or {}).get("url", "").strip()
    fmt = (data or {}).get("format", "mp4").lower()
    quality = (data or {}).get("quality", "best")
    playlist_title = (data or {}).get("playlist_title", "").strip()
    is_playlist = bool((data or {}).get("is_playlist", False))

    if not url or not is_youtube_url(url):
        return jsonify({"error": "Invalid URL"}), 400
    if fmt not in ("mp3", "mp4"):
        return jsonify({"error": "Format must be mp3 or mp4"}), 400

    task_id = str(uuid.uuid4())
    with tasks_lock:
        tasks[task_id] = {
            "status": "queued",
            "progress": 0,
            "speed": "",
            "eta": "",
            "filename": None,
            "title": "",
            "error": None,
            "format": fmt,
            "is_playlist": is_playlist,
        }

    thread = threading.Thread(
        target=run_download,
        args=(task_id, url, fmt, quality, is_playlist, playlist_title),
        daemon=True
    )
    thread.start()
    return jsonify({"task_id": task_id})


def run_download(task_id: str, url: str, fmt: str, quality: str,
                 is_playlist: bool = False, playlist_title: str = ""):
    # Use a per-task subdirectory so concurrent downloads never collide
    task_dir = DOWNLOAD_DIR / task_id
    task_dir.mkdir(exist_ok=True)
    output_template = str(task_dir / "%(playlist_index)s - %(title)s.%(ext)s")

    # Track which track is currently downloading for the UI label
    current_index = [0]
    total_count   = [0]

    def progress_hook(d):
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

                # For playlists: scale per-track 0-100 into overall progress
                if is_playlist and n > 1:
                    overall = ((idx - 1) * 100 + pct) / n
                    task["progress"] = round(overall, 1)
                else:
                    task["progress"] = round(pct, 1)

                task["status"] = "downloading"
                task["speed"]  = d.get("_speed_str", "").strip()
                task["eta"]    = d.get("_eta_str", "").strip()
                task["title"]  = info.get("title", "")

                current_index[0] = idx
                total_count[0]   = n

            elif d["status"] == "finished":
                task["status"]   = "processing"
                task["progress"] = 99

    if fmt == "mp3":
        quality_map = {"128k": "5", "192k": "2", "320k": "0"}
        audio_quality = quality_map.get(quality, "2")

        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": output_template,
            "progress_hooks": [progress_hook],
            "quiet": True,
            "no_warnings": True,
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": audio_quality,
            }],
            "prefer_ffmpeg": True,
        }
    else:
        quality_map = {
            "360p":  "bestvideo[height<=360]+bestaudio/best[height<=360]",
            "480p":  "bestvideo[height<=480]+bestaudio/best[height<=480]",
            "720p":  "bestvideo[height<=720]+bestaudio/best[height<=720]",
            "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
            "best":  "bestvideo+bestaudio/best",
        }
        fmt_str = quality_map.get(quality, "bestvideo+bestaudio/best")

        ydl_opts = {
            "format": fmt_str,
            "outtmpl": output_template,
            "progress_hooks": [progress_hook],
            "quiet": True,
            "no_warnings": True,
            "merge_output_format": "mp4",
            "postprocessors": [{
                "key": "FFmpegVideoConvertor",
                "preferedformat": "mp4",
            }],
            "prefer_ffmpeg": True,
        }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        # Collect everything that was written into the task subdirectory
        downloaded_files = sorted(task_dir.glob("*"))
        # Filter out any temp/partial files left by yt-dlp
        downloaded_files = [f for f in downloaded_files if f.is_file() and not f.suffix in (".part", ".ytdl")]

        if not downloaded_files:
            with tasks_lock:
                tasks[task_id]["status"] = "error"
                tasks[task_id]["error"]  = "Output file not found after download"
            shutil.rmtree(task_dir, ignore_errors=True)
            return

        if len(downloaded_files) > 1 or is_playlist:
            # ── Playlist: package all files into a ZIP ───────────────────────
            safe_title = sanitize_filename(playlist_title) if playlist_title else f"playlist_{task_id[:8]}"
            zip_name   = f"{safe_title}.zip"
            zip_path   = DOWNLOAD_DIR / zip_name

            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for f in downloaded_files:
                    zf.write(f, arcname=f.name)

            # Cleanup the temp subdirectory now that zip is ready
            shutil.rmtree(task_dir, ignore_errors=True)

            with tasks_lock:
                tasks[task_id]["status"]   = "done"
                tasks[task_id]["progress"] = 100
                tasks[task_id]["filename"] = zip_name

            register_file(zip_path)

        else:
            # ── Single file: move out of subdirectory ────────────────────────
            src  = downloaded_files[0]
            dest = DOWNLOAD_DIR / f"{task_id}_{src.name}"
            shutil.move(str(src), str(dest))
            shutil.rmtree(task_dir, ignore_errors=True)

            with tasks_lock:
                tasks[task_id]["status"]   = "done"
                tasks[task_id]["progress"] = 100
                tasks[task_id]["filename"] = dest.name

            register_file(dest)

    except yt_dlp.utils.DownloadError as e:
        msg = str(e).replace("ERROR: ", "")[:300]
        with tasks_lock:
            tasks[task_id]["status"] = "error"
            tasks[task_id]["error"]  = msg
        shutil.rmtree(task_dir, ignore_errors=True)
    except Exception as e:
        with tasks_lock:
            tasks[task_id]["status"] = "error"
            tasks[task_id]["error"]  = str(e)[:300]
        shutil.rmtree(task_dir, ignore_errors=True)


@app.route("/api/progress/<task_id>")
def sse_progress(task_id):
    def generate():
        while True:
            with tasks_lock:
                task = tasks.get(task_id)

            if not task:
                yield f"data: {json.dumps({'status': 'error', 'error': 'Task not found'})}\n\n"
                break

            yield f"data: {json.dumps(task)}\n\n"

            if task["status"] in ("done", "error"):
                break

            time.sleep(0.4)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.route("/api/file/<task_id>")
def serve_file(task_id):
    with tasks_lock:
        task = tasks.get(task_id)

    if not task or task["status"] != "done" or not task["filename"]:
        return jsonify({"error": "File not ready or not found"}), 404

    file_path = DOWNLOAD_DIR / task["filename"]
    if not file_path.exists():
        return jsonify({"error": "File has expired or been deleted"}), 410

    # Playlist ZIPs are named "{PlaylistTitle}.zip" — no task_id prefix to strip.
    # Single files are named "{task_id}_{original}" — strip the prefix.
    fname = task["filename"]
    if task.get("is_playlist") or fname.endswith(".zip"):
        download_name = fname
    else:
        download_name = fname.split("_", 1)[-1]

    return send_file(
        file_path,
        as_attachment=True,
        download_name=download_name,
    )


@app.route("/api/status/<task_id>")
def task_status(task_id):
    with tasks_lock:
        task = tasks.get(task_id)
    if not task:
        return jsonify({"error": "Not found"}), 404
    return jsonify(task)


if __name__ == "__main__":
    print("─" * 50)
    print("  YT Downloader running at http://localhost:5000")
    print("─" * 50)
    app.run(debug=True, host="0.0.0.0", port=5000, threaded=True)
