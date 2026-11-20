"""Reproducible standalone windowed build; never copies over another program."""
from pathlib import Path
import os
import subprocess
import sys

root = Path(__file__).resolve().parent
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication
from calibration.ui.theme import app_icon
app = QApplication.instance() or QApplication([])
assets = root / 'assets'
assets.mkdir(exist_ok=True)
icon = assets / 'app.ico'
if not app_icon().pixmap(256, 256).save(str(icon), 'ICO'):
    raise RuntimeError('Application icon could not be written')

arguments = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile', '--windowed',
    '--name', '교정자동화', '--icon', str(icon), '--distpath', str(root / 'dist'),
    '--version-file', str(assets / 'version_info.txt'),
    '--workpath', str(root / 'build'), '--specpath', str(root), '--paths', str(root),
    '--copy-metadata', 'pyvisa', '--collect-submodules', 'pyvisa', '--collect-submodules', 'serial',
    '--add-data', str(root / 'THIRD_PARTY_NOTICES.md') + os.pathsep + '.',
    '--exclude-module', 'tkinter', '--exclude-module', 'numpy', '--exclude-module', 'pandas',
    '--exclude-module', 'matplotlib', '--exclude-module', 'PySide6.QtQml', '--exclude-module', 'PySide6.QtQuick',
    '--exclude-module', 'PySide6.QtWebEngineCore', '--exclude-module', 'PySide6.QtWebEngineWidgets',
    '--exclude-module', 'PySide6.QtNetwork', '--exclude-module', 'PySide6.QtMultimedia',
    str(root / 'app.py')]
# Do not let tool-host PATH entries supply unrelated copies of DLLs. For example,
# Poppler's ICU has versioned exports while Qt uses the Windows ICU API; bundling
# that same-named DLL makes QtWidgets fail only in the packaged application.
build_env = os.environ.copy()
if os.name == 'nt':
    windows = Path(os.environ.get('SystemRoot', 'C:/Windows'))
    build_env['PATH'] = os.pathsep.join(map(str, (Path(sys.executable).parent,
        Path(sys.base_prefix), windows / 'System32', windows, windows / 'System32' / 'Wbem')))
subprocess.run(arguments, cwd=root, check=True, env=build_env)
print(root / 'dist' / '교정자동화.exe')
