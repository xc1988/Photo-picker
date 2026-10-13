# -*- coding: utf-8 -*-
"""摄影师选片工具 — 本地 Web 应用
RAW + JPG 同名自动合并为一张照片，键盘快速标记，选片结果复制到新文件夹。
"""
import hashlib
import json
import os
import shutil
import string
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file
from PIL import Image, ImageOps

APP_DIR = Path(__file__).resolve().parent
PORT = 8765
THUMB_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "PhotoPicker" / "thumbs"
THUMB_DIR.mkdir(parents=True, exist_ok=True)
THUMB_SIZE = 360
FULL_SIZE = 2200
STATE_FILE = ".photopicker.json"

RAW_EXTS = {".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".srf", ".sr2",
            ".raf", ".dng", ".rw2", ".orf", ".pef", ".ptx", ".raw", ".3fr",
            ".fff", ".srw"}
JPG_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}
SKIP_DIRS = {"已选照片", "选片结果", "@eaDir", "$RECYCLE.BIN", "System Volume Information"}

app = Flask(__name__, template_folder=str(APP_DIR / "templates"),
            static_folder=str(APP_DIR / "static"))

S = {"folder": None, "photos": {}, "order": [], "lock": threading.Lock()}
_thumb_pool = ThreadPoolExecutor(max_workers=3)
_gen_locks: dict[str, threading.Lock] = {}
_gen_locks_guard = threading.Lock()


def gen_lock(key: str) -> threading.Lock:
    with _gen_locks_guard:
        return _gen_locks.setdefault(key, threading.Lock())


# ----------------------------------------------------------- thumbnail ------
def cache_key(path: Path) -> str:
    st = path.stat()
    return hashlib.md5(f"v2|{path}|{st.st_mtime_ns}|{st.st_size}".encode()).hexdigest()


def _save_jpg(pil_img: Image.Image, dest: Path):
    pil_img.thumbnail((THUMB_SIZE, THUMB_SIZE))
    pil_img.convert("RGB").save(dest, "JPEG", quality=80)


def make_display(path: Path, dest: Path, max_size: int):
    ext = path.suffix.lower()
    if ext in JPG_EXTS:
        with Image.open(path) as im:
            im.draft("rgb", (max_size * 2, max_size * 2))
            im = ImageOps.exif_transpose(im)
            im.thumbnail((max_size, max_size))
            im.convert("RGB").save(dest, "JPEG", quality=88)
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
        im.convert("RGB").save(dest, "JPEG", quality=85)


def get_thumb(photo: dict) -> Path | None:
    display = Path(photo["display"])
    key = cache_key(display)
    dest = THUMB_DIR / (key + ".jpg")
    if not dest.exists():
        try:
            with gen_lock(key):
                if not dest.exists():
                    make_display(display, dest, THUMB_SIZE)
        except Exception:
            return None
    return dest


def get_full(photo: dict) -> Path:
    """大图：jpg 直接原文件；raw 生成一张缓存大图。"""
    display = Path(photo["display"])
    if display.suffix.lower() in JPG_EXTS:
        return display
    key = cache_key(display)
    dest = THUMB_DIR / (key + "_full.jpg")
    if not dest.exists():
        with gen_lock(key + "full"):
            if not dest.exists():
                make_display(display, dest, FULL_SIZE)
    return dest


def _prefetch(photo_id):
    with S["lock"]:
        photo = S["photos"].get(photo_id)
    if photo:
        get_thumb(photo)


# --------------------------------------------------------------- scan -------
def scan_folder(folder: Path):
    groups = {}
    for entry in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_file():
            continue
        ext = entry.suffix.lower()
        if ext in JPG_EXTS or ext in RAW_EXTS:
            g = groups.setdefault(entry.stem.lower(), {"jpg": None, "raw": None})
            g["jpg" if ext in JPG_EXTS else "raw"] = entry

    photos, order = {}, []
    state = load_state(folder)
    for stem, g in groups.items():
        files = []
        if g["raw"]:
            files.append(str(g["raw"]))
        if g["jpg"]:
            files.append(str(g["jpg"]))
        display = g["jpg"] or g["raw"]
        pid = hashlib.md5(stem.encode()).hexdigest()[:12]
        photos[pid] = {
            "id": pid, "name": display.stem,
            "display": str(display), "files": files,
            "hasRaw": bool(g["raw"]), "hasJpg": bool(g["jpg"]),
            "status": state.get(stem, ""),
        }
        order.append(pid)
    order.sort(key=lambda pid: photos[pid]["name"].lower())
    with S["lock"]:
        S["folder"], S["photos"], S["order"] = folder, photos, order
    for pid in order[:60]:
        _thumb_pool.submit(_prefetch, pid)


