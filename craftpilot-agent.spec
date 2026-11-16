# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller-spec för Windows-exe:n (craftpilot-agent.exe).

Agenten läser all konfiguration ur miljövariabler och skriver allt till
state-katalogen, så det finns inga datafiler att bunta. cryptography kommer in
via migration_transfer och PyInstaller har egna hooks för den.

Versionen som exe:n rapporterar till mastern genereras här, från bygget:
`CRAFTPILOT_AGENT_VERSION` om CI sätter den (taggens namn), annars
`git describe --tags --always`, annars "dev". Den skrivs som en liten modul
som agenten importerar, så ingen behöver komma ihåg att bumpa en konstant.

Byggs med: pyinstaller --noconfirm craftpilot-agent.spec
"""

import os
import pathlib
import re
import subprocess

spec_root = pathlib.Path(SPECPATH)


def resolve_build_version() -> str:
    """Taggens/commitens version, normaliserad till masterns tillåtna tecken."""
    override = os.environ.get("CRAFTPILOT_AGENT_VERSION", "").strip()
    if override:
        version = override
    else:
        try:
            version = subprocess.check_output(
                ["git", "describe", "--tags", "--always", "--dirty"],
                cwd=spec_root,
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        except (OSError, subprocess.CalledProcessError):
            version = ""
    version = version.lstrip("v") or "dev"

    return re.sub(r"[^A-Za-z0-9._+-]", ".", version) or "dev"


version_dir = spec_root / "build"
version_dir.mkdir(exist_ok=True)
(version_dir / "_build_version.py").write_text(
    '"""Genererad av craftpilot-agent.spec — ändra inte."""\n'
    f"BUILD_VERSION = {resolve_build_version()!r}\n",
    encoding="utf-8",
)

a = Analysis(
    ["craftpilot_runtime_agent.py"],
    pathex=[str(version_dir)],
    binaries=[],
    datas=[],
    hiddenimports=["_build_version"],
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
