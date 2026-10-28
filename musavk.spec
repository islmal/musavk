# -*- mode: python ; coding: utf-8 -*-
# Build:  python -m PyInstaller musavk.spec --noconfirm --clean
# Output: dist\musavk\musavk.exe   (one-folder, windowed)
#
# - libmpv-2.dll is bundled next to musavk.exe (contents_directory='.').
#   A newer libmpv-2.dll can be dropped in place over it.
# - certifi's cacert.pem is bundled at certifi\cacert.pem; player_widget.ca_bundle_path()
#   resolves it via sys._MEIPASS when frozen (libmpv's curl has no CA store).
# - yt-dlp ships its own PyInstaller hook (yt_dlp.__pyinstaller), which collects all
#   extractor modules, so dynamic extractor loading keeps working.
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(SPECPATH)


def find_libmpv():
    for d in (ROOT, os.path.join(ROOT, "lib")):
        for name in ("libmpv-2.dll", "mpv-2.dll"):
            p = os.path.join(d, name)
            if os.path.isfile(p):
                return p
    raise SystemExit("libmpv-2.dll not found in the project root or lib\\ - cannot build.")


a = Analysis(
    ["main.py"],
    pathex=[ROOT],
    binaries=[(find_libmpv(), ".")],
    datas=collect_data_files("certifi"),          # certifi/cacert.pem
    hiddenimports=["mpv", *collect_submodules("yt_dlp.extractor")],
    excludes=["tkinter", "unittest", "pydoc_data", "PyQt6.QtWebEngineCore",
              "PyQt6.QtQml", "PyQt6.QtQuick", "PyQt6.Qt3DCore", "PyQt6.QtMultimedia"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="musavk",
    console=False,               # --windowed: no console window behind the GUI
    upx=False,
    contents_directory=".",      # bundle files (incl. libmpv-2.dll) sit next to the .exe
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="musavk",
    upx=False,
)
