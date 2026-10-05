# -*- coding: utf-8 -*-
"""剪贴板 / 打开外部程序 —— 零第三方依赖，ctypes 直调 Win32。

这个模块只干三件事：
  1. 把**图片**放进剪贴板（`copy_image`，走 CF_DIB）；
  2. 把**文本 / 文件**放进剪贴板（`copy_text` / `copy_files`）；
  3. 打开文件或在资源管理器里定位它。

关于「一键找高清」：早先版本还带一份国内识图站清单（百度 / 搜狗 / 360 /
通义 / 豆包 … 12 个），点了会「复制图片 + 自动开网页 + 提示按 Ctrl+V」。
按使用者的要求**这一整套已经删掉，只留「复制到剪贴板」** —— 搜不搜、去哪儿搜
由使用者自己决定，程序不替他开网页。

（顺带留个记录，免得以后又想去啃接口：自己上传后调接口这条路实测走不通 ——
国外图床几乎全灭：0x0.st 503、sm.ms 接口已废、postimages 403 明说禁止自动化；
国内识图站的上传接口也全灭：百度 graph.baidu.com/upload 一律回
{"status":1,"msg":"Reject"}（换 4 种参数组合带 Cookie 同样 Reject）、
搜狗 ris_upload 500、360 upload 404。硬啃这些私有接口既不稳定也不合规。）
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import time
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

CF_TEXT = 1
CF_DIB = 8
CF_UNICODETEXT = 13
CF_HDROP = 15
GMEM_MOVEABLE = 0x0002

user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.restype = wintypes.BOOL
user32.CloseClipboard.restype = wintypes.BOOL
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.restype = wintypes.BOOL


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class DROPFILES(ctypes.Structure):
    _fields_ = [("pFiles", wintypes.DWORD), ("x", ctypes.c_long),
                ("y", ctypes.c_long), ("fNC", wintypes.BOOL),
                ("fWide", wintypes.BOOL)]


# ---------------------------------------------------------------------------
# 剪贴板
# ---------------------------------------------------------------------------

def bgra_to_dib(w: int, h: int, bgra: bytes, bits: int = 24) -> bytes:
    """BGRA（自上而下）-> CF_DIB 需要的「BITMAPINFOHEADER + 自下而上 BGR」。

    三个必须记住的点：
      1. CF_DIB 的像素行是**自下而上**的，且每行按 4 字节对齐；
      2. 内存里必须是 **B,G,R** 顺序（winimg 给的是 BGRA，正好前三个字节
         就是 B,G,R，**不要**倒过来写 —— 倒过来贴出去就是红蓝互换）；
      3. 用 24 位（无 alpha）兼容性最好。32 位若 alpha 全 0，
         不少程序会把它当全透明 → 贴出来是一片空白或全黑。
    """
    if bits == 32:
        stride = w * 4
    else:
        stride = ((w * 3 + 3) // 4) * 4
    rows = []
    for y in range(h - 1, -1, -1):
        base = y * w * 4
        row = bytearray(stride)
        k = 0
        for x in range(w):
            i = base + x * 4
            row[k] = bgra[i]            # B
            row[k + 1] = bgra[i + 1]    # G
            row[k + 2] = bgra[i + 2]    # R
            k += 3
            if bits == 32:
                row[k] = 255            # 32 位时把 alpha 填满，别留 0
                k += 1
        rows.append(bytes(row))
    pix = b"".join(rows)

    hdr = BITMAPINFOHEADER()
    hdr.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    hdr.biWidth = w
    hdr.biHeight = h                    # 正数 = 自下而上
    hdr.biPlanes = 1
    hdr.biBitCount = bits
    hdr.biCompression = 0
    hdr.biSizeImage = len(pix)
    return ctypes.string_at(ctypes.byref(hdr), hdr.biSize) + pix


def _with_clipboard(fn, timeout: float = 2.5, tries_extra: int = 5):
    """独占打开剪贴板执行 fn。剪贴板经常被别人占着，要重试。"""
    end = time.time() + timeout
    n = 0
    while True:
        if user32.OpenClipboard(None):
            try:
                user32.EmptyClipboard()
                return fn()
            finally:
                user32.CloseClipboard()
        n += 1
        if time.time() >= end and n > tries_extra:
            return False
        time.sleep(0.08)


def _put(data: bytes, fmt: int) -> bool:
    h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not h:
        return False
    p = kernel32.GlobalLock(h)
    if not p:
        return False
    try:
        ctypes.memmove(p, data, len(data))
    finally:
        kernel32.GlobalUnlock(h)
    # 交接成功后所有权归系统，绝对不要再 GlobalFree
    return bool(user32.SetClipboardData(fmt, h))


def copy_image(path: str, size: int = 1024) -> tuple:
    """把一张图放进剪贴板。返回 (成功?, 尺寸说明)。

    走 winimg 解码成 BGRA，再转 CF_DIB。上传网页时用 1024 长边足够，
    原图几千万像素塞剪贴板又慢又没必要。
    """
    import winimg
    got = winimg.load_pixels(path, size)
    if not got:
        return False, "读不出这张图"
    w, h, bgra = got
    dib = bgra_to_dib(w, h, bgra, 24)
    ok = _with_clipboard(lambda: _put(dib, CF_DIB))
    return bool(ok), "%dx%d" % (w, h)


def copy_text(text: str) -> bool:
    data = (text + "\0").encode("utf-16-le")
    return bool(_with_clipboard(lambda: _put(data, CF_UNICODETEXT)))


def copy_files(paths) -> bool:
    """把文件本身放进剪贴板（CF_HDROP），可以粘到资源管理器 / 聊天窗口。"""
    paths = [os.path.abspath(p) for p in paths]
    if not paths:
        return False
    df = DROPFILES()
    df.pFiles = ctypes.sizeof(DROPFILES)
    df.fWide = True
    blob = ctypes.string_at(ctypes.byref(df), ctypes.sizeof(DROPFILES))
    blob += "\0".join(paths).encode("utf-16-le") + "\0\0".encode("utf-16-le")
    return bool(_with_clipboard(lambda: _put(blob, CF_HDROP)))


# ---------------------------------------------------------------------------
# 打开外部的程序 / 资源管理器
# ---------------------------------------------------------------------------

def open_path(path: str) -> bool:
    try:
        os.startfile(path)                   # noqa: S606
        return True
    except (OSError, AttributeError):
        return False


def reveal_in_explorer(path: str) -> bool:
    """在资源管理器里定位到文件（选中它）。"""
    try:
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# 剪贴板可用性 + 降级
# ---------------------------------------------------------------------------

def clipboard_available() -> bool:
    """探一下剪贴板能不能用。

    受限环境下（例如以非交互方式运行的进程）系统会直接**拒绝访问**剪贴板：
    OpenClipboard 返回 0、GetLastError = 5（ERROR_ACCESS_DENIED），
    连系统自带的 clip.exe 和 PowerShell 的 Set-Clipboard 也一样报「拒绝访问」。
    这时就只能走「导出一份临时副本」的兜底。
    """
    if user32.OpenClipboard(None):
        user32.CloseClipboard()
        return True
    return False


def export_copy(path: str, size: int = 1280) -> tuple:
    """剪贴板用不了时的兜底：把图另存一份临时副本，让用户自己拖进网页。

    返回 (副本路径 或 None, 说明)。
    """
    import tempfile
    import thumbs
    import winimg
    got = winimg.load_pixels(path, size)
    if not got:
        return None, "读不出这张图"
    w, h, bgra = got
    base = os.path.splitext(os.path.basename(path))[0][:40] or "image"
    out_dir = os.path.join(tempfile.gettempdir(), "ImageScout")
    try:
        os.makedirs(out_dir, exist_ok=True)
        out = os.path.join(out_dir, "%s_%dx%d.png" % (base, w, h))
        with open(out, "wb") as f:
            f.write(thumbs.bgra_to_png(w, h, bgra))
    except OSError as e:
        return None, "写临时副本失败：%s" % e
    return out, "%dx%d 的副本已存到 %s" % (w, h, out)
