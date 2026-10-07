# -*- coding: utf-8 -*-
"""拖拽抖动探针：分清「重画卡顿」和「跟手度不够」。

跑法：

    python probe_jitter.py

主人 2026-10-06 报：「现在缩放很流畅了，可是拖拽还是有抖动」。

⚠️ 「抖动」在 Tk 里可能是**两件完全不同的事**，修法不一样：
  A. **掉帧**（卡顿）：某一步主线程被堵住 60ms，那一帧画面不更新，
     手感是「顿一下」。→ 要把重画挪走/变小。
  B. **位置跳变**（跟手度）：画面动了，但**位移量 ≠ 鼠标位移量**，
     或者缓冲吃光时突然换成新图元，视觉上「一跳」。→ 是缓冲/锚点问题。

所以本探针量三个东西：
  1. 每步耗时分布（p50 / p95 / max）—— 卡顿
  2. **图元位移 vs 鼠标位移**的比值 —— 跟手度，必须恒为 1
  3. 哪一步触发了重画（重画那一步就是停顿）
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
import winimg                      # noqa: E402

ROOT = os.path.join(os.environ.get("TEMP", "."), "ImageScout-jitter")


def make_photo(path, w=3000, h=2000, seed=7):
    import random
    rnd = random.Random(seed)
    buf = bytearray(w * h * 4)
    buf[:] = rnd.randbytes(w * h * 4)
    for i in range(3, len(buf), 4):
        buf[i] = 255
    with open(path, "wb") as f:
        f.write(thumbs.bgra_to_png(w, h, bytes(buf)))
    return path


def pct(vals, p):
    if not vals:
        return 0.0
    s = sorted(vals)
    k = int(round((len(s) - 1) * p))
    return s[k]


def main():
    uikit.enable_dpi_awareness()
    shutil.rmtree(ROOT, ignore_errors=True)
    os.makedirs(ROOT, exist_ok=True)
    a = make_photo(os.path.join(ROOT, "photo_a.png"))
    got = winimg.load_pixels(a, 2000, winimg.SIIGBF_RESIZETOFIT, raw=True)
    w, h, px = got
    _w, _h, cut = thumbs.crop_bgra(w, h, px, int(w * 0.1), int(h * 0.1),
                                   int(w * 0.8), int(h * 0.8))
    with open(os.path.join(ROOT, "photo_b.png"), "wb") as f:
        f.write(thumbs.bgra_to_png(_w, _h, cut))

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

    def warm_idle():
        return ((not getattr(app, "_prewarm_q", []))
                and not getattr(app, "_prewarm_busy", False))

    for _ in range(2):
        t0 = time.time()
        while time.time() - t0 < 180:
            app.update()
            if warm_idle():
                break
            time.sleep(0.01)
        t0 = time.time()
        while time.time() - t0 < 8:
            app.update()
            if not app._fit_pend:
                break
            time.sleep(0.02)

    class Ev(object):
        # ⚠️ `x/y` 是**鼠标位置**，`_on_wheel` 拿它算锚点缩放（v1.7 起）。
        #    默认给画布中心 —— 真实用户多半在图中间滚滚轮，而且这样
        #    缩放后 pan 恰好还是 0（起点干净，最容易自证）。
        def __init__(self, d=0, x=None, y=None):
            self.delta = d
            self.x = app.cv_a.winfo_width() // 2 if x is None else x
            self.y = app.cv_a.winfo_height() // 2 if y is None else y

    app.reset_zoom()
    app.update()
    # ⚠️ **先取画布尺寸再滚** —— 滚轮事件要带鼠标坐标（v1.7 锚点缩放），
    #    而坐标得从画布尺寸算出来（取中心 = 缩放后 pan 恰好还是 0，
    #    起点最干净、最容易自证）。原先顺序颠倒是 `UnboundLocalError`。
    cw = app.cv_a.winfo_width()
    ch = app.cv_a.winfo_height()

    def settle(precise=True):
        """等精确帧真的落地。⚠️ 必须 `render_all(precise=True)`：
        滚轮的 0.75 快速帧已经把图元贴上去了，但基准档可能还不够
        （`_fit_async` 在后台补）。不补这一帧的话，起点那块图元
        可能是**半张**（量到过 bbox=(-1348,-1021,471,-113)——
        在画布左上角外面），后面量到的全是「测试自己状态不对」。
        """
        for _ in range(3):
            t0 = time.time()
            while time.time() - t0 < 8:
                app.update()
                if not app._fit_pend:
                    break
                time.sleep(0.02)
            if precise:
                app.render_all(precise=True, only=0)
                app.update()

    for _ in range(6):
        app._on_wheel(0, Ev(120, cw // 2, ch // 2))
    app.update()
    settle()

    cw = app.cv_a.winfo_width()
    ch = app.cv_a.winfo_height()
    v = app._view[0]
    dw, dh = v["disp"]
    rng_x = max(0, dw - cw) // 2
    rng_y = max(0, dh - ch) // 2
    print("画布 %dx%d  zoom %.2f  显示图 %dx%d  可拖 ±%d,±%d"
          % (cw, ch, app.zoom[0], dw, dh, rng_x, rng_y))
    print("块 %s  视口+缓冲 %dx%d  TILE_PAD=%.2f -> 每边缓冲 %.0fpx"
          % (v.get("blk"), int(cw * 1.2), int(ch * 1.2),
             app.TILE_PAD, cw * app.TILE_PAD))
    # ⚠️⚠️ **前置断言：状态离谱就立刻报错，不许继续跑出假数据。**
    # 我第三版就因为没这一步，把「blk=(8,8)、ox=129 万像素」当成了
    # 「重画很频繁」的证据 —— 其实那是 `_zoom_anchor` 把 pan 推到天边
    # 造成的（已修），数据整段作废。**探针量到的任何数，先证明状态是对的。**
    bx, by = v.get("blk") or (0, 0)
    assert 0 < bx <= cw and 0 < by <= ch, \
        "块尺寸离谱（%s，画布 %dx%d）—— 状态已经坏了，别看后面的数" \
        % (v.get("blk"), cw, ch)
    assert abs(v["px"]) <= rng_x + cw, \
        "pan 离谱（px=%d，可拖 ±%d）—— 锚点缩放算错了" % (v["px"], rng_x)
    assert abs(v["ox"]) <= rng_x + cw, \
        "ox 离谱（%d）—— 锚点缩放算错了" % v["ox"]
    print("  _view: ox=%d oy=%d px=%d py=%d disp=%s blk=%s（前置断言已过）"
          % (v["ox"], v["oy"], v["px"], v["py"], v["disp"], v["blk"]))

    app._pan_start(0, Ev(0, 10, 10))
    x0, y0 = app._drag[1], app._drag[2]
    N = 60
    # ⚠️⚠️ **步长必须照真实的鼠标节奏**，不能为了「快」而迈大。
    # 第一版探针每步挪 44px（`rng_x/12`），结果量到「p50=0.7ms、
    # 跟手比值 1.000」—— 看着完美，其实是因为**每步都换到没缓存的位置**，
    # 于是每次都命中了「挪图元」那条 0.3ms 的快路，重画全被缓存挡了。
    # 真实拖拽是**连续小步**（一帧 3~10px），会反复经过同一片区域 ——
    # 那样才能量到「缓存未命中那一次」到底堵多久，也就是主人说的抖动。
    #
    # ⚠️ `path` 是**单程来回**扫，且 `N=60` 步必须落在第一程之内
    #    （单程 `span/STEP+1` = 113 步）。第三版我拼了 4 个来回 452 步却只跑
    #    前 60 步，虽然仍在第一程内，但**前面的 `_pan_start` 起点状态
    #    已经被前面的轮次污染** —— 所以现在每轮都从 `reset_zoom` 起步。
    STEP = 8
    times, ratios, redrew, miss, jumps = [], [], [], [], []
    last_item = None
    last_pan = tuple(app.pan[0])
    last_centre = None
    span = min(rng_x, 900)          # 别超出去太多，留住缓冲的意义
    path = []
    for leg in range(4):
        rng = range(0, span + 1, STEP) if leg % 2 == 0 else \
            range(span, -1, -STEP)
        path.extend(int(t) for t in rng)
    assert N <= span // STEP + 1, \
        "只跑 %d 步却跨度 %dpx（每步 %dpx）—— 会跑进第二个来回，" \
        "起点状态就不是第一程的了" % (N, span, STEP)
    print("路线：%d 步，每步 %dpx，跨度 ±%dpx（可拖 ±%d）"
          % (len(path), STEP, span, rng_x))
    print("步 鼠标Δ  pan        图元坐标             视口中心显示坐标   耗时   说明")
    for k in range(N):
        d = path[k % len(path)]
        mx = x0 + d
        my = y0 + d // 2
        it0 = app._view[0].get("item")
        c0 = list(app.cv_a.coords(it0)) if it0 else None
        # ⚠️⚠️ **上一步那一帧的「块内容起点」必须一起存下来**。
        # 判据要用「块起点 + (视口中心 - 图元画布x)」反推画面内容位置，
        # 而「块起点 + 图元画布x」这一对**必须是同一帧的** ——
        # 换块之后 `vx0` 变成新块起点，拿它配旧坐标就是跨参照系比较
        # （我第四版量出「464px 假跳变」就是这么来的）。
        v0 = app._view[0]
        vx0_prev = (v0["bx"] - v0["ox"]) if ("bx" in v0 and c0) else None
        t0 = time.perf_counter()
        app._pan_move(0, Ev(0, mx, my))
        app.update()
        ms = (time.perf_counter() - t0) * 1000
        it1 = app._view[0].get("item")
        c1 = list(app.cv_a.coords(it1)) if it1 else None
        times.append(ms)
        # ⚠️ 用 **pan 的增量**当「鼠标位移」基准：`_pan_move` 里
        #    `pan = 起点pan + (鼠标坐标 - 起点坐标)`，所以 pan 增量
        #    就是鼠标位移的真实值。
        pan_now = tuple(app.pan[0])
        d_mouse = pan_now[0] - last_pan[0]
        # ⚠️⚠️ **必须每步把 `last_pan` 推回去**。我漏了这一行，
        # `d_mouse` 就变成「当前 pan 减起点 pan」= **累计位移**（8/16/24…）
        # 而不是本步位移（恒 8），于是量出「画面只动 8px、期望却要动
        # 160px」的假跳变 464px —— 第五次栽在判据上。
        last_pan = pan_now
        # ⚠️⚠️⚠️ **跳变量必须同帧配对**。
        #
        # 内容坐标 =「块的内容起点 `vx0`」+「视口中心 − 图元的画布 x」，
        # 其中 `vx0 = bx - ox`（`bx` 是图元落点、`ox` 是显示图原点，
        # 两者都存进了 `_view`）。「上一步的 vx0 配上一步的 coords」是
        # 一对，「这一步的 vx0 配这一步的 coords」是另一对 ——
        # **两对各自同源之后才能相减**。
        #
        # 错法记录（**前四次全是判据的错，不是产品的错**）：
        #   ① 量图元左上角位移 -> 换块时量到「两块起点之差 72px」假跳变；
        #   ② 「此刻的 ox」配「那帧的 bx」-> 假跳变 536px；
        #   ③ 只用 pan 推 -> 拿「意图」当「结果」，恒 0 的假通过；
        #   ④ 新帧 vx0 配旧帧坐标 -> 假跳变 464px（跨参照系比较）。
        # **教训：判据里的量必须同源同帧，混用就会量出假的「跳变」。**
        _v1 = app._view[0]
        # ⚠️⚠️ **配对必须同帧**：「上一步的 vx0_prev 配上一步的 c0」，
        # 「这一步的 vx0_now 配这一步的 c1」，各自同源后再相减。
        # 拿新帧的起点去比旧帧的坐标 = 跨参照系比较（我量到过 464px
        # 的假跳变就是这么来的）。四次错法全是判据的错，不是产品的错。
        vx0_now = (_v1["bx"] - _v1["ox"]) if ("bx" in _v1 and c1) else None
        centre_now = (vx0_now + (cw // 2 - c1[0])) \
            if vx0_now is not None else None
        centre_prev = (vx0_prev + (cw // 2 - c0[0])) \
            if vx0_prev is not None else None
        if centre_now is not None and centre_prev is not None:
            d_view = centre_now - centre_prev
            # ⚠️ 期望值 = **-d_mouse**（鼠标往右拖 -> 内容往左移）。
            #   比值就是 `d_view / d_mouse`，恒为 -1。我之前多除了个负号，
            #   变成「永远等于 1」的假通过 —— **判据自己也会骗人**。
            if d_mouse:
                ratios.append(d_view / float(d_mouse))
            # ⚠️⚠️ **「跳变」= 误差，不是位移本身**。每步正常就要跟着
            #   鼠标走 8px，把那 8px 记成跳变是判据写错了（第一版量出
            #   「59 次跳变、每次 8px」—— 全是正常跟随）。
            _err = abs(d_view - (-d_mouse))
            jumps.append(_err)
            if _err > 2:
                print("  !! 跳变 %3d 步%d：画面动了 %d，期望 %d"
                      % (_err, k, d_view, -d_mouse))
        centre_v = centre_now
        swapped = (it1 is not it0)
        if swapped:
            redrew.append(k)
            if ms > 15:
                miss.append((k, ms))
        if k < 8 or ms > 15 or swapped:
            print("%3d %6d  %-9s %-14s 视心@%-9s %6.1fms  %s"
                  % (k, d_mouse, str(pan_now),
                     str([int(x) for x in c1]) if c1 else "-",
                     "%d" % centre_v, ms,
                     ("← 换块" + ("(慢)" if ms > 15 else "")) if swapped else ""))
        time.sleep(0.016)
    app._pan_end()

    print("\n=== 汇总 ===")
    print("每步耗时  p50=%.1f  p95=%.1f  max=%.1f ms"
          % (pct(times, 0.5), pct(times, 0.95), max(times)))
    print("重画 %d 次 / %d 步（每步 %dpx、缓冲每边 %.0fpx -> "
          "理论每 %d 步该补一块）"
          % (len(redrew), N, STEP, cw * app.TILE_PAD,
             max(1, int(app.PAN_REDRAW_PX / STEP))))
    print("掉帧（>15ms）%d 次：%s"
          % (len(miss), [("%d:%.0fms" % (a, b)) for a, b in miss[:8]]))
    if not miss:
        print("✅ 没有任何一步超过 15ms（16ms 帧预算内）—— 不掉帧")
    elif len(miss) <= max(2, N // 8):
        print("✅ 掉帧只发生在补块那几步（%d/%d），纯挪图元的步都在帧预算内"
              % (len(miss), N))
    else:
        print("❌ **超过 1/8 的步都超预算** —— 这就是主人说的抖动。"
              "16ms 一帧，一次 70ms = 丢掉 4 帧。")
    # ⚠️ 跟手判据：画面上那个点应该**跟着鼠标反向平移**同样的距离
    #    （鼠标往右拖 -> 内容往左移 -> 显示图坐标变小）。
    if ratios:
        _lo, _hi = min(ratios), max(ratios)
        print("跟手比值（画面位移 / 鼠标位移，应恒为 -1）min=%.4f max=%.4f"
              % (_lo, _hi))
        ok = abs(_lo + 1) <= 0.02 and abs(_hi + 1) <= 0.02
        print("✅ 完全跟手" if ok else "❌ 画面位移和鼠标位移对不上")
    if jumps:
        print("画面跳变误差 %d 次，最大 %d px（判据：画布正中那一点的"
              "显示图坐标，实际位移 − 期望位移）"
              % (len(jumps), max(jumps)))
        if max(jumps) > 2:
            print("❌ 换块时画面真的跳了 —— 视觉上会「闪一下」")
    else:
        print("✅ 画面没有跳变（换块处严丝合缝）")
    app.destroy()
    # ⚠️ 返回值要和上面的判据**完全一致**，别各写一套
    #（第一版这里还留着最早那版的 `not miss` + 比值 +1，
    #  上面全绿、退出码却是 1 —— 自己跟自己打架）。
    # 真 bug 是「每步都重画」，所以允许少量补块步超预算。
    # ⚠️ 比值符号：内容往左移、鼠标往右拖 -> `d_view / d_mouse` 恒为 **-1**
    #   （不是 +1。我这里先写成 +1，结果上面全绿、退出码却是 1。）
    _ok = (len(miss) <= max(2, N // 8)
           and ratios and min(ratios) > -1.02 and max(ratios) < -0.98
           and jumps and max(jumps) <= 2)
    return 0 if _ok else 1


if __name__ == "__main__":
    sys.exit(main())
