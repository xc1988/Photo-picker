# -*- coding: utf-8 -*-
"""摄影师择影工具 · PySide6 桌面版（单文件，方便拷贝到其他电脑）

功能：RAW+JPG 同名合并显示 / 磁盘缩略图缓存 / 键盘快速标记 / 导出已选(成对复制)
依赖：pip install PySide6 Pillow rawpy
用法：python photopicker.py [照片文件夹]，或双击 择影工具.bat，或拖文件夹到窗口
"""
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

from PIL import Image, ImageOps
from PIL.ExifTags import TAGS as EXIF_TAGS
from PySide6.QtCore import (QAbstractListModel, QModelIndex, QEvent, QPoint,
                            QPointF, QRect, QRectF, QSize, QSettings, Qt,
                            QThreadPool, QItemSelectionModel, QSortFilterProxyModel,
                            Signal, QObject)
from PySide6.QtGui import (QColor, QFont, QIcon, QImage, QImageReader, QPainter,
                           QPainterPath, QPen, QPixmap, QPolygonF, QShortcut)
from PySide6.QtWidgets import (QApplication, QDialog, QFileDialog,
                               QHBoxLayout, QLabel, QListView, QMainWindow,
                               QMenu, QStyle, QStyledItemDelegate, QToolButton,
                               QPushButton, QSizePolicy, QStatusBar,
                               QVBoxLayout, QWidget)

APP = "PhotoPicker"
VERSION = "1.1.0"
AUTHOR = "DP"
# PyInstaller 打包后资源在解压目录（sys._MEIPASS），源码运行时在同级 assets/
if getattr(sys, "frozen", False):
    ASSETS = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "assets"
else:
    ASSETS = Path(__file__).resolve().parent / "assets"
THUMB_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / APP / "thumbs"
THUMB_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = ".photopicker.json"


def set_hidden(path: Path):
    if os.name == "nt":
        import ctypes
        ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x2)  # FILE_ATTRIBUTE_HIDDEN
THUMB_SIZE, FULL_SIZE = 360, 2200
CARD_W, CARD_H, IMG_H = 244, 180, 152

RAW_EXTS = {".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".srf", ".sr2",
            ".raf", ".dng", ".rw2", ".orf", ".pef", ".ptx", ".raw", ".3fr",
            ".fff", ".srw"}
JPG_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}

# 与 Web 版共用的缓存/状态约定：
#   缩略图 = THUMB_DIR/<md5(path|mtime|size)>.jpg，大图 = 同键 + "_full.jpg"
#   状态   = 照片文件夹/.photopicker.json  {文件名小写stem: "pick"|"no"}

# 标记状态元数据：图标 / 颜色 / 中文标签（图标+颜色双通道，不依赖色觉区分状态）
STATUS_META = {
    "pick": ("✓", "#34c06b", "已选"),
    "no":   ("✕", "#e5484d", "已淘汰"),
}


# ------------------------------------------------------------- core --------
def cache_key(path: Path) -> str:
    st = path.stat()
    return hashlib.md5(f"v2|{path}|{st.st_mtime_ns}|{st.st_size}".encode()).hexdigest()


def make_display_file(path: Path, dest: Path, max_size: int):
    """生成 JPEG 缓存文件（缩略图或 RAW 大图）。线程安全：先写临时再改名。"""
    ext = path.suffix.lower()
    tmp = dest.with_suffix(".tmp")
    if ext in JPG_EXTS:
        with Image.open(path) as im:
            im.draft("rgb", (max_size * 2, max_size * 2))
            im = ImageOps.exif_transpose(im)
            im.thumbnail((max_size, max_size))
            im.convert("RGB").save(tmp, "JPEG", quality=85)
    else:
        import rawpy
        with rawpy.imread(str(path)) as raw:
            try:
                arr = raw.postprocess(use_camera_wb=True,
                                      half_size=(max_size <= THUMB_SIZE),
                                      output_bps=8)
            except Exception:
                arr = raw.postprocess(output_bps=8)
        im = Image.fromarray(arr)
        im.thumbnail((max_size, max_size))
        im.convert("RGB").save(tmp, "JPEG", quality=85)
    os.replace(tmp, dest)


def ensure_thumb(display: Path) -> Path:
    dest = THUMB_DIR / (cache_key(display) + ".jpg")
    if not dest.exists():
        make_display_file(display, dest, THUMB_SIZE)
    return dest


def ensure_full(display: Path) -> Path:
    if display.suffix.lower() in JPG_EXTS:
        return display
    dest = THUMB_DIR / (cache_key(display) + "_full.jpg")
    if not dest.exists():
        make_display_file(display, dest, FULL_SIZE)
    return dest


def scan_folder(folder: Path):
    groups = {}
    for entry in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_file():
            continue
        ext = entry.suffix.lower()
        if ext in JPG_EXTS or ext in RAW_EXTS:
            g = groups.setdefault(entry.stem.lower(), {"jpg": None, "raw": None})
            g["jpg" if ext in JPG_EXTS else "raw"] = entry
    state_file = folder / STATE_FILE
    state = {}
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text("utf-8"))
        except Exception:
            state = {}
        if state:
            set_hidden(state_file)
        else:
            state_file.unlink(missing_ok=True)   # 空状态文件没必要存在
    photos = []
    for stem, g in groups.items():
        display = g["jpg"] or g["raw"]
        photos.append({
            "stem": stem, "name": display.stem,
            "display": display,
            "files": [f for f in (g["raw"], g["jpg"]) if f],
            "kind": "RAW+JPG" if g["raw"] and g["jpg"] else ("RAW" if g["raw"] else "JPG"),
            "status": state.get(stem, ""),
        })
    photos.sort(key=lambda p: p["name"].lower())
    return photos


# ------------------------------------------------------------- model -------
ID, NAME, KIND, STATUS, THUMB, DISPLAY = range(Qt.UserRole, Qt.UserRole + 6)


class PhotoModel(QAbstractListModel):
    def __init__(self):
        super().__init__()
        self.items = []
        self.thumbs = {}          # row -> QPixmap

    def rowCount(self, _=QModelIndex()):
        return len(self.items)

    def flags(self, index):
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        p = self.items[index.row()]
        if role == THUMB:
            return self.thumbs.get(index.row())
        if role == STATUS:
            return p["status"]
        if role == NAME:
            return p["name"]
        if role == KIND:
            return p["kind"]
        if role == DISPLAY:
            return p["display"]
        return None

    def set_status(self, row, status):
        self.items[row]["status"] = status
        idx = self.index(row)
        self.dataChanged.emit(idx, idx, [STATUS])

    def put_thumb(self, row, pm):
        if row >= len(self.items):
            return
        self.thumbs[row] = pm
        idx = self.index(row)
        self.dataChanged.emit(idx, idx, [THUMB])

    def load(self, photos):
        self.beginResetModel()
        self.items, self.thumbs = photos, {}
        self.endResetModel()


class StatusFilter(QSortFilterProxyModel):
    wanted = "all"

    def filterAcceptsRow(self, row, _parent):
        s = self.sourceModel().data(self.sourceModel().index(row), STATUS)
        if self.wanted == "all":
            return True
        return (s or "todo") == self.wanted


# ------------------------------------------------------------ worker -------
class Ready(QObject):
    thumb_done = Signal(int, str)   # row, cache-file-path
    full_done = Signal(str, str)    # token(display path), file-path


