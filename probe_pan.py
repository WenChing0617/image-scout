# -*- coding: utf-8 -*-
"""拖动专项探针：查「拖动时画面盖不满画布」和「拖动最慢一帧」。

⚠️ 这不是断言型测试，是**定位用**的探针（主程序和 test_ui 都不 import 它）。
跑法：

    python probe_pan.py

会造两张 3000×2000 的**噪声图**（最坏输入），滚到 zoom 6 那一档，
然后按真实鼠标节奏（16ms/帧）来回扫 40 步，每一步都打印：

  * 图元的真实 bbox 与覆盖率
  * 这一帧走的是哪条路（缓存命中 / 同步 / 占位+后台）
  * **画出来的图元尺寸** vs **本应显示的尺寸 (twant, thwant)**

最后一条是关键：`_render_tile` 把图元放在 `ox + px0/sx`，那是**显示坐标**；
而占位图 / 异步帧的尺寸如果和 `(twant, thwant)` 对不上，画面就会缺一块。
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

ROOT = os.path.join(tempfile.gettempdir(), "ImageScout-probe-pan")

LOG = []


def make_photo(path, w=3000, h=2000, seed=7):
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


def hook(app):
    """把关键路径挂上计时 + 尺寸记录（含 `_render_tile` 的阶段拆分）。"""
    st = {"n": 0}
    stage = []

    def tick(label, fn):
        """测一段纯计算耗时，记进本步的阶段表。"""
        def inner(*a, **kw):
            t0 = time.perf_counter()
            r = fn(*a, **kw)
            stage.append((label, (time.perf_counter() - t0) * 1000))
            return r
        return inner

    # ---- `_render_tile` 内部的阶段拆分（猴子补丁，不改主程序）----
    orig_tile = app._render_tile

    def tile(*a, **kw):
        col, cv, cap, path, box, zoom, precise = a[:7]
        rec = {"name": "render_tile"}
        t0 = time.perf_counter()
        r = orig_tile(*a, **kw)
        rec["ms"] = (time.perf_counter() - t0) * 1000
        rec["zoom"] = zoom
        rec["precise"] = precise
        rec["pan"] = tuple(app.pan[col])
        rec["stages"] = stage[:]
        del stage[:]
        LOG.append(rec)
        return r
    app._render_tile = tile

    app._placeholder = wrap("placeholder", app._placeholder)
    app._fit_async = wrap("fit_async", app._fit_async)
    app._fit_ready = wrap("fit_ready", app._fit_ready)
    app.render_all = wrap("render_all", app.render_all)
    # ---- 阶段计时挂在**模块函数**上（模块内部是 `thumbs.crop_bgra(...)`
    #      这么直接调的，给 App 实例挂属性根本不生效 —— 踩过）
    app.crop_bgra = tick("crop_bgra", thumbs.crop_bgra)
    app.fit_ppm = tick("fit_ppm", uikit.fit_ppm)
    thumbs.crop_bgra = app.crop_bgra
    uikit.fit_ppm = app.fit_ppm
    return st


def wrap(name, fn):
    """给单个方法挂计时 + 尺寸记录（注意是绑定方法，self 不在参数里）。"""
    def inner(*a, **kw):
        t0 = time.perf_counter()
        r = fn(*a, **kw)
        dt = (time.perf_counter() - t0) * 1000
        rec = {"name": name, "ms": dt}
        if name == "placeholder":
            col, path, cw, ch = a[0:4]
            rec["want"] = (cw, ch)
            rec["got"] = (r[1], r[2]) if r and r[0] is not None else None
        elif name == "fit_async":
            # a = (col, path, key, w, h, bgra, want, radius)
            rec["blk"] = (a[3], a[4])
            rec["want"] = a[6]
            rec["need_gdip"] = kw.get("need_gdip")
        elif name == "fit_ready":
            rec["key"] = str(a[2])[:60]
        LOG.append(rec)
        return r
    return inner


def drain(app):
    out = LOG[:]
    del LOG[:]
    return out


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

    hook(app)

    class Ev(object):
        def __init__(self, d=0, x=10, y=10):
            self.delta, self.x, self.y = d, x, y

    NZ = 6
    app.reset_zoom()
    app.update()
    settle(app)
    for _ in range(NZ):
        app._on_wheel(0, Ev(120))
    app.update()
    settle(app)
    app.render_all(precise=True, only=0)
    settle(app)
    drain(app)

    v = app._view[0]
    cw = app.cv_a.winfo_width()
    ch = app.cv_a.winfo_height()
    dw, dh = v["disp"]
    bw, bh = v["base"]
    sx = bw / float(dw)
    sy = bh / float(dh)
    blkW, blkH = v["blk"]
    twant = int(round(blkW / sx))
    thwant = int(round(blkH / sy))
    print("画布 %dx%d  zoom %.2f  显示图 %dx%d  基准 %s  块 %s"
          % (cw, ch, app.zoom[0], dw, dh, (bw, bh), (blkW, blkH)))
    print("→ 本应显示的块尺寸 = (blkW/sx, blkH/sy) = (%d, %d)  "
          "放大倍数 up=%.3f" % (twant, thwant, max(twant / blkW,
                                                   thwant / blkH)))
    img = v.get("img")
    if img is not None:
        print("→ 精确帧实际画出来的尺寸 = (%d, %d)"
              % (img.width(), img.height()))
    print("→ 视口+缓冲 = %dx%d" % (int(cw * 1.2), int(ch * 1.2)))

    rng_x = max(0, dw - cw) // 2
    rng_y = max(0, dh - ch) // 2
    print("可拖 ±%d,±%d\n" % (rng_x, rng_y))

    app._pan_start(0, Ev(0, 10, 10))
    x0, y0 = app._drag[1], app._drag[2]
    worst = 1.0
    worst_ms = 0.0
    worst_step = -1
    for k in range(40):
        t = k / 39.0 * 2 - 1
        p = Ev(0, x0 + int(t * rng_x), y0 + int(t * rng_y))
        drain(app)
        t0 = time.perf_counter()
        app._pan_move(0, p)
        app.update()
        ms = (time.perf_counter() - t0) * 1000
        time.sleep(0.016)
        vv = app._view.get(0, {})
        item = vv.get("item")
        bb = app.cv_a.bbox(item) if item else None
        cov = 0.0
        drawn = None
        if bb:
            iw = min(bb[2], cw) - max(bb[0], 0)
            ih = min(bb[3], ch) - max(bb[1], 0)
            cov = max(0, iw) * max(0, ih) / float(cw * ch)
            drawn = (bb[2] - bb[0], bb[3] - bb[1])
        recs = drain(app)
        if cov < worst or ms > worst_ms:
            pass
        if cov < worst:
            worst, worst_step = cov, k + 1
        if ms > worst_ms:
            worst_ms = ms
        if cov < 0.95 or ms > 60:
            path_desc = []
            for r in recs:
                d = "%s %.1fms" % (r["name"], r["ms"])
                if r["name"] == "placeholder":
                    d += " want=%s got=%s" % (r["want"], r["got"])
                elif r["name"] == "fit_async":
                    d += " blk=%s want=%s gdip=%s" % (r["blk"], r["want"],
                                                      bool(r["need_gdip"]))
                elif r["name"] == "render_tile":
                    d += " zoom=%.2f precise=%s pan=%s" % (r["zoom"],
                                                           r["precise"],
                                                           r["pan"])
                    if r.get("stages"):
                        d += "\n          阶段 %s" % "  ".join(
                            "%s=%.1f" % (k, v) for k, v in r["stages"])
                path_desc.append(d)
            print("step%-3d 覆盖 %3.0f%% 画出的 %s 本应 (%d,%d) "
                  "本帧 %.1fms bbox=%s"
                  % (k + 1, cov * 100, drawn, twant, thwant, ms, bb))
            for d in path_desc:
                print("        · %s" % d)
    app._pan_end()
    settle(app)
    print("\n最差覆盖 %.0f%%（第 %d 步）  最慢一帧 %.1f ms"
          % (worst * 100, worst_step, worst_ms))
    app.destroy()


if __name__ == "__main__":
    main()
