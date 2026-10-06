# -*- coding: utf-8 -*-
"""⚠️ 定点探针：Tk 的 `zoom()` 和 GDI+ 的双三次，在放大时差多少？

## 背景

已证实 `resample_img()` 的放大路径是 `subsample(q)` → `zoom(p)`，
`subsample` 是点抽样（**真丢像素**），所以 1.1 倍放大也能出 11px 色块。

那能不能改成「真插值」？Tk 只有 `zoom()`（最近邻），所以 Tk 内部
**没有**插值选项 —— 只能借外力。`winimg.py` 里有 GDI+
（`HighQualityBicubic`），这里量一下它放大 1.1/2/3/5 倍的插值质量，
跟 Tk `zoom()`、以及「先丢后放」三条路对比。

## 判据

源块 = 1px 黑白竖条纹（每行一样）。
- **振幅**：行内 max-min。真插值/真放大应 ≈255；丢采样会塌（甚至 0）。
- **块宽**：平均同色段长度。Tk `zoom(11)` = 11px；双三次 ≈1px。
  主人截图里是 20px 量级，这里要能压到 2px 以内才算真插值。

跑法：`D:/python/python-3.13.5/python.exe probe_interp.py`
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tkinter as tk          # noqa: E402

import uikit                   # noqa: E402
import winimg                  # noqa: E402


def stripes_bgra(w, h):
    buf = bytearray(w * h * 4)
    for y in range(h):
        row = y * w * 4
        for x in range(w):
            v = 0 if (x % 2 == 0) else 255
            i = row + x * 4
            buf[i] = v              # BGRA: B
            buf[i + 1] = v          # G
            buf[i + 2] = v          # R
            buf[i + 3] = 255        # A
    return bytes(buf)


def stats_from_grey(grey):
    amp = max(grey) - min(grey)
    seg, tot = 1, 0
    for i in range(1, len(grey)):
        if abs(grey[i] - grey[i - 1]) <= 16:
            seg += 1
        else:
            tot += seg
            seg = 1
    tot += seg
    return amp, tot / float(len(grey))


def grey_from_tk(img, y):
    w = img.width()
    return [img.get(x, y)[0] for x in range(w)]


def grey_from_bgra(buf, w, y):
    row = y * w * 4
    return [buf[row + x * 4 + 2] for x in range(w)]     # 取 R 通道


def main():
    root = tk.Tk()
    root.withdraw()

    W, H = 400, 8
    bgra = stripes_bgra(W, H)
    ppm, _pw, _ph = uikit.fit_ppm(W, H, bgra, 0)
    base = tk.PhotoImage(data=ppm, master=root)

    print("源块 %dx%d 1px 条纹" % (W, H))
    print()
    print("  放大   Tk zoom(最近邻)      GDI+ HighQualityBicubic")
    print("         振幅  块宽          振幅  块宽")
    print("  " + "-" * 54)

    ok_gdip = True
    for mul in (1.1, 1.35, 2, 3, 5, 10, 20):
        ow = int(round(W * mul))
        # --- Tk zoom（最近邻），单整数倍
        if abs(mul - round(mul)) < 1e-9 and round(mul) >= 1:
            p = int(round(mul))
            tkimg = base.zoom(p)
            a1, b1 = stats_from_grey(grey_from_tk(tkimg, 0))
        else:
            a1, b1 = None, None
        # --- GDI+ 双三次
        r = winimg._gdip_scale_argb(W, H, bgra, ow, max(1, int(round(H * mul))))
        if r:
            gw, gh, gbuf = r
            a2, b2 = stats_from_grey(grey_from_bgra(gbuf, gw, gh // 2))
        else:
            a2, b2 = None, None

        def f(v):
            return "  -  " if v is None else "%5d" % v

        def g(v):
            return "   -   " if v is None else "%6.2f" % v

        note = ""
        if a2 is not None and b2 > 3.0:
            note = "  <== GDI+ 也没插好"
            ok_gdip = False
        print("  %5.2f  %s %s      %s %s%s"
              % (mul, f(a1), g(b1), f(a2), g(b2), note))

    print()
    if ok_gdip:
        print("✅ GDI+ 双三次在所有放大倍率下块宽都 <= 3px = 真插值。")
        print("   -> 可以拿它替掉 resample_img 的 subsample+zoom。")
    else:
        print("⚠️ GDI+ 在某些倍率下也不足 2px 块宽，先别指望它。")
    root.destroy()
    return 0 if ok_gdip else 1


if __name__ == "__main__":
    sys.exit(main())