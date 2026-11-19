# PyInstaller spec for winstaller.exe (onefile, windowed). Same file for x64 and ARM64:
# the build architecture is the architecture of the Python interpreter running PyInstaller.
#
#   python -m PyInstaller winstaller.spec --noconfirm --workpath <tmp>\build --distpath <tmp>\dist
#
# Deliberately NOT used:
# - UPX: antivirus false positives and slower start (every DLL is decompressed at run time).
# - uac_admin manifest: the app elevates itself at run time (platform.relaunch_as_admin), so
#   --smoke / --version / --dry-run never trigger UAC (CI runs the exe unattended).
from __future__ import annotations

import os
import re
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)

ROOT = Path(SPECPATH).resolve()  # noqa: F821  (SPECPATH is injected by PyInstaller)
PACKAGE = ROOT / "winstaller"

# ------------------------------------------------------------------ version

_version_src = (PACKAGE / "core" / "version.py").read_text(encoding="utf-8")
_m = re.search(r'return "(\d+)\.(\d+)\.(\d+)"', _version_src)
if not _m:
    raise SystemExit('winstaller.spec: no `return "X.Y.Z"` in winstaller/core/version.py')
VERSION = ".".join(_m.groups())
_vtuple = (*(int(p) for p in _m.groups()), 0)
_name_m = re.search(r'APP_NAME\s*=\s*"([^"]+)"', _version_src)
APP_NAME = _name_m.group(1) if _name_m else "Winstaller"

version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=_vtuple, prodvers=_vtuple, mask=0x3F, flags=0x0, OS=0x40004,
                      fileType=0x1, subtype=0x0, date=(0, 0)),
    kids=[
        StringFileInfo([
            StringTable("041504B0", [  # Polish, Unicode
                StringStruct("CompanyName", "ZMASLO"),
                StringStruct("FileDescription", APP_NAME),
                StringStruct("FileVersion", VERSION),
                StringStruct("InternalName", "winstaller"),
                StringStruct("LegalCopyright", "ZMASLO"),
                StringStruct("OriginalFilename", "winstaller.exe"),
                StringStruct("ProductName", APP_NAME),
                StringStruct("ProductVersion", VERSION),
            ])
        ]),
        VarFileInfo([VarStruct("Translation", [0x0415, 1200])]),
    ],
)

# ------------------------------------------------------------------ icon (optional)


def _find_icon() -> str | None:
    for rel in ("packaging/winstaller.ico", "packaging/icon.ico", "assets/winstaller.ico"):
        if (ROOT / rel).is_file():
            return str(ROOT / rel)
    ui = PACKAGE / "ui"
    if ui.is_dir():
        for name in ("winstaller.ico", "app.ico", "icon.ico"):
            hits = sorted(ui.rglob(name))
            if hits:
                return str(hits[0])
    return None


ICON = _find_icon()

# ------------------------------------------------------------------ package data
# Every non-Python file inside winstaller/ (catalog.toml, future UI .qss/.svg/.ico/...) is bundled
# under the same relative path, i.e. _MEIPASS/winstaller/..., as platform.resource_path expects.
# Computed at build time, so new UI resources are picked up without editing this file.

_SKIP_DIRS = {"__pycache__"}
_SKIP_SUFFIXES = {".py", ".pyc", ".pyo", ".pyi", ".pyd"}

datas = []
for dirpath, dirnames, filenames in os.walk(PACKAGE):
    dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
    for fn in filenames:
        src = Path(dirpath) / fn
        if fn.startswith(".") or src.suffix.lower() in _SKIP_SUFFIXES:
            continue
        datas.append((str(src), str(src.parent.relative_to(ROOT))))

if not any(Path(s).name == "catalog.toml" for s, _ in datas):
    raise SystemExit("winstaller.spec: winstaller/core/catalog.toml not found")

# ------------------------------------------------------------------ modules

# The UI imports these; forcing them keeps the build representative even before winstaller.ui
# exists. collect_submodules picks up modules the UI may import lazily.
hiddenimports = [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "PySide6.QtSvg",
    *collect_submodules("winstaller", filter=lambda name: not name.startswith("winstaller.tests")),
]

