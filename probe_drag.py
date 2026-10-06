# -*- coding: utf-8 -*-
"""大图缩放/拖动的**诊断探针**：分环节计时 + 清晰度/覆盖率量化。

主程序和 test_ui 都不 import 它 —— 这是「出问题时用来定位」的，
不是断言型测试。跑法：

    python probe_drag.py

会造两张 3000×2000 的**噪声图**（最坏输入），然后依次量：
  1. 精确帧的视口口径欠采样
  2. 滚轮快速帧的欠采样（这一项抓过「全程马赛克」）
  3. 真实拖动（按真实鼠标节奏 16ms/帧，在图能容纳的范围内来回扫）的
     画面覆盖率与最慢一帧
"""
from __future__ import annotations

import os
import random
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import image_scout as ui            # noqa: E402
import thumbs                      # noqa: E402
import uikit                       # noqa: E402
import winimg                      # noqa: E402

ROOT = os.path.join(tempfile.gettempdir(), "ImageScout-probe")


def make_photo(path, w=3000, h=2000, seed=7):
    """造一张**有真实高频细节**的图：噪声 + 横向渐变。

    ⚠️ 别用规则条纹图：条纹放大后还是规则条纹，**看起来永远是清晰的**。
    主程序上一版的清晰度 bug（欠采样 1.9 倍）在条纹图上量不出来，
    噪声才量得出来。
    """
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


def pump(app, until=None, timeout=120.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        app.update()
        if until is not None and until():
            return True
        time.sleep(0.01)
    return False


def settle(app, timeout=8.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        app.update()
        if not app._fit_pend:
            break
        time.sleep(0.02)
    app.update()


def main():
    uikit.enable_dpi_awareness()
    shutil.rmtree(ROOT, ignore_errors=True)
    os.makedirs(ROOT, exist_ok=True)
    a = make_photo(os.path.join(ROOT, "photo_a.png"))
    got = winimg.load_pixels(a, 2000, winimg.SIIGBF_RESIZETOFIT, raw=True)
    w, h, px = got
    _w, _h, cut = thumbs.crop_bgra(w, h, px, int(w * 0.1), int(h * 0.1),
                                   int(w * 0.8), int(h * 0.8))
    with open(os.path.join(ROOT, "photo_b.png"), "wb") as f:
        f.write(thumbs.bgra_to_png(_w, _h, cut))

    uikit.clear_photo_caches()
    app = ui.App([ROOT])
    app.update()
    app.start_scan()
    pump(app, until=lambda: not app.busy and bool(app.files), timeout=300)
    app.set_view("all")
    row = next(i for i, it in enumerate(app.glist.items) if it["tag"] == a)
    app.glist.select(row)
    app.update()

    def warm_idle():
        return ((not getattr(app, "_prewarm_q", []))
                and not getattr(app, "_prewarm_busy", False))

    pump(app, until=warm_idle, timeout=180)
    settle(app)
    pump(app, until=warm_idle, timeout=180)
    settle(app)
    print("预热档位 %s" % sorted(app.big._b.get(a, {}).keys()))

    class Ev(object):
        def __init__(self, d=0, x=10, y=10):
            self.delta, self.x, self.y = d, x, y

    def under():
        """视口口径的欠采样倍数：屏幕上真正看得见的那一块由多少源像素撑起。"""
        v = app._view.get(0)
        if not v or "base" not in v:
            return -1.0
        cw, ch = app.cv_a.winfo_width(), app.cv_a.winfo_height()
        dw, dh = v["disp"]
        bw, bh = v["base"]
        sx, sy = bw / float(dw), bh / float(dh)
        ox, oy = v["ox"], v["oy"]
        vx = min(dw, max(0, cw - ox)) - max(0, -ox)
        vy = min(dh, max(0, ch - oy)) - max(0, -oy)
        if vx <= 0 or vy <= 0:
            return -1.0
        return max(cw / float(vx * sx), ch / float(vy * sy))

    # ---- 1. 精确帧清晰度 ----
    print("\n=== 精确帧清晰度（停手后那张）===")
    app.reset_zoom()
    app.update()
    settle(app)
    for i in range(6):
        app._on_wheel(0, Ev(120))
        app.update()
        settle(app)
        app.render_all(precise=True, only=0)
        settle(app)
        v = app._view.get(0, {})
        print("  第%d档 zoom=%.2f 欠采样 %.2fx  基准 %s  块 %s"
              % (i + 1, app.zoom[0], under(), v.get("base"), v.get("blk")))

    # ---- 2. 滚轮快速帧（用户滚动过程中真正看到的）----
    print("\n=== 滚轮快速帧（precise=0.75，不停手）===")
    app.reset_zoom()
    app.update()
    settle(app)
    for i in range(5):
        app._on_wheel(0, Ev(120))
        app.update()
        v = app._view.get(0, {})
        print("  第%d档 zoom=%.2f 基准 %s 欠采样 %.2fx"
              % (i + 1, app.zoom[0], v.get("base"), under()))

    # ---- 3. 真实拖动 ----
    print("\n=== 真实拖动（16ms/帧，在图能容纳的范围内来回扫）===")
    app.reset_zoom()
    app.update()
    settle(app)
    for _ in range(3):
        app._on_wheel(0, Ev(120))
    app.update()
    settle(app)
    app.render_all(precise=True, only=0)
    settle(app)
    cw, ch = app.cv_a.winfo_width(), app.cv_a.winfo_height()
    dw, dh = app._view[0]["disp"]
    # ⚠️ 只能拖「图能容纳的范围」。越界后露白是**正常的**（图的边界就是块的
    #    边界），拿越界的 pan 去能量到一堆假的露白。
    rng_x = max(0, dw - cw) // 2
    rng_y = max(0, dh - ch) // 2
    print("  画布 %dx%d  显示图 %dx%d  可拖 ±%d,±%d  zoom %.2f"
          % (cw, ch, dw, dh, rng_x, rng_y, app.zoom[0]))

    app._pan_start(0, Ev(0, 100, 100))
    x0, y0 = app._drag[1], app._drag[2]
    bad = 0
    worst = 0.0
    for k in range(40):
        t = k / 39.0 * 2 - 1
        p = Ev(0, x0 + int(t * rng_x), y0 + int(t * rng_y))
        t0 = time.perf_counter()
        app._pan_move(0, p)
        app.update()
        dt = (time.perf_counter() - t0) * 1000
        worst = max(worst, dt)
        # ⚠️ **必须 sleep**：拖动补块有节流（PAN_LAG），不 sleep 的话
        #    40 步只花十几毫秒、一次都触发不了，量到的「露白 40/40」全是假的。
        time.sleep(0.016)
        v = app._view.get(0, {})
        item = v.get("item")
        bb = app.cv_a.bbox(item) if item else None
        if not bb:
            print("  第%d步：**画布上没有图元**  (%.1f ms)" % (k + 1, dt))
            bad += 1
            continue
        iw = min(bb[2], cw) - max(bb[0], 0)
        ih = min(bb[3], ch) - max(bb[1], 0)
        cov = max(0, iw) * max(0, ih) / float(cw * ch)
        if cov < 0.95:
            bad += 1
            if bad <= 8:
                print("  第%d步：只盖住 %.0f%%  bbox=%s 基准 %s (%.1f ms)"
                      % (k + 1, cov * 100, bb, v.get("base"), dt))
    app._pan_end()
    settle(app)
    print("  露白/空白 %d / 40 步，最慢一帧 %.1f ms" % (bad, worst))

    app.destroy()


if __name__ == "__main__":
    main()
