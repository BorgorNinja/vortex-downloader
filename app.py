import os
import uuid
import json
import time
import threading
import re
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

def cleanup_file(path: Path, delay: int = 120):
    def _cleanup():
        time.sleep(delay)
        try:
            if path.exists():
                path.unlink()
                print(f"[cleanup] Deleted {path.name}")
        except Exception as e:
            print(f"[cleanup] Error: {e}")
    threading.Thread(target=_cleanup, daemon=True).start()

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
        }

    thread = threading.Thread(
        target=run_download,
        args=(task_id, url, fmt, quality),
        daemon=True
    )
    thread.start()
    return jsonify({"task_id": task_id})


def run_download(task_id: str, url: str, fmt: str, quality: str):
    output_template = str(DOWNLOAD_DIR / f"{task_id}_%(title)s.%(ext)s")

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

                task["status"] = "downloading"
                task["progress"] = round(pct, 1)
                task["speed"] = d.get("_speed_str", "").strip()
                task["eta"] = d.get("_eta_str", "").strip()
                task["title"] = d.get("info_dict", {}).get("title", "")

            elif d["status"] == "finished":
                task["status"] = "processing"
                task["progress"] = 99

    if fmt == "mp3":
        # Quality map: 128k → 5, 192k → 2, 320k → 0
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
        # MP4 quality map
        quality_map = {
            "360p": "bestvideo[height<=360]+bestaudio/best[height<=360]",
            "480p": "bestvideo[height<=480]+bestaudio/best[height<=480]",
            "720p": "bestvideo[height<=720]+bestaudio/best[height<=720]",
            "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
            "best": "bestvideo+bestaudio/best",
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

        # Find the output file
        files = sorted(DOWNLOAD_DIR.glob(f"{task_id}_*"))
        if files:
            output_file = files[0]
            with tasks_lock:
                tasks[task_id]["status"] = "done"
                tasks[task_id]["progress"] = 100
                tasks[task_id]["filename"] = output_file.name
            cleanup_file(output_file, delay=300)  # cleanup after 5 minutes
        else:
            with tasks_lock:
                tasks[task_id]["status"] = "error"
                tasks[task_id]["error"] = "Output file not found after download"

    except yt_dlp.utils.DownloadError as e:
        msg = str(e).replace("ERROR: ", "")[:300]
        with tasks_lock:
            tasks[task_id]["status"] = "error"
            tasks[task_id]["error"] = msg
    except Exception as e:
        with tasks_lock:
            tasks[task_id]["status"] = "error"
            tasks[task_id]["error"] = str(e)[:300]


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

    return send_file(
        file_path,
        as_attachment=True,
        download_name=task["filename"].split("_", 1)[-1],  # strip task_id prefix
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
