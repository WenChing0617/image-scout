# -*- coding: utf-8 -*-
"""量「扫描时界面卡不卡」—— 判据是**主线程最长多久没被服务**。

## 为什么不用「回调耗时」

`probe_prog.py` 已经证过：`_progress` 里的 7.7 秒**跟控件无关**
（无后台线程时 300 次只要 33.8ms，4 线程抢 GIL 时要 133s）。
真正影响手感的是**事件循环被堵住的时长**，所以这里直接量它：

  在主线程挂一个 `after(20ms)` 的 tick，记相邻两次 tick 的间隔。
  正常应该 ~20ms；如果主线程在别处抢 GIL/干重活，tick 就会被推后。

## 判据

U1  tick 间隔 p99 <= 120ms
U2  tick 间隔 最大 <= 280ms
U3  主线程被 progress 占用 <= 25%
U4  扫描总时长没被拖垮
U5  **收尾那一跳单独看**：`_done` + `_finish_view` 合计 <= 1500ms

## ⚠️ 为什么 U1/U2 要**排除收尾那一下**

`_done` 之后 `_finish_view` 要现场把第一对预览解出来 + 编码 + 建 Tk 图
（冷缓存实测 1221ms）。**这不是「扫描时的卡」**，是「结果第一次显示」
的一次性成本，性质不同：

  · 扫描中卡 -> 用户不知道发生了什么，纯浪费
  · 收尾卡   -> 是「正在把结果显示出来」，且已被拆到独立的一轮，
                前面「100% · 50 组」的文字先落地了

混在一起算的话，一个 1200ms 的收尾会把整条曲线的 p99/max 全带歪，
看不出**扫描过程本身**到底顺不顺 —— 这正是我第一版判据的毛病
（只看 max，永远只看到那一下）。所以两个口径都报：
`全程` 给手感，`不含收尾` 给扫描过程的回归保护。

用法：python probe_ui_lag.py [张数]
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

ROOT = os.path.join(os.environ.get("TEMP", "."), "ImageScout-prog")

FAIL = []
OK = []


def check(cond, label, extra=""):
    (OK if cond else FAIL).append(label)
    print("   %s %s%s" % ("[OK]" if cond else "[XX]", label,
                          ("  " + extra) if extra else ""))


def pct(v, q):
    s = sorted(v)
    return s[min(len(s) - 1, int(len(s) * q))] if s else 0.0


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 400
    uikit.enable_dpi_awareness()
    sys.setswitchinterval(float(os.environ.get("SWI", "0.005")))
    print("GIL switchinterval = %s s" % sys.getswitchinterval())
    app = ui.App([ROOT])
    # ⚠️⚠️ **必须换掉指纹缓存**：上一次跑完会把指纹写进
    #    `%LOCALAPPDATA%\ImageScout\finger-cache.json`，第二次跑全是
    #    缓存命中 -> 3 秒就跑完、根本没有 GIL 争抢 -> 量出来的「不卡」是假的。
    #    我第一版就踩了这个坑（跑到 3.9s / 只有 33 次进度回调）。
    app.store = scan.FingerStore(os.path.join(
        os.environ["TEMP"], "_uilag_cache_%d.json" % time.time()))
    app.update()
    print("图 %d 张，用独立指纹缓存（保证是真解码）" % len(app.files))

    # ---- 冻帧计时器：主线程每隔 20ms 该被服务一次 --------------------
    TS = []                     # tick 的绝对时刻
    HEAVY = []                  # 收尾重活的 (起, 止) 窗口
    busy = [0.0]

    def tick():
        TS.append(time.perf_counter())
        app.after(20, tick)

    # ---- 同时记「主线程真正在执行 Python 的时间」 --------------------
    PROG = {"n": 0, "sum": 0.0, "flush": 0}

    def prog(frac, text):
        t1 = time.perf_counter()
        app._prog_orig(frac, text)
        dt = (time.perf_counter() - t1) * 1000
        PROG["n"] += 1
        PROG["sum"] += dt
        busy[0] += dt

    app._prog_orig = app._progress
    app._progress = prog

    # ---- 把队列里每个回调逐个计时（找出冻帧的真凶）------------------
    # ⚠️ 不要整个替换 `_pump_queue`：`__init__` 里已经 `after(60, ...)`
    #    排过一次，那个排的是**原来的绑定方法**，替换属性只会多出一个泵
    #    跟它抢队列。改成在 `_ui` 入队时就套一层计时，泵本身不动。
    CALLS = []
    orig_ui = app._ui

    # 「收尾重活」：这两个决定了第一对预览什么时候出来。
    # 它们不参与 U1/U2（见文件头），单独由 U5 判。
    HEAVY_NAMES = ("_done", "_finish_view")

    def timed_ui(fn, *a):
        name = getattr(fn, "__name__", repr(fn))

        def timed(*aa):
            t1 = time.perf_counter()
            try:
                fn(*aa)
            finally:
                t2 = time.perf_counter()
                CALLS.append((name, (t2 - t1) * 1000))
                if name in HEAVY_NAMES:
                    HEAVY.append((t1, t2))
        timed.__name__ = name
        orig_ui(timed, *a)

    app._ui = timed_ui

    # ⚠️ `_finish_view` 是 `_done` 里用 `self.after(1, self._finish_view)` 排的，
    #    **不走 `_ui` 队列**，所以上面那层包装看不到它 —— 得单独套一层。
    #    （第一版就漏了这一步，等待循环永远等不到，直接把探针挂死。）
    orig_finish = app._finish_view

    def finish():
        t1 = time.perf_counter()
        try:
            orig_finish()
        finally:
            t2 = time.perf_counter()
            CALLS.append(("_finish_view", (t2 - t1) * 1000))
            HEAVY.append((t1, t2))
            DONE_FLAG.append(True)

    DONE_FLAG = []
    app._finish_view = finish

    app.after(20, tick)
    t0 = time.perf_counter()
    app.start_scan()
    while app.busy or not app.files:
        app.update()
        if time.perf_counter() - t0 > 600:
            break
        time.sleep(0.002)
    # ⚠️ 收尾的 `_finish_view` 是 `after(1, ...)` 排的，`busy` 一置 False
    #    主循环就退出了 —— 必须再泵到它真的跑完，否则量到的
    #    「扫描墙钟」里根本不含它，U5 也拿不到数。
    while not DONE_FLAG:
        app.update()
        if time.perf_counter() - t0 > 60:
            print("   ⚠️ 等 `_finish_view` 超时")
            break
        time.sleep(0.002)
    for _ in range(20):
        app.update()
        time.sleep(0.003)
    wall = time.perf_counter() - t0
    GAP = [(TS[i + 1] - TS[i]) * 1000 for i in range(len(TS) - 1)]
    GAP_T = [(TS[i], TS[i + 1]) for i in range(len(TS) - 1)]

    # 丢掉开头那段（窗口刚起来、还在建控件）
    g = GAP[3:]
    print("\n扫描墙钟 %.2fs，_progress 落地 %d 次，其中主线程占用 %.0fms"
          % (wall, PROG["n"], PROG["sum"]))

    # 全程口径 + 「不含收尾重活」口径
    def overlaps(t0_, t1_):
        for h0, h1 in HEAVY:
            if t1_ > h0 and t0_ < h1:
                return True
        return False

    g_all = GAP
    g_scan = [ms for ms, (a, b) in zip(GAP, GAP_T) if not overlaps(a, b)]
    heavy_ms = sum((b - a) * 1000 for a, b in HEAVY)

    def line(tag, v):
        if not v:
            return "   %s  （无样本）" % tag
        return ("   %s p50=%.0f p90=%.0f p99=%.0f max=%.0f ms（%d 个样本）"
                % (tag, pct(v, .5), pct(v, .9), pct(v, .99), max(v), len(v)))

    print(line("tick 间隔 · 全程      ", g_all))
    print(line("tick 间隔 · 不含收尾  ", g_scan))
    print("   收尾重活（_done + _finish_view）合计 %.0fms，出现在 %d 段"
          % (heavy_ms, len(HEAVY)))

    check(pct(g_scan, .99) <= 120.0, "U1 扫描过程 tick p99 <= 120ms",
          "p99=%.0fms" % pct(g_scan, .99))
    check(max(g_scan) <= 280.0, "U2 扫描过程 tick 最大 <= 280ms",
          "max=%.0fms" % max(g_scan))
    check(PROG["sum"] / max(1.0, wall * 1000) <= 0.25,
          "U3 主线程被 progress 占用 <= 25%",
          "%.1f%%" % (PROG["sum"] / max(1.0, wall * 1000) * 100))
    check(wall <= 60.0, "U4 扫描总时长没被拖垮", "%.1fs" % wall)
    check(heavy_ms <= 1500.0, "U5 收尾重活 <= 1500ms（冷缓存首帧）",
          "%.0fms" % heavy_ms)
    # 参考：全程最大冻帧（含收尾），只报不判 —— 它必定被收尾那一跳主导
    print("   [参考] 全程最大冻帧 %.0fms" % max(g_all or [0]))

    # ---- 谁吃掉了主线程 ----------------------------------------------
    agg = {}
    for name, ms in CALLS:
        agg.setdefault(name, []).append(ms)
    print("\n主线程回调耗时（按合计降序）：")
    for name, v in sorted(agg.items(), key=lambda kv: -sum(kv[1]))[:8]:
        v2 = sorted(v)
        print("   %-22s n=%4d 合计 %8.1fms  中位 %6.2f  最大 %8.1fms"
              % (name, len(v), sum(v), v2[len(v2) // 2], v2[-1]))

    app.destroy()
    print("\n== 结果：%d 通过 / %d 失败 ==" % (len(OK), len(FAIL)))
    for f in FAIL:
        print("   FAIL %s" % f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
