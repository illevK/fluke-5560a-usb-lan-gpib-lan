# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import copy_metadata

datas = []
hiddenimports = []
datas += copy_metadata('pyvisa')
hiddenimports += collect_submodules('pyvisa')
hiddenimports += collect_submodules('serial')


a = Analysis(
    ['C:/Users/HCT-2714/Documents/Codex/2026-10-29/fluke-5560a-usb-lan-gpib-lan/work/exe_entry.py'],
    pathex=['C:/Users/HCT-2714/Documents/Codex/2026-10-29/fluke-5560a-usb-lan-gpib-lan/outputs'],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Fluke 교정기',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
