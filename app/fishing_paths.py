"""Read-only bundled assets and persistent per-user app data."""
import os
from pathlib import Path
import sys


ASSET_DIR = Path(__file__).resolve().parent


def data_dir():
    if not getattr(sys,'frozen',False):
        return ASSET_DIR
    base = Path(os.environ.get('LOCALAPPDATA') or Path.home()/'AppData'/'Local')
    folder = base/'FishingHelper'
    folder.mkdir(parents=True,exist_ok=True)
    return folder


def settings_path():
    path = data_dir()/'fishing-settings.json'
    # Carry over the existing script app settings on the first EXE launch.
    if getattr(sys,'frozen',False) and not path.exists():
        old = Path(sys.executable).resolve().parent/path.name
        if old.is_file():
            try: path.write_bytes(old.read_bytes())
            except OSError: pass
    return path


def node_path():
    bundled = ASSET_DIR/'node-runtime'/'node.exe'
    if bundled.is_file(): return str(bundled)
    import shutil
    system = shutil.which('node')
    if system: return system
    fallback = Path(r'C:\Program Files\nodejs\node.exe')
    if fallback.is_file(): return str(fallback)
    raise RuntimeError('Node.js is unavailable for percentage OCR.')
