# -*- coding: utf-8 -*-
"""左侧列表「虚拟化 + 自绘下拉条」探针。

主人 2026-10-07 两条反馈：
  1. 「加载 400 图片就会很慢很卡了」—— 原来 `redraw()` 无条件画全部行，
     400 项实测 **2001 个画布图元**。
  2. 「左边选图只能鼠标下滑，如果图片太多会很难查看最下面的分组，
     可以添加下拉条，方便快速下滑。」

## 判据（**先在注入版上验证能变红，再认全绿**）

P1  图元数与项数**解耦**：400 项时图元数 <= (可见行数 + 4) * 8 + 2。
    注入法：把 `_visible_range` 改成 `return (0, len(self.items))`
    -> 图元数回到 2000+ -> P1 必须变红。
P2  滚到底后**最后一行真的画出来了**（不是「图元少」但内容空掉）。
P3  内容装得下时**不画**滑块（`_sb is None`）；装不下时**画**。
P4  拖滑块到底 -> `yview` 到底部（`_sb[1]` 贴住视口底边）。
P5  点轨道 -> 翻一屏（不是翻 1px，也不是直接跳底）。
P6  `select()` 一个远处的项 -> `see()` 把它滚进可见范围，且那一行**被画出来**。
    （虚拟化最典型的回归：选中项在视口外时什么都不显示。）
P7  `_redraw_rows([视口外的行])` **不会**在视口外画东西 —— 图元数不变。
P8  重入保护有效：设 scrollregion 不会触发无限递归（`sys.setrecursionlimit`
    调低到 200，若递归就是 `RecursionError`）。
P9  命中测试仍准：`_at()` 对可见行的判定跟行 y 一致
    （虚拟化改了 `canvasy` 的用法，容易连带打坏点选）。

用法：python probe_list.py
"""
from __future__ import annotations

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import tkinter as tk                # noqa: E402

import uikit                        # noqa: E402

FAIL = []
OK = []


def check(cond, label, extra=""):
    (OK if cond else FAIL).append(label)
    print("   %s %s%s" % ("[OK]" if cond else "[XX]", label,
                          ("  " + extra) if extra else ""))


class Ev(object):
    def __init__(self, x=0, y=0, delta=0):
        self.x, self.y, self.delta = x, y, delta


