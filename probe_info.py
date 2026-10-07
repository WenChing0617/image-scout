# -*- coding: utf-8 -*-
"""信息卡（左右两栏的图片信息）的布局判据。

主人 2026-10-07：「等清理完可以将分辨率和图片大小的字体放大，便于看到，
可以单独一行，其他信息单独一行，当然主要还是不要让信息窗口太大，
主要空间还是给展示窗口」

拆成三条可量的：
  1. 「分辨率 + 文件大小」**单独一行**、字号**明显大于**其他信息行；
  2. 「其他信息」（格式 / 时间 / 质量）在**另外的行**上
     ⚠️ v1.12 起「目录」那一行整个删了（主人：「最下面小信息可以去掉，
        占空间」）⇒ 每栏只剩 2 行，I5 的下限同步 3 -> 2。
  3. ⚠️ **信息卡不能因此变高** —— 高度是从「展示窗口」身上割下来的，
     所以第 3 条才是主人真正在意的（他两次说过「信息窗口占比太大」）。

用法：
    python probe_info.py            # 量数据 + 跑判据
"""
from __future__ import annotations

import os
import random
import re
import shutil
import sys
import time
import tkinter as tk
import tkinter.font as tkfont

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import image_scout as ui            # noqa: E402
import scan                         # noqa: E402
import thumbs                       # noqa: E402
import uikit                        # noqa: E402

D = os.path.join(os.environ.get("TEMP", "."), "ImageScout-info")
OK, BAD = [], []


def check(cond, label, extra=""):
    (OK if cond else BAD).append(label)
    print("   [%s] %s%s" % ("OK" if cond else "XX", label,
                            ("  " + extra) if extra else ""))


