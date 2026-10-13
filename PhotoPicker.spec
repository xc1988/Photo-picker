# -*- mode: python ; coding: utf-8 -*-
# 打包：pyinstaller --noconfirm --clean PhotoPicker.spec
from PyInstaller.utils.hooks import collect_dynamic_libs

binaries = collect_dynamic_libs('rawpy')

a = Analysis(
    ['photopicker.py'],
    pathex=[],
    binaries=binaries,
    datas=[('assets', 'assets')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 只用到 QtWidgets/QtGui/QtCore，其余 Qt 模块全部不参与分析
    excludes=[
        'PySide6.QtNetwork', 'PySide6.QtQml', 'PySide6.QtQuick',
        'PySide6.QtQuickWidgets', 'PySide6.QtOpenGL', 'PySide6.QtOpenGLWidgets',
        'PySide6.QtPdf', 'PySide6.QtPdfWidgets', 'PySide6.QtSvg',
        'PySide6.QtSvgWidgets', 'PySide6.QtTest', 'PySide6.QtDBus',
        'PySide6.QtXml', 'PySide6.QtBluetooth', 'PySide6.QtMultimedia',
        'yaml', 'pydoc', 'unittest', 'tkinter', 'ssl',
    ],
    noarchive=False,
    optimize=0,
)

# Qt 的 hook 会顺手把软件渲染 GL、QML/Quick、PDF、Direct2D 后端、多语言翻译、
# Pillow 的 AVIF 解码等拖进来，本程序一概用不到，直接按目标路径剔除（约省 65MB）
DROP = (
    'opengl32sw', 'qt6quick', 'qt6qml', 'qt6opengl', 'qt6pdf', 'qt6network',
    'qt6svg', 'qt6virtualkeyboard', 'qdirect2d', 'libcrypto-3-x64',
    'pyside6/translations/', 'plugins/tls/', 'plugins/networkinformation/',
    'plugins/iconengines/', 'plugins/generic/', 'pil/_avif',
)


def keep(entry):
    dest = str(entry[0]).replace('\\', '/').lower()
    return not any(d in dest for d in DROP)


a.binaries = [b for b in a.binaries if keep(b)]
a.datas = [d for d in a.datas if keep(d)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='PhotoPicker',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets/app.ico'],
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='PhotoPicker',
)
