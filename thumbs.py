"""缩略图 / PNG 编码：拿系统解码出来的像素 -> PNG 字节 -> 直接喂给 Tk。

手写 PNG 编码（zlib + CRC32），零依赖。
IHDR 用 color type 2（真彩 RGB，不带 alpha）是**故意的**：
GDI 位图的 alpha 字节通常是 0，如果声明带 alpha，Tk 里显示出来就是
全透明 / 一片白，看着像「缩略图没加载出来」。
"""

from __future__ import annotations

import struct
import zlib

import winimg

__all__ = ["bgra_to_png", "thumb_png", "crop_bgra", "bgra_to_gray"]


def bgra_to_png(w: int, h: int, bgra: bytes, level: int = 1) -> bytes:
    """BGRA -> PNG 字节。level=1 优先速度（缩略图不需要高压缩比）。

    ⚠️ 这里**不能写成逐像素的 Python 循环**：一张 900x600 的预览图就是 54 万次
    迭代，光转换要 0.3~0.5 秒，界面切一张卡一下。

    改用「扩展切片赋值」把整行的通道分离和交错都交给 C 层：
        mv[base:end:4]      -> 取一整行的某一通道（步长 4，C 速度）
        row[0::3] = R       -> 按步长 3 写回去（C 速度）
    同样的图快两个数量级。

    ⚠️ 通道别搞反：源内存序是 **B,G,R,A**，PNG 要的是 **R,G,B**。
    所以 R 要从偏移 **2** 取、B 从偏移 **0** 取（写反了就是红蓝互换，
    而且在只看尺寸/格式的断言里完全测不出来）。
    """
    mv = memoryview(bgra)
    step = w * 4
    span = w * 3
    rows = []
    for y in range(h):
        base = y * step
        end = base + step
        row = bytearray(span)
        row[0::3] = mv[base + 2:end:4]          # R（偏移 2）
        row[1::3] = mv[base + 1:end:4]          # G
        row[2::3] = mv[base:end:4]              # B（偏移 0）
        rows.append(b"\x00" + bytes(row))
    raw = b"".join(rows)

    def chunk(t, d):
        return (struct.pack(">I", len(d)) + t + d
                + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, level))
            + chunk(b"IEND", b""))


def thumb_png(path: str, size: int = 160, flags: int | None = None):
    """返回 (宽, 高, PNG 字节)。失败返回 None。"""
    got = winimg.load_pixels(
        path, size,
        winimg.SIIGBF_RESIZETOFIT if flags is None else flags)
    if not got:
        return None
    w, h, bgra = got
    try:
        return w, h, bgra_to_png(w, h, bgra)
    except Exception:
        return None


def crop_bgra(w: int, h: int, bgra: bytes, x: int, y: int, cw: int, ch: int):
    """从 BGRA 里裁一块。坐标会被夹到图像范围内。"""
    x = max(0, min(x, w - 1))
    y = max(0, min(y, h - 1))
    cw = max(1, min(cw, w - x))
    ch = max(1, min(ch, h - y))
    if x == 0 and cw == w:
        # 整行连续，直接切片，快很多
        return cw, ch, bgra[y * w * 4:(y + ch) * w * 4]
    out = bytearray()
    for yy in range(y, y + ch):
        base = (yy * w + x) * 4
        out += bgra[base:base + cw * 4]
    return cw, ch, bytes(out)


def bgra_to_gray(w: int, h: int, bgra: bytes, gw: int, gh: int):
    """BGRA 面积平均 -> gw x gh 灰度二维列表。"""
    return winimg.gray_fit(w, h, bgra, gw, gh)
