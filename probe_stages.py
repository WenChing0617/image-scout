# -*- coding: utf-8 -*-
"""⚠️ 给 `_render_tile` 的每个阶段装计时器 —— 不再猜。

## 为什么需要这个

修完马赛克后 `test_ui` 放大最慢从 86ms（v1.5）涨到 117~150ms。
我连着猜错了三次：

  1. 猜「`_placeholder` 的 `resample_img` 慢」-> 不是
  2. 猜「GDI+ 缩小多叠了一层拷贝」-> 改完反而更慢（139ms）
  3. 猜「1.09 倍的插值跑了 4 次全图拷贝」-> 改完还是 150ms

三次都改错，说明**瓶颈不在我猜的那儿**。cProfile 也帮不上忙：
它抓到的那一格只有 16ms（**缓存命中**），而慢的是**缓存未命中**那一格。
cProfile 抓不到那一次。

这个探针用**猴子补丁**给每个阶段挂计时器（不碰源码），
在**冷缓存**下逐格量：裁块、GDI+ 缩放、PPM 编码、PhotoImage 建图。

跑法：`D:/python/python-3.13.5/python.exe probe_stages.py`
"""
import os
import random
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import thumbs                  # noqa: E402
import uikit                   # noqa: E402
import winimg                  # noqa: E402

ACC = {}


def timed(name, fn):
    """包一层：累计耗时 + 次数。"""
    def wrap(*a, **kw):
        t0 = time.perf_counter()
        try:
            return fn(*a, **kw)
        finally:
            d = time.perf_counter() - t0
            e = ACC.setdefault(name, [0.0, 0])
            e[0] += d
            e[1] += 1
    return wrap


def install():
    thumbs.crop_bgra = timed("crop_bgra", thumbs.crop_bgra)
    uikit.fit_ppm = timed("fit_ppm", uikit.fit_ppm)
    winimg._gdip_scale_argb = timed("gdip_scale", winimg._gdip_scale_argb)

    import tkinter as tk
    orig_photo = tk.PhotoImage

    class TimedPhoto(orig_photo):
        """PhotoImage 建图 —— Tk 里最贵的一步，必须单独量。

        ⚠️ 用**子类**而不是包装函数：`tk.PhotoImage` 在别处被
        `isinstance` 检查过，也被 `_prewarm_done` 之类直接调用，
        换成普通函数会破坏那些路径。
        """
        def __init__(self, *a, **kw):
            t0 = time.perf_counter()
            try:
                orig_photo.__init__(self, *a, **kw)
            finally:
                d = time.perf_counter() - t0
                e = ACC.setdefault("PhotoImage", [0.0, 0])
                e[0] += d
                e[1] += 1

    tk.PhotoImage = TimedPhoto
    import image_scout as S
    S.tk.PhotoImage = TimedPhoto


def make_noise(path, w=3000, h=2000, seed=7):
    rnd = random.Random(seed)
    buf = bytearray(w * h * 4)
    for y in range(h):
        row = y * w * 4
        for x in range(w):
            i = row + x * 4
            v = rnd.randrange(256)
            g = 128 + int(80 * ((x - w / 2.0) / (w / 2.0)))
            buf[i] = (v + g) // 2
            buf[i + 1] = v
            buf[i + 2] = (255 - g) // 2
            buf[i + 3] = 255
    with open(path, "wb") as f:
        f.write(thumbs.bgra_to_png(w, h, bytes(buf)))
    return path


def pump(app, until, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        app.update()
        if until():
            return True
        time.sleep(0.01)
    return False


def settle(app, timeout=60):
    pump(app, lambda: not any(app._fit_pend.values()), timeout)
    for _ in range(2):
        pump(app, lambda: not getattr(app, "_prewarm_q", [])
             and not getattr(app, "_prewarm_busy", False), timeout)


class Ev(object):
    def __init__(self, d):
        self.delta = d
        self.x = 10
        self.y = 10


def reset():
    for k in ACC:
        ACC[k][0] = 0.0
        ACC[k][1] = 0


def main():
    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_stg")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    a = make_noise(os.path.join(tmp, "big_a.png"))
    got = winimg.load_pixels(a, 2000, winimg.SIIGBF_RESIZETOFIT, raw=True)
    w, h, px = got
    _w, _h, cut = thumbs.crop_bgra(w, h, px, int(w * 0.1), int(h * 0.1),
                                   int(w * 0.8), int(h * 0.8))
    with open(os.path.join(tmp, "big_b.png"), "wb") as f:
        f.write(thumbs.bgra_to_png(_w, _h, cut))

    install()
    import image_scout as S
    app = S.App([tmp])
    app.update()                      # ⚠️ 不设 geometry（跟 test_ui 一致）
    app.start_scan()
    pump(app, lambda: not app.busy and bool(app.files), 300)
    app.set_view("all")
    row = next(i for i, it in enumerate(app.glist.items)
               if it["tag"] == os.path.abspath(a))
    app.glist.select(row)
    app.update()
    settle(app)
    settle(app)
    print("画布 %dx%d" % (app.cv_a.winfo_width(), app.cv_a.winfo_height()))
    print()
    print("  zoom    总计   裁块    GDI+   编码  建图  |  其它   块->图")
    print("  " + "-" * 66)

    for i in range(8):
        reset()
        t0 = time.perf_counter()
        app._on_wheel(0, Ev(120))
        app.update()
        total = (time.perf_counter() - t0) * 1000
        v = app._view.get(0) or {}
        blk = v.get("blk") or (0, 0)
        disp = v.get("disp") or (0, 0)
        img = v.get("img")
        iw, ih = (img.width(), img.height()) if img is not None else (0, 0)
        g = lambda k: ACC.get(k, [0.0, 0])[0] * 1000
        acct = g("crop_bgra") + g("gdip_scale") + g("fit_ppm") \
            + g("PhotoImage")
        print("  %5.2f %6.0f %6.0f %6.0f %6.0f %6.0f  | %5.0f   %dx%d"
              % (app.zoom[0], total, g("crop_bgra"), g("gdip_scale"),
                 g("fit_ppm"), g("PhotoImage"), total - acct,
                 iw, ih))
        settle(app)

    print()
    print("⚠️「其它」= 总时间减去这四项 = Tk 画布贴图 + 取基准档 + "
          "防抖调度等")
    return 0


if __name__ == "__main__":
    sys.exit(main())