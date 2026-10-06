# -*- coding: utf-8 -*-
"""定位 v1.6 引入的性能回归到底在哪一格。

## 为什么单独写这个

修完马赛克后 `test_ui` 有 4 项性能断言挂了（放大 117ms、重复位置
100ms、拖动 161ms）。我先前在小窗口（975×432）做过分解，看到
「缓存命中 0.3ms」，以为已经好了 —— 但 test_ui 的窗口是 **1515×733**，
画布大 1.5 倍、块也大，结论不能直接套。

## 这个探针干什么

**照抄 test_ui 的窗口尺寸和操作序列**，然后对「放大最慢的那一格」
开 cProfile，把耗时按调用栈摊开。别再靠猜。

跑法：`D:/python/python-3.13.5/python.exe probe_perf.py`
"""
import os
import cProfile
import io
import pstats
import random
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import thumbs                  # noqa: E402
import winimg                  # noqa: E402


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
        try:
            app.update()
        except Exception:
            return False
        if until():
            return True
        time.sleep(0.01)
    return False


def settle(app, timeout=60):
    pump(app, until=lambda: not any(app._fit_pend.values()), timeout=timeout)
    for _ in range(2):
        pump(app, until=lambda: not getattr(app, "_prewarm_q", [])
             and not getattr(app, "_prewarm_busy", False), timeout=timeout)


class Ev(object):
    def __init__(self, d):
        self.delta = d
        self.x = 10
        self.y = 10


def main():
    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_perf")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    a = make_noise(os.path.join(tmp, "big_a.png"))
    got = winimg.load_pixels(a, 2000, winimg.SIIGBF_RESIZETOFIT, raw=True)
    w, h, px = got
    _w, _h, cut = thumbs.crop_bgra(w, h, px, int(w * 0.1), int(h * 0.1),
                                   int(w * 0.8), int(h * 0.8))
    with open(os.path.join(tmp, "big_b.png"), "wb") as f:
        f.write(thumbs.bgra_to_png(_w, _h, cut))

    import image_scout as S
    app = S.App([tmp])
    # ⚠️⚠️ **别设 geometry** —— test_ui 也不设，靠 Tk 默认尺寸拿到
    # 1515×733 的画布。我第一版设了 `geometry("1560x1030")`，画布被压到
    # 975×473（不到一半），于是量出「放大最慢 48ms」、比门槛还宽裕 ——
    # **完全不可比的结论**。窗口大小必须跟被测对象一致。
    app.update()
    app.start_scan()
    pump(app, lambda: not app.busy and bool(app.files), 300)
    app.set_view("all")
    row = next(i for i, it in enumerate(app.glist.items)
               if it["tag"] == os.path.abspath(a))
    app.glist.select(row)
    app.update()
    settle(app)
    settle(app)
    print("画布 %dx%d\n" % (app.cv_a.winfo_width(), app.cv_a.winfo_height()))

    # ---- 放大 8 格，逐格计时，找出最慢的 ----
    up = []
    for _ in range(8):
        t0 = time.perf_counter()
        app._on_wheel(0, Ev(120))
        app.update()
        up.append(((time.perf_counter() - t0) * 1000, app.zoom[0]))
        settle(app)
    print("放大 %s" % " ".join("%.0f" % v for v, _z in up))
    worst_ms, worst_z = max(up)
    print("最慢 %.0f ms @ zoom %.2f\n" % (worst_ms, worst_z))

    # ---- 回到那一格，profile ----
    while app.zoom[0] > worst_z + 1e-6:
        app._on_wheel(0, Ev(-120))
        app.update()
        settle(app)
    assert abs(app.zoom[0] - worst_z) < 1e-6, app.zoom[0]

    pr = cProfile.Profile()
    pr.enable()
    app._on_wheel(0, Ev(120))
    app.update()
    pr.disable()
    s = io.StringIO()
    pstats.Stats(pr, stream=s).sort_stats("tottime").print_stats(16)
    out = s.getvalue()
    print(out[:3200])
    return 0


if __name__ == "__main__":
    sys.exit(main())