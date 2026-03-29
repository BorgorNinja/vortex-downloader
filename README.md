# VORTEX — YouTube Downloader

A sleek, full-stack web app for downloading YouTube videos and playlists as MP3 or MP4.

## Features

- Download any YouTube video as **MP4** (360p, 480p, 720p, 1080p, Best)
- Download any YouTube video as **MP3** (128k, 192k, 320k)
- Full **playlist support** — shows all tracks, downloads entire playlist
- Real-time **download progress** via Server-Sent Events
- Video metadata preview (thumbnail, title, channel, duration, views)
- Auto-cleanup of temp files after 5 minutes
- Dark industrial UI

## Requirements

- Python 3.9+
- `ffmpeg` installed on your system
- Internet connection

## Quick Start

```bash
# Install dependencies
pip install flask flask-cors yt-dlp

# Make sure ffmpeg is installed
# Ubuntu/Debian: sudo apt install ffmpeg
# macOS:         brew install ffmpeg
# Windows:       https://ffmpeg.org/download.html

# Run the server
chmod +x run.sh
./run.sh

# Or directly:
python app.py
```

Then open **http://localhost:5000** in your browser.

## How It Works

1. Paste any YouTube URL (video or playlist)
2. Choose **MP4** or **MP3** and the quality
3. Click **Fetch** to load video metadata
4. Click **Start Download** — a live progress bar appears
5. When done, click **Save File** to download to your device

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/` | Web UI |
| `POST` | `/api/info` | Fetch video/playlist metadata |
| `POST` | `/api/download` | Start a download task |
| `GET`  | `/api/progress/<id>` | SSE stream for progress |
| `GET`  | `/api/file/<id>` | Download completed file |
| `GET`  | `/api/status/<id>` | Poll task status |

## Tech Stack

- **Backend**: Python + Flask + yt-dlp + ffmpeg
- **Frontend**: Vanilla HTML/CSS/JS (no frameworks, no dependencies)
- **Fonts**: Bebas Neue + Azeret Mono + Barlow (Google Fonts)
