# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller-spec för Windows-exe:n (craftpilot-agent.exe).

Agenten läser all konfiguration ur miljövariabler och skriver allt till
state-katalogen, så det finns inga datafiler att bunta. cryptography kommer in
via migration_transfer och PyInstaller har egna hooks för den.

Byggs med: pyinstaller --noconfirm craftpilot-agent.spec
"""

a = Analysis(
    ["craftpilot_runtime_agent.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "test_runtime_agent", "test_migration_transfer"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="craftpilot-agent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
