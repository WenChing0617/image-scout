# -*- coding: utf-8 -*-
"""生成 `app.ico` —— 纯标准库，不需要 Pillow。

图形：圆角方块（天蓝渐变，跟 `uikit.Palette` 的主色一致）+ 白色放大镜。
抗锯齿用**带符号距离场（SDF）+ 每像素 3×3 超采样**，边缘不会有台阶。

ICO 结构（每个尺寸一条）：
    ICONDIR(6B) + ICONDIRENTRY(16B × n)
    + [ BITMAPINFOHEADER(40B, biHeight = 2h) + 自下而上的 BGRA + AND 掩码（全 0） ]

尺寸给 16/24/32/48/64/128/256，约 370 KB，PyInstaller 内嵌无压力。
顺带导出 `app-icon-preview.png`（手写 PNG，zlib + crc32）方便直接看图。

用法：python make_icon.py
"""

from __future__ import annotations

import os
import struct
import zlib

# 跟 uikit.Palette 对齐：改主题时只改这两处（再重跑一次本脚本）
C_TOP = (0x6f, 0xc8, 0xf2)      # 渐变起（亮天蓝，比 PRIMARY 再亮一档）
C_BOTTOM = (0x0b, 0x5f, 0x96)   # 渐变止（PRIMARY_D）
C_GLYPH = (255, 255, 255)       # 放大镜

SIZES = (16, 24, 32, 48, 64, 128, 256)
SS = 3                          # 超采样倍数

HERE = os.path.dirname(os.path.abspath(__file__))
ICO_PATH = os.path.join(HERE, "app.ico")
PNG_PATH = os.path.join(HERE, "app-icon-preview.png")


# --------------------------------------------------------------------------
# SDF：放大镜 = 「圆环」∪ 「手柄」
# --------------------------------------------------------------------------

def _sd_circle(px, py, cx, cy, r):
    return ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5 - r


def _sd_segment(px, py, ax, ay, bx, by):
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    t = (wx * vx + wy * vy) / (vx * vx + vy * vy)
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return ((wx - t * vx) ** 2 + (wy - t * vy) ** 2) ** 0.5


def glyph_sdf(u, v):
    """归一化坐标 (0~1) 下的放大镜距离场：内部为负、外部为正。"""
    cx, cy, r = 0.44, 0.42, 0.225
    ring = abs(_sd_circle(u, v, cx, cy, r)) - 0.055          # 圆环（有宽度）
    k = 0.7071
    ax, ay = cx + r * k, cy + r * k                          # 手柄起点在环上
    handle = _sd_segment(u, v, ax, ay, 0.80, 0.80) - 0.055
    return min(ring, handle)


def round_rect_alpha(u, v, radius=0.22):
    """圆角方块的覆盖度（1 = 完全在内，0 = 完全在外，中间是边缘羽化）。"""
    # 把 uv 折到第一象限，离圆角圆心比距离
    du = abs(u - 0.5)
    dv = abs(v - 0.5)
    # 到「去掉圆角后的矩形」的距离场
    dx = du - (0.5 - radius)
    dy = dv - (0.5 - radius)
    outside = ((max(dx, 0.0) ** 2 + max(dy, 0.0) ** 2) ** 0.5
               + min(max(dx, dy), 0.0) - radius)
    return 1.0 if outside < 0 else 0.0      # 边缘由超采样解决


def mix(a, b, t):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


def _smooth(edge):
    """把 SDF（负=内）转成 0~1 覆盖度，过渡带宽 = 1 个像素。"""
    t = 0.5 - edge
    return 0.0 if t <= 0.0 else (1.0 if t >= 1.0 else t)


def render(size):
    """返回 size×size 的 RGBA 列表（自上而下，每行是可变 bytearray 的元组列表）。"""
    rows = []
    px_w = 1.0 / size                       # 一个输出像素在归一化坐标里的宽度
    sub = 1.0 / (size * SS)
    for y in range(size):
        row = []
        for x in range(size):
            ar = gr = bl = 0.0
            aa = 0.0
            n = 0
            for sy in range(SS):
                for sx in range(SS):
                    u = (x + (sx + 0.5) / SS) / size
                    v = (y + (sy + 0.5) / SS) / size
                    n += 1
                    grad = mix(C_TOP, C_BOTTOM, 0.15 + 0.85 * (u + v) / 2.0)
                    # 圆角方块：超采样硬判定即可（边缘有 9 级过渡）
                    box_a = round_rect_alpha(u, v)
                    if box_a <= 0.0:
                        continue
                    cov = _smooth(glyph_sdf(u, v) / sub)
                    ar += grad[0] * (1 - cov) + C_GLYPH[0] * cov
                    gr += grad[1] * (1 - cov) + C_GLYPH[1] * cov
                    bl += grad[2] * (1 - cov) + C_GLYPH[2] * cov
                    aa += box_a
            if n == 0:
                row.append((0, 0, 0, 0))
                continue
            a = aa / n
            if a <= 0.0:
                row.append((0, 0, 0, 0))
                continue
            row.append((int(ar / n), int(gr / n), int(bl / n), int(a * 255 + 0.5)))
        rows.append(row)
    return rows


# --------------------------------------------------------------------------
# 手写 PNG（只为预览用）
# --------------------------------------------------------------------------

def write_png(path, rows):
    h = len(rows)
    w = len(rows[0])
    raw = bytearray()
    for row in rows:
        raw.append(0)                        # filter type 0
        for r, g, b, a in row:
            raw += bytes((r, g, b, a))

    def chunk(tag, payload):
        out = struct.pack(">I", len(payload)) + tag + payload
        return out + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)


# --------------------------------------------------------------------------
# ICO
# --------------------------------------------------------------------------

def build_ico(path):
    blobs = []
    for size in SIZES:
        rows = render(size)
        # 自下而上的 BGRA
        pix = bytearray()
        for row in reversed(rows):
            for r, g, b, a in row:
                pix += bytes((b, g, r, a))
        # AND 掩码：每行按 4 字节对齐，全 0（用 alpha 通道就够了）
        mask_stride = ((size + 31) // 32) * 4
        mask = bytes(mask_stride * size)

        dib = struct.pack("<IiiHHIIiiII",
                          40,             # biSize
                          size,           # biWidth
                          size * 2,       # biHeight（含掩码 = 2 倍）
                          1,              # biPlanes
                          32,             # biBitCount
                          0,              # biCompression = BI_RGB
                          len(pix) + len(mask), 0, 0, 0, 0)
        blobs.append(dib + bytes(pix) + mask)

    out = bytearray(struct.pack("<HHH", 0, 1, len(SIZES)))
    offset = 6 + 16 * len(SIZES)
    for size, blob in zip(SIZES, blobs):
        out += struct.pack("<BBBBHHII",
                           0 if size < 256 else 0,   # bWidth（256 记 0）
                           0 if size < 256 else 0,   # bHeight
                           0, 0,                     # 调色板 / 保留
                           1, 32,                    # 颜色平面 / 位深
                           len(blob), offset)
        offset += len(blob)
    for blob in blobs:
        out += blob

    with open(path, "wb") as f:
        f.write(bytes(out))
    return len(out)


def main():
    n = build_ico(ICO_PATH)
    write_png(PNG_PATH, render(256))
    print("ico  -> %s (%d bytes, %d sizes)" % (ICO_PATH, n, len(SIZES)))
    print("png  -> %s (预览 256x256)" % PNG_PATH)


if __name__ == "__main__":
    main()
