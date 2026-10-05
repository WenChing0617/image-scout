# -*- coding: utf-8 -*-
"""造测试图片：纯标准库写 PNG，零依赖。

用途：
  - 生成「有结构」的图案（像照片一样有渐变和形状），而不是纯噪声
  - 由同一张底图派生出**各种裁剪变体**（不同比例 / 裁边 / 偏心 / 缩放）
  - 生成互不相干的干扰图，用来量误判
"""

from __future__ import annotations

import math
import os
import struct
import zlib

__all__ = ["png_bytes", "render", "write_png", "crop_rgb", "scale_rgb",
           "jitter_rgb", "KINDS", "build_set", "build_scene_set", "PATTERNS"]


# ---------------------------------------------------------------------------
# 图案
# ---------------------------------------------------------------------------

def render(kind, w, h, seed=0):
    """生成 w x h 的 RGB 字节（bytes，每像素 3 字节）。"""
    fn = PATTERNS[kind]
    out = bytearray(w * h * 3)
    i = 0
    for y in range(h):
        for x in range(w):
            r, g, b = fn(x, y, w, h, seed)
            out[i] = r & 255
            out[i + 1] = g & 255
            out[i + 2] = b & 255
            i += 3
    return bytes(out)


def _wave(x, y, w, h, s):
    v = math.sin((x + s * 13) / max(1.0, w * 0.14)) * math.cos((y + s * 7) / max(1.0, h * 0.11))
    r = int(128 + 110 * v)
    g = int(140 + 90 * math.sin((x + y) / max(1.0, w * 0.09)))
    b = int(150 + 80 * math.cos((x - y) / max(1.0, w * 0.17)))
    return r, g, b


def _stripes(x, y, w, h, s):
    k = (x + y) // max(4, w // (6 + s % 5))
    r = 60 + (k % 2) * 150
    g = 90 + (k % 3) * 60
    b = 200 - (k % 2) * 120
    return r, g, b


def _dots(x, y, w, h, s):
    cx, cy = w * (0.25 + 0.1 * s), h * 0.3
    d = math.hypot(x - cx, y - cy) / max(1.0, min(w, h) * 0.3)
    v = 200 - 160 * min(1.0, d)
    return int(v), int(120 + 100 * (y / max(1.0, h))), int(80 + 120 * (x / max(1.0, w)))


def _checker(x, y, w, h, s):
    c = max(6, w // (8 + s % 6))
    k = (x // c) + (y // c)
    base = 200 if k % 2 else 55
    return base, (base + 30) % 256, (base + 70) % 256


def _gradient(x, y, w, h, s):
    r = int(255 * x / max(1, w - 1))
    g = int(255 * y / max(1, h - 1))
    b = int(255 * (1 - (x + y) / max(1.0, w + h - 2)))
    return r, g, b


def _rings(x, y, w, h, s):
    cx, cy = w / 2.0, h / 2.0
    d = math.hypot(x - cx, y - cy) / max(1.0, min(w, h) / 2.0)
    v = int(128 + 120 * math.sin(d * (4 + s % 4) * math.pi))
    return v, (v * 3) % 256, (255 - v),


def _spiral(x, y, w, h, s):
    cx, cy = w / 2.0, h / 2.0
    dx, dy = x - cx, y - cy
    a = math.atan2(dy, dx)
    d = math.hypot(dx, dy) / max(1.0, min(w, h) / 2.0)
    v = int(128 + 120 * math.sin(a * 3 + d * (6 + s % 5) * math.pi))
    return v, int(v * 0.7), 255 - v


def _blobs(x, y, w, h, s):
    v = 0.0
    for k in range(3):
        cxk = w * (0.2 + 0.3 * ((k + s) % 3))
        cyk = h * (0.25 + 0.25 * ((k * 2 + s) % 3))
        rk = min(w, h) * (0.18 + 0.06 * ((k + s) % 3))
        v += math.exp(-((x - cxk) ** 2 + (y - cyk) ** 2) / (2 * rk * rk)) * (120 + 40 * k)
    return int(v) & 255, int(v * 0.6) & 255, int(255 - v) & 255


PATTERNS = {
    "wave": _wave,
    "stripes": _stripes,
    "dots": _dots,
    "checker": _checker,
    "gradient": _gradient,
    "rings": _rings,
    "spiral": _spiral,
    "blobs": _blobs,
}

KINDS = list(PATTERNS)


# ---------------------------------------------------------------------------
# PNG 写盘
# ---------------------------------------------------------------------------

def png_bytes(w, h, rgb, level=6):
    rows = []
    step = w * 3
    for y in range(h):
        rows.append(b"\x00" + rgb[y * step:(y + 1) * step])
    raw = b"".join(rows)

    def chunk(t, d):
        return (struct.pack(">I", len(d)) + t + d
                + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, level))
            + chunk(b"IEND", b""))


