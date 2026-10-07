# -*- coding: utf-8 -*-
"""加载性能探针：400 张图的「扫描 -> 分组 -> 填列表 -> 滚动」各段耗时。

主人 2026-10-07 反馈：「加载 400 图片就会很慢很卡了，加载方面是否能够优化」。

## 怀疑点（**先量再改，不靠猜**）

1. `NiceList.redraw()` **无条件画全部行** —— 400 项就是 400 × 约 6 个
   画布图元 = 2400+ 个。而屏幕只显示得下十几行，其余全是白画的。
   每次 `set_items` / `_redraw_rows` 都要重来一遍。
2. `ThumbCache.get()` 在填列表时对**每一项**都同步解一张缩略图
   （`winimg.load_pixels` + 圆角 PNG 编码 + `PhotoImage` 构造）。
   400 张 = 400 次，全在主线程。

## 判据

量「扫描完 + 列表填好」这一段的总耗时，以及主线程单次 `update()` 的最长阻塞。
基线应该是**秒级**（卡），优化目标：填列表 <= 0.5s、单次阻塞 <= 100ms。

用法：python probe_load.py [张数]
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

ROOT = os.path.join(os.environ.get("TEMP", "."), "ImageScout-load")


def make_photo(path, w, h, seed):
    """造一张**有结构**的图（不是纯噪声）。

    ⚠️ 别用纯随机噪声：那种图每张都互不相似，扫描出的分组数是 0，
    就测不到「填分组列表」那一段（而 0 组时走的是另一条兜底路径）。
    这里用「随机色块 + 渐变色带」，同 seed 的邻图会产生相似度。
    """
    import random
    rnd = random.Random(seed)
    buf = bytearray(w * h * 4)
    # 低分辨率色块网格（8x8 块）-> 放大后是平滑大色块，图之间容易相似
    gw, gh = 8, 8
    cols = [(rnd.randrange(40, 216), rnd.randrange(40, 216),
             rnd.randrange(40, 216)) for _ in range(gw * gh)]
    for y in range(h):
        by = (y * gh) // h
        rowbase = y * w * 4
        for x in range(w):
            c = cols[by * gw + ((x * gw) // w)]
            i = rowbase + x * 4
            buf[i] = c[2]
            buf[i + 1] = c[1]
            buf[i + 2] = c[0]
            buf[i + 3] = 255
    with open(path, "wb") as f:
        f.write(thumbs.bgra_to_png(w, h, bytes(buf)))
    return path


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 400
    uikit.enable_dpi_awareness()
    shutil.rmtree(ROOT, ignore_errors=True)
    os.makedirs(ROOT, exist_ok=True)

    t0 = time.perf_counter()
    need = not (os.path.isdir(ROOT) and len(os.listdir(ROOT)) >= n)
    if need:
        for i in range(n):
            # 做成几组「相似」：每 8 张共用同一套色块（只轻微扰动）
            # 这样扫描会真的分组出来，测得到分组列表
            make_photo(os.path.join(ROOT, "p%04d.png" % i), 800, 600, i // 8)
        print("造图 %d 张，用时 %.1fs" % (n, time.perf_counter() - t0))
    else:
        print("复用已有 %d 张图（跳过造图）" % n)

    uikit.clear_photo_caches()
    app = ui.App([ROOT])
    app.update()

    # ---- 埋点：每次 `_pump_queue` 抽干队列时，逐个回调计时 ----------
    # ⚠️ 为什么要逐回调：`_pump_queue` 是 `while True: get_nowait()` ——
    #    **一次 update 里会把当时的队列全部抽干**。所以「单次 update 3 秒」
    #    可能不是某一个回调慢，而是 400 个进度回调 + 收尾的填列表
    #    **攒在一次抽干里**。不逐回调量就分不清是哪种。
    CALLS = []
    orig_pump = app._pump_queue

    def pump():
        t0 = time.perf_counter()
        n_before = len(CALLS)
        try:
            while True:
                fn, a = app._q.get_nowait()
                t1 = time.perf_counter()
                try:
                    fn(*a)
                except Exception as e:
                    print("   回调出错 %s: %s" % (type(e).__name__, e))
                CALLS.append((getattr(fn, "__name__", repr(fn)),
                              (time.perf_counter() - t1) * 1000))
        except Exception:
            pass
        dt = (time.perf_counter() - t0) * 1000
        if dt > 30:
            print("   [泵] 一轮抽干 %.0fms，处理 %d 个回调"
                  % (dt, len(CALLS) - n_before))
        app.after(60, pump)

    app._pump_queue = pump

    # ---- 把 `_progress` 拆成两半计时 ---------------------------------
    # ⚠️ 为什么必须拆：`_progress` 一个「设进度条数值 + 改状态文字」的
    #    回调实测平均 **18.5ms**、最大 94.7ms，明显不合理。
    #    `pb` 是 `ttk.Progressbar`、`stat` 是 `tk.Label` ——
    #    到底是「ttk 主题重画」贵，还是「Label 改文字触发几何重算、
    #    连带整窗 relayout（进而打到画布的 Configure -> render_all）」贵？
    #    不拆开量就只能猜。
    SPLIT = {"pb": [], "stat": []}
    orig_prog = app._progress

    def prog(frac, text):
        t1 = time.perf_counter()
        app.pb["value"] = max(0.0, min(1.0, frac)) * 100
        t2 = time.perf_counter()
        app.stat.configure(text=text)
        t3 = time.perf_counter()
        SPLIT["pb"].append((t2 - t1) * 1000)
        SPLIT["stat"].append((t3 - t2) * 1000)

    app._progress = prog

    t0 = time.perf_counter()
    app.start_scan()
    scan_wall = 0.0
    busy_ms = 0.0
    while True:
        t1 = time.perf_counter()
        app.update()
        busy_ms = max(busy_ms, (time.perf_counter() - t1) * 1000)
        if not app.busy and app.files:
            scan_wall = time.perf_counter() - t0
            break
        if time.perf_counter() - t0 > 600:
            scan_wall = time.perf_counter() - t0
            print("   ⚠️ 超时仍未扫完")
            break
        time.sleep(0.005)
    print("扫描 + 分组 + 首次填列表：%.2fs（单次 update 最长 %.0fms）"
          % (scan_wall, busy_ms))
    print("   files=%d  groups_raw=%d  当前视图列表项=%d"
          % (len(app.files), len(app.groups_raw), len(app.glist.items)))

    # ---- 逐回调耗时汇总 ----------------------------------------------
    agg = {}
    for name, ms in CALLS:
        agg.setdefault(name, []).append(ms)
    print("   主线程回调耗时（按合计降序）：")
    for name, v in sorted(agg.items(), key=lambda kv: -sum(kv[1]))[:8]:
        v.sort()
        print("      %-22s n=%4d 合计 %8.1fms  最大 %8.1fms"
              % (name, len(v), sum(v), v[-1]))

    for k in ("pb", "stat"):
        v = sorted(SPLIT[k])
        if v:
            print("      _progress 拆解 · %-5s n=%4d 合计 %8.1fms  "
                  "中位 %5.2f 最大 %6.2fms"
                  % (k, len(v), sum(v), v[len(v) // 2], v[-1]))

    # ---- 单次「填列表」单独量 ----------------------------------------
    for name, fn in (("填分组列表", lambda: app._fill_group_list()),):
        t0 = time.perf_counter()
        fn()
        app.update()
        print("   %s：%.0fms（%d 项）"
              % (name, (time.perf_counter() - t0) * 1000, len(app.glist.items)))

    # ---- 画布图元数（虚拟化判据）------------------------------------
    app.set_view("all")
    app.update()
    items = len(app.glist.find_all())
    print("   「全部图片」视图：列表 %d 项 -> 画布图元 **%d** 个"
          % (len(app.glist.items), items))
    print("   （虚拟化之后应该只跟**可见行数**有关，而不是跟项数成正比）")

    # ---- 单次整表重画 ------------------------------------------------
    t0 = time.perf_counter()
    app.glist.redraw()
    app.update()
    print("   整表 redraw：%.0fms" % ((time.perf_counter() - t0) * 1000))

    # ---- 滚轮 ---------------------------------------------------------
    class Ev(object):
        def __init__(self, d, y=0):
            self.delta = d
            self.y = y
    t0 = time.perf_counter()
    worst = 0.0
    for _ in range(30):
        t2 = time.perf_counter()
        app.glist._wheel(Ev(-120))
        app.update()
        worst = max(worst, (time.perf_counter() - t2) * 1000)
    print("   滚轮 30 次：合计 %.0fms，单次最长 %.0fms"
          % ((time.perf_counter() - t0) * 1000, worst))

    app.destroy()


if __name__ == "__main__":
    main()
