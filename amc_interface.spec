# -*- mode: python ; coding: utf-8 -*-
# AMC Interface — PyInstaller build spec
#
# Build command:
#   pip install pyinstaller
#   pyinstaller amc_interface.spec
#
# Output: dist/AMC_Interface/AMC_Interface.exe
# Distribute: zip the entire dist/AMC_Interface/ folder to field engineers.

from PyInstaller.utils.hooks import collect_data_files

a = Analysis(
    ['amc_interface_qt.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('assets/*.png', 'assets'),
        ('assets/*.ico', 'assets'),
        ('docs/*.md', 'docs'),
    ] + collect_data_files('qtawesome'),
    hiddenimports=[
        'protocol', 'settings', 'elf_reader',
        'scope_qt', 'terminal_qt', 'si_format',
        'electrical_params_qt', 'inertia_param_qt',
        'load_params_qt', 'save_params_qt', 'psif_param',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter', 'unittest', 'test', 'pydoc'],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='AMC_Interface',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon='assets/LogoAmcComm2.png',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='AMC_Interface',
)
