# -*- coding: utf-8 -*-
"""补块那 108ms 到底花在哪 —— **决定能不能搬后台**的前提。

为什么必须先量这个（而不是直接搬）：
    `thumbs.bgra_to_ppm` 的实现是「扩展切片赋值」：
        rgb[0::3] = mv[2::4]      # C 层 memcpy
    这是 C 层代码，但 **bytearray 扩展切片赋值不释放 GIL**。
    所以「搬到后台线程」不一定能并行 —— 主线程照样被卡住。
    `_fit_async` 之所以有效，是因为它里面最贵的是 `winimg._gdip_scale_argb2`
    （ctypes 调 GDI+，**ctypes 会释放 GIL**）。

所以这里把补块路径拆成 5 段分别计时，并**额外量一次「真放到后台线程
再 join 回来」的总耗时** —— 如果后台总耗时 ≈ 主线程总耗时，
就说明 GIL 没被释放，搬后台纯亏；反之才值得搬。

用法：
    python probe_fillcost.py
"""
from __future__ import annotations

import os
import shutil
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import thumbs                      # noqa: E402
import uikit                       # noqa: E402
import winimg                      # noqa: E402

ROOT = os.path.join(os.environ.get("TEMP", "."), "ImageScout-fillcost")


def make_photo(path, w=3000, h=2000, seed=11):
    import random
    rnd = random.Random(seed)
    buf = bytearray(w * h * 4)
    buf[:] = rnd.randbytes(w * h * 4)
    for i in range(3, len(buf), 4):
        buf[i] = 255
    with open(path, "wb") as f:
        f.write(thumbs.bgra_to_png(w, h, bytes(buf)))
    return path


def timeit(fn, n=5):
    """跑 n 次取最小值（最小值 = 排除 GC / 调度噪声的「真实成本」）。"""
    best = 1e9
    for _ in range(n):
        t = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t)
    return best * 1000.0


