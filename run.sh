#!/bin/bash
# VORTEX — YouTube Downloader
# Auto-restarts on file changes and git branch switches

echo ""
echo "  ██╗   ██╗ ██████╗ ██████╗ ████████╗███████╗██╗  ██╗"
echo "  ██║   ██║██╔═══██╗██╔══██╗╚══██╔══╝██╔════╝╚██╗██╔╝"
echo "  ██║   ██║██║   ██║██████╔╝   ██║   █████╗   ╚███╔╝ "
echo "  ╚██╗ ██╔╝██║   ██║██╔══██╗   ██║   ██╔══╝   ██╔██╗ "
echo "   ╚████╔╝ ╚██████╔╝██║  ██║   ██║   ███████╗██╔╝ ██╗"
echo "    ╚═══╝   ╚═════╝ ╚═╝  ╚═╝   ╚═╝   ╚══════╝╚═╝  ╚═╝"
echo ""
echo "  YouTube Downloader — http://localhost:5000"
echo "  Auto-restart: ON (watching .py, .html, .css, .js + git branch)"
echo ""

# ── Checks ────────────────────────────────────────────────────────────────
if ! command -v ffmpeg &>/dev/null; then
  echo "  [WARN] ffmpeg not found — MP3 conversion and some MP4 merges won't work."
  echo "         Install with: sudo apt install ffmpeg   OR   brew install ffmpeg"
  echo ""
fi

# ── Deps ──────────────────────────────────────────────────────────────────
pip install flask flask-cors yt-dlp watchdog --break-system-packages -q 2>/dev/null || \
pip install flask flask-cors yt-dlp watchdog -q 2>/dev/null

mkdir -p downloads

# ── Watcher script (inline Python) ────────────────────────────────────────
python3 - << 'PYEOF'
import subprocess, sys, time, os, signal
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

ROOT        = Path(__file__).parent if '__file__' in dir() else Path('.')
WATCH_EXTS  = {'.py', '.html', '.css', '.js'}
DEBOUNCE    = 1.2   # seconds — ignore rapid successive saves
HEAD_FILE   = ROOT / '.git' / 'HEAD'

proc      = [None]
last_restart = [0.0]
current_branch = ['']

def get_branch():
    try:
        return Path(HEAD_FILE).read_text().strip()
    except Exception:
        return ''

def start():
    if proc[0]:
        try:
            os.killpg(os.getpgid(proc[0].pid), signal.SIGTERM)
        except Exception:
            pass
        proc[0].wait()
    print(f'\n  [vortex] Starting app.py ...', flush=True)
    proc[0] = subprocess.Popen(
        [sys.executable, str(ROOT / 'app.py')],
        cwd=str(ROOT),
        start_new_session=True,
    )
    last_restart[0] = time.time()

def restart(reason=''):
    now = time.time()
    if now - last_restart[0] < DEBOUNCE:
        return
    print(f'\n  [vortex] Restarting — {reason}', flush=True)
    start()

class ChangeHandler(FileSystemEventHandler):
    def on_modified(self, event):
        if event.is_directory:
            return
        if Path(event.src_path).suffix in WATCH_EXTS:
            rel = Path(event.src_path).relative_to(ROOT)
            restart(f'{rel} changed')

    def on_created(self, event):
        self.on_modified(event)

# Initial start
current_branch[0] = get_branch()
start()

observer = Observer()
observer.schedule(ChangeHandler(), str(ROOT), recursive=True)
observer.start()

print(f'  [vortex] Watching {ROOT} for changes  (Ctrl+C to stop)\n', flush=True)

try:
    while True:
        time.sleep(0.5)
        # Detect git branch / HEAD change (covers checkout, pull, merge)
        b = get_branch()
        if b != current_branch[0]:
            print(f'\n  [vortex] Branch changed: {current_branch[0]} → {b}', flush=True)
            current_branch[0] = b
            time.sleep(0.3)   # let git finish writing files
            restart('git HEAD changed')
except KeyboardInterrupt:
    print('\n  [vortex] Stopping...', flush=True)
    observer.stop()
    if proc[0]:
        try:
            os.killpg(os.getpgid(proc[0].pid), signal.SIGTERM)
        except Exception:
            pass
observer.join()
PYEOF
