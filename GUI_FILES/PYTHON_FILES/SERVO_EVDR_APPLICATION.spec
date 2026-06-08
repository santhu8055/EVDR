# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['D:\\PROJECT_FOLDER\\EVDR\\GUI_FILES\\PYTHON_FILES\\WEBUI_APP.py'],
    pathex=[],
    binaries=[],
    datas=[('D:\\PROJECT_FOLDER\\EVDR\\GUI_FILES\\UI_FILES', 'WEBUI')],
    hiddenimports=[],
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
    name='SERVO_EVDR_APPLICATION',
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
    icon=['D:\\PROJECT_FOLDER\\EVDR\\GUI_FILES\\UI_FILES\\ASSETS\\ICONS\\APP_ICON.ICO'],
)
