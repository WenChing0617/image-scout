# -*- coding: utf-8 -*-
"""占位图/插值方案的成本对照（拖动露白的修法选型用）。

跑法：

    python probe_placeholder.py

背景：`probe_pan.py` 量到拖动时占位图 want=(1818,880) 却 got=(1472,982)
—— `resample_img` 只能整数倍，尺寸永远对不上，图元右侧/下侧就缺一块
（覆盖 87%/72%/54% 三值循环）。而块本该显示 1818×880，基准像素只有
1324×641，即 up=1.373 > 1，必须插值。

所以问题是：**用什么办法既把尺寸做到精确的 (twant,thant)，又足够快？**

⚠️ 上一版这个探针把 `noise()` 写在 lambda 里，于是「PPM 编码 979ms」
其实量的是「生成 640 万个随机数」—— 探针自己骗自己，跟之前踩过的
「判据/测试图自己有问题」是同一类坑。**所有输入都必须在计时前备好。**
"""
from __future__ import annotations

import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import thumbs            # noqa: E402
import uikit             # noqa: E402
import winimg            # noqa: E402


def noise(w, h, seed=11):
    """纯噪声 BGRA（最坏输入：高频细节多，插值和编码都走最长的路）。"""
    rnd = random.Random(seed)
    buf = bytearray(w * h * 4)
    # 用 `randbytes` 一次性拿齐，比逐个 randrange 快几十倍
    buf[:] = rnd.randbytes(w * h * 4)
    for i in range(3, len(buf), 4):
        buf[i] = 255
    return bytes(buf)


def timeit(fn, n=5):
    """取 n 次里的**最快**那次。"""
    best = 1e9
    out = None
    for _ in range(n):
        t0 = time.perf_counter()
        out = fn()
        best = min(best, (time.perf_counter() - t0) * 1000)
    return best, out


def main():
    uikit.enable_dpi_awareness()
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()

    # zoom 3.81 那一档的真实数字（probe_pan.py 量到的）
    blkW, blkH = 1324, 641
    tw, th = 1818, 880
    src = noise(blkW, blkH)
    print("块 %dx%d -> 本应显示 %dx%d  (up=%.3f)"
          % (blkW, blkH, tw, th, tw / float(blkW)))

    # ---- A  Tk resample_img（现状）：尺寸对不上，直接排除 ----
    base_ppm = thumbs.bgra_to_ppm(blkW, blkH, src)
    img = tk.PhotoImage(data=base_ppm, master=root)
    ms, out = timeit(lambda: uikit.resample_img(img, tw, th, 8.0))
    print("\n--- A  Tk resample_img（现状）---")
    print("  %6.1f ms  -> (%d,%d)  尺寸误差 %+d,%+d  只盖住 %.1f%%"
          % (ms, out.width(), out.height(),
             out.width() - tw, out.height() - th,
             100.0 * out.width() / tw))

    # ---- B/C/D  GDI+ 各插值模式：尺寸严格精确 ----
    print("\n--- GDI+ 各插值模式（输出尺寸必须严格 = (%d,%d)）---" % (tw, th))
    combos = [
        ("HighQualityBicubic  offset=HQ  ", winimg.IM_BICUBIC, 2),
        ("HighQualityBicubic  offset=None", winimg.IM_BICUBIC, 0),
        ("Bilinear            offset=HQ  ", winimg.IM_BILINEAR, 2),
        ("Bilinear            offset=None", winimg.IM_BILINEAR, 0),
        ("HighQualityBilinear offset=None", winimg.IM_BILINEARHQ, 0),
        ("NearestNeighbor     offset=HQ  ", winimg.IM_NEAREST, 2),
        ("NearestNeighbor     offset=None", winimg.IM_NEAREST, 0),
    ]
    for label, mode, off in combos:
        try:
            ms, r = timeit(lambda: winimg._gdip_scale_argb2(
                blkW, blkH, src, tw, th, mode, off))
        except Exception as e:
            print("  %s 失败 %s" % (label, e))
            continue
        ok = "精确" if r and r[:2] == (tw, th) else "✗ 尺寸 %s" % (r[:2],)
        print("  %s %6.1f ms  %s" % (label, ms, ok))

    # ---- 固定开销：编码 + 建图（输入预先备好）----
    print("\n--- 固定开销 ---")
    full = noise(tw, th)
    ms, ppm_full = timeit(lambda: thumbs.bgra_to_ppm(tw, th, full))
    print("  PPM 编码 %dx%d(%.1f 万像素)  %6.1f ms  %d 字节"
          % (tw, th, tw * th / 1e4, ms, len(ppm_full)))
    ms, _ = timeit(lambda: tk.PhotoImage(data=ppm_full, master=root))
    print("  PhotoImage 建图              %6.1f ms" % ms)

    # 小一档的块（zoom 小的档 twant 也小）
    for w2, h2 in ((848, 411), (1100, 530)):
        f2 = noise(w2, h2)
        ms2, p2 = timeit(lambda: thumbs.bgra_to_ppm(w2, h2, f2))
        ms3, _ = timeit(lambda: tk.PhotoImage(data=p2, master=root))
        print("  对照 %dx%d(%.1f 万像素)：编码 %5.1f ms  建图 %5.1f ms"
              % (w2, h2, w2 * h2 / 1e4, ms2, ms3))

    # ---- E  「1:1 显示 + 多取」：基准像素直接给显示尺寸 ----
    print("\n--- E  「不插值，1:1 显示」需要基准像素够大 ---")
    print("  基准（被原图封顶）只有 %dx%d，取不到 %dx%d" % (blkW, blkH, tw, th))
    print("  => 放大档**只能插值**，1:1 这条路在这些档位上不存在")
    BW, BH = 3000, 2000
    base = noise(BW, BH)
    cw_, chh_, cut = thumbs.crop_bgra(BW, BH, base, 100, 100, tw, th)
    ms, _ = timeit(lambda: thumbs.crop_bgra(BW, BH, base, 100, 100, tw, th))
    print("  但若基准够大：从 %dx%d 裁 %dx%d  %6.1f ms"
          % (BW, BH, cw_, chh_, ms))
    ms, _ = timeit(lambda: thumbs.bgra_to_ppm(cw_, chh_, cut))
    print("     编码 %dx%d              %6.1f ms" % (cw_, chh_, ms))

    root.destroy()


if __name__ == "__main__":
    main()
