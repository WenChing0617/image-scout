# -*- coding: utf-8 -*-
"""NIQE 门槛的**离线标定脚本**（可选，用不到就别装依赖）。

造一批「清晰 → 逐级劣化」的图，走**真实管线**（GDI+ 精确解码 → 长边 640 → NIQE）算分，
用来定 `niqe.py` 里的 `NIQE_GOOD / NIQE_FAIR` 和 README「NIQE」那一节的表格。

⚠️ **本文件需要 numpy + Pillow**（高斯模糊 / JPEG 编解码 / 分形噪声）。
   主程序 `image_scout.py`、`niqe.py` 以及五个 `test_*.py` **都不 import 它**，
   项目「零第三方依赖」的性质不受影响。跑法：

       C:\\Users\\WQ\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\python.exe calib_niqe.py

   （那个 venv 里有 numpy/Pillow；不想装就用不依赖第三方的那条对照 ——
    `test_niqe.py` 里的 `test_thresholds()` 用盒式模糊 + 均匀噪声，
    只靠标准库就能验「分数随劣化单调上升」和 8.0 这条门槛。）

劣化方式：高斯模糊（σ = 0.6 / 1.2 / 2.5 / 5）、JPEG（q = 30 / 15 / 5）、
均匀噪声、降采样再放大（模拟被压扁过的小图）。
"""
import math
import os
import re
import sys

import numpy as np
from PIL import Image, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import niqe as N          # noqa: E402
import winimg             # noqa: E402

TMP = os.path.join(os.environ.get("TEMP", "."), "niqe-calib")
CAP = 640                 # 与 image_scout.NIQE_MAX_SIDE 一致


def im(a):
    """np.uint8 数组 -> PIL 图。**不要传 mode 参数**（新版 Pillow 会报 not enough image data）。"""
    return Image.fromarray(np.ascontiguousarray(a, dtype=np.uint8))


def fractal_noise(w, h, octaves=7, seed=7):
    """1/f 分形噪声 —— 频谱像自然图像，用来当「更大尺寸」的第二个基准。"""
    rng = np.random.RandomState(seed)
    acc = np.zeros((h, w))
    amp = 1.0
    for o in range(octaves):
        gw, gh = max(2, w >> (octaves - o - 1)), max(2, h >> (octaves - o - 1))
        small = rng.rand(gh, gw)
        big = np.asarray(Image.fromarray((small * 255).astype(np.uint8))
                         .resize((w, h), Image.BICUBIC), dtype=np.float64) / 255.0
        acc += amp * (big - 0.5)
        amp *= 0.55
    acc -= acc.min()
    acc = acc / max(1e-9, acc.max())
    return (30 + acc * 190).astype(np.uint8)


def blur(img, sigma):
    return np.asarray(im(img).filter(ImageFilter.GaussianBlur(sigma)))


def jpeg(img, q):
    p = os.path.join(TMP, "_jq.jpg")
    im(img).save(p, "JPEG", quality=q)
    return np.asarray(Image.open(p).convert("RGB"))


def noisy(img, sd, seed=3):
    rng = np.random.RandomState(seed)
    a = img.astype(np.float64) + rng.normal(0, sd, img.shape)
    return np.clip(a, 0, 255).astype(np.uint8)


def shrink(img, factor):
    """降采样再最近邻拉回原尺寸 —— 模拟「被压扁过的小图」。"""
    h, w = img.shape[:2]
    src = im(img)
    small = src.resize((max(8, w // factor), max(8, h // factor)), Image.BILINEAR)
    return np.asarray(small.resize((w, h), Image.NEAREST))


def shoot(name, img):
    """存成 PNG（无损）再走真实管线，避免存储环节再引入劣化。"""
    safe = re.sub(r"[^0-9A-Za-z_.-]", "_", name)
    p = os.path.join(TMP, safe + ".png")
    im(img).save(p, "PNG")
    got = winimg.load_pixels_exact(p, CAP)
    if not got:
        return None, None, os.path.basename(p)
    w, h, bgra = got
    s, note = N.niqe_bgra(w, h, bgra)
    return s, (w, h), note


def main():
    os.makedirs(TMP, exist_ok=True)
    print("解码上限 max_side=%d，门槛标定目录 %s" % (CAP, TMP))
    samples = []

    # --- 样本 1：真实照片（baboon，480x480，不触发缩放） ---
    anchor = os.path.join(HERE, "testdata", "baboon480.png")
    samples.append(("baboon 原始", np.asarray(Image.open(anchor).convert("RGB"))))

    # --- 样本 2：1/f 合成「自然图」，1600x1200（会触发 640 缩放） ---
    # 注意：**不能**直接把灰度值塞进三个通道 —— luma_rows 走的是 BT.601 studio range
    # （0..255 -> 16..235），会把对比度压掉，算出来的分虚高。所以要反解回去。
    y = fractal_noise(1600, 1200).astype(np.float64)
    v = np.clip(np.round((y - 16.0) * 255.0 / 219.0), 0, 255).astype(np.uint8)
    samples.append(("1/f 1600x1200 原始", np.stack([v] * 3, -1)))

    rows = []
    for tag, base in samples:
        rows.append((tag, base))
        for s in (0.6, 1.2, 2.5, 5.0):
            rows.append(("%s 高斯模糊 σ=%.1f" % (tag, s), blur(base, s)))
        for q in (30, 15, 5):
            rows.append(("%s JPEG q=%d" % (tag, q), jpeg(base, q)))
        rows.append(("%s 噪声 σ=15" % tag, noisy(base, 15)))
        rows.append(("%s 压扁 4 倍" % tag, shrink(base, 4)))
        rows.append(("%s 压扁 8 倍" % tag, shrink(base, 8)))

    print()
    print("%-38s %9s %10s  %s" % ("样本", "NIQE", "实际算的尺寸", "说明"))
    print("-" * 100)
    out = []
    for tag, img in rows:
        s, wh, note = shoot(tag.replace(" ", "_").replace("=", ""), img)
        if s is None:
            print("%-38s %9s %10s  %s" % (tag, "算不了", "-", note))
            continue
        out.append((tag, s))
        gr = N.grade(s)[1]
        print("%-38s %9.4f %10s  %s <- %s" % (tag, s, "%dx%d" % wh, note, gr))

    print()
    print("汇总（**只在同一基准内比较**，跨基准比没有意义）：")
    base = {}
    for tag, s in out:
        base.setdefault("baboon" if tag.startswith("baboon") else "1/f", []).append((tag, s))
    for key, items in base.items():
        vals = sorted(items, key=lambda kv: kv[1])
        print("  [%s] 最低 %.4f（%s）  最高 %.4f（%s）"
              % (key, vals[0][1], vals[0][0], vals[-1][1], vals[-1][0]))


if __name__ == "__main__":
    main()
