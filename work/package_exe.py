"""Build one Windows GUI executable using the isolated packaging environment."""
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parent.parent
subprocess.run([sys.executable, '-B', str(root / 'work/build_improved.py')], check=True)
subprocess.run([
    sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onefile', '--windowed',
    '--name', 'Fluke 교정기', '--paths', str(root / 'outputs'),
    '--distpath', str(root / 'outputs'), '--workpath', str(root / 'work/exe_build'),
    '--specpath', str(root / 'work'), '--collect-submodules', 'pyvisa',
    '--collect-submodules', 'serial', '--copy-metadata', 'pyvisa',
    str(root / 'work/exe_entry.py'),
], cwd=root, check=True)
