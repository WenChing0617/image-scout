# -*- coding: utf-8 -*-
"""信息卡空位探针：逐行量 reqheight，找出多出来的高度是谁的。

跑法：

    python probe_card.py

主人 2026-10-06 报：「看得到下面还有点空位，可以适当的调整」。

⚠️ 为什么必须**逐行**量：`req_height()` 只给一个总数
（`body.reqheight + 2*(pad+inset)`），光看总数不知道多出来的高度
藏在哪儿 —— 有可能是某一行的 Label 多折了一行，也有可能是 Card 的
inset/pad 算多了。逐行打印才能定位到具体控件。
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
import tkinter as tk              # noqa: E402
import uikit                       # noqa: E402

ROOT = os.path.join(os.environ.get("TEMP", "."), "ImageScout-card")


def make_photo(path, w=1080, h=1330, seed=5):
    """造一张「噪声 + 色块」的图：让 NIQE 给得出分，而不是「算不了」。"""
    import random
    rnd = random.Random(seed)
    buf = bytearray(w * h * 4)
    buf[:] = rnd.randbytes(w * h * 4)
    for i in range(3, len(buf), 4):
        buf[i] = 255
    with open(path, "wb") as f:
        f.write(thumbs.bgra_to_png(w, h, bytes(buf)))
    return path


def walk(w, depth=0):
    """深度优先列出所有子控件。"""
    out = [(depth, w)]
    for c in w.winfo_children():
        out.extend(walk(c, depth + 1))
    return out


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

    # 等 NIQE 算完（布局最终态）
    t0 = time.time()
    while time.time() - t0 < 30:
        app.update()
        if app.niqec.cached(a) is not None:
            break
        time.sleep(0.05)
    app.update()

    card = app.info_card
    card.fit_to_content()
    app.update()
    body = card.body
    print("=== 信息卡逐行 ===")
    print("min_h=%s  cget(height)=%s  实际高=%d"
          % (card.min_h, card.cget("height"), card.winfo_height()))
    print("body.reqheight=%d  card.req_height()=%d  (pad+inset 合计 %d)"
          % (body.winfo_reqheight(), card.req_height(),
             2 * (card._pad + card._inset)))
    print("body 实际高=%d" % body.winfo_height())

    tot = 0
    print("\n--- body 的直接子控件（pack 的那些，各自占一行）---")
    for d, w in walk(body):
        if d != 1:
            continue
        cls = w.winfo_class()
        try:
            rh = w.winfo_reqheight()
        except tk.TclError:
            rh = -1
        txt = ""
        try:
            if cls == "Label":
                txt = (w.cget("text") or "")[:44]
        except tk.TclError:
            pass
        tot += max(0, rh)
        print("  %-10s reqh=%3d  h=%3d  %s" % (cls, rh, w.winfo_height(),
                                               txt))
    print("  子控件 reqh 合计 = %d" % tot)

    print("\n--- 两列里各有什么 ---")
    for i, box in enumerate(app._info_grid.winfo_children()):
        print("  第 %d 列:" % (i + 1))
        s = 0
        for d, w in walk(box):
            if d != 1:
                continue
            cls = w.winfo_class()
            rh = w.winfo_reqheight()
            s += max(0, rh)
            txt = ""
            try:
                if cls == "Label":
                    txt = (w.cget("text") or "")[:50]
            except tk.TclError:
                pass
            print("    %-10s reqh=%3d  %s" % (cls, rh, txt))
        print("    小计 reqh = %d" % s)

    slack = card.winfo_height() - card.req_height()
    pad = 2 * (card._pad + card._inset)
    print("\n=== 结论 ===")
    print("空位 = 卡片高 %d - 内容请求高 %d = %d px"
          % (card.winfo_height(), card.req_height(), slack))
    print("（Card 内边距 pad+inset 合计 %d px，那是设计留白，**不算空位**）"
          % pad)
    print("body.reqheight=%d 逐行合计=%d  —— 两者相等说明内容没有多余空隙"
          % (body.winfo_reqheight(), tot))
    if slack > 4:
        print("⚠️ 卡片比内容高 %d px：这 %d px 是**真空位**（内边距只有 %d）"
              % (slack, slack, pad))
        print("   来源多半是 `_fit_card` 的「只增不减」把历史最大值钉住了"
              "（`_info_min_h`=%d）—— 内容变矮了它也不会缩回去。"
              % app._info_min_h)
    else:
        print("✅ 没有空位（卡片高度就是内容高度）")
    app.destroy()


if __name__ == "__main__":
    main()