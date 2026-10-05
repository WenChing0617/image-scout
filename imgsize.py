"""纯标准库读图片文件头拿原始像素尺寸（不依赖任何图像库）。

为什么要自己读：shell32 返回的是缩略图（已缩放），拿不到「原图多少像素」。
而用户最关心的恰恰是「哪张是高清原图」，所以必须读真实尺寸。
"""

from __future__ import annotations

import os
import struct

__all__ = ["image_size", "size_of_file", "is_image", "IMG_EXT",
           "size_text", "megapixels", "format_of"]

IMG_EXT = {
    ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".gif", ".bmp", ".webp",
    ".tif", ".tiff", ".heic", ".heif", ".avif", ".ico", ".jxl",
}

# 一眼能看出「这不是图片」的扩展名，扫盘时直接跳过
NOT_IMAGE = {
    ".txt", ".md", ".json", ".xml", ".html", ".htm", ".css", ".js", ".ts",
    ".py", ".java", ".kt", ".c", ".cpp", ".h", ".cs", ".go", ".rs", ".rb",
    ".php", ".sql", ".db", ".sqlite", ".log", ".ini", ".cfg", ".yml", ".yaml",
    ".toml", ".bat", ".cmd", ".ps1", ".sh", ".exe", ".dll", ".sys", ".msi",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso", ".img",
    ".mp3", ".wav", ".flac", ".ape", ".m4a", ".ogg", ".aac",
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".ts",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".pdf", ".csv",
    ".ttf", ".otf", ".woff", ".woff2", ".lnk", ".url", ".tmp",
}

_FORMAT_BY_EXT = {
    ".jpg": "JPEG", ".jpeg": "JPEG", ".jpe": "JPEG", ".jfif": "JPEG",
    ".png": "PNG", ".gif": "GIF", ".bmp": "BMP", ".webp": "WebP",
    ".tif": "TIFF", ".tiff": "TIFF", ".heic": "HEIC", ".heif": "HEIF",
    ".avif": "AVIF",
}


def format_of(path: str) -> str:
    """按文件头判真实格式，读不出来才退回扩展名。"""
    try:
        data = _head(path, 32)
    except Exception:
        data = b""

    if data.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "GIF"
    if data.startswith(b"BM"):
        return "BMP"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WebP"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "TIFF"
    if data[4:12] in (b"ftypheic", b"ftypheix", b"ftyphevc", b"ftypmif1"):
        return "HEIC"
    if data[4:12] == b"ftypavif":
        return "AVIF"
    return _FORMAT_BY_EXT.get(os.path.splitext(path)[1].lower(), "?")


def is_image(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    if ext in NOT_IMAGE:
        return False
    return ext in IMG_EXT


def _head(path: str, n: int) -> bytes:
    with open(path, "rb") as f:
        return f.read(n)


# ---------------------------------------------------------------------------
# 各格式的尺寸解析
# ---------------------------------------------------------------------------

def _png(d: bytes):
    if len(d) >= 24 and d[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", d[16:24])
        return int(w), int(h)
    return None


def _gif(d: bytes):
    if len(d) >= 10 and d[:6] in (b"GIF87a", b"GIF89a"):
        w, h = struct.unpack("<HH", d[6:10])
        return int(w), int(h)
    return None


def _bmp(d: bytes):
    if len(d) >= 26 and d[:2] == b"BM":
        w, h = struct.unpack("<ii", d[18:26])
        return abs(int(w)), abs(int(h))
    return None


def _webp(d: bytes):
    if len(d) < 30 or d[:4] != b"RIFF" or d[8:12] != b"WEBP":
        return None
    fourcc = d[12:16]
    if fourcc == b"VP8 ":
        # 关键帧: 3 字节起始码 + 2 字节宽 + 2 字节高（各 14 位有效）
        i = d.find(b"\x9d\x01\x2a", 20)
        if i > 0 and len(d) >= i + 7:
            w = struct.unpack("<H", d[i + 3:i + 5])[0] & 0x3FFF
            h = struct.unpack("<H", d[i + 5:i + 7])[0] & 0x3FFF
            return w, h
    elif fourcc == b"VP8L":
        if len(d) >= 25 and d[20] == 0x2F:
            bits = struct.unpack("<I", d[21:25])[0]
            w = (bits & 0x3FFF) + 1
            h = ((bits >> 14) & 0x3FFF) + 1
            return w, h
    elif fourcc == b"VP8X":
        if len(d) >= 30:
            w = (d[24] | (d[25] << 8) | (d[26] << 16)) + 1
            h = (d[27] | (d[28] << 8) | (d[29] << 16)) + 1
            return w, h
    return None


def _jpeg(d: bytes):
    """扫 JPEG 的 SOFn 段。EXIF 可能很大，所以要允许在更大的数据里找。"""
    n = len(d)
    i = 2
    while i + 9 < n:
        if d[i] != 0xFF:
            i += 1
            continue
        marker = d[i + 1]
        if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if i + 4 > n:
            return None
        seg_len = struct.unpack(">H", d[i + 2:i + 4])[0]
        # SOF0..SOF15，但排除 DHT(C4)/JPG(C8)/DAC(CC)
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if i + 9 > n:
                return None
            h, w = struct.unpack(">HH", d[i + 5:i + 9])
            return int(w), int(h)
        i += 2 + seg_len
    return None


def _tiff(d: bytes):
    if len(d) < 8:
        return None
    if d[:4] == b"II*\x00":
        end = "<"
    elif d[:4] == b"MM\x00*":
        end = ">"
    else:
        return None
    off = struct.unpack(end + "I", d[4:8])[0]
    if off + 2 > len(d):
        return None
    cnt = struct.unpack(end + "H", d[off:off + 2])[0]
    w = h = None
    for i in range(cnt):
        e = off + 2 + i * 12
        if e + 12 > len(d):
            break
        tag, typ = struct.unpack(end + "HH", d[e:e + 4])
        if typ in (3, 4):        # SHORT / LONG
            val = struct.unpack(end + ("H" if typ == 3 else "I"),
                                d[e + 8:e + 8 + (2 if typ == 3 else 4)])[0]
            if tag == 256:
                w = val
            elif tag == 257:
                h = val
        if w and h:
            return int(w), int(h)
    if w and h:
        return int(w), int(h)
    return None


_PARSERS = (_png, _jpeg, _gif, _bmp, _webp, _tiff)


def image_size(data: bytes):
    """按文件头解析出 (宽, 高)。认不出来返回 None。"""
    for fn in _PARSERS:
        try:
            got = fn(data)
        except Exception:
            got = None
        if got and got[0] > 0 and got[1] > 0:
            return got
    return None


def size_of_file(path: str):
    """读文件头拿尺寸。先读 256KB，失败再补读 4 倍（应对 EXIF 超大的 JPEG）。"""
    try:
        if os.path.getsize(path) == 0:
            return None
    except OSError:
        return None
    for n in (256 * 1024, 1024 * 1024, 4 * 1024 * 1024):
        try:
            with open(path, "rb") as f:
                data = f.read(n)
        except OSError:
            return None
        got = image_size(data)
        if got:
            return got
        if len(data) < n:          # 文件已经读完了，再读也白搭
            break
    return None


# ---------------------------------------------------------------------------
# 显示辅助
# ---------------------------------------------------------------------------

def size_text(wh) -> str:
    if not wh:
        return "? × ?"
    return "%d × %d" % (wh[0], wh[1])


def megapixels(wh) -> float:
    if not wh:
        return 0.0
    return wh[0] * wh[1] / 1_000_000.0