class Pool:
    def __init__(self, max_threads=4):
        self.pool = QThreadPool()
        self.pool.setMaxThreadCount(max_threads)
        self.ready = Ready()
        self.pending = set()
        self.full_pending = set()

    def reset(self):
        self.pending.clear()
        self.full_pending.clear()

    def submit_thumb(self, row, display: Path):
        if row in self.pending:
            return
        self.pending.add(row)
        self.pool.start(lambda: self._run(row, str(display), "thumb"))

    def submit_full(self, token, display: Path):
        if token in self.full_pending:
            return
        self.full_pending.add(token)
        self.pool.start(lambda: self._run(str(token), str(display), "full"))

    def _run(self, key, path_str, mode):
        try:
            p = ensure_thumb(Path(path_str)) if mode == "thumb" \
                else ensure_full(Path(path_str))
            result = str(p)
        except Exception:
            result = ""
        if mode == "thumb":
            self.pending.discard(key)
            self.ready.thumb_done.emit(key, result)
        else:
            self.full_pending.discard(key)
            self.ready.full_done.emit(path_str, result)


# ------------------------------------------------------------- widgets -----
class ProgressLine(QWidget):
    """极细进度线（上）+ 小号 X / N 数字（下），跟随当前浏览位置"""
    def __init__(self, parent=None, width=132):
        super().__init__(parent)
        self.setFixedSize(width, 22)
        self.frac = 0.0
        self.txt = ""

    def set_pos(self, pos, total):
        self.frac = (pos / total) if total > 0 else 0.0
        self.txt = f"{pos} / {total}" if total > 0 else ""
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w = self.width()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#23252c"))
        p.drawRoundedRect(0, 3, w, 2, 1, 1)
        if self.frac > 0:
            p.setBrush(QColor("#e8912a"))
            p.drawRoundedRect(0, 3, max(3, round(w * self.frac)), 2, 1, 1)
        p.setPen(QColor("#7c8089"))
        p.drawText(QRect(0, 7, w, 15), Qt.AlignHCenter | Qt.AlignVCenter, self.txt)
        p.end()


class WinBtn(QToolButton):
    """无边框窗控按钮：自绘 Fluent 风细线图标（─ / □ / ❐ / ✕），hover 圆角底"""
    def __init__(self, kind, tip, parent=None):
        super().__init__(parent)
        self.kind, self._maxed = kind, False
        self.setFixedSize(40, 30)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setToolTip(tip)
        self._hover = False

    def set_maxed(self, m):
        self._maxed = m
        self.update()

    def enterEvent(self, e):
        self._hover = True
        self.update()

    def leaveEvent(self, e):
        self._hover = False
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect()
        if self._hover:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#e5484d") if self.kind == "close"
                       else QColor(255, 255, 255, 26))
            p.drawRoundedRect(QRect(r).adjusted(2, 3, -2, -3), 6, 6)
        pen = QPen(QColor("#ff5555") if (self._hover and self.kind == "close")
                   else QColor("#d0d3d9"))
        pen.setWidthF(1.3)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        cx, cy = r.width() / 2, r.height() / 2
        if self.kind == "min":
            p.drawLine(QPointF(cx - 5, cy + 5), QPointF(cx + 5, cy + 5))
        elif self.kind == "close":
            p.drawLine(QPointF(cx - 5, cy - 5), QPointF(cx + 5, cy + 5))
            p.drawLine(QPointF(cx + 5, cy - 5), QPointF(cx - 5, cy + 5))
        elif self.kind == "restore":
            p.drawRect(QRect(QPoint(int(cx) - 5, int(cy) - 2),
                             QPoint(int(cx) + 3, int(cy) + 5)))
            p.drawPolyline(QPolygonF([QPointF(cx - 2, cy - 5),
                                      QPointF(cx + 5, cy - 5),
                                      QPointF(cx + 5, cy + 2)]))
            p.drawLine(QPointF(cx - 5, cy - 2), QPointF(cx - 2, cy - 5))
        else:
            p.drawRect(QRect(QPoint(int(cx) - 5, int(cy) - 5),
                             QPoint(int(cx) + 5, int(cy) + 5)))
        p.end()


