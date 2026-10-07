# -*- coding: utf-8 -*-
"""缩小路径的耗时探针：定位「8ms vs 85ms」那个二分模式。

跑法：

    python probe_shrink.py

背景：test_ui 报「大图缩小不卡（最慢 84 ms < 80 ms）」，且**三轮完全
可复现**（缩小序列 10/62/65/15/85/55/54/7，第 5 步每次都 85ms）。
这不是随机抖动，是个稳定的二分：

    要么 ~8ms（够用的档已在缓存里）
    要么 ~85ms（`precise >= 1` 时 `peek_base` 返回 None ->
                `self.big.base(...)` **同步解码**一档，49~95ms）

所以本探针逐档打印：
  * zoom / need_side（该档想要多少像素）
  * `big` 缓存里**实际有哪几档**
  * `peek_base` 命中没有 / 同步解码了没有
  * 内部耗时分解（查缓存 / crop / GDI+ / PPM 编码 / PhotoImage）
"""
from __future__ import annotations

import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import image_scout as ui            # noqa: E402
import thumbs                      # noqa: E402
import uikit                       # noqa: E402

ROOT = os.path.join(os.environ.get("TEMP", "."), "ImageScout-shrink")


def make_photo(path, w=3000, h=2000, seed=3):
    import random
    rnd = random.Random(seed)
    buf = bytearray(w * h * 4)
    buf[:] = rnd.randbytes(w * h * 4)
    for i in range(3, len(buf), 4):
        buf[i] = 255
    with open(path, "wb") as f:
        f.write(thumbs.bgra_to_png(w, h, bytes(buf)))
    return path