_QT_EXCLUDES = [
    f"PySide6.{m}"
    for m in (
        "Qt3DAnimation", "Qt3DCore", "Qt3DExtras", "Qt3DInput", "Qt3DLogic", "Qt3DRender",
        "QtAxContainer", "QtBluetooth", "QtCharts", "QtConcurrent", "QtDataVisualization",
        "QtDBus", "QtDesigner", "QtGraphs", "QtHelp", "QtHttpServer", "QtLocation",
        "QtMultimedia", "QtMultimediaWidgets", "QtNetwork", "QtNetworkAuth", "QtNfc", "QtOpenGL",
        "QtOpenGLWidgets", "QtPdf", "QtPdfWidgets", "QtPositioning", "QtPrintSupport", "QtQml",
        "QtQuick", "QtQuick3D", "QtQuickControls2", "QtQuickTest", "QtQuickWidgets",
        "QtRemoteObjects", "QtScxml", "QtSensors", "QtSerialBus", "QtSerialPort",
        "QtSpatialAudio", "QtSql", "QtStateMachine", "QtSvgWidgets", "QtTest",
        "QtTextToSpeech", "QtUiTools", "QtWebChannel", "QtWebEngineCore", "QtWebEngineQuick",
        "QtWebEngineWidgets", "QtWebSockets", "QtWebView", "QtXml",
    )
]

excludes = [
    # 1.x GUI stack and other GUI toolkits
    "tkinter", "_tkinter", "customtkinter", "darkdetect", "PIL", "PyQt5", "PyQt6", "PySide2",
    # dev / test tooling that must never end up in the exe
    "pytest", "_pytest", "pytestqt", "pytest_benchmark", "ruff", "PyInstaller", "setuptools",
    "pkg_resources", "distutils", "pip", "wheel",
    # unused stdlib
    "test", "idlelib", "lib2to3", "pydoc", "pydoc_data", "doctest", "unittest", "pdb",
    "xmlrpc", "sqlite3", "_sqlite3", "curses", "_curses", "turtle", "turtledemo",
    "ensurepip", "venv", "msilib", "tracemalloc", "cProfile", "profile", "pstats",
    *_QT_EXCLUDES,
]

# ------------------------------------------------------------------ Qt binary filtering
# The PySide6 hooks collect DLLs/plugins by dependency analysis and are generous. Keep only the
# plugins a QtWidgets app on Windows needs and drop large unused libraries.

_KEEP_PLUGINS = {
    "platforms": {"qwindows.dll"},
    "styles": None,  # all (qmodernwindowsstyle)
    "imageformats": {"qsvg.dll", "qico.dll"},
    "iconengines": {"qsvgicon.dll"},
}

_DROP_DLL = re.compile(
    r"^(opengl32sw|d3dcompiler_\d+|"
    r"qt6(network|qml\w*|quick\w*|pdf\w*|opengl\w*|virtualkeyboard\w*|designer\w*|sql|"
    r"labs\w*|printsupport|dbus|test|xml|concurrent|statemachine|uitools|help|websockets)"
    r")\.dll$",
    re.IGNORECASE,
)


def _keep(dest: str) -> bool:
    parts = Path(dest).parts
    if not parts or parts[0] != "PySide6":
        return True
    if len(parts) >= 2 and parts[1] == "translations":
        return False
    if len(parts) >= 4 and parts[1] == "plugins":
        allowed = _KEEP_PLUGINS.get(parts[2], set())
        return allowed is None or parts[-1].lower() in allowed
    if len(parts) >= 2 and parts[1] in ("qml", "resources", "metatypes", "typesystems",
                                        "include", "glue", "scripts", "support", "doc"):
        return False
    return not _DROP_DLL.match(parts[-1])


a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,  # core uses assert for invariants; keep them
)

a.binaries = [entry for entry in a.binaries if _keep(entry[0])]
a.datas = [entry for entry in a.datas if _keep(entry[0])]

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="winstaller",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=version_info,
    icon=ICON if ICON else "NONE",
    uac_admin=False,
)