class FilmStrip(QWidget):
    """底部胶片条：横向缩略图，点击跳转，当前张高亮，滚轮横滚"""
    clicked = Signal(int)          # proxy 位置

    def __init__(self, model, proxy, parent=None):
        super().__init__(parent)
        self.model, self.proxy = model, proxy
        self.current, self.offset = -1, 0
        self.cell_w, self.cell_h, self.gap = 104, 70, 6
        self.setFixedHeight(self.cell_h + 12)
        self.setCursor(Qt.PointingHandCursor)

    def _src(self, i):
        return self.proxy.mapToSource(self.proxy.index(i, 0)).row()

    def _total_w(self):
        n = self.proxy.rowCount()
        return n * (self.cell_w + self.gap) - self.gap if n else 0

    def set_current(self, idx):
        self.current = idx
        self._ensure_visible()
        self.update()

    def _ensure_visible(self):
        if self.current < 0:
            return
        x = self.current * (self.cell_w + self.gap)
        vw = self.width()
        if x - self.offset < 0:
            self.offset = x
        elif x + self.cell_w - self.offset > vw:
            self.offset = x + self.cell_w - vw
        self.offset = max(0, min(self.offset,
                                 max(0, self._total_w() - vw)))

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.fillRect(self.rect(), QColor("#0c0d10"))
        n = self.proxy.rowCount()
        for i in range(n):
            x = i * (self.cell_w + self.gap) - self.offset
            if x > self.width() or x + self.cell_w < 0:
                continue
            cell = QRect(x, 6, self.cell_w, self.cell_h)
            p.fillRect(cell, QColor("#17181d"))
            pm = self.model.thumbs.get(self._src(i))
            if pm:
                s = max(cell.width() / pm.width(), cell.height() / pm.height())
                w, h = round(pm.width() * s), round(pm.height() * s)
                p.save()
                p.setClipRect(cell)
                p.drawPixmap(cell.x() + (cell.width() - w) // 2,
                             cell.y() + (cell.height() - h) // 2, w, h, pm)
                p.restore()
            if i == self.current:
                p.setPen(QPen(QColor("#e8912a"), 2))
                p.setBrush(Qt.NoBrush)
                p.drawRoundedRect(QRect(cell).adjusted(1, 1, -1, -1), 4, 4)
        p.end()

    def mousePressEvent(self, e):
        i = (e.position().x() + self.offset) // (self.cell_w + self.gap)
        if 0 <= i < self.proxy.rowCount():
            self.clicked.emit(int(i))

    def wheelEvent(self, e):
        d = e.angleDelta().y() or e.angleDelta().x()
        self.offset = max(0, min(self.offset - d,
                                 max(0, self._total_w() - self.width())))
        self.update()


# ------------------------------------------------------------ delegate -----
class CardDelegate(QStyledItemDelegate):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.scale = 1.0

    def sizeHint(self, _opt, _index):
        return QSize(round(CARD_W * self.scale), round(CARD_H * self.scale))

    def paint(self, painter, option, index):
        p = painter
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        rect = option.rect.adjusted(3, 3, -3, -3)
        img_r = QRect(rect.left(), rect.top(), rect.width(),
                      round(IMG_H * self.scale))
        status = index.data(STATUS)
        thumb = index.data(THUMB)

        path = QPainterPath()
        path.addRoundedRect(img_r, 6, 6)
        p.fillPath(path, QColor("#1b1d23"))
        if thumb:
            src = thumb
            s = max(img_r.width() / src.width(), img_r.height() / src.height())
            w, h = int(src.width() * s), int(src.height() * s)
            x = img_r.left() + (img_r.width() - w) // 2
            y = img_r.top() + (img_r.height() - h) // 2
            p.setClipPath(path)
            p.drawPixmap(QRect(x, y, w, h), src)
            p.setClipping(False)
        else:
            p.setClipPath(path)
            p.fillRect(img_r, QColor("#20222a"))
            p.setClipping(False)
        if status == "no":
            p.setClipPath(path)
            p.fillPath(path, QColor(0, 0, 0, 120))
            p.setClipping(False)

        meta = STATUS_META.get(status)
        border = meta[1] if meta else None
        if not border and option.state & (QStyle.State_Selected | QStyle.State_HasFocus):
            border = "#4f8cff"
        if not border and option.state & QStyle.State_MouseOver:
            border = "#3a3e4a"                    # hover 细边框反馈
        if border:
            p.setPen(QColor(border))
            p.drawPath(path)

        # 角标
        kind = index.data(KIND)
        p.setFont(option.font)
        tw = p.fontMetrics().horizontalAdvance(kind) + 12
        badge = QRect(img_r.right() - tw - 6, img_r.top() + 6, tw, 17)
        p.fillPath(self._round(badge, 6), QColor(0, 0, 0, 170))
        p.setPen(QColor("#aeb3bd"))
        p.drawText(badge, Qt.AlignCenter, kind)
        if meta:
            flag, color, _label = meta
            r = QRect(img_r.left() + 6, img_r.top() + 6, 17, 17)
            p.fillPath(self._round(r, 6), QColor(color))
            p.setPen(QColor("#fff"))
            p.drawText(r, Qt.AlignCenter, flag)

        p.setFont(option.font)
        p.setPen(QColor("#d7d9de" if status != "no" else "#7c8089"))
        name_r = QRect(rect.left(), img_r.bottom() + 4, rect.width(), 18)
        elided = p.fontMetrics().elidedText(index.data(NAME), Qt.ElideMiddle,
                                            name_r.width())
        p.drawText(name_r, Qt.AlignLeft, elided)

    @staticmethod
    def _round(r, rad):
        path = QPainterPath()
        path.addRoundedRect(r, rad, rad)
        return path


# ------------------------------------------------------------- grid view ---
class GridListView(QListView):
    """Ctrl+滚轮缩放缩略图；普通滚轮照常滚动。"""
    card_zoom = Signal(bool)

    def wheelEvent(self, e):
        if e.modifiers() & Qt.ControlModifier:
            e.accept()
            self.card_zoom.emit(e.angleDelta().y() > 0)
            return
        super().wheelEvent(e)


# ------------------------------------------------------------- viewer ------
class Viewer(QDialog):
    marked = Signal(int, str)    # source-row, status

    def __init__(self, win, model, proxy):
        super().__init__(win)
        self.setWindowTitle("择影 · 大图")
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.win, self.model, self.proxy = win, model, proxy
        self.idx = -1            # proxy 中的位置
        self._fs = False
        self._fs_direct = False  # 从网格直接全屏打开：退出时回到主界面
        self._src = QImage()     # 原尺寸图像，缩放时惰性加载
        self._scale = None       # None=适应窗口，否则为相对原图的倍率
        self._center = QPointF() # 缩放视口中心（源图像素坐标）
        self._pan = None
        self.resize(1200, 800)
        self.setStyleSheet("background:#0c0d10")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 14)

        self.img = QLabel("加载中…", alignment=Qt.AlignCenter)
        self.img.setStyleSheet("color:#7c8089")
        # Ignored：不把自己的 sizeHint（=Pixmap 尺寸）反馈给布局，
        # 否则 窗口→占位图→更大窗口 形成正反馈，无边框窗口会一路长高
        self.img.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.img.installEventFilter(self)
        lay.addWidget(self.img, 1)

        # 胶片浏览：缩略图条（仅最大化模式；全屏隐藏）
        self._stripbox = QWidget()
        striprow = QHBoxLayout(self._stripbox)
        striprow.setContentsMargins(0, 0, 0, 0)
        self.strip = FilmStrip(model, proxy)
        self.strip.clicked.connect(self._jump)
        striprow.addWidget(self.strip, 1)
        lay.addWidget(self._stripbox)
        model.dataChanged.connect(lambda *a: self.strip.update())

        # 大图上的状态角标（✓已选 / ✕已淘汰）
        self.badge = QLabel(self)
        self.badge.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.badge.hide()

        # EXIF 浮层（Tab 切换，全屏模式用）
        self.exif = QLabel(self)
        self.exif.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.exif.setStyleSheet(
            "QLabel{background:rgba(18,19,23,225);color:#e6e8ec;"
            "border:1px solid #2a2c33;border-radius:8px;padding:14px 18px;}")
        self.exif.hide()

        win.pool.ready.full_done.connect(self._on_full)
        for seq in ("Return", "Enter"):
            sc = QShortcut(seq, self)
            sc.setContext(Qt.WindowShortcut)
            sc.activated.connect(self._toggle_fs)
        force_grayscale_text(self)

    # ---- navigation ----
    def open_at_source(self, src_row, fullscreen=False):
        rows = [self.proxy.mapToSource(self.proxy.index(r, 0)).row()
                for r in range(self.proxy.rowCount())]
        if src_row not in rows:
            return
        self.idx = rows.index(src_row)
        self.show()
        if fullscreen and not self._fs:
            self._fs_direct = True
            self._toggle_fs()
        elif not fullscreen and not self.isMaximized():
            self._fs_direct = False
            self.showMaximized()
        self.raise_()
        self.activateWindow()
        self.refresh()

    def current_src_row(self):
        if 0 <= self.idx < self.proxy.rowCount():
            return self.proxy.mapToSource(self.proxy.index(self.idx, 0)).row()
        return -1

    def move(self, d):
        n = self.proxy.rowCount()
        if n == 0:
            return
        self.idx = (self.idx + d) % n      # 到头/到尾循环
        self.refresh()

    def _jump(self, pos):
        if pos == self.idx:              # 再点当前张 = 关闭大图
            self.close()
            return
        self.idx = pos
        self.refresh()

    def refresh(self):
        row = self.current_src_row()
        if row < 0:
            self.close()
            return
        p = self.model.items[row]
        self.win.set_progress_row(row)
        self.strip.set_current(self.idx)
        self._last_file = None
        self._exif_cache = None
        if self.exif.isVisible():          # 浮层保持打开：先占位，全图到达后刷新
            self._exif_pending = True
            self.exif.setText("加载中…")
            self.exif.adjustSize()
        self._token = str(p["display"])
        self._src = QImage()
        self._scale = None
        self._pan = None
        self._show_placeholder(row)          # 先即时显示缩略图，消除空白等待
        self._update_badge(p["status"])
        self.win.pool.submit_full(self._token, p["display"])
        self._prefetch()                     # 后台预取相邻，翻页即命中缓存

    def _update_badge(self, status):
        meta = STATUS_META.get(status)
        if not meta:
            self.badge.hide()
            return
        glyph, color, label = meta
        c = QColor(color)
        style = ("QLabel{border-radius:9px;padding:5px 16px;"
                 "color:#fff;background:rgba(%s,215)}"
                 % f"{c.red()},{c.green()},{c.blue()}")
        self.badge.setText(f"{glyph} {label}")
        self.badge.setStyleSheet(style)
        self.badge.adjustSize()
        self._place_badge()
        self.badge.show()
        self.badge.raise_()

    def _place_badge(self):
        self.badge.move(self.img.x() + self.img.width() - self.badge.width() - 10,
                        self.img.y() + 10)

    def _show_placeholder(self, row):
        pm = self.model.thumbs.get(row)
        if pm and not pm.isNull():
            limit = self._avail()
            if limit.width() > 0 and limit.height() > 0:
                self.img.setPixmap(pm.scaled(limit, Qt.KeepAspectRatio,
                                             Qt.SmoothTransformation))
            else:
                self.img.setPixmap(pm)
        else:
            self.img.setText("加载中…")

    def _prefetch(self):
        n = self.proxy.rowCount()
        for d in (1, -1, 2):                 # 向前为主，兼顾后退
            i = self.idx + d
            if 0 <= i < n:
                r = self.proxy.mapToSource(self.proxy.index(i, 0)).row()
                q = self.model.items[r]
                self.win.pool.submit_full(str(q["display"]), q["display"])

    def _on_full(self, token, path_str):
        if token != getattr(self, "_token", None):
            return
        if not path_str:
            self.img.setText("无法显示该照片")
            return
        self._last_file = path_str
        self._src = QImage()
        self._scale = None
        pm = self._load_scaled(Path(path_str))
        if pm:
            self.img.setPixmap(pm)
        else:
            self.img.setText("无法显示该照片")
        if self.exif.isVisible() or getattr(self, "_exif_pending", False):
            self._exif_pending = False     # 翻页时浮层保持打开并刷新内容
            self._show_exif()

    # ---- 缩放 ----
    def _avail(self):
        """图片区可用尺寸：全屏只剩边距，最大化另扣胶片条"""
        return self.size() - QSize(60, 30 if self._fs else 120)

    def _ensure_src(self):
        if not self._src.isNull():
            return True
        f = getattr(self, "_last_file", None)
        if not f:
            return False
        r = QImageReader(str(f))
        r.setAutoTransform(True)
        img = r.read()
        if img.isNull():
            return False
        self._src = img
        return True

    def _fit_scale(self):
        avail = self._avail()
        return min(avail.width() / self._src.width(),
                   avail.height() / self._src.height(), 1.0)

    def _clamp_center(self):
        iw, ih = self._src.width(), self._src.height()
        avail = self._avail()
        sw, sh = avail.width() / self._scale, avail.height() / self._scale
        cx = iw / 2 if sw >= iw else min(max(self._center.x(), sw / 2), iw - sw / 2)
        cy = ih / 2 if sh >= ih else min(max(self._center.y(), sh / 2), ih - sh / 2)
        self._center = QPointF(cx, cy)

    def _render(self):
        if self._src.isNull() or self._scale is None:
            return
        avail = self._avail()
        vw, vh = avail.width(), avail.height()
        if vw <= 0 or vh <= 0:
            return
        iw, ih = self._src.width(), self._src.height()
        sw, sh = vw / self._scale, vh / self._scale
        if sw >= iw and sh >= ih:
            out = self._src.scaled(vw, vh, Qt.KeepAspectRatio,
                                   Qt.SmoothTransformation)
        else:
            x0 = (iw - sw) / 2 if sw >= iw else min(max(self._center.x() - sw / 2, 0), iw - sw)
            y0 = (ih - sh) / 2 if sh >= ih else min(max(self._center.y() - sh / 2, 0), ih - sh)
            canvas = QImage(vw, vh, QImage.Format_RGB32)
            canvas.fill(QColor("#0c0d10"))
            p = QPainter(canvas)
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            p.drawImage(QRectF(0, 0, vw, vh), self._src, QRectF(x0, y0, sw, sh))
            p.end()
            out = canvas
        self.img.setPixmap(QPixmap.fromImage(out))

    def _set_scale(self, s):
        if s is None:                       # 适应窗口
            self._scale = None
            f = getattr(self, "_last_file", None)
            if f:
                pm = self._load_scaled(Path(f))
                if pm:
                    self.img.setPixmap(pm)
            return
        if not self._ensure_src():
            return
        if self._scale is None:
            self._center = QPointF(self._src.width() / 2, self._src.height() / 2)
        self._scale = max(min(s, 16.0), self._fit_scale())
        self._clamp_center()
        self._render()

    def _zoom_step(self, up, pos):
        if not self._ensure_src():
            return
        fit = self._fit_scale()
        cur = self._scale or fit
        new = min(max(cur * (1.25 if up else 0.8), fit), 16.0)
        if abs(new - cur) < 1e-9:
            return
        base = QPointF(self._center) if self._scale else \
            QPointF(self._src.width() / 2, self._src.height() / 2)
        lc = QPointF(self.img.width() / 2, self.img.height() / 2)
        off = (pos - lc) / cur
        self._center = base + off - (pos - lc) / new
        self._scale = new
        self._clamp_center()
        self._render()

    def eventFilter(self, obj, e):
        if obj is self.img:
            t = e.type()
            if t == QEvent.Wheel:
                self._zoom_step(e.angleDelta().y() > 0, e.position())
                return True
            if t == QEvent.MouseButtonPress and e.button() == Qt.LeftButton \
                    and self._scale:
                self._pan = (e.position(), QPointF(self._center))
                self.img.setCursor(Qt.ClosedHandCursor)
                return True
            if t == QEvent.MouseMove and self._pan:
                p0, c0 = self._pan
                d = e.position() - p0
                self._center = QPointF(c0.x() - d.x() / self._scale,
                                       c0.y() - d.y() / self._scale)
                self._clamp_center()
                self._render()
                return True
            if t == QEvent.MouseButtonRelease and e.button() == Qt.LeftButton:
                self._pan = None
                self.img.unsetCursor()
        return super().eventFilter(obj, e)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.badge.isVisible():
            self._place_badge()
        if self._scale and not self._src.isNull():
            self._clamp_center()
            self._render()
            return
        f = getattr(self, "_last_file", None)
        if f and self.img.pixmap() and not self.img.pixmap().isNull():
            pm = self._load_scaled(Path(f))
            if pm:
                self.img.setPixmap(pm)

    def _load_scaled(self, file: Path):
        r = QImageReader(str(file))
        r.setAutoTransform(True)          # 应用 EXIF 方向
        sz = r.size()
        limit = self._avail()
        if sz.isValid() and limit.width() > 0:
            s = min(limit.width() / sz.width(), limit.height() / sz.height(), 1.0)
            if s < 1:
                r.setScaledSize(sz * s)
        img = r.read()
        if img.isNull():
            return None
        if img.width() > limit.width() or img.height() > limit.height():
            img = img.scaled(limit, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        return QPixmap.fromImage(img)

    def mark(self, status):
        row = self.current_src_row()
        if row < 0:
            return
        cur = self.model.items[row]["status"]
        new = "" if (status and cur == status) else status
        self.marked.emit(row, new)
        self._update_badge(new)
        if new != "":
            self.move(1)

    # ---- EXIF 浮层 ----
    def _toggle_exif(self):
        if self.exif.isVisible():
            self._exif_pending = False
            self.exif.hide()
            return
        self._exif_pending = True
        self.exif.setText("加载中…")
        self.exif.adjustSize()
        self.exif.move(24, 24)
        self.exif.show()
        self.exif.raise_()
        if getattr(self, "_last_file", None):
            self._show_exif()              # 全图已就绪则立即填充

    def _show_exif(self):
        text = self._build_exif()
        if not text:
            return
        self._exif_pending = False
        self.exif.setText(text)
        self.exif.adjustSize()
        self.exif.move(24, 24)
        self.exif.show()
        self.exif.raise_()

    @staticmethod
    def _r(v):
        return float(v) if isinstance(v, (int, float)) else (
            v.numerator / v.denominator if v and hasattr(v, "numerator") else None)

    def _build_exif(self):
        f = getattr(self, "_last_file", None)
        if not f:
            return ""
        if getattr(self, "_exif_cache", None) == f:
            return self._exif_cache_text
        try:
            p = Path(f)
            with Image.open(p) as im:
                raw = im.getexif()
                w, h = im.size
                fmt, mode = im.format, im.mode
        except Exception:
            return ""
        d = {}
        for tag_id, val in raw.items():
            d[EXIF_TAGS.get(tag_id, str(tag_id))] = val
        try:                      # 拍摄参数在 ExifIFD 子目录里
            for tag_id, val in raw.get_ifd(0x8769).items():
                d[EXIF_TAGS.get(tag_id, str(tag_id))] = val
        except Exception:
            pass
        mtime = time.strftime("%Y/%m/%d %H:%M:%S", time.localtime(p.stat().st_mtime))
        size_mb = f"{p.stat().st_size / 1048576:.1f}MB"
        ORIENT = {1: "0°", 3: "180°", 6: "90° CW", 8: "270° CW"}
        PROG = {0: "Not specified", 1: "Manual", 2: "Program AE",
                3: "Aperture priority (semi-auto)", 4: "Shutter priority (semi-auto)",
                5: "Creative Program", 6: "Action Program", 7: "Portrait", 8: "Landscape"}
        METER = {1: "Average", 2: "CenterWeightedAverage", 3: "Spot",
                 4: "MultiSpot", 5: "Pattern", 6: "Partial", 255: "Other"}
        def g(k):
            v = d.get(k)
            return str(v).strip().strip("\x00") if v not in (None, "") else ""
        lines = [f"文件名: {p.name} ({self.idx + 1}/{self.proxy.rowCount()})",
                 f"图片大小: {size_mb}",
                 f"修改日期: {mtime}"]
        dim = f"{w}x{h}"
        bits = [b for b in (fmt, mode) if b]
        if bits:
            dim += f" ({','.join(bits)})"
        lines.append(f"图片信息: {dim}")
        cam = []
        for label, key in (("相机制造商", "Make"), ("相机型号", "Model"),
                           ("软件", "Software"), ("艺术家", "Artist"),
                           ("版权信息", "Copyright")):
            v = g(key)
            if v:
                cam.append(f"{label}: {v}")
        dt = g("DateTimeOriginal") or g("DateTimeDigitized") or g("DateTime")
        if dt:
            dt = dt[:10].replace(":", "/") + dt[10:]   # EXIF 用冒号日期，改斜杠
            tz = g("OffsetTimeOriginal") or g("OffsetTime")
            cam.append(f"拍摄日期: {dt}" + (f" ({tz})" if tz else ""))
        flash = d.get("Flash")
        if flash is not None:
            cam.append(f"闪光灯: {'Yes' if int(flash) & 1 else 'No'}")
        fl = self._r(d.get("FocalLength"))
        if fl:
            cam.append(f"焦距: {fl:.0f}mm")
        et = self._r(d.get("ExposureTime"))
        if et:
            cam.append(f"快门速度: {et:.5f}s" + (f" (1/{round(1 / et):.0f})" if et < 1 else ""))
        fn = self._r(d.get("FNumber"))
        if fn:
            cam.append(f"光圈数: f/{fn:g}")
        iso = g("ISOSpeedRatings")
        if iso:
            cam.append(f"ISO 感光度: {iso}")
        ep = d.get("ExposureProgram")
        if ep is not None:
            cam.append(f"曝光程序: {PROG.get(int(ep), ep)}")
        mm = d.get("MeteringMode")
        if mm is not None:
            cam.append(f"测光模式: {METER.get(int(mm), mm)}")
        ori = d.get("Orientation")
        if ori is not None:
            cam.append(f"旋转信息: {ORIENT.get(int(ori), ori)}")
        self._exif_cache, self._exif_cache_text = f, "\n".join(lines + [""] + cam)
        return self._exif_cache_text

    # ---- keys ----
    def keyPressEvent(self, e):
        k = e.key()
        if k in (Qt.Key_Return, Qt.Key_Enter):
            self._toggle_fs()
        elif k == Qt.Key_Tab:
            self._toggle_exif()
        elif k == Qt.Key_Escape:
            if self._fs:
                self._toggle_fs()
            else:
                self.close()
        elif k in (Qt.Key_Left, Qt.Key_A):
            self.move(-1)
        elif k == Qt.Key_Right:
            self.move(1)
        elif k == Qt.Key_S:
            self.mark("pick")
        elif k == Qt.Key_D:
            self.mark("no")
        elif k == Qt.Key_F:
            self.mark("")
        elif k in (Qt.Key_0, Qt.Key_Insert):        # Insert=小键盘0 (NumLock关)
            self._set_scale(None)
        elif k in (Qt.Key_1, Qt.Key_End):           # End=小键盘1
            self._set_scale(1.0)
        elif k in (Qt.Key_2, Qt.Key_Down):          # Down=小键盘2
            self._set_scale(2.0)
        else:
            super().keyPressEvent(e)

    def paintEvent(self, e):
        # 带 WA_TranslucentBackground 的无边框对话框不自绘样式背景，
        # 必须手动铺底，否则全屏/最大化时桌面透视
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#0c0d10"))
        p.end()
        super().paintEvent(e)

    def _toggle_fs(self):
        if self._fs:
            self._fs = False
            direct = self._fs_direct
            self._fs_direct = False
            self._stripbox.setVisible(True)    # 退出全屏恢复胶片条
            self.exif.hide()
            if direct:                       # 网格直接进入的全屏：退出回主界面
                self.close()
                return
            self.showNormal()
            self.showMaximized()
        else:
            self._fs = True
            self._stripbox.setVisible(False)   # 全屏只看图片
            self.showFullScreen()
        self.raise_()
        self.activateWindow()


# ------------------------------------------------------------- main --------
DARK = """
QMainWindow#mainWin, QListView, QStatusBar { background:#101114; color:#d7d9de }
QListView { outline:0; border:0; background:transparent }
QListView::item:hover { background:#1a1c22 }
QListView::item:selected { background:#1e2230; border:1px solid #4f8cff40; border-radius:8px }
QToolBar { background:#17181d; border:0; spacing:8; padding:6px }
QToolButton, QComboBox { background:#262830; color:#d7d9de; border:1px solid #23252c;
                         border-radius:6px; padding:6px 14px }
QToolButton:hover, QComboBox:hover { background:#30323c }
QComboBox::drop-down { border:0; width:20px }
QComboBox QAbstractItemView { background:#1d1f26; color:#d7d9de;
                              selection-background-color:#2a4d8f }
QLabel#title { font-size:16px; font-weight:700; color:#d7d9de }
QLabel#dim { color:#7c8089 }
QStatusBar::item { border:0 }
QScrollBar:vertical { background:transparent; width:10px; margin:2px }
QScrollBar::handle:vertical { background:#2e313a; border-radius:5px; min-height:40px }
QScrollBar::handle:vertical:hover { background:#3a3e4a }
QScrollBar:horizontal { background:transparent; height:10px; margin:2px }
QScrollBar::handle:horizontal { background:#2e313a; border-radius:5px; min-width:40px }
QScrollBar::handle:horizontal:hover { background:#3a3e4a }
QScrollBar::add-line, QScrollBar::sub-line { height:0; width:0 }
QScrollBar::add-page, QScrollBar::sub-page { background:transparent }
"""


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("择影 · Photo Picker")
        self.setObjectName("mainWin")
        self.setStyleSheet(DARK)
        self.resize(1280, 800)
        self.setAcceptDrops(True)
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)   # 无边框，工具栏即标题栏
        self._maxed = False
        self._normal_geom = None
        self.folder = None
        self.pool = Pool()
        self.pool.ready.thumb_done.connect(self._thumb_ready)

        self.model = PhotoModel()
        self.proxy = StatusFilter()
        self.proxy.setSourceModel(self.model)
        self.proxy.setDynamicSortFilter(False)
        # 关闭动态过滤是为省掉缩略图(THUMB)刷新；但标记状态变化时需实时重排
        self.model.dataChanged.connect(self._refilter_if_status)

        self.view = GridListView()
        self.view.setViewMode(QListView.IconMode)
        self.view.setResizeMode(QListView.Adjust)
        self.view.setWrapping(True)
        self.view.setUniformItemSizes(True)
        self.view.setSpacing(8)                       # 卡片间距，透口气
        self.view.setViewportMargins(10, 6, 10, 6)    # 网格四周留白，卡片不贴窗边
        self.view.viewport().setCursor(Qt.PointingHandCursor)
        self.delegate = CardDelegate(self)
        self.view.setItemDelegate(self.delegate)
        self.view.setModel(self.proxy)
        self._prog_row = -1
        self.view.selectionModel().currentChanged.connect(self._on_current_row)
        self.view.setSelectionMode(QListView.ExtendedSelection)   # Ctrl/Shift 多选
        self.view.setSelectionBehavior(QListView.SelectItems)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._context_menu)
        self.view.doubleClicked.connect(self._open_from_index)
        self.view.viewport().setMouseTracking(True)
        self.view.card_zoom.connect(self._card_zoom)
        self.setCentralWidget(self.view)

        # 空状态引导（覆盖在网格中央，加载照片后自动隐藏）
        # 用两个纯文本标签：富文本(RichText)会绕过灰度抗锯齿仍走 ClearType
        self.empty_hint = QWidget(self.view)
        self.empty_hint.setAttribute(Qt.WA_TransparentForMouseEvents)
        eh = QVBoxLayout(self.empty_hint)
        eh.setContentsMargins(0, 0, 0, 0)
        eh.setSpacing(6)
        t1 = QLabel("打开文件夹开始择影")
        t1.setAlignment(Qt.AlignCenter)
        t2 = QLabel("也可以直接把文件夹拖进窗口")
        t2.setAlignment(Qt.AlignCenter)
        t2.setStyleSheet("color:#7c8089")
        eh.addWidget(t1)
        eh.addWidget(t2)
        self.empty_hint.hide()

        # toolbar（无边框模式下兼作标题栏：空白处可拖动、双击切最大化）
        tb = self.addToolBar("main")
        tb.setMovable(False)
        tb.setStyleSheet("border-bottom:1px solid #23252c")
        tb.installEventFilter(self)
        self._tb = tb
        # 标题位置用"择"字 logo（V13 定稿 PNG），文字"择影"不再显示；点击弹关于窗
        brand = QLabel()
        if (ASSETS / "logo-ze.png").exists():
            brand.setPixmap(self._logo_pixmap())
            brand.setCursor(Qt.PointingHandCursor)
            brand.setToolTip("关于")
            brand.mousePressEvent = lambda e: self._show_about()
        else:
            brand.setText("  择影")
            brand.setObjectName("title")
        sub = QLabel("用心选择每一刻")
        sub.setStyleSheet("color:#e8912a")
        self.lbl_folder = sub            # 打开文件夹后显示其名字，未打开时显示标语
        brand_box = QWidget()          # logo 与标语同容器，间距收紧
        bl = QHBoxLayout(brand_box)
        bl.setContentsMargins(4, 0, 8, 0)
        bl.setSpacing(4)
        bl.addWidget(brand)
        bl.addWidget(sub)
        tb.addWidget(brand_box)
        self._act(tb, "打开文件夹", self.choose_folder)
        self.filter_btns = {}
        for label, key in (("全部", "all"), ("待选", "todo"), ("已选", "pick"), ("已废", "no")):
            b = QPushButton(label)
            b.setCursor(Qt.PointingHandCursor)
            b.setFocusPolicy(Qt.NoFocus)
            b.clicked.connect(lambda _, k=key: self._set_filter(k))
            tb.addWidget(b)
            self.filter_btns[key] = (label, b)
        self._update_filter_ui()
        spacer = _spacer()
        tb.addWidget(spacer)
        self.btn_export = self._act(tb, "导出已选 ▸", self.export_picks, accent=True)
        b_min = WinBtn("min", "最小化")
        b_min.clicked.connect(self.showMinimized)
        tb.addWidget(b_min)
        self.btn_max = WinBtn("max", "最大化")
        self.btn_max.clicked.connect(self._toggle_max)
        tb.addWidget(self.btn_max)
        b_close = WinBtn("close", "关闭")
        b_close.clicked.connect(self.close)
        tb.addWidget(b_close)

        # status bar
        self.progress = ProgressLine()
        self.progress.setToolTip("当前第几张 / 总数")
        self.progress.hide()
        self.statusBar().addPermanentWidget(self.progress)
        self.lbl_hint = QLabel("双击/空格 大图 · Enter 全屏大图 · S 选 D 淘汰 F 清除 · "
                               "Ctrl/Shift 多选 · 右键批量标记 · Ctrl+滚轮 缩放缩略图")
        self.lbl_hint.setObjectName("dim")
        self.statusBar().addPermanentWidget(self.lbl_hint)

        # 网格内快捷键
        for seq, fn in (("S", lambda: self.mark_current("pick")),
                        ("D", lambda: self.mark_current("no")),
                        ("F", lambda: self.mark_current("")),
                        ("Space", lambda: self._open_current()),
                        ("Return", lambda: self._open_current(fullscreen=True)),
                        ("Enter", lambda: self._open_current(fullscreen=True))):
            sc = QShortcut(seq, self)
            sc.setContext(Qt.WindowShortcut)
            sc.activated.connect(fn)

        self.settings = QSettings("PhotoPicker", "desktop")
        force_grayscale_text(self)
        self.delegate.scale = self.settings.value("cardScale", 1.0, type=float)
        self._relayout_cards()
        last = self.settings.value("lastFolder", "", type=str)
        if last and Path(last).is_dir():
            self.load_folder(Path(last))
        else:
            self._place_empty_hint()

    # ---- 网格缩略图缩放 ----
    def _relayout_cards(self):
        s = self.delegate.scale
        # 固定行距/列距：防止 IconMode 把行间距拉伸填满视口
        self.view.setGridSize(QSize(round((CARD_W + 8) * s), round((CARD_H + 8) * s)))
        self.view.reset()               # 使 sizeHint 缓存失效，重新布局

    def _card_zoom(self, up):
        lo, hi = 0.6, 2.2
        step = 1.1 if up else 1 / 1.1
        new = min(max(self.delegate.scale * step, lo), hi)
        if abs(new - self.delegate.scale) < 1e-3:
            return
        self.delegate.scale = new
        self.settings.setValue("cardScale", new)
        self._relayout_cards()
        self.statusBar().showMessage(f"缩略图 {int(new * 100)}%", 1200)

    @staticmethod
    def _logo_pixmap(side=34, ss=3):
        """加载 logo PNG；ss 倍过采样保证高 DPI 锐利。"""
        pm = QPixmap(str(ASSETS / "logo-ze.png")).scaled(
            side * ss, side * ss, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        pm.setDevicePixelRatio(ss)
        return pm

    def _show_about(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("关于")
        dlg.setFixedWidth(300)
        dlg.setStyleSheet(
            "QDialog{background:#17181d;color:#d7d9de}"
            "QPushButton{background:#262830;color:#d7d9de;border:1px solid #23252c;"
            "border-radius:6px;padding:5px 18px} QPushButton:hover{background:#30323c}")
        lay = QVBoxLayout(dlg)
        lay.setSpacing(2)
        logo = QLabel()
        logo.setPixmap(self._logo_pixmap(64))
        logo.setAlignment(Qt.AlignCenter)
        lay.addWidget(logo)
        slogan = QLabel("用心选择每一刻")
        slogan.setAlignment(Qt.AlignCenter)
        slogan.setStyleSheet("color:#e8912a")
        lay.addWidget(slogan)           # 标语在 logo 正下方，间距收紧
        name = QLabel("择影 · Photo Picker")
        name.setAlignment(Qt.AlignCenter)
        name.setStyleSheet("font-size:16px;font-weight:700")
        lay.addSpacing(6)
        lay.addWidget(name)
        info = QLabel(f"版本 {VERSION}    作者 {AUTHOR}\n"
                      "RAW+JPG 合并浏览 · 键盘快速标记 · 成对导出\n"
                      "PySide6 单文件桌面应用")
        info.setAlignment(Qt.AlignCenter)
        info.setStyleSheet("color:#7c8089")
        lay.addWidget(info)
        btnrow = QHBoxLayout()
        btnrow.addStretch()
        b = QPushButton("关闭")
        b.setCursor(Qt.PointingHandCursor)
        b.setFocusPolicy(Qt.NoFocus)
        b.clicked.connect(dlg.accept)
        btnrow.addWidget(b)
        btnrow.addStretch()
        lay.addLayout(btnrow)
        force_grayscale_text(dlg)
        dlg.exec()

    def _act(self, tb, text, fn, accent=False):
        b = QToolButton()
        b.setText(text)
        b.setCursor(Qt.PointingHandCursor)
        if accent:
            b.setStyleSheet("QToolButton{background:#4f8cff;color:#fff;border:0;"
                            "} QToolButton:hover{background:#639cff}")
        b.clicked.connect(fn)
        tb.addWidget(b)
        return b

    # ---- 无边框窗口：拖动 / 边缘缩放 ----
    def paintEvent(self, e):
        # 无边框窗口样式表底色不会自动铺满，手动铺底防透明
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#101114"))
        p.end()
        super().paintEvent(e)

    def _toggle_max(self):
        if self._maxed:
            self._maxed = False
            self.btn_max.set_maxed(False)
            if self._normal_geom:
                self.setGeometry(self._normal_geom)
        else:
            self._maxed = True
            self.btn_max.set_maxed(True)
            self._normal_geom = self.geometry()
            scr = QApplication.screenAt(self.geometry().center()) or self.screen()
            self.setGeometry(scr.availableGeometry())   # 不遮任务栏

    def eventFilter(self, obj, e):
        if obj is getattr(self, "_tb", None):
            if e.type() == QEvent.MouseButtonPress and e.button() == Qt.LeftButton:
                wh = self.windowHandle()
                if wh:
                    wh.startSystemMove()
                return True
            if e.type() == QEvent.MouseButtonDblClick:
                self._toggle_max()
                return True
        return super().eventFilter(obj, e)

    def showEvent(self, e):
        super().showEvent(e)
        if not getattr(self, "_rounded_done", False):
            self._rounded_done = True
            if os.name == "nt":
                try:
                    import ctypes
                    pref = ctypes.c_int(2)               # DWMWCP_ROUND
                    ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        int(self.winId()), 33, ctypes.byref(pref), 4)
                except Exception:
                    pass

    def nativeEvent(self, eventType, message):
        if os.name == "nt" and eventType == b"windows_generic_MSG" and not self._maxed:
            try:
                from ctypes import wintypes
                import ctypes
                msg = wintypes.MSG.from_address(int(message))
                if msg.message == 0x0084:                # WM_NCHITTEST
                    lp = msg.lParam
                    x = ctypes.c_short(lp & 0xFFFF).value
                    y = ctypes.c_short((lp >> 16) & 0xFFFF).value
                    r = wintypes.RECT()
                    ctypes.windll.user32.GetWindowRect(int(self.winId()), ctypes.byref(r))
                    B = 12                               # 物理像素手感边宽
                    l, t = x - r.left, y - r.top
                    rr, bb = r.right - x, r.bottom - y
                    hz = 10 if l < B else (11 if rr < B else 0)
                    vt = 3 if t < B else (15 if bb < B else 0)
                    code = {(3, 10): 13, (3, 0): 3, (3, 11): 14,
                            (15, 10): 12, (15, 0): 15, (15, 11): 16,
                            (0, 10): 10, (0, 11): 11}.get((vt, hz))
                    if code:
                        return True, code
            except Exception:
                pass
        return super().nativeEvent(eventType, message)

    # ---- folder ----
    def choose_folder(self):
        d = QFileDialog.getExistingDirectory(self, "选择照片文件夹",
                                             str(self.folder or self._start_dir()))
        if d:
            self.load_folder(Path(d))

    @staticmethod
    def _start_dir():
        return Path.home() / "Pictures"

    def load_folder(self, folder: Path):
        t0 = time.time()
        photos = scan_folder(folder)
        self.folder = folder
        self.pool.reset()
        self.model.load(photos)
        self._prog_row = -1
        self.view.scrollToTop()            # 换文件夹回到网格顶部
        self.settings.setValue("lastFolder", str(folder))
        self.setWindowTitle(f"择影 · {folder.name or folder}")
        self.lbl_folder.setText(folder.name or str(folder))
        for row in range(len(photos)):
            self.pool.submit_thumb(row, photos[row]["display"])
        self._update_stats()
        self.statusBar().showMessage(
            f"{len(photos)} 张照片（{sum(1 for p in photos if p['kind'] == 'RAW+JPG')} 组 RAW+JPG 已合并）"
            f"  {time.time() - t0:.2f}s", 5000)

    # ---- thumbs ----
    def _thumb_ready(self, row, path_str):
        if not path_str or row < 0:
            return
        img = QImage()
        if not img.load(path_str):
            return
        self.model.put_thumb(row, QPixmap.fromImage(img))

    # ---- marking ----
    def selected_src_rows(self):
        sel = self.view.selectionModel().selectedRows()
        return sorted({self.proxy.mapToSource(i).row() for i in sel})

    def mark_current(self, status):
        rows = self.selected_src_rows()
        if not rows:
            return
        # 全部已是该状态 → 清除；否则统一设为该状态
        if all(self.model.items[r]["status"] == status for r in rows):
            status = ""
        for r in rows:
            self.model.set_status(r, status)
        self.save_state()
        self._update_stats()

    def _context_menu(self, pos):
        idx = self.view.indexAt(pos)
        if not idx.isValid():
            return
        rows = self.selected_src_rows()
        src = self.proxy.mapToSource(idx).row()
        if src not in rows:
            self.view.clearSelection()
            self.view.selectionModel().select(
                idx, QItemSelectionModel.Select | QItemSelectionModel.Rows)
            rows = [src]
        m = QMenu(self)
        force_grayscale_text(m)
        m.setStyleSheet("QMenu{background:#1d1f26;color:#d7d9de;border:1px solid #23252c}"
                        "QMenu::item{padding:6px 26px;border-radius:6px}"
                        "QMenu::item:selected{background:#2a4d8f}")
        multi = f"（{len(rows)} 张）" if len(rows) > 1 else ""
        m.addAction(f"选它{multi}", lambda: self.mark_selected(rows, "pick"))
        m.addAction(f"淘汰{multi}", lambda: self.mark_selected(rows, "no"))
        m.addAction(f"清除{multi}", lambda: self.mark_selected(rows, ""))
        m.addSeparator()
        m.addAction("打开大图", lambda: self._viewer().open_at_source(rows[0]))
        m.addAction("打开所在文件夹", lambda: self._reveal(rows))
        m.exec(self.view.mapToGlobal(pos))

    def _reveal(self, rows):
        import subprocess
        p = self.model.items[rows[0]]["display"]
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", str(p)])   # 打开文件夹并选中该图
        else:
            subprocess.Popen(["xdg-open", str(p.parent)])

    def mark_selected(self, rows, status):
        for r in rows:
            self.model.set_status(r, status)
        self.save_state()
        self._update_stats()

    def apply_mark(self, row, status):
        self.model.set_status(row, status)
        self.save_state()
        self._update_stats()

    def save_state(self):
        if not self.folder:
            return
        state = {p["stem"]: p["status"] for p in self.model.items if p["status"]}
        f = self.folder / STATE_FILE
        try:
            if not state:
                f.unlink(missing_ok=True)        # 全部清除后不留空文件
                return
            f.write_text(json.dumps(state, ensure_ascii=False, indent=1), "utf-8")
            set_hidden(f)
        except OSError:
            pass

    def _update_stats(self):
        c = {"pick": 0, "no": 0}
        for p in self.model.items:
            if p["status"]:
                c[p["status"]] = c.get(p["status"], 0) + 1
        todo = len(self.model.items) - c["pick"] - c["no"]
        n = {"all": len(self.model.items), "todo": todo,
             "pick": c["pick"], "no": c["no"]}
        for key, (label, b) in self.filter_btns.items():
            b.setText(f"{label} {n[key]}" if self.model.items else label)
        self.btn_export.setText(f"导出已选 {n['pick']} ▸" if n["pick"] else "导出已选 ▸")
        self._update_progress()
        self._place_empty_hint()

    def _on_current_row(self, index):
        self._prog_row = self.proxy.mapToSource(index).row() if index.isValid() else -1
        self._update_progress()

    def _update_progress(self):
        total = len(self.model.items)
        pos = self._prog_row + 1 if 0 <= self._prog_row < total else 0
        self.progress.setVisible(total > 0)
        self.progress.set_pos(pos, total)

    def set_progress_row(self, src_row):
        self._prog_row = src_row
        self._update_progress()

    def _place_empty_hint(self):
        show = not self.model.items
        self.empty_hint.setVisible(show)
        if show:
            self.empty_hint.adjustSize()
            self.empty_hint.move(
                (self.view.width() - self.empty_hint.width()) // 2,
                (self.view.height() - self.empty_hint.height()) // 2)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self, "empty_hint"):
            self._place_empty_hint()

    # ---- navigation / viewer ----
    def _current_src_row(self):
        rows = self.selected_src_rows()
        return rows[0] if rows else -1

    def _open_current(self, fullscreen=False):
        row = self._current_src_row()
        if row >= 0:
            self._viewer().open_at_source(row, fullscreen=fullscreen)

    def _open_from_index(self, proxy_index):
        self._viewer().open_at_source(self.proxy.mapToSource(proxy_index).row())

    def _viewer(self):
        if not hasattr(self, "_viewer_dlg"):
            self._viewer_dlg = Viewer(self, self.model, self.proxy)
            self._viewer_dlg.marked.connect(self.apply_mark)
        return self._viewer_dlg

    # ---- filter ----
    def _refilter_if_status(self, _tl, _br, roles):
        # 仅状态变化时重排；缩略图刷新(THUMB)不触发，避免抖动
        if not roles or STATUS in roles:
            self.proxy.invalidateFilter()

    def _set_filter(self, key):
        self.proxy.wanted = key
        self.proxy.invalidateFilter()
        self._update_filter_ui()

    def _update_filter_ui(self):
        for key, (label, b) in self.filter_btns.items():
            active = key == self.proxy.wanted
            b.setStyleSheet(
                "QPushButton{background:transparent;border:none;padding:5px 10px;"
                "color:%s}"
                "QPushButton:hover{color:#d7d9de}" %
                ("#4f8cff" if active else "#7c8089"))

    # ---- export ----
    def export_picks(self):
        picks = [p for p in self.model.items if p["status"] == "pick"]
        if not picks:
            self.statusBar().showMessage("还没有标记任何“已选”照片（看大图按 S）", 4000)
            return
        default = str(self.folder / "已选照片")
        out = QFileDialog.getExistingDirectory(self, "导出已选照片到…", default)
        if not out:
            out = default
            Path(out).mkdir(parents=True, exist_ok=True)
        n = 0
        for p in picks:
            for f in p["files"]:
                tgt = Path(out) / f.name
                if not tgt.exists():
                    shutil.copy2(f, tgt)
                n += 1
        self.statusBar().showMessage(
            f"完成：{len(picks)} 张照片（{n} 个文件）已复制到 {out}", 8000)

    # ---- drag & drop ----
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() and \
           any(u.toLocalFile().strip() and Path(u.toLocalFile()).is_dir()
               for u in e.mimeData().urls()):
            e.acceptProposedAction()

    def dropEvent(self, e):
        for u in e.mimeData().urls():
            p = Path(u.toLocalFile())
            if p.is_dir():
                self.load_folder(p)
                break

    def closeEvent(self, e):
        self.pool.pool.waitForDone(1500)
        super().closeEvent(e)


def _spacer():
    w = QLabel()
    w.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
    w.setAttribute(Qt.WA_TransparentForMouseEvents)   # 让工具栏收到拖动
    return w


def force_grayscale_text(root):
    """Windows ClearType 子像素渲染在深色背景上产生红蓝彩边。
    Qt6/Windows 唯一有效的关闭方式：给每个绘制文字的控件设
    WA_TranslucentBackground（样式表已铺不透明底色，不会漏底）。
    顶层窗口不设——无边框窗口带该属性整体会变透明，需自绘背景。"""
    for w in root.findChildren(QWidget):
        w.setAttribute(Qt.WA_TranslucentBackground)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP)
    icon = ASSETS / "logo-ze.png"
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))    # 标题栏/任务栏图标
    # 优先微软雅黑 UI（小字号最清晰），其次阿里巴巴普惠体，都没有则系统默认
    from PySide6.QtGui import QFontDatabase
    fams = {f.lower() for f in QFontDatabase.families()}
    f = app.font()
    for want in ("Microsoft YaHei UI", "Microsoft YaHei",
                 "Alibaba PuHuiTi 3.0", "阿里巴巴普惠体 3.0", "Alibaba PuHuiTi"):
        if want.lower() in fams:
            f = QFont(want)
            break
    f.setPixelSize(12)   # 全局统一字号：除软件标题外不再单独设置大小/粗细
    # 深色主题禁用 ClearType 子像素渲染 → 灰度抗锯齿，消除文字红蓝彩边
    f.setStyleStrategy(QFont.StyleStrategy.NoSubpixelAntialias)
    app.setFont(f)
    win = MainWindow()
    win.show()
    args = [a for a in sys.argv[1:] if Path(a).is_dir()]
    if args:
        win.load_folder(Path(args[0]))
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
