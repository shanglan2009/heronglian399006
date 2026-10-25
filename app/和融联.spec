# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import ssl
project = Path(SPECPATH).parent
# The bundled runtime's SSLContext loads this public certificate policy file
# relative to ssl.py. Keep certificate validation intact in the frozen app.
ssl_policy = Path(ssl.__file__).with_name('codex-rejected-cas.pem')
runtime_data = [(str(ssl_policy), '.')] if ssl_policy.is_file() else []
a = Analysis(
    [str(project/'app/main.py')],
    pathex=[str(project)],
    binaries=[],
    datas=[(str(project/'app/ui'), 'app/ui'),(str(project/'data/article_rule_registry.json'),'data')]+runtime_data,
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
    [],
    exclude_binaries=True,
    name='和融联',
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
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='和融联',
)