def load_state(folder: Path) -> dict:
    fp = folder / STATE_FILE
    if fp.exists():
        try:
            return json.loads(fp.read_text("utf-8"))
        except Exception:
            return {}
    return {}


def save_state():
    folder = S["folder"]
    if not folder:
        return
    state = {p["name"].lower(): p["status"] for p in S["photos"].values() if p["status"]}
    fp = folder / STATE_FILE
    if not state:
        fp.unlink(missing_ok=True)          # 无标记时不留空文件
        return
    fp.write_text(json.dumps(state, ensure_ascii=False, indent=1), "utf-8")
    if os.name == "nt":
        import ctypes
        ctypes.windll.kernel32.SetFileAttributesW(str(fp), 0x2)  # 隐藏


# --------------------------------------------------------------- api --------
@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/browse")
def browse():
    raw = request.args.get("path", "").strip()
    if not raw:
        drives = [f"{d}:\\" for d in string.ascii_uppercase
                  if os.path.exists(f"{d}:\\") and d not in "AB"]
        return jsonify({"parent": None, "drives": drives,
                        "dirs": [{"name": str(Path(p).name), "path": p}
                                 for p in drives]})
    cur = Path(raw)
    if not cur.is_dir():
        abort(404)
    dirs = []
    try:
        for e in sorted(cur.iterdir(), key=lambda p: p.name.lower()):
            if e.is_dir() and not e.name.startswith((".", "$")) \
               and e.name not in SKIP_DIRS:
                dirs.append({"name": e.name, "path": str(e)})
    except PermissionError:
        abort(403)
    parent = str(cur.parent) if cur.parent != cur else None
    return jsonify({"parent": parent, "current": str(cur), "dirs": dirs})


@app.post("/api/scan")
def api_scan():
    folder = Path(request.json["path"])
    if not folder.is_dir():
        abort(400)
    scan_folder(folder)
    return jsonify({"folder": str(folder), "count": len(S["order"])})


@app.get("/api/photos")
def api_photos():
    with S["lock"]:
        return jsonify({"folder": str(S["folder"]) if S["folder"] else None,
                        "photos": [S["photos"][pid] for pid in S["order"]]})


@app.post("/api/mark")
def api_mark():
    data = request.json
    with S["lock"]:
        photo = S["photos"].get(data["id"])
        if not photo:
            abort(404)
        photo["status"] = data.get("status", "")
        save_state()
        return jsonify({"ok": True, "status": photo["status"]})


@app.get("/thumb/<pid>")
def thumb(pid):
    with S["lock"]:
        photo = S["photos"].get(pid)
    if not photo:
        abort(404)
    tp = get_thumb(photo)
    if not tp:
        abort(422)
    return send_file(tp, mimetype="image/jpeg",
                     max_age=3600 * 24 * 30, conditional=True)


@app.get("/full/<pid>")
def full(pid):
    with S["lock"]:
        photo = S["photos"].get(pid)
    if not photo:
        abort(404)
    fp = get_full(photo)
    return send_file(fp, mimetype="image/jpeg", conditional=True)


@app.post("/api/export")
def api_export():
    dest = Path(request.json["output"])
    dest.mkdir(parents=True, exist_ok=True)
    picked = []
    with S["lock"]:
        for pid in S["order"]:
            p = S["photos"][pid]
            if p["status"] == "pick":
                picked.append(p)
        copied = 0
        for p in picked:
            for f in p["files"]:
                src = Path(f)
                tgt = dest / src.name
                if not tgt.exists():
                    shutil.copy2(src, tgt)
                copied += 1
    return jsonify({"ok": True, "photos": len(picked), "files": copied})


@app.get("/api/stats")
def api_stats():
    with S["lock"]:
        c = {"pick": 0, "no": 0, "todo": 0}
        for p in S["photos"].values():
            c[p["status"] or "todo"] += 1
    return jsonify(c)


def main():
    start_folder = sys.argv[1] if len(sys.argv) > 1 and os.path.isdir(sys.argv[1]) else None
    if start_folder:
        scan_folder(Path(start_folder))
    url = f"http://127.0.0.1:{PORT}"
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=PORT, threaded=True)


if __name__ == "__main__":
    main()
