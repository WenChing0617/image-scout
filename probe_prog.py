# -*- coding: utf-8 -*-
"""`_progress` 尾部耗时归因探针。

## 已量到的事实（probe_load.py）

400 张图：`_progress` 被调 447 次、**合计 7.7 秒**。
拆开看 `pb["value"]=` 中位 0.12ms / 最大 78.6ms，
`stat.configure(text=)` 中位 0.48ms / 最大 64.1ms。

**中位极小、最大极大** —— 这个形状排除了「控件本身慢」
（那样中位就该是几十毫秒）。剩下两个可能：

  A. 冷启动：前几次慢（字体度量缓存、ttk 主题元素首次创建），
     之后都很快。 -> 那总量应该是「几毫秒 × 几次」，凑不出 7.7 秒。
  B. 偶发撞上**整窗 relayout**：状态文字宽度一变，
     `tk.Label` 的请求尺寸就变，Tk 的 pack 传播到 toplevel，
     整窗重新分配空间 -> 图像画布收到 `<Configure>` -> `render_all`。

## 本探针怎么判 A / B

  1. 打印 `pb` / `stat` 的分位数（p50/p90/p99/max）与「慢调用（>5ms）的序号」。
     · 若是 A：慢的集中在前几次（序号 0~10）。
     · 若是 B：慢的**散布**在全程。
  2. 全程统计 toplevel 的宽度变化次数、toplevel `<Configure>` 次数、
     图像画布 `<Configure>` 次数。
     · 若是 B：这三个数会跟「慢调用次数」同量级。
  3. 把状态文字长度和耗时对照：若 B 成立，**文字长度变大那次**明显更慢。

用法：python probe_prog.py [张数]
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
import thumbs                       # noqa: E402
import uikit                        # noqa: E402

ROOT = os.path.join(os.environ.get("TEMP", "."), "ImageScout-prog")


def make_photo(path, w, h, seed):
    import random
    rnd = random.Random(seed)
    buf = bytearray(w * h * 4)
    gw, gh = 8, 8
    cols = [(rnd.randrange(40, 216), rnd.randrange(40, 216),
             rnd.randrange(40, 216)) for _ in range(gw * gh)]
    for y in range(h):
        by = (y * gh) // h
        rb = y * w * 4
        for x in range(w):
            c = cols[by * gw + ((x * gw) // w)]
            i = rb + x * 4
            buf[i], buf[i + 1], buf[i + 2], buf[i + 3] = c[2], c[1], c[0], 255
    with open(path, "wb") as f:
        f.write(thumbs.bgra_to_png(w, h, bytes(buf)))


def pct(v, q):
    if not v:
        return 0.0
    s = sorted(v)
    return s[min(len(s) - 1, int(len(s) * q))]


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 400
    uikit.enable_dpi_awareness()
    if not (os.path.isdir(ROOT) and len(os.listdir(ROOT)) >= n):
        shutil.rmtree(ROOT, ignore_errors=True)
        os.makedirs(ROOT, exist_ok=True)
        for i in range(n):
            make_photo(os.path.join(ROOT, "p%04d.png" % i), 800, 600, i // 8)
    app = ui.App([ROOT])
    app.update()

    # ---- 计数器：整窗/画布的 Configure -------------------------------
    CNT = {"win_cfg": 0, "root_w": [], "cv_cfg": 0}
    app.bind("<Configure>", lambda e: (CNT.__setitem__(
        "win_cfg", CNT["win_cfg"] + 1), CNT["root_w"].append(e.width)),
        add="+")
    cv = getattr(app, "cv", None) or getattr(app, "canvas", None)
    if cv is not None:
        cv.bind("<Configure>", lambda e: CNT.__setitem__(
            "cv_cfg", CNT["cv_cfg"] + 1), add="+")

    SPLIT = {"pb": [], "stat": [], "len": []}
    t_prog0 = {"t": 0.0}

    def prog(frac, text):
        t1 = time.perf_counter()
        app.pb["value"] = max(0.0, min(1.0, frac)) * 100
        t2 = time.perf_counter()
        app.stat.configure(text=text)
        t3 = time.perf_counter()
        SPLIT["pb"].append((t2 - t1) * 1000)
        SPLIT["stat"].append((t3 - t2) * 1000)
        SPLIT["len"].append(len(text))

    app._progress = prog
    app.start_scan()
    t0 = time.perf_counter()
    while True:
        app.update()
        if not app.busy and app.files:
            break
        if time.perf_counter() - t0 > 600:
            break
        time.sleep(0.005)
    wall = time.perf_counter() - t0

    print("扫描全程 %.1fs，_progress 调用 %d 次" % (wall, len(SPLIT["pb"])))
    for k in ("pb", "stat"):
        v = SPLIT[k]
        slow = [(i, round(x, 1)) for i, x in enumerate(v) if x > 5.0]
        print("  %-5s p50=%5.2f p90=%6.2f p99=%7.2f max=%7.2f  合计 %7.1fms"
              "  >5ms 共 %d 次"
              % (k, pct(v, .5), pct(v, .9), pct(v, .99),
                 max(v) if v else 0, sum(v), len(slow)))
        print("        慢调用的序号（前 25 个）：%s"
              % [i for i, _ in slow[:25]])
        print("        慢调用占总耗时：%.1fms"
              % sum(x for _, x in slow))

    print("\n整窗 / 画布 Configure 统计：")
    print("  toplevel <Configure> 次数 = %d" % CNT["win_cfg"])
    print("  图像画布 <Configure> 次数 = %d" % CNT["cv_cfg"])
    ws = CNT["root_w"]
    if ws:
        ch = sum(1 for a, b in zip(ws, ws[1:]) if a != b)
        print("  窗口宽度变化次数 = %d（%d -> %d，范围 %d~%d）"
              % (ch, ws[0], ws[-1], min(ws), max(ws)))

    # 文字长度 vs 耗时：相关的话说明是「宽度变 -> relayout」
    print("\n按「文字长度是否变化」分组：")
    both = list(zip(SPLIT["len"], SPLIT["stat"]))
    chg = [ms for i, (ln, ms) in enumerate(both)
           if i and ln != both[i - 1][0]]
    same = [ms for i, (ln, ms) in enumerate(both)
            if i and ln == both[i - 1][0]]
    print("  长度变化后那次：n=%d 平均 %.2fms 最大 %.2fms"
          % (len(chg), sum(chg) / max(1, len(chg)), max(chg or [0])))
    print("  长度没变那次：  n=%d 平均 %.2fms 最大 %.2fms"
          % (len(same), sum(same) / max(1, len(same)), max(same or [0])))

    app.destroy()


if __name__ == "__main__":
    main()
