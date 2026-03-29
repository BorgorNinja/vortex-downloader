#!/bin/bash
# VORTEX — YouTube Downloader
# Run this from the project directory

echo ""
echo "  ██╗   ██╗ ██████╗ ██████╗ ████████╗███████╗██╗  ██╗"
echo "  ██║   ██║██╔═══██╗██╔══██╗╚══██╔══╝██╔════╝╚██╗██╔╝"
echo "  ██║   ██║██║   ██║██████╔╝   ██║   █████╗   ╚███╔╝ "
echo "  ╚██╗ ██╔╝██║   ██║██╔══██╗   ██║   ██╔══╝   ██╔██╗ "
echo "   ╚████╔╝ ╚██████╔╝██║  ██║   ██║   ███████╗██╔╝ ██╗"
echo "    ╚═══╝   ╚═════╝ ╚═╝  ╚═╝   ╚═╝   ╚══════╝╚═╝  ╚═╝"
echo ""
echo "  YouTube Downloader — http://localhost:5000"
echo ""

# Check ffmpeg
if ! command -v ffmpeg &>/dev/null; then
  echo "  [WARN] ffmpeg not found — MP3 conversion and some MP4 merges won't work."
  echo "         Install with: sudo apt install ffmpeg   OR   brew install ffmpeg"
  echo ""
fi

# Check Python deps
pip install flask flask-cors yt-dlp --break-system-packages -q 2>/dev/null || \
pip install flask flask-cors yt-dlp -q 2>/dev/null

# Make downloads dir
mkdir -p downloads

# Run
python app.py
