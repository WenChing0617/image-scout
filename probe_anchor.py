# -*- coding: utf-8 -*-
"""锚点缩放探针：验证「滚轮缩放后鼠标底下那点还在原地」。

跑法：

    python probe_anchor.py

主人 2026-10-06 报：「拖拽到一个位置，想要放大，他又会重新回到画面中间」。

根因（我自己写出来的）：`_zoom_anchor()` 里缩放不变量写成了乘法。
图元画在 `ox + vx0`（显示图坐标），而显示尺寸 `disp_w ∝ zoom`，所以
「鼠标底下那点」的**基准像素**坐标是 `(mx - ox)/zoom` —— 必须**除以**
zoom。我第一版写成 `(mx - ox) * cur`，每滚一格误差乘 `cur²`，连滚 6 格
把平移推到 **129 万像素**（视口才 1515 宽），整块图飞出天边。

⚠️ 判据怎么才算「抓住 bug」：直接把 `_zoom_anchor` 的公式抄一份当
**正确参考**（`ref_anchor`），跟产品实算值逐档比。参考实现只依赖
数学定义、不依赖产品代码，所以它不会跟产品一起错 ——
这跟「拿产品自己的输出当判据」是两种东西。
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

ROOT = os.path.join(os.environ.get("TEMP", "."), "ImageScout-anchor")


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


def ref_anchor(cw, ch, wh, fit, cur, pan, z, mx, my):
    """**独立参考实现**：只按坐标定义算，不复用产品任何代码。

    坐标定义（来自 `_render_tile`）：
        fit         = min(box_w/wh_w, box_h/wh_h)     「适应窗口」系数，常数
        disp_w(zoom) = round(wh_w * fit * zoom)        显示图宽（高同理）
        ox(zoom, pan) = (cw - disp_w(zoom))//2 + pan   图元左缘的画布坐标
        显示坐标 vx 画在画布 ox + vx

    ⚠️⚠️ **不变量是「显示图坐标」**（不是基准像素坐标 —— 我第一版
    写成基准像素，除以了 `fit*cur`，于是凭空差一个 `fit≈0.36` 的倍数，
    量出 ±2000px 的假偏差，判据自己先错了）。
    理由：显示图是原图的等比放大，所以**同一个原图位置**的显示坐标
    满足 `vx' = vx * z/cur`。于是
        mx = ox + vx = ox' + vx*z/cur
    →    pan' = mx - vx*z/cur - (cw - disp_w(z))//2
    """
    def dw_of(zoom):
        return int(round(wh[0] * fit * zoom))

    def dh_of(zoom):
        return int(round(wh[1] * fit * zoom))

    ox_cur = (cw - dw_of(cur)) // 2 + pan[0]
    oy_cur = (ch - dh_of(cur)) // 2 + pan[1]
    vx = mx - ox_cur
    vy = my - oy_cur
    nox = (cw - dw_of(z)) // 2
    noy = (ch - dh_of(z)) // 2
    return (int(round(mx - vx * z / cur - nox)),
            int(round(my - vy * z / cur - noy)))


def main():
    uikit.enable_dpi_awareness()
    shutil.rmtree(ROOT, ignore_errors=True)
    os.makedirs(ROOT, exist_ok=True)
    a = make_photo(os.path.join(ROOT, "photo_a.png"))

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

    for _ in range(2):
        t0 = time.time()
        while time.time() - t0 < 180:
            app.update()
            if not getattr(app, "_prewarm_q", []) \
                    and not getattr(app, "_prewarm_busy", False):
                break
            time.sleep(0.01)

    class Ev(object):
        def __init__(self, d=0, x=10, y=10):
            self.delta, self.x, self.y = d, x, y

    def settle():
        t0 = time.time()
        while time.time() - t0 < 8:
            app.update()
            if not app._fit_pend:
                break
            time.sleep(0.02)
        app.render_all(precise=True, only=0)
        t0 = time.time()
        while time.time() - t0 < 8:
            app.update()
            if not app._fit_pend:
                break
            time.sleep(0.02)

    def fit_of():
        """当前这一帧的 `fit`（`disp_w = wh_w * fit * zoom` 里的那个常数 fit）。

        ⚠️ **不能直接用 `disp_w / wh_w`** —— 那是 `fit * zoom`，每滚一格
        就变 1.25 倍（我第一版就这么写的，结果参考实现算出 ±2000px 的
        「偏差」，全是判据自己的错，不是产品的错）。要除掉 zoom。
        """
        v = app._view.get(0) or {}
        disp = v.get("disp")
        if not disp or not wh:
            return None
        return disp[0] / float(wh[0] * app.zoom[0])

    cw = app.cv_a.winfo_width()
    ch = app.cv_a.winfo_height()
    wh = ui.META.of(a)["wh"]
    print("画布 %dx%d  原图 %s" % (cw, ch, wh))

    # ⚠️ 鼠标点选**偏离中心**的位置 —— 正中心的话锚点算错也算不出来
    #    （中心处 `mx - ox ≈ disp_w/2`，误差容易被吸收掉）。
    tests = [(cw // 4, ch // 4), (cw - cw // 5, ch - ch // 5),
             (cw // 2, 10), (10, ch // 2)]
    ok_all = True
    for (mx, my) in tests:
        print("\n--- 鼠标在 (%d, %d) ---" % (mx, my))
        app.reset_zoom()
        app.update()
        settle()
        # ⚠️⚠️ **必须先放大到 >1 才能拖**（`_pan_move` 里
        #    `zoom <= 1.001` 直接 return，zoom=1 时图是「适应窗口」、
        #    本来就铺满，没有可拖的地方）。主人报错的前提是
        #    「拖到某个位置」，那就得先有非零 pan —— 第一版探针
        #    在 zoom=1 上拖，量到的 pan 恒为 (0,0)，等于没测这个场景。
        for _ in range(2):
            app._on_wheel(0, Ev(120, cw // 2, ch // 2))
            app.update()
            settle()
        app._pan_start(0, Ev(0, 10, 10))
        app._pan_move(0, Ev(0, 10 + 140, 10 + 70))
        app._pan_end()
        app.update()
        settle()
        pan0 = tuple(app.pan[0])
        fit = fit_of()
        print("  起点 pan=%s zoom=%.2f fit=%.5f"
              % (pan0, app.zoom[0], fit or 0))
        if pan0 == (0, 0):
            print("  ⚠️ pan 还是 (0,0)，这个点没测到「拖过之后」的场景")
            ok_all = False

        worst = 0
        for k in range(6):
            cur = app.zoom[0]
            z = min(ui.ZOOM_MAX, cur * ui.ZOOM_STEP)
            got = app._zoom_anchor(0, mx, my, cur, z)
            want = ref_anchor(cw, ch, wh, fit, cur, pan0, z, mx, my)
            err = (abs(got[0] - want[0]), abs(got[1] - want[1]))
            worst = max(worst, max(err))
            print("    %.2f -> %.2f  产品=(%d,%d) 参考=(%d,%d) 差=%d,%d"
                  % (cur, z, got[0], got[1], want[0], want[1], err[0], err[1]))
            # 真滚一格，让产品走完整路径
            app._on_wheel(0, Ev(120, mx, my))
            app.update()
            settle()
            pan0 = tuple(app.pan[0])
            f2 = fit_of()
            if f2 and fit:
                if abs(f2 - fit) > 1e-6:
                    print("      ⚠️ fit 变了 %.5f -> %.5f" % (fit, f2))
                fit = f2
        # 判据：6 档累计偏差必须 < 2px（1px 是取整误差）
        good = worst <= 2
        ok_all = ok_all and good
        print("  最大偏差 %d px  ->  %s" % (worst, "✅" if good else "❌"))

    print("\n=== 汇总 ===")
    print("全部锚点：%s" % ("✅ 全部 4 个鼠标位置都跟手（<=2px）"
                        if ok_all else "❌ 有位置跟手不住"))
    app.destroy()
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())