def main():
    uikit.enable_dpi_awareness()
    shutil.rmtree(ROOT, ignore_errors=True)
    os.makedirs(ROOT, exist_ok=True)
    a = make_photo(os.path.join(ROOT, "big_a.png"))

    uikit.clear_photo_caches()
    app = ui.App([ROOT])
    app.update()
    app.start_scan()
    t0 = time.time()
    while time.time() - t0 < 300:
        app.update()
        if not app.busy and app.files:
            break
        time.sleep(0.01)
    app.set_view("all")
    row = next(i for i, it in enumerate(app.glist.items) if it["tag"] == a)
    app.glist.select(row)
    app.update()

    def warm_idle():
        return (not getattr(app, "_prewarm_q", [])
                and not getattr(app, "_prewarm_busy", False))

    for _ in range(3):
        t0 = time.time()
        while time.time() - t0 < 180:
            app.update()
            if warm_idle():
                break
            time.sleep(0.01)

    def settle():
        t0 = time.time()
        while time.time() - t0 < 8:
            app.update()
            if not app._fit_pend:
                break
            time.sleep(0.02)

    class Ev(object):
        def __init__(self, d=0, x=700, y=350):
            self.delta, self.x, self.y = d, x, y

    settle()
    cw = app.cv_a.winfo_width()
    ch = app.cv_a.winfo_height()
    cx, cy = cw // 2, ch // 2
    print("画布 %dx%d  原图 %s" % (cw, ch, ui.META.of(a)["wh"]))

    # ---- 先滚到最大，模拟 test_ui 的前置状态 ----
    for _ in range(8):
        app._on_wheel(0, Ev(120, cx, cy))
        app.update()
        settle()
    print("滚到 %.2f 倍，缓存里的档位 = %s"
          % (app.zoom[0], sorted(app.big._b.get(a, {}).keys())))

    # ---- 装计时器：只量「同步解码」和「查缓存」，其他不拆 ----
    #      （拆到 GDI+/PPM 那层要改产品代码，先看这两个够不够定位）
    hits = {"peek_ok": 0, "decode": 0, "peek_best_only": 0}
    real_base = app.big.base
    real_peek = app.big.peek_base
    real_crop = thumbs.crop_bgra
    real_ppm = uikit.fit_ppm
    real_gdip = ui.winimg._gdip_scale_argb2
    inner = {"crop": 0.0, "gdip": 0.0, "ppm": 0.0}

    def timed_base(*args, **kw):
        t0 = time.perf_counter()
        r = real_base(*args, **kw)
        ms = (time.perf_counter() - t0) * 1000
        hits["decode"] += 1
        print("        ↳ **同步解码** %.0f ms（要 %s 档）" % (ms, args[1:3]))
        return r

    def timed_peek(*args, **kw):
        r = real_peek(*args, **kw)
        if r is not None:
            hits["peek_ok"] += 1
        return r

    def timed_crop(*a, **kw):
        t0 = time.perf_counter()
        r = real_crop(*a, **kw)
        inner["crop"] += (time.perf_counter() - t0) * 1000
        return r

    def timed_ppm(*a, **kw):
        t0 = time.perf_counter()
        r = real_ppm(*a, **kw)
        inner["ppm"] += (time.perf_counter() - t0) * 1000
        return r

    def timed_gdip(*a, **kw):
        t0 = time.perf_counter()
        r = real_gdip(*a, **kw)
        inner["gdip"] += (time.perf_counter() - t0) * 1000
        return r

    app.big.base = timed_base
    app.big.peek_base = timed_peek
    thumbs.crop_bgra = timed_crop
    uikit.fit_ppm = timed_ppm
    ui.winimg._gdip_scale_argb2 = timed_gdip

    print("\n=== 缩小 8 档：逐步看要哪档、缓存里有没有 ===")
    print("⚠️ `need_side` 印的是**滚完之后**那一帧的（打印在 _on_wheel 之后）")
    print(" 档 zoom   need_side   _level  要的档   命中?   耗时")
    for k in range(8):
        t0 = time.perf_counter()
        app._on_wheel(0, Ev(-120, cx, cy))
        app.update()
        ms = (time.perf_counter() - t0) * 1000
        c0, g0, p0 = (inner["crop"], inner["gdip"], inner["ppm"])
        inner["crop"] = inner["gdip"] = inner["ppm"] = 0.0
        v = app._view.get(0) or {}
        disp = v.get("disp") or (0, 0)
        need = max(disp) if disp else 0
        print("        blk=%s want=%s base=%s 成品图缓存数=%d"
              % (v.get("blk"), v.get("want"), v.get("base"),
                 len(app.big._p)))
        want_lv = app.big._level(need, 3000)
        cached = sorted(app.big._b.get(a, {}).keys())
        have = want_lv in cached
        print(" %2d %6.2f %9d %8d  %-6s  %6.1f ms   "
              "（crop %.0f / gdip %.0f / ppm %.0f）"
              % (k + 1, app.zoom[0], need, want_lv,
                 "有" if have else "缺", ms,
                 c0, g0, p0))
        if not have:
            print("        ⚠️ 缓存里没有 %d 这一档（现有 %s）"
                  % (want_lv, cached))
        settle()
    app.big.base = real_base
    app.big.peek_base = real_peek
    thumbs.crop_bgra = real_crop
    uikit.fit_ppm = real_ppm
    ui.winimg._gdip_scale_argb2 = real_gdip
    print("\n  peek_base 命中 %d 次、同步解码 %d 次"
          % (hits["peek_ok"], hits["decode"]))
    if hits["decode"]:
        print("  → **慢的那几步是同步解码**：_render_tile 里 "
              "`precise >= 1` 时 peek_base 落空就 `big.base()`，"
              "一档 49~95ms。缓存里缺的那一档就是元凶。")
    else:
        print("  → 一档都没同步解码，耗时全在别处（裁块/编码/建图）。")

    # ---- 档位表有没有缺口 ----
    print("\n=== 档位表 ===")
    print("  BASE_STEP=%s  _level 表：%s"
          % (getattr(ui, "BASE_STEP", "?"),
             [app.big._level(n, 3000) for n in
              (1063, 1329, 1661, 2076, 2596, 3245, 4056, 4211)]))
    print("  ⚠️ 档位是按 256 步长的**幂次表**，3000 宽的图能解出来的档：%s"
          % sorted(app.big._b.get(a, {}).keys()))
    app.destroy()


if __name__ == "__main__":
    main()