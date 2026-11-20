"""Check the packaged application with isolated simulated equipment only."""
from pathlib import Path
import json
import os
import subprocess
import time
from calibration import __version__

root = Path(__file__).resolve().parent
exe = root / 'dist' / '교정자동화.exe'
report = root / 'self-check-exe.json'
preview = root / 'preview-exe.png'
empty = root / 'empty-exe.png'
started = time.monotonic()
result = subprocess.run([str(exe), '--self-check', str(report), '--render-preview', str(preview)],
    timeout=60, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0, check=True)
seconds = time.monotonic() - started
data = json.loads(report.read_text(encoding='utf-8'))
assert data['version'] == __version__ and data['passed'] and data['empty_start_rows'] == 0 and not data['tk_loaded'], data
assert data['simulated_only'] and preview.stat().st_size > 10000
subprocess.run([str(exe), '--render-empty', str(empty)], timeout=60,
    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0, check=True)
assert empty.stat().st_size > 10000

from PyInstaller.archive.readers import CArchiveReader
from PyInstaller.utils.win32.versioninfo import read_version_info_from_executable
import pefile
archive = CArchiveReader(str(exe))
assert any(name.lower().endswith('qwindows.dll') for name in archive.toc)
assert not any('webengine' in name.lower() or '_tkinter' in name.lower() for name in archive.toc)
assert not any(name.lower().startswith('icudt') for name in archive.toc), 'Unrelated ICU runtime bundled'
embedded = archive.open_embedded_archive(next(name for name in archive.toc if name.endswith('.pyz')))
required = ['calibration.services.engine', 'calibration.services.drivers', 'calibration.instruments.fluke',
            'calibration.instruments.dut', 'calibration.domain.clipboard', 'serial', 'pyvisa']
assert all(name in embedded.toc for name in required)
assert not any(name.startswith(('tkinter', 'tests.', 'numpy', 'pandas')) for name in embedded.toc)
version = read_version_info_from_executable(str(exe))
major, minor, patch = map(int, __version__.split('.'))
assert (version.ffi.fileVersionMS, version.ffi.fileVersionLS) == ((major << 16) | minor, patch << 16)
with pefile.PE(str(exe), fast_load=True) as pe:
    assert pe.OPTIONAL_HEADER.Subsystem == 2, 'Console subsystem must be disabled'
data.update(packaged_seconds=round(seconds, 2), exe_bytes=exe.stat().st_size,
            console=False, native_windows_plugin=True, communication_modules_bundled=True)
report.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(data, ensure_ascii=False, indent=2))
