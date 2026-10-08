# -*- coding: utf-8 -*-
"""界面自检：本进程内跑一次完整流程并抓图（不给鼠标留任何不确定性）。

比 `shot_exe2.py`（真去点按钮）稳得多 —— 那边会撞上「第一次点击被用来激活窗口」
「PyInstaller 单文件残留子进程」「工具提示窗口抢了 HWND」这些跟被测量对象
**毫无关系**的坑。exe 那份截图只需要证明「能起来、新按钮在」，剩下的界面
验收交给这里。

用法：python shot_ui.py <图片目录> <输出前缀>
"""
from __future__ import annotations

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import image_scout as ui            # noqa: E402
import scan                         # noqa: E402
import uikit                        # noqa: E402
import uishot                       # noqa: E402


def pump(app, seconds):
    t0 = time.time()
    while time.time() - t0 < seconds:
        app.update()
        time.sleep(0.005)


def main():
    folder = os.path.abspath(sys.argv[1])
    prefix = os.path.abspath(sys.argv[2])
    uikit.enable_dpi_awareness()

    app = ui.App([folder])
    app.store = scan.FingerStore(os.path.join(
        os.environ["TEMP"], "_shot_ui_cache.json"))
    pump(app, 2.0)
    print("列出 %d 张" % len(app.files))

    t0 = time.time()
    app.start_scan()
    while app.busy:
        app.update()
        if time.time() - t0 > 120:
            break
        time.sleep(0.005)
    pump(app, 3.0)                     # 等 _finish_view / 首帧渲染
    print("扫描 %.1fs -> %d 组" % (time.time() - t0, len(app.groups)))

    def shot(tag):
        p = "%s-%s.png" % (prefix, tag)
        w, h = uishot.grab(app.winfo_id(), p)
        print("   %dx%d -> %s" % (w, h, os.path.basename(p)))

    shot("1-groups")

    # ---- 成员表点选两张（v1.10）：组内 8 张 >= 3，会挂 A / B 徽章 ----
    order = app._member_order()
    if len(order) >= app.PICK_MIN:
        app._pick_pair_click(order[1], order)
        app._pick_pair_click(order[3], order)
        pump(app, 1.0)
        shot("1b-pick-two")

    app.toggle_sort()
    pump(app, 1.5)
    shot("2-sorted")
    app.toggle_sort()
    pump(app, 1.0)

    app.set_view("all")
    pump(app, 2.0)
    shot("3-all")

    # 滚到列表中段，验证下拉条在滚
    for _ in range(20):
        app.glist.yview_scroll(3 * app.glist._pitch(), "units")
        app.update()
    pump(app, 0.8)
    shot("4-scrolled")

    # ---- v1.11：工具栏上的「撤销 / 说明」两个新按钮 --------------------
    # ⚠️ 抓「工具栏那一条」必须用 box 裁：`uishot.grab` 会把子控件句柄
    #    换成根窗口（GA_ROOT），直接传 line2 的 id 抓出来还是整窗。
    app.update_idletasks()
    w, h = uishot.grab_widget(app, app.btn_help, "%s-6-toolbar.png" % prefix,
                              margin=uikit.sc(8), span=uikit.sc(300))
    print("   %dx%d -> %s-6-toolbar.png" % (w, h, os.path.basename(prefix)))

    # ---- v1.11：说明 / 快捷键弹窗 --------------------------------------
    app.show_help()
    pump(app, 1.5)
    hw = getattr(app, "_help_win", None)
    if hw is None:
        print("   ?? 说明弹窗没开起来")
    else:
        w, h = uishot.grab(hw.winfo_id(), "%s-7-help.png" % prefix)
        print("   %dx%d -> %s-7-help.png" % (w, h, os.path.basename(prefix)))
        app._close_help()
        pump(app, 0.6)

    # ---- v1.12：信息卡排版（分辨率+大小放大、独立一行） -----------------
    # ---- v1.13：画布下方那条 cap 不上了（画布一直延伸到信息卡） ---------
    # ⚠️ 信息卡只在看图（pair / 单图）下才填内容，扫描列表模式是空的。
    fs = list(app.files)[:2]
    if len(fs) >= 2:
        app.set_pair(fs[0], fs[1])
        pump(app, 4.0)                     # 等首帧 + NIQE 那行填完
        app.update_idletasks()
        shot("9-pair")                     # 整窗：看画布下方有没有那条小字
        w, h = uishot.grab_widget(app, app.info_card,
                                  "%s-8-info.png" % prefix,
                                  margin=uikit.sc(6))
        print("   %dx%d -> %s-8-info.png" % (w, h, os.path.basename(prefix)))

    # ---- 单独抓一条窄列表：下拉条只有 7px，整窗截图缩放后根本看不清 ----
    import tkinter as tk                     # noqa: E402
    top = tk.Toplevel(app)
    top.title("列表 / 下拉条")
    top.geometry("260x340+40+40")
    lst = uikit.NiceList(top, row_h=44, thumb=32, page=uikit.Palette.CARD,
                         size=9)
    lst.pack(fill="both", expand=True)
    lst.set_items([{"title": "第 %d 组 · %d 张" % (i + 1, 60 - i),
                    "sub": "同源 / 裁剪 0.98%d" % (i % 10),
                    "tone": "strong"} for i in range(50)])
    top.update()
    pump(app, 0.6)
    # 滚到中段，滑块才明显离开顶端
    for _ in range(12):
        lst.yview_scroll(3 * lst._pitch(), "units")
        top.update()
    pump(app, 0.6)
    w, h = uishot.grab(top.winfo_id(), "%s-5-scrollbar.png" % prefix)
    print("   %dx%d -> %s-5-scrollbar.png（滑块 %s）"
          % (w, h, os.path.basename(prefix), lst._sb))
    top.destroy()

    app.destroy()
    return 0


if __name__ == "__main__":
    sys.exit(main())
