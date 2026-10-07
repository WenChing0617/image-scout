# -*- coding: utf-8 -*-
"""主人 2026-10-07 报的两个问题（v1.8）。

## 报的现象

1. 「直接放缩画面不同步，要拖一下才同步上」
2. 「拖拽有卡顿，特别是放大后，当拖到未显示的画面的时候会卡顿」

## ⚠️⚠️⚠️ 判据被自己推翻过五次，教训全写在下面

第一版量「两侧归一化位置之差 < 0.02」，量到 0.077~0.19，一度以为代码错。
**五次都是判据自己的错**：

  ① **两侧图内容必须一致**。第一版用「同 seed 的两张噪声图」——
     像素完全不同，「同一处」含义本就不同，norm 有差是应该的。
  ② **两侧 disp 必须不同**。第二版「同一张图放两侧」-> disp 相同，
     「归一化」与「抄 pan」**结果一样**，判据**没有分辨力却全绿**。
     （想拉窄画布制造差异，可 `ttk.Frame.configure(width=)` 对 ttk.Frame
     **无效** —— 判据全绿时先自证它有分辨力。）
  ③ **终态要settle**。`_view["disp"]` 是上一帧渲染时的尺寸，
     滚完立刻量会拿到「新 pan 配旧 disp」。
  ④ **测试之间必须隔离目录**。所有案例都写 `ROOT/a.png`，
     而 `BigCache` **按路径缓存** -> 案例2 读到的是案例1 的图，
     量到 `disp=(597,920)`（1328×2048@1.25 的尺寸）。
     同路径=同一张图是**特性**；但**测试之间**必须隔离。
  ⑤ ⚠️⚠️ **终态判据抓不到缩放 bug**（注入验证实测）。
     把档位换算退化掉后，逐格 norm 偏差只有 **0.0013、判据全绿**——
     因为每次滚完的 `render_all` 会把两侧按新 zoom 重画，**偏差被抹平**。
     终态是对的，但**缩放那一瞬间另一侧是错的**（用户会看到画面跳一下）。
     ⇒ 必须**在滚完、`render_all` 之前**量两侧图元的实际落点。

## 判据能抓到什么（注入验证实测）

  · 档位换算退化 -> 逐帧落点差 >0.02（终态全绿也抓得到）
  · norm 取时机错 -> 同上
  · 纵向块长改回「跟视口」-> 缓存命中 0%、中位帧耗时 107ms（`probe_stall` 红）

用法：
    python probe_diag.py
"""
from __future__ import annotations

import os
import random
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import image_scout as ui  # noqa: E402
import thumbs  # noqa: E402
import uikit  # noqa: E402

ROOT = os.path.join(os.environ.get("TEMP", "."), "ImageScout-diag")
FAILS = []
_SRC = []
FRAME = []


def check(ok, msg):
    print("   %s   %s" % ("OK  " if ok else "FAIL", msg))
    if not ok:
        FAILS.append(msg)
    return ok


def make_photo(path, w, h, seed):
    rnd = random.Random(seed)
    buf = bytearray(rnd.randbytes(w * h * 4))
    for i in range(3, len(buf), 4):
        buf[i] = 255
    with open(path, "wb") as f:
        f.write(thumbs.bgra_to_png(w, h, bytes(buf)))
    return path


def make_same_content(tag, wa, ha, wb, hb):
    """从**同一张源图**缩出两个长宽比 —— 判据成立的前提（①②）。

    ⚠️ 每个案例一个独立目录（坑④）：`BigCache` 按**路径**缓存，
    跨案例共用 `a.png` 会让后一个案例读到前一个的图。
    """
    import winimg
    d = os.path.join(ROOT, tag)
    os.makedirs(d, exist_ok=True)
    if not _SRC:
        p = os.path.join(d, "big.png")
        make_photo(p, 1400, 1400, 21)
        r = winimg.load_pixels_exact(p)
        if r is None:
            raise SystemExit("源图解不出像素 —— 探针自己坏了")
        _SRC.append(r)
    w, h, px = _SRC[0]
    out = []
    for name, (ow, oh) in (("a.png", (wa, ha)), ("b.png", (wb, hb))):
        dst = os.path.join(d, name)
        r = winimg._gdip_scale_argb(w, h, px, ow, oh)
        rw, rh, rp = r if r else (w, h, px)
        with open(dst, "wb") as f:
            f.write(thumbs.bgra_to_png(rw, rh, rp))
        out.append(dst)
    return out[0], out[1]


class Ev(object):
    def __init__(self, d=0, x=0, y=0):
        self.delta = d
        self.x = x
        self.y = y