def main():
    uikit.enable_dpi_awareness()
    shutil.rmtree(ROOT, ignore_errors=True)
    os.makedirs(ROOT, exist_ok=True)
    a = make_photo(os.path.join(ROOT, "a.png"))
    got = winimg.load_pixels(a, 2000, winimg.SIIGBF_RESIZETOFIT, raw=True)
    bw, bh, bbgra = got
    print("基准档 %dx%d (%.1f MB bgra)" % (bw, bh, len(bbgra) / 1048576.0))

    # ⚠️ 块尺寸要**照抄 probe_pan2 的真实场景**：视口 1515×757、
    #   zoom≈3.81、TILE_PAD=0.16 -> 横向定长 cw+2*padx、纵向跟视口。
    CW, CH = 1515, 757
    padx = int(min(2000, CW) * 0.16)
    pady = int(min(2000, CH) * 0.16)
    blkW = CW + 2 * padx
    blkH = CH + pady
    print("块 %dx%d (pad %dx%d)" % (blkW, blkH, padx, pady))

    px0, py0 = 400, 500
    # ⚠️ **必须用裁完之后的真实尺寸**：块请求 1999×878，但基准档只有
    #   2000 宽，px0=400 之后只剩 1600 —— `crop_bgra` 会夹。
    #   拿标称尺寸去调 GDI+ 会读越界（access violation）。
    cw_r, ch_r, cbgra = thumbs.crop_bgra(bw, bh, bbgra, px0, py0,
                                        blkW, blkH)
    blkW, blkH = cw_r, ch_r
    print("实际裁到 %dx%d" % (blkW, blkH))

    print("\n--- 同步分段（毫秒，最小值）---")
    t_crop = timeit(lambda: thumbs.crop_bgra(bw, bh, bbgra, px0, py0,
                                             blkW, blkH))
    print("  1 crop_bgra          %7.1f" % t_crop)

    # 放大帧才走 GDI+；缩小帧 up<=1 直接按块原尺寸贴。
    # 这里两种都量 —— 补块 precise=0.75，up 由基准档决定。
    tw, th = int(blkW * 1.37), int(blkH * 1.37)      # 模拟 up=1.37
    t_gdip = timeit(lambda: winimg._gdip_scale_argb2(blkW, blkH, cbgra,
                                                     tw, th,
                                                     winimg.IM_BILINEAR))
    print("  2 GDI+ 双线性(1.37x) %7.1f   <- ctypes，**释放 GIL**" % t_gdip)

    sw, sh, sbgra = winimg._gdip_scale_argb2(blkW, blkH, cbgra, tw, th,
                                             winimg.IM_BILINEAR)
    t_ppm = timeit(lambda: thumbs.bgra_to_ppm(sw, sh, sbgra))
    print("  3 bgra_to_ppm %dx%d  %7.1f   <- 扩展切片，**不释放 GIL**"
          % (sw, sh, t_ppm))

    t_ppm_raw = timeit(lambda: thumbs.bgra_to_ppm(blkW, blkH, cbgra))
    print("  3' bgra_to_ppm（不插值）%7.1f" % t_ppm_raw)

    # ---- 关键实验：整条链搬后台线程，主线程「像真拖动那样」等事件 ----
    def whole_chain():
        _w, _h, c = thumbs.crop_bgra(bw, bh, bbgra, px0, py0, blkW, blkH)
        r = winimg._gdip_scale_argb2(blkW, blkH, c, tw, th, winimg.IM_BILINEAR)
        if r:
            _w, _h, c = r
        return thumbs.bgra_to_ppm(_w, _h, c)

    t_sync = timeit(whole_chain, n=3)
    print("\n  同步整链（裁+插值+编码） %7.1f" % t_sync)

    # ⚠️⚠️ **主线程必须「像真拖动那样」等事件，不能 busy-spin**。
    # 上一版主线程 `while th_.is_alive(): spins += 1` —— 那是**纯 Python
    # 抢 GIL**，量出「后台比同步慢 226%」。可真实拖动时主线程在
    # `app.update()` 里（Tcl_DoOneEvent 等输入，期间**释放 GIL**）。
    # 两种主线程状态量出来的结论完全相反，不重测就是拿假数据做决策。
    import tkinter as tk
    root = tk.Tk()
    root.geometry("300x200+0+0")

    def bg_run_realistic():
        box = {}

        def w():
            box["v"] = whole_chain()
        th_ = threading.Thread(target=w)
        t = time.perf_counter()
        th_.start()
        # 主线程 = 模拟事件循环：Tk 等输入（释放 GIL）
        while th_.is_alive():
            root.update()
        th_.join()
        return (time.perf_counter() - t) * 1000.0

    best_bg = min(bg_run_realistic() for _ in range(3))
    print("  后台整链 + 主线程 update()  %7.1f" % best_bg)
    print("  => 后台比同步 %+.0f%%（负=更快，搬后台有收益）"
          % ((best_bg / t_sync - 1.0) * 100.0))

    # ---- 对照：只把「不释放 GIL 的两段」搬后台（GDI+ 留主线程）----
    def bg_ppm_only():
        """裁块 + 编码搬后台（这两段最贵且不释放 GIL），GDI+ 留主线程。"""
        box = {}

        def w():
            box["c"] = thumbs.crop_bgra(bw, bh, bbgra, px0, py0, blkW, blkH)
        th_ = threading.Thread(target=w)
        t = time.perf_counter()
        th_.start()
        while th_.is_alive():
            root.update()
        th_.join()
        t_cropjoin = (time.perf_counter() - t) * 1000.0
        r = winimg._gdip_scale_argb2(blkW, blkH, box["c"][2], tw, th,
                                     winimg.IM_BILINEAR)
        if r:
            box["c"] = r
        ppm = thumbs.bgra_to_ppm(box["c"][0], box["c"][1], box["c"][2])
        return t_cropjoin, ppm

    t0 = time.perf_counter()
    t_join, _ppm = bg_ppm_only()
    t_all = (time.perf_counter() - t0) * 1000.0
    print("\n  只把 crop 搬后台（编码留主线程）：join 等了 %.1fms，"
          "主线程总计 %.1fms" % (t_join, t_all))
    print("  => 主线程净省 %.1fms（同步 %.1f -> 异步 %.1f）"
          % (t_sync - t_all, t_sync, t_all))

    # ---- 剩下那 ~60ms 在哪：PhotoImage + canvas 贴图 + blit ----
    # 这一段**只能在主线程**（Tk 只认主线程），所以量清楚才知道
    # 「到底还有多少可搬」。
    ppm = whole_chain()
    root = tk.Tk()
    root.geometry("1600x1000+0+0")
    cv = tk.Canvas(root, width=1600, height=1000, highlightthickness=0)
    cv.pack()
    root.update()

    print("\n--- 主线程专属段（毫秒，最小值）---")
    holder = []

    def mk():
        img = tk.PhotoImage(data=ppm, master=root)
        holder.append(img)
        return img
    t_pi = timeit(mk, n=3)
    print("  4 tk.PhotoImage(%d MB)  %7.1f   <- **只能主线程**"
          % (len(ppm) // 1048576, t_pi))
    img = holder[-1]

    def blit():
        cv.delete("all")
        cv.create_image(0, 0, anchor="nw", image=img)
        root.update()
    t_blit = timeit(blit, n=5)
    print("  5 delete+create_image+blit %7.1f   <- 只能主线程" % t_blit)

    def blit_coords():
        cv.coords(cv.find_all()[0], 8, 8)
        root.update()
    t_coords = timeit(blit_coords, n=5)
    print("  6 纯挪图元 coords+blit    %7.1f   <- 拖动每帧的正常成本"
          % t_coords)
    print("\n  => 补块一帧 = 链 %.1f + PhotoImage %.1f + 贴图 %.1f = %.1f"
          % (t_sync, t_pi, t_blit, t_sync + t_pi + t_blit))
    print("     其中**能搬后台的只有链里的 GDI+ 段**（%.1fms，ctypes）；"
          % t_gdip)
    print("     crop(%.1f)+ppm(%.1f) 不释放 GIL，PhotoImage/贴图只能在主线程。"
          % (t_crop, t_ppm))
    root.destroy()


if __name__ == "__main__":
    main()