def make_photo(path, w, h, cols):
    buf = bytearray(w * h * 4)
    gw = gh = 8
    for y in range(h):
        by = (y * gh) // h
        rb = y * w * 4
        for x in range(w):
            c = cols[by * gw + ((x * gw) // w)]
            i = rb + x * 4
            buf[i], buf[i + 1], buf[i + 2], buf[i + 3] = c[2], c[1], c[0], 255
    with open(path, "wb") as f:
        f.write(thumbs.bgra_to_png(w, h, bytes(buf)))


def build_dir():
    """同基底 + 少量扰动 -> 同尺寸档内部一定成组（否则没有「组」可选）。"""
    shutil.rmtree(D, ignore_errors=True)
    os.makedirs(D, exist_ok=True)
    for ti, (w, h) in enumerate(((640, 480), (1600, 1200))):
        rnd = random.Random(500 + ti)
        base = [(rnd.randrange(40, 216), rnd.randrange(40, 216),
                 rnd.randrange(40, 216)) for _ in range(64)]
        for k in range(4):
            cols = list(base)
            r = random.Random(10 * ti + k)
            for _ in range(3):
                cols[r.randrange(64)] = (r.randrange(40, 216),
                                         r.randrange(40, 216),
                                         r.randrange(40, 216))
            # 名字长一点，顺便验「文件名那行不会把布局顶歪」
            make_photo(os.path.join(D, "相册-2026-秋游-%s-%d.png"
                                    % ("高清原图" if ti else "手机截图", k)),
                       w, h, cols)


def pump(app, sec):
    t0 = time.time()
    while time.time() - t0 < sec:
        app.update()
        time.sleep(0.004)


def rows_of(box):
    """`box` 里**直接 pack** 的 Label，按 pack 顺序（= 从上到下）。

    返回 [(文本, 字号, 控件)]。字号取 `actual("size")` —— `cget("font")`
    只会给你一个字体名（`TkDefaultFont` / 匿名规格串），比不了大小。
    """
    out = []
    for ch in box.pack_slaves():
        if not isinstance(ch, tk.Label):
            continue
        try:
            size = tkfont.Font(root=box, font=ch.cget("font")).actual("size")
        except Exception:                                    # noqa: BLE001
            size = -1
        out.append((ch.cget("text"), size, ch))
    return out


def boxes_of(app):
    """信息卡里左右两栏的容器（`_info_grid` 的每个格子）。"""
    return [c for c in app._info_grid.grid_slaves() if isinstance(c, tk.Frame)]


def main():
    uikit.enable_dpi_awareness()
    build_dir()
    app = ui.App([D])
    app.store = scan.FingerStore(os.path.join(
        os.environ["TEMP"], "_info_cache.json"))
    pump(app, 1.2)
    app.start_scan()
    t0 = time.time()
    while app.busy and time.time() - t0 < 90:
        app.update()
        time.sleep(0.005)
    pump(app, 1.5)
    print("扫出 %d 张 / %d 组" % (len(app.files), len(app.groups_raw)))
    if app.groups:
        app.select_group(0)
    pump(app, 1.2)

    boxes = boxes_of(app)
    check(bool(boxes), "I0 信息卡里有内容（前置条件）", "%d 栏" % len(boxes))
    if not boxes:
        app.destroy()
        return 1

    box = boxes[0]
    rows = rows_of(box)
    print("\n   ── 第 1 栏的行 ──")
    for i, (t, s, _c) in enumerate(rows):
        print("     [%d] size=%-3s %s" % (i, s, (t[:76] + "…") if len(t) > 76 else t))

    # 分类：哪一行是「分辨率 + 大小」、哪些是「其他信息」
    def is_pix(t):
        return ("MP" in t or "×" in t) and \
            any(u in t for u in (" B", " KB", " MB", " GB"))

    pix_i = [i for i, (t, _s, _c) in enumerate(rows) if is_pix(t)]
    check(len(pix_i) >= 1, "I1 「分辨率 + 文件大小」有独立的一行",
          "行号 %s" % pix_i)

    if pix_i:
        pi = pix_i[0]
        pix_size = rows[pi][1]
        others = [(i, s) for i, (_t, s, _c) in enumerate(rows) if i != pi]
        check(all(s < pix_size for _i, s in others),
              "I2 这一行的字号**大于**其他所有行",
              "它 %s / 其他 %s" % (pix_size, [s for _i, s in others]))
        check(pix_size >= 11, "I3 字号够大（>= 11）", "%s" % pix_size)
        # 「其他信息」在另外的行上：分辨率行里不该再混着格式/时间
        check("." in rows[pi][0] or "KB" in rows[pi][0],
              "I4 这一行确实带了文件大小",
              rows[pi][0][:50])
        check(len(rows) >= 2, "I5 其他信息占的行数 >= 1（没有全挤回一行）",
              "共 %d 行" % len(rows))
        # ⚠️⚠️ I5 只**数行数** —— 把「格式/时间/质量」又塞回分辨率行、同时
        #     再留一个空行，行数照样够，它照样绿（注入验证实测：
        #     `f1 = ["分辨率 " + pix, ...]` 这种回流它抓不到）。
        #     所以必须直接查**内容**：分辨率行里不许出现别的字段。
        rx_date = re.compile(r"\d{4}-\d{2}-\d{2}")
        check("质量" not in rows[pi][0] and not rx_date.search(rows[pi][0]),
              "I6 分辨率行里没有混进格式/时间/质量（其他信息确实另起一行）",
              rows[pi][0][:60])
        joined = " ".join(t for i, (t, _s, _c) in enumerate(rows) if i != pi)
        check("质量" in joined and rx_date.search(joined) is not None,
              "I7 时间 / 质量确实搬到了别的行上", joined[:70])
        # ⚠️ v1.12：主人明确要删的「目录」那一行，加判据钉住 ——
        #    否则以后很容易「顺手」加回来（它最长、最容易折行，
        #    正是把卡片顶高的那一条）。
        #    绝对路径的特征：盘符 + 分隔符（`C:\` / `D:/`）。
        rx_abs = re.compile(r"[A-Za-z]:[\\/]")
        check(not any(rx_abs.search(t) for t, _s, _c in rows),
              "I10 目录（绝对路径）那一行已被删掉",
              " / ".join(t[:26] for t, _s, _c in rows))

    # ---- 高度：这是主人最在意的那条 --------------------------------
    info_h = app.info_card.min_h
    cv_h = app.cv_a.winfo_height()
    win_h = app.winfo_height()
    print("\n   信息卡 min_h=%s  req=%s  画布高=%s  窗口高=%s"
          % (info_h, app.info_card.req_height(), cv_h, win_h))
    # ⚠️ 阈值别放太松。v1.12 拆行前是 162px、拆完 196px —— 卡在 220 才真的
    #    能拦住「下一版又不小心多塞一行」。放 300 等于没约束。
    check(info_h <= ui.S(220), "I8 信息卡高度没失控（<= 220 逻辑 px）",
          "%s" % info_h)
    share = cv_h / float(win_h or 1)
    check(share >= 0.45, "I9 展示画布仍占窗口高度的一半上下（>= 45%）",
          "%.0f%%" % (share * 100))

    app.destroy()
    print("\n== 结果：%d 通过 / %d 失败 ==" % (len(OK), len(BAD)))
    for b in BAD:
        print("   XX " + b)
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