def settle(app, n=3):
    app.update()
    for _ in range(n):
        t0 = time.time()
        while time.time() - t0 < 8:
            app.update()
            if not app._fit_pend:
                break
            time.sleep(0.02)
        app.render_all(precise=True)
        app.update()


def frame_norm(app):
    """滚完**那一瞬间**（`render_all` 之前）两侧注视位置差（主判据）。

    ## 为什么必须量这一帧（v1.8 注入验证的核心结论）

    把档位换算退化掉之后：
      · 终态norm 偏差只有 **0.0013** -> 判据全绿
      · 而用户实际看到的是「缩放时画面跳一下」
    原因：每次滚完的 `render_all` 会把两侧按新 zoom 重画，
    **偏差被抹平了**。终态对，不等于过程对。

    ## 量的方式（判据被推翻过六次，这是最终版）

    只量 `pan` 与 `disp` 推出来的**归一化位置**，且两侧都用
    **各自当前的 `_view["disp"]`**（各自的，不能换算到同一档 ——
    换算需要「`disp` 是哪一档记下的」这个额外信息，判据里没有）。

    ⚠️ 曾试过量「图元理论中心之差」，**数学上就不成立**：
    两侧 disp 不同时居中偏移本就该不同，开滚前就是 0.125。
    ⇒ 唯一跨 disp 可比的量就是**归一化位置**。
    """
    out = []
    for col in (0, 1):
        v = app._view.get(col)
        if not v:
            return None
        disp = v.get("disp") or (0, 0)
        cv = v.get("cv")
        if disp[0] <= 0 or cv is None:
            return None
        cw = max(1, cv.winfo_width())
        chh = max(1, cv.winfo_height())
        m = ((cw - disp[0]) // 2, (chh - disp[1]) // 2)
        out.append(((app.pan[col][0] - m[0]) / float(disp[0]),
                    (app.pan[col][1] - m[1]) / float(disp[1])))
    return max(abs(out[0][0] - out[1][0]), abs(out[0][1] - out[1][1]))


def boot(tag, wa, ha, wb, hb):
    uikit.clear_photo_caches()
    a, b = make_same_content(tag, wa, ha, wb, hb)
    app = ui.App([os.path.join(ROOT, tag)])
    app.update()
    app.start_scan()
    t0 = time.time()
    while time.time() - t0 < 300:
        app.update()
        if not app.busy and app.files:
            break
        time.sleep(0.01)
    app.set_pair(a, b)
    settle(app)
    return app


def run_zoom(title, tag, wa, ha, wb, hb, need_diff):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)
    app = boot(tag, wa, ha, wb, hb)
    cw = app.cv_a.winfo_width()
    ch = app.cv_a.winfo_height()
    d0 = (app._view.get(0) or {}).get("disp")
    d1 = (app._view.get(1) or {}).get("disp")
    print("   画布 %dx%d  初始 disp 左 %s 右 %s" % (cw, ch, d0, d1))

    if need_diff:
        check(d0 != d1, "两侧 disp 不同（%s vs %s）—— 相同则判据无分辨力"
                        "（归一化与抄 pan 恒等）" % (d0, d1))
    app.toggle_link()
    app.update()
    check(app.link_view.get() is True, "联动已打开")

    # ⚠️⚠️⚠️ **基线必须在「联动打开之后」量**（v1.8 判据第七次修正）。
    #
    # 我原来在`toggle_link` **之前**量，案例2 开滚前就是 0.1293 ——
    # 而那不是bug：`set_pair` 之后两侧 `pan` 各有各的初值
    # （实测 -94,76 / 0,0），**本来就还没对齐**，联动开关打开后
    # 才对齐。把「本来就不同步」当成前置条件，判据永远红。
    #
    # ⇒ 判据要问的是「**联动打开后**，缩放之前两侧是否一致」。
    fm0 = frame_norm(app)
    check(fm0 is not None and fm0 < 0.02,
          "联动打开后、缩放前两侧就一致（逐帧差 %s）" %
          ("None" if fm0 is None else "%.4f" % fm0))

    worst_final = 0.0
    worst_frame = 0.0
    worst_at = 0
    for i in range(1, 7):
        app._on_wheel(0, Ev(120, cw // 4, ch // 3))   # 鼠标偏离中心
        app.update()
        # ⚠️ 逐帧量在 settle **之前**（坑⑤）
        fd = frame_norm(app)
        settle(app)                                   # 终态量在之后（坑③）
        n0 = app._link_norm(0)
        n1 = app._link_norm(1)
        d = (max(abs(n0[0] - n1[0]), abs(n0[1] - n1[1]))
             if n0 and n1 else -1)
        if fd is not None and fd > worst_frame:
            worst_frame = fd
        if d > worst_final:
            worst_final = d
        print("   第%d格 逐帧差 %s 终态偏差 %.4f" % (
            i, "None  " if fd is None else "%.4f " % fd, d))
        if (fd is not None and fd >= 0.02) or d >= 0.02:
            worst_at = i
    print("   >> 最差：逐帧 %.4f / 终态 %.4f" % (worst_frame, worst_final))
    # ⚠️⚠️ **逐帧是主判据**：终态会被 render_all 抹平（注入验证实测 0.0013）
    check(worst_frame < 0.02,
          "缩放那一瞬间两侧就同步（最差逐帧注视位置差 %.4f < 0.02）"
          "—— 终态会被重画抹平，只有逐帧抓得到" % worst_frame)
    check(worst_final < 0.02,
          "缩放稳定后两侧仍同步（最差终态偏差 %.4f < 0.02）" % worst_final)

    # 拖动仍要同步（回归：修缩放不能碰坏拖动）
    app._pan_start(0, Ev(0, cw // 2, ch // 2))
    for k in range(1, 9):
        app._pan_move(0, Ev(0, cw // 2 - k * 9, ch // 2 - k * 5))
        app.update()
    app._pan_end()
    settle(app)
    n0, n1 = app._link_norm(0), app._link_norm(1)
    if n0 and n1:
        d = max(abs(n0[0] - n1[0]), abs(n0[1] - n1[1]))
        check(d < 0.02, "拖动后两侧仍同步（偏差 %.4f）" % d)
    app.destroy()


def run_stall(w, h):
    print("\n" + "=" * 70)
    print("★ 放大后拖到未显示画面：帧耗时")
    print("=" * 70)
    sd = os.path.join(ROOT, "c3")
    os.makedirs(sd, exist_ok=True)
    p = make_photo(os.path.join(sd, "s.png"), w, h, 11)
    uikit.clear_photo_caches()
    app = ui.App([sd])
    app.update()
    app.start_scan()
    t0 = time.time()
    while time.time() - t0 < 300:
        app.update()
        if not app.busy and app.files:
            break
        time.sleep(0.01)
    app.set_pair(p, None)
    settle(app)
    cw = app.cv_a.winfo_width()
    ch = app.cv_a.winfo_height()
    for _ in range(6):
        app._on_wheel(0, Ev(120, cw // 2, ch // 2))
        app.update()
    settle(app)
    print("   zoom %.3f disp %s 画布 %dx%d" % (
        app.zoom[0], (app._view.get(0) or {}).get("disp"), cw, ch))

    times = []
    fills = []
    prev = (app._view.get(0) or {}).get("item")
    app._pan_start(0, Ev(0, cw // 2, ch // 2))
    for k in range(1, 151):
        t0 = time.time()
        app._pan_move(0, Ev(0, cw // 2 - k * 7, ch // 2 - k * 4))
        app.update()
        times.append((time.time() - t0) * 1000)
        it = (app._view.get(0) or {}).get("item")
        if it is not prev:
            fills.append(times[-1])
            prev = it
    app._pan_end()
    app.update()
    srt = sorted(times)
    p50 = srt[len(srt) // 2]
    p95 = srt[int(len(srt) * .95)]
    print("   拖 150 步：帧 中位 %.1f / p95 %.1f ms" % (p50, p95))
    print("   其中补块 %d 步%s" % (
        len(fills),
        ("，平均 %.1f ms" % (sum(fills) / len(fills))) if fills else ""))
    check(p50 < 20, "拖动中位帧耗时 %.1f ms < 20 ms（修前 87.1）" % p50)
    check(p95 < 60, "拖动 p95 %.1f ms < 60 ms（修前 103.9）" % p95)
    app.destroy()


def main():
    uikit.enable_dpi_awareness()
    shutil.rmtree(ROOT, ignore_errors=True)
    os.makedirs(ROOT, exist_ok=True)

    run_zoom("★ 案例1：同一张图 1328×2048 放两侧（主人截图的情形）",
             "c1", 1328, 2048, 1328, 2048, need_diff=False)
    run_zoom("案例2：同一张图缩成 1400×1750 / 1750×1400（disp 不同）",
             "c2", 1400, 1750, 1750, 1400, need_diff=True)
    run_stall(1328, 2048)

    print("\n" + "=" * 60)
    if FAILS:
        print("❌ %d 项不通过：%s" % (len(FAILS), FAILS[0]))
        sys.exit(1)
    print("✅ 缩放同步 + 拖动流畅，全部通过")


if __name__ == "__main__":
    main()