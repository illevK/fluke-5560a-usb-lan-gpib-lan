"""Per-user settings and atomic documents. Never writes the previous application's settings."""
from __future__ import annotations
import json
import os
import tempfile
from pathlib import Path

def data_dir() -> Path:
    override = os.environ.get('CALIBRATION_DATA_DIR')
    folder = Path(override) if override else Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'CalibrationAutomation'
    folder.mkdir(parents=True, exist_ok=True)
    return folder

def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return default

class Settings:
    def __init__(self):
        self.path = data_dir() / 'settings.json'
        self.values = read_json(self.path, {})
        if not isinstance(self.values, dict):
            self.values = {}

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value
        write_json(self.path, self.values)