def main():
    uikit.enable_dpi_awareness()
    root = tk.Tk()
    root.geometry("%dx%d" % (uikit.sc(300), uikit.sc(420)))
    root.update()

    lst = uikit.NiceList(root, row_h=54)
    lst.pack(fill="both", expand=True)
    root.update()

    N = 400
    lst.set_items([{"title": "item %03d" % i, "sub": "sub %03d" % i}
                   for i in range(N)])
    root.update()

    vis_rows = lst._visible_range()
    n_vis = max(1, vis_rows[1] - vis_rows[0])
    items = len(lst.find_all())
    H = lst.winfo_height()

    # ⚠️⚠️ 上限**必须用视口高度自己算**，不能拿 `_visible_range()` 的结果算。
    #    第一版就是这么写的 —— 结果注入「返回全部行」之后，`n_vis` 也跟着
    #    变成 400，上限跟着放大到 3234，801 个图元反而判成通过。
    #    **判据被被测对象污染 = 判据失效**（这是探针的第 N 号老坑了）。
    n_fit = max(1, H // lst._pitch())
    cap = (n_fit + 4) * 8 + 2

    print("\n-- 基本量 --")
    print("   视口高 %d px，行距 %d px，可见行 %s（%d 行）"
          % (H, lst._pitch(), vis_rows, n_vis))
    print("   画布图元 %d 个（%d 项）" % (items, N))

    # ---- P1 图元数与项数解耦 ----------------------------------------
    # 每行最多 6 个图元（背景 + 预览 + 徽章底 + 徽章字 + 标题 + 副标题），
    # 再多留 4 行缓冲，另加 1 个滑块图元，取 8 做余量。
    check(items <= cap, "P1 图元数与项数解耦（%d <= %d）" % (items, cap),
          "400 项 / 视口容得下 %d 行" % n_fit)

    # ---- P3 装不下 -> 画滑块 ----------------------------------------
    check(lst._sb is not None, "P3a 内容装不下时画出滑块",
          "滑块 %s" % (lst._sb,))

    # ---- P2 滚到底，最后一行画出来 ----------------------------------
    lst.yview_moveto(1.0)
    root.update()
    last_tag = lst._row_tag(N - 1)
    drawn_last = bool(lst.find_withtag(last_tag))
    rng = lst._visible_range()
    check(drawn_last, "P2 滚到底后最后一行被画出来",
          "可见范围 %s，最后一行标签 %s 有 %d 个图元"
          % (rng, last_tag, len(lst.find_withtag(last_tag))))
    check(len(lst.find_all()) <= cap,
          "P2b 滚到底图元数仍在界内（%d <= %d）"
          % (len(lst.find_all()), cap))

    # ---- P4 拖滑块到底 ----------------------------------------------
    lst.yview_moveto(0.0)
    root.update()
    geo = lst._sb
    ev = Ev(x=lst._sb_x() + 2, y=geo[0] + 2)
    lst._click(ev)                      # 抓住滑块（记录偏移）
    check(lst._sb_drag is not None, "P4a 点滑块进入拖动状态")
    lst._drag(Ev(x=ev.x, y=10 ** 6))    # 拖到远超底部
    root.update()
    top = lst.canvasy(0)
    bottom = top + lst.winfo_height()
    content = lst._content_h()
    check(bottom >= content - 2,
          "P4b 拖滑块到底 -> 视口贴住内容底",
          "视口底 %.0f vs 内容高 %.0f" % (bottom, content))
    lst._release()

    # ---- P5 点轨道翻一屏 --------------------------------------------
    lst.yview_moveto(0.0)
    root.update()
    t0 = lst.canvasy(0)
    geo = lst._sb
    before = lst.yview()[0]
    lst._click(Ev(x=lst._sb_x() + 2, y=min(H - 2, geo[1] + 40)))
    root.update()
    after = lst.yview()[0]
    moved = after - before
    # 「一屏」= H / 内容高
    one_screen = H / float(lst._content_h())
    check(moved > one_screen * 0.4,
          "P5 点轨道翻一屏（不是 1px）",
          "位移 %.4f，一屏 %.4f" % (moved, one_screen))

    # ---- P6 select 远处项 -> see 滚进来 + 画出来 ---------------------
    lst.yview_moveto(0.0)
    root.update()
    target = N - 5
    lst.select(target, notify=False)
    root.update()
    rng = lst._visible_range()
    drawn = bool(lst.find_withtag(lst._row_tag(target)))
    check(drawn and rng[0] <= target < rng[1],
          "P6 select 远处项后它被滚进可见范围并画出",
          "目标 %d，可见 %s" % (target, rng))

    # ---- P7 视口外的 _redraw_rows 不画东西 --------------------------
    lst.yview_moveto(0.0)
    root.update()
    n_before = len(lst.find_all())
    lst._redraw_rows([0, N - 1])        # 0 可见、N-1 不可见
    root.update()
    n_after = len(lst.find_all())
    check(lst.find_withtag(lst._row_tag(N - 1)) == (),
          "P7a 视口外的行不会被 _redraw_rows 画出来")
    check(abs(n_after - n_before) <= 6,
          "P7b _redraw_rows 后图元数基本不变",
          "%d -> %d" % (n_before, n_after))

    # ---- P8 重入保护（不会无限递归）---------------------------------
    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(200)
    err = None
    try:
        lst.set_items([{"title": "x%d" % i} for i in range(N)])
        root.update()
        lst.yview_moveto(0.5)
        root.update()
    except RecursionError as e:
        err = e
    finally:
        sys.setrecursionlimit(old_limit)
    check(err is None, "P8 scrollregion 变更不触发无限递归",
          ("%s" % err) if err else "")

    # ---- P9 命中测试仍准 --------------------------------------------
    lst.yview_moveto(0.0)
    root.update()
    p = lst._pitch()
    mid = lst.row_h // 2
    bad = []
    for k in (0, 1, 3):
        got = lst._at(Ev(y=int(k * p + mid)))
        if got != k:
            bad.append((k, got))
    check(not bad, "P9a 可见行的点选命中正确", "异常 %s" % (bad,) if bad else "")

    # 滚到中间后再测一次（虚拟化改了 canvasy 的读法，这里最容易错）
    lst.yview_moveto(0.42)
    root.update()
    top = lst.canvasy(0)
    p0 = int(top // p)
    want = p0 + 2
    y_px = int(want * p + mid - top)
    got = lst._at(Ev(y=y_px))
    check(got == want, "P9b 滚动后命中仍准确",
          "屏幕 y=%d 期望第 %d 行，得到 %d" % (y_px, want, got))

    # ---- P11 滑块**真的画在屏幕上**（不是只有交互逻辑对）-------------
    # ⚠️⚠️ 这条是补的。第一版 `_draw_thumb` 把视口坐标当画布坐标用，
    #     一滚动滑块就被卷到视口外一千多像素 —— 而当时 P1~P10 **全绿**：
    #     它们只验「按住拖动后 `yview` 对不对」，没验「滑块画在哪」。
    #     是截图时肉眼看不滑块才发现的。交互正确 ≠ 画出来了。
    print("\n-- 滑块可见性（滚动到中段）--")
    lst.yview_moveto(0.0)
    root.update()
    top0 = lst.canvasy(0)
    sb0 = lst._sb
    check(sb0 is not None, "P11a 顶部时滑块存在")
    lst.yview_moveto(0.45)
    root.update()
    sb = lst._sb
    top = lst.canvasy(0)
    ids = lst.find_withtag("sb")
    check(len(ids) == 1, "P11b 画布上恰好一个滑块图元", "实际 %d 个" % len(ids))
    if ids:
        x0, y0 = lst.coords(ids[0])[:2]
        # 画布坐标 -> 视口坐标
        wy = y0 - top
        print("   滚动到 45%%：top=%.0f，滑块画布 y=%.0f -> 视口 y=%.0f"
              "（视口高 %d）" % (top, y0, wy, H))
        check(-1 <= wy <= H, "P11c 滑块落在可见视口内（视口 y=%.0f）" % wy)
        check(abs(wy - sb[0]) <= 1,
              "P11d 滑块的绘制位置 = `_thumb_geo()` 给的位置（差 %.0f）"
              % abs(wy - sb[0]))
    check(top > 100 and sb0 != sb, "P11e 确实滚动了（滑块位置该变）")

    # ---- P12 选中行不许盖住滑块（v1.10，主人截图报的）-----------------
    # ⚠️⚠️ 根因是**两条重绘路径不一致**：
    #     · `_repaint()`（全量）—— 先画所有行、**最后**画滑块 -> 正确
    #     · `_redraw_rows()`（局部，`select()` 走这条）—— 新画的行
    #       `create_image` 在后 -> **盖在滑块上**
    #     所以这个 bug **只在"点选"时才出现**：截图里选中行的高亮矩形
    #     正好把滑块截断。而 P1~P11 **全绿** —— 没有一条验层级。
    #     ⇒ 又一次「全量正确 ≠ 局部正确」，和 P11 那次同一类。
    print("\n-- 选中行 vs 滑块（点选后的层级与让位）--")
    lst.set_items([{"title": "row %03d" % i, "sub": "sub",
                    "badge": "体积小", "tone": "strong"}
                   for i in range(N)])
    lst.yview_moveto(0.35)
    root.update()
    # ⚠️⚠️ **必须选一个「已经在视口里」的行**。第一版选的是第 8 行，
    #     它早就被滚出视口 -> `select()` 里的 `see()` 触发滚动 ->
    #     `_on_yscroll` 发现可见范围变了 -> 走 **`_repaint()` 全量重绘**
    #     -> 滑块最后画 -> 于是**绕过 bug 路径、注入后照样绿**。
    #     注入验证当场把这个坑抓出来了：判据跑的场景不是它声称的场景。
    i0, i1 = lst._vis
    pick = (i0 + i1) // 2
    lst.select(pick)                   # 视口内 -> 只走 `_redraw_rows`
    root.update()
    print("   选中第 %d 行（可见范围 %d~%d，全程只走局部重画）"
          % (pick, i0, i1))
    order = list(lst.find_all())
    sb_ids = [i for i in order if "sb" in lst.gettags(i)]
    row_ids = [i for i in order
               if any(str(t).startswith("row") for t in lst.gettags(i))]
    check(len(sb_ids) == 1, "P12a 点选后滑块仍在", "实际 %d 个" % len(sb_ids))
    if sb_ids and row_ids:
        z_sb = order.index(sb_ids[0])
        z_row = max(order.index(i) for i in row_ids)
        check(z_sb > z_row,
              "P12b 滑块在行的**上层**（z=%d，最高行 z=%d）" % (z_sb, z_row))
        sbx = lst._sb_x()
        over = []
        for i in row_ids:
            b = lst.bbox(i)
            if b and b[2] > sbx + 1:
                over.append((i, lst.gettags(i), b[2]))
        check(not over,
              "P12c 行内图元（背景/徽章/文字）都不越过滑块左缘 x=%d" % sbx,
              "越界 %d 个，首个 tags=%s 右缘=%s"
              % (len(over), over[0][1], over[0][2]) if over else "")

    # ---- P3b 内容装得下 -> 不画滑块 ---------------------------------
    lst.set_items([{"title": "a"}, {"title": "b"}])
    root.update()
    check(lst._sb is None, "P3b 内容装得下时不画滑块", "内容 %d / 视口 %d"
          % (lst._content_h(), H))

    # ---- P10 滚动性能 -------------------------------------------------
    lst.set_items([{"title": "item %03d" % i} for i in range(N)])
    root.update()
    t0 = time.perf_counter()
    worst = 0.0
    for _ in range(60):
        t1 = time.perf_counter()
        lst._wheel(Ev(delta=-120))
        root.update()
        worst = max(worst, (time.perf_counter() - t1) * 1000)
    total = (time.perf_counter() - t0) * 1000
    check(worst <= 25.0, "P10 滚轮单次 <= 25ms",
          "60 次合计 %.0fms，单次最长 %.1fms" % (total, worst))

    root.destroy()

    print("\n== 结果：%d 通过 / %d 失败 ==" % (len(OK), len(FAIL)))
    for f in FAIL:
        print("   FAIL %s" % f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