def write_png(path, w, h, rgb):
    for attempt in range(3):
        try:
            with open(path, "wb") as f:
                f.write(png_bytes(w, h, rgb))
            return path
        except OSError:
            if attempt == 2:
                raise
    return path


# ---------------------------------------------------------------------------
# 几何变换（裁剪 / 缩放）
# ---------------------------------------------------------------------------

def crop_rgb(w, h, rgb, x0, y0, cw, ch):
    """裁一块出来。坐标夹到范围内。"""
    x0 = max(0, min(int(x0), w - 1))
    y0 = max(0, min(int(y0), h - 1))
    cw = max(1, min(int(cw), w - x0))
    ch = max(1, min(int(ch), h - y0))
    out = bytearray()
    for y in range(y0, y0 + ch):
        base = (y * w + x0) * 3
        out += rgb[base:base + cw * 3]
    return cw, ch, bytes(out)


def scale_rgb(w, h, rgb, dw, dh):
    """面积平均缩放（模拟「另存为小图」）。"""
    out = bytearray(dw * dh * 3)
    i = 0
    for ty in range(dh):
        y0 = ty * h // dh
        y1 = max(y0 + 1, (ty + 1) * h // dh)
        for tx in range(dw):
            x0 = tx * w // dw
            x1 = max(x0 + 1, (tx + 1) * w // dw)
            r = g = b = 0
            n = 0
            for y in range(y0, y1):
                base = y * w * 3
                for x in range(x0, x1):
                    j = base + x * 3
                    r += rgb[j]
                    g += rgb[j + 1]
                    b += rgb[j + 2]
                    n += 1
            out[i] = r // n
            out[i + 1] = g // n
            out[i + 2] = b // n
            i += 3
    return bytes(out)


# ---------------------------------------------------------------------------
# 造整套测试数据
# ---------------------------------------------------------------------------

def jitter_rgb(w, h, rgb, contrast=1.0, bright=0, noise=0, seed=0):
    """模拟「同一个场景，第二次拍」：整体对比度 / 亮度略变 + 一点噪点。

    两张照片拍同一个场景，不可能是像素级相同的 —— 曝光、白平衡、轻微位移、
    传感器噪声都会让像素值差一点。这里用最朴素的方式造出这种差异。
    """
    out = bytearray(len(rgb))
    for i in range(0, len(rgb), 3):
        for c in range(3):
            v = rgb[i + c]
            v = (v - 128) * contrast + 128 + bright
            if noise:
                n = ((i * 7919 + c * 104729 + seed * 15485863) >> 3) % (2 * noise + 1)
                v += n - noise
            out[i + c] = 0 if v < 0 else (255 if v > 255 else int(v))
    return bytes(out)


# 同一块大画布上的不同取景（模拟同场景不同构图）。范围是相对画布的比例。
SCENE_WINDOWS = (
    (0.04, 0.06, 0.66, 0.58),
    (0.30, 0.16, 0.62, 0.74),
    (0.08, 0.32, 0.84, 0.62),
)


def build_scene_set(root, canvas=(760, 570), kinds=None, out_size=(360, 270)):
    """造「同场景不同构图」的测试集。

    对每种图案渲染一张大画布，然后在画布上取 3 个不同的取景窗口
    （位置、大小都不同，部分重叠），各自再套上亮度/对比度抖动和噪点。
    这些窗口之间**应该被判为同场景**，但它们往往谁也不包含谁 ——
    这正是「包含」模型覆盖不到、需要单独一条通道的那种情况。

    返回 {场景组名: [文件路径...]}
    """
    kinds = kinds or KINDS
    os.makedirs(root, exist_ok=True)
    groups = {}

    for si, kind in enumerate(kinds):
        cw, ch = canvas
        big = render(kind, cw, ch, seed=si)
        d = os.path.join(root, "%02d_%s" % (si, kind))
        os.makedirs(d, exist_ok=True)
        files = []
        for wi, (fx, fy, fw, fh) in enumerate(SCENE_WINDOWS):
            x0 = int(cw * fx)
            y0 = int(ch * fy)
            w = max(8, min(int(cw * fw), cw - x0))
            h = max(8, min(int(ch * fh), ch - y0))
            _, _, sub = crop_rgb(cw, ch, big, x0, y0, w, h)
            # 每张取景的「拍摄条件」都不同：一张偏亮、一张对比强、一张偏暗带噪
            cont = (1.0, 1.08, 0.92)[wi % 3]
            bri = (0, 10, -9)[wi % 3]
            noi = (0, 2, 4)[wi % 3]
            sub = jitter_rgb(w, h, sub, cont, bri, noi, seed=si * 7 + wi)
            ow, oh = out_size
            if (w, h) != (ow, oh):
                sub = scale_rgb(w, h, sub, ow, oh)
                w, h = ow, oh
            p = os.path.join(d, "shot_%d.png" % wi)
            write_png(p, w, h, sub)
            files.append(p)
        groups["%02d_%s" % (si, kind)] = files
    return groups


def build_set(root, base_w=480, base_h=360, kinds=None, variants=None,
              unrelated=2):
    """造测试集，返回 {底图名: [该底图的所有变体路径]} 和干扰图列表。

    每个底图派生一批变体，变体之间都「应该被判为相似」。
    """
    kinds = kinds or KINDS
    variants = variants or ["full", "sq", "wide", "tall", "trim80", "off70", "half"]
    os.makedirs(root, exist_ok=True)

    groups = {}
    for si, kind in enumerate(kinds):
        rgb = render(kind, base_w, base_h, seed=si)
        d = os.path.join(root, "%02d_%s" % (si, kind))
        os.makedirs(d, exist_ok=True)
        files = []

        def put(name, w, h, data):
            p = os.path.join(d, name + ".png")
            write_png(p, w, h, data)
            files.append(p)

        if "full" in variants:
            put("full", base_w, base_h, rgb)

        if "sq" in variants:            # 方形裁剪（1:1）
            s = min(base_w, base_h)
            cw, ch, cd = crop_rgb(base_w, base_h, rgb,
                                  (base_w - s) // 2, (base_h - s) // 2, s, s)
            put("crop_square", cw, ch, cd)

        if "wide" in variants:          # 16:9 裁剪
            cw = base_w
            ch2 = int(base_w * 9 / 16)
            cw, ch, cd = crop_rgb(base_w, base_h, rgb, 0, (base_h - ch2) // 2, cw, ch2)
            put("crop_16_9", cw, ch, cd)

        if "tall" in variants:          # 3:4 竖裁剪
            ch3 = base_h
            cw3 = int(base_h * 3 / 4)
            cw, ch, cd = crop_rgb(base_w, base_h, rgb, (base_w - cw3) // 2, 0, cw3, ch3)
            put("crop_3_4", cw, ch, cd)

        if "trim80" in variants:        # 同比例裁掉一圈
            cw = int(base_w * 0.8)
            ch = int(base_h * 0.8)
            cw, ch, cd = crop_rgb(base_w, base_h, rgb,
                                  (base_w - cw) // 2, (base_h - ch) // 2, cw, ch)
            put("trim_80", cw, ch, cd)

        if "off70" in variants:         # 偏心裁剪
            cw = int(base_w * 0.7)
            ch = int(base_h * 0.7)
            cw, ch, cd = crop_rgb(base_w, base_h, rgb,
                                  int(base_w * 0.2), int(base_h * 0.1), cw, ch)
            put("off_70", cw, ch, cd)

        if "half" in variants:          # 整图缩小一半
            dw, dh = base_w // 2, base_h // 2
            put("half", dw, dh, scale_rgb(base_w, base_h, rgb, dw, dh))

        groups["%02d_%s" % (si, kind)] = files

    # 干扰图：跟上面都无关
    d = os.path.join(root, "zz_noise")
    os.makedirs(d, exist_ok=True)
    noise = []
    for k in range(unrelated):
        w, h = 400 + k * 60, 300 + k * 40
        data = bytes((((i * 7919 + k * 104729) % 251) for i in range(w * h * 3)))
        p = os.path.join(d, "noise_%d.png" % k)
        write_png(p, w, h, data)
        noise.append(p)

    return groups, noise
