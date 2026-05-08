# -*- mode: python ; coding: utf-8 -*-
import os, sys
from pathlib import Path

block_cipher = None

a = Analysis(
    ['server.py'],
    pathex=[str(Path('server.py').resolve().parent)],
    binaries=[],
    datas=[
        ('manifest.json', '.'),
        ('sources', 'sources'),
    ],
    hiddenimports=[
        'uvicorn.logging',
        'uvicorn.loops.auto',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan.on',
        'anyio',
        'anyio._backends._asyncio',
        'httpx',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='audimo-audiobooks',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    onefile=True,
)
