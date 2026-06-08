# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('d:\\PROJECT_FOLDER\\ARDUINO_PROJECTS\\ESP_BLE_DEBUGGING_ARDUINO_CODE\\PYTHON_TEST\\app_icon.ico', '.'), ('d:\\PROJECT_FOLDER\\ARDUINO_PROJECTS\\ESP_BLE_DEBUGGING_ARDUINO_CODE\\PYTHON_TEST\\app_icon.png', '.')]
binaries = []
hiddenimports = ['serial.tools.list_ports']
tmp_ret = collect_all('bleak')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['d:\\PROJECT_FOLDER\\ARDUINO_PROJECTS\\ESP_BLE_DEBUGGING_ARDUINO_CODE\\PYTHON_TEST\\PYTHON_TEST.PY'],
    pathex=[],
    binaries=binaries,
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
    [],
    exclude_binaries=True,
    name='SERVO_EVDR_APPLICATION',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['d:\\PROJECT_FOLDER\\ARDUINO_PROJECTS\\ESP_BLE_DEBUGGING_ARDUINO_CODE\\PYTHON_TEST\\app_icon.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='SERVO_EVDR_APPLICATION',
)
