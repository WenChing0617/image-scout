# -*- coding: utf-8 -*-
"""验证 test_ui 里新加的「图元尺寸精确」断言**真的抓得住已知 bug**。

跑法：

    python probe_size_guard.py

⚠️⚠️ 判据本身必须先验证能抓住已知 bug，否则测试只是打印数字的机器
（这是本项目的反复教训：判据/测试图自己有问题、自己骗自己，都踩过）。

做法：把 GDI+ 插值**打掉**（`_gdip_scale_argb2` 返回 None，逼代码退回
「按块原尺寸显示」），也就是 v1.6 修复前的行为。然后真跑一遍 test_ui 的
大图缩放 + 拖动两段，看新增的两条尺寸断言会不会 FAIL。

  - `zoom_up_drawn` > 2px  -> 逐档尺寸断言抓到了
  - `worst_size_err` > 2px -> 拖动尺寸断言抓到了
"""
from __future__ import annotations

import io
import os
import sys
import time
import contextlib

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import image_scout as ui            # noqa: E402
import test_ui                      # noqa: E402
import thumbs                      # noqa: E402
import uikit                       # noqa: E402
import winimg                      # noqa: E402


def break_gdip():
    """把 GDI+ 插值打掉 —— 复现 v1.6 修复前的「按块原尺寸显示」。"""
    def dead(*a, **kw):
        return None
    winimg._gdip_scale_argb2 = dead
    winimg._gdip_scale_argb = dead


def main():
    uikit.enable_dpi_awareness()
    break_gdip()
    print("已打掉 GDI+ 插值（逼代码退回「按块原尺寸显示」）\n")

    root_dir = test_ui.SAMPLE_ROOT
    test_ui.reset_sample_root(root_dir)
    import makeset
    makeset.build_set(root_dir)

    app = ui.App([root_dir])
    app.update()
    app.start_scan()
    t0 = time.time()
    while time.time() - t0 < 300:
        app.update()
        if not app.busy and app.files:
            break
        time.sleep(0.01)

    class Ev(object):
        def __init__(self, d=0, x=10, y=10):
            self.delta, self.x, self.y = d, x, y

    def warm_idle():
        return ((not getattr(app, "_prewarm_q", []))
                and not getattr(app, "_prewarm_busy", False))

    t0 = time.time()
    while time.time() - t0 < 180:
        app.update()
        if warm_idle():
            break
        time.sleep(0.01)
    test_ui.settle_render(app)

    # ---- 滚 6 档，量每一档的图元尺寸误差 ----
    print("=== 滚轮 6 档（逐档图元尺寸）===")
    bad = []
    for i in range(6):
        app._on_wheel(0, Ev(120))
        app.update()
        test_ui.settle_render(app)
        v = app._view.get(0, {})
        img = v.get("img")
        want = v.get("want")
        if img is None or not want:
            continue
        got = (img.width(), img.height())
        # 口径：量 `want - img`（正数 = 小了那么多像素 = 露白量）。
        # 画大了（负数）是安全的 —— `up<1` 那一档走的就是按原尺寸画。
        d = max(want[0] - got[0], want[1] - got[1])
        bad.append((app.zoom[0], d))
        print("  zoom %5.2f  画出 %-12s 应有 %-12s 少了 %d px %s"
              % (app.zoom[0], got, want, d, "<== 抓到了" if d > 2 else ""))

    # ---- 拖动，量覆盖率 ----
    print("\n=== 拖动（覆盖率）===")
    app.reset_zoom()
    app.update()
    test_ui.settle_render(app)
    for _ in range(6):
        app._on_wheel(0, Ev(120))
    app.update()
    test_ui.settle_render(app)
    app.render_all(precise=True, only=0)
    test_ui.settle_render(app)
    v = app._view[0]
    cw = app.cv_a.winfo_width()
    ch = app.cv_a.winfo_height()
    dw, dh = v["disp"]
    rng_x = max(0, dw - cw) // 2
    rng_y = max(0, dh - ch) // 2
    print("  画布 %dx%d  可拖 ±%d,±%d  zoom %.2f"
          % (cw, ch, rng_x, rng_y, app.zoom[0]))

    def cover():
        vv = app._view.get(0)
        if not vv or not vv.get("item"):
            return 0.0
        bb = app.cv_a.bbox(vv["item"])
        if not bb:
            return 0.0
        iw = min(bb[2], cw) - max(bb[0], 0)
        ih = min(bb[3], ch) - max(bb[1], 0)
        return max(0, iw) * max(0, ih) / float(cw * ch)

    app._pan_start(0, Ev(0, 10, 10))
    x0, y0 = app._drag[1], app._drag[2]
    worst = 1.0
    for k in range(40):
        t = k / 39.0 * 2 - 1
        app._pan_move(0, Ev(0, x0 + int(t * rng_x), y0 + int(t * rng_y)))
        app.update()
        time.sleep(0.016)
        worst = min(worst, cover())
    app._pan_end()
    print("  最差覆盖 %.0f%%" % (worst * 100))

    app.destroy()

    print("\n=== 结论 ===")
    nbad = sum(1 for _z, d in bad if d > 2)
    print("逐档尺寸断言：%d/%d 档会 FAIL（差 >2px）" % (nbad, len(bad)))
    print("覆盖率断言：最差 %.0f%%（门槛 95%%）-> %s"
          % (worst * 100, "会 FAIL" if worst < 0.95 else "不会 FAIL"))
    ok = nbad > 0 and worst < 0.95
    print("\n%s" % ("✅ 两条新断言都能抓住已知 bug"
                    if ok else "❌ 有断言抓不住 —— 门槛或口径有问题"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
