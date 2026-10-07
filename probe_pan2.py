# -*- coding: utf-8 -*-
"""拖拽抖动探针 v2：**四个方向 × 拖到边缘**。

为什么要有 v2（v1 探针量错了场景）：
  v1 只测了「从中心往右 + 往下半速」这一个方向，而且**从不贴到图的边缘**。
  主人 2026-10-06 反馈：「拖拽还是严重抖动……特别是**向上拖拽**抖动最严重」。
  这两个 v1 都没覆盖：
    · **向上拖** -> v1 的y 只走了 `d//2`（半速），且全程向右偏
    · **贴边**   -> v1 的 `span=min(rng,900)`，离边缘还差好几百 px

所以这里四个方向各测一遍，且**每一路都拖到不能再拖为止**（贴到图边缘），
把每一步的耗时、补块次数、露白都记下来。

用法：
    python probe_pan2.py
"""
from __future__ import annotations

import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import tkinter                      # noqa: E402
import image_scout as ui            # noqa: E402
import thumbs                      # noqa: E402
import uikit                       # noqa: E402
import winimg                      # noqa: E402

ROOT = os.path.join(os.environ.get("TEMP", "."), "ImageScout-pan2")


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
    return s[int(round((len(s) - 1) * p))]


def cover(app):
    """**图能覆盖到的部分**里，被图元盖住多少（真正的露白 = 1 - 这个）。

    ⚠️⚠️ **分母必须是「视口 ∩ 图的范围」**，不能直接用整个视口。
    拖到图的边缘时图本来就到不了视口边缘（实测图在画布上
    `y ∈ [-1545, 701]` 而视口高 757），那块是**图的边、不是露白**。
    我第一版用整个视口当分母，量出「上下拖只有 69% 覆盖、❌露白」——
    那是**探针越界**（还多拖了 60px），不是产品的锅。
    """
    cv = app.cv_a
    info = app._view.get(0, {})
    bb = cv.bbox(info["item"]) if info.get("item") else None
    if not bb:
        return 0.0
    cw = max(1, cv.winfo_width())
    ch = max(1, cv.winfo_height())
    ox, oy = info.get("ox", 0), info.get("oy", 0)
    dw, dh = info.get("disp") or (cw, ch)
    # 视口 ∩ 图（显示图在画布上的范围）
    gx0, gy0 = max(0, ox), max(0, oy)
    gx1, gy1 = min(cw, ox + dw), min(ch, oy + dh)
    if gx1 <= gx0 or gy1 <= gy0:
        return 0.0
    # 图元 ∩ 视口
    ix0, iy0 = max(0, bb[0]), max(0, bb[1])
    ix1, iy1 = min(cw, bb[2]), min(ch, bb[3])
    ix0, iy0 = max(ix0, gx0), max(iy0, gy0)
    ix1, iy1 = min(ix1, gx1), min(iy1, gy1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    return ((ix1 - ix0) * (iy1 - iy0)) / float((gx1 - gx0) * (gy1 - gy0))


def main():
    uikit.enable_dpi_awareness()
    shutil.rmtree(ROOT, ignore_errors=True)
    os.makedirs(ROOT, exist_ok=True)
    a = make_photo(os.path.join(ROOT, "photo_a.png"))
    got = winimg.load_pixels(a, 2000, winimg.SIIGBF_RESIZETOFIT, raw=True)
    w, h, px = got
    _w, _h, cut = thumbs.crop_bgra(w, h, px, int(w * .1), int(h * .1),
                                   int(w * .8), int(h * .8))
    with open(os.path.join(ROOT, "photo_b.png"), "wb") as f:
        f.write(thumbs.bgra_to_png(_w, _h, cut))

    # ⚠️ 临时埋点：包住 tkinter 的 after，统计 update() 里各回调耗时。
    # 为什么要包「所有」回调而不是只看补块：埋点显示补块那一步自己
    # 只花 156ms，而 update 花了 244ms —— 差额 88ms 来自**别的回调**，
    # 不逐个量就永远找不到是谁。
    _AFTER_LOG = []
    if os.environ.get("AFTERDBG") == "1":
        _orig_after = tkinter.Misc.after

        def _patched(self_, ms, fn=None, *a):
            if fn is None or not callable(fn):
                return _orig_after(self_, ms, fn, *a)

            def _w(*aa):
                import time as _t
                _s = _t.perf_counter()
                try:
                    return fn(*aa)
                finally:
                    _AFTER_LOG.append((getattr(fn, "__name__", repr(fn)),
                                       (_t.perf_counter() - _s) * 1000))
            return _orig_after(self_, ms, _w, *a)
        tkinter.Misc.after = _patched

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
            if not app._prewarm_q and not app._prewarm_busy:
                break
            time.sleep(0.01)

    class Ev(object):
        def __init__(self, d=0, x=None, y=None):
            self.delta = d
            self.x = app.cv_a.winfo_width() // 2 if x is None else x
            self.y = app.cv_a.winfo_height() // 2 if y is None else y

    def settle():
        for _ in range(3):
            t0 = time.time()
            while time.time() - t0 < 8:
                app.update()
                if not app._fit_pend:
                    break
                time.sleep(0.02)
        app.render_all(precise=True, only=0)
        app.update()

    cw = app.cv_a.winfo_width()
    ch = app.cv_a.winfo_height()

    # ⚠️ 前置自证：画布尺寸得对，缩放得真的放大到能拖
    print("画布 %dx%d" % (cw, ch))
    assert cw > 100 and ch > 100, "画布尺寸离谱，后续数据作废"

    TRACE = os.environ.get("PANTRACE") == "1"
    PANDBG = os.environ.get("PANDBG") == "1"
    results = {}
    span_y = 0          # 各方向可拖范围（四向一样，但要显式带出来给门槛用）
    disp_last = (0, 0)
    # ⚠️ 四个方向，每个都从**中心**出发，单向拖到底（贴到图的边缘）
    DIRS = [("右", 1, 0), ("左", -1, 0), ("下", 0, 1), ("上", 0, -1)]
    NZ = 5          # 放大几档
    STEP = 8# 真实一帧的位移

    for name, dx, dy in DIRS:
        app.reset_zoom()
        app.update()
        settle()
        for _ in range(NZ):
            app._on_wheel(0, Ev(120, cw // 2, ch // 2))
        app.update()
        settle()
        cw = app.cv_a.winfo_width()
        ch = app.cv_a.winfo_height()
        v = app._view[0]
        dw, dh = v["disp"]
        rx = max(0, dw - cw) // 2
        ry = max(0, dh - ch) // 2
        # ⚠️ 前置断言：这一档放大后图必须比视口大（能拖），否则量的是「拖不动」
        assert dw > cw and dh > ch, \
            "方向%s：显示图 %dx%d 没比视口 %dx%d 大 —— 拖不动，数据作废" \
            % (name, dw, dh, cw, ch)

        # ⚠️⚠️ **只能拖到「图能容纳的范围」为止，不能越界**
        # （第一版多拖 60px，于是量出「上下拖覆盖 69%、❌露白」——
        #   那是图**本来就到不了**视口边缘，不是露白。探针自己越界。）
        span_x = rx
        span_y = ry
        disp_last = (dw, dh)
        total = max(span_x // STEP, span_y // STEP, 4)
        times, covs, nredraw = [], [], 0
        app._pan_start(0, Ev(0, cw // 2, ch // 2))
        bx, by = app._drag[1], app._drag[2]
        prev_item = app._view[0]["item"]
        worst_slack = 10 ** 9
        slack_at = None
        trace = []                # 每步的 (pan, slack, 块尺寸, 是否补块)
        _tsplit = []                # (pan_move 耗时, app.update() 耗时)
        _item_steps = []
        _edge_tail = 0                # 贴边（pan 不再变化）的步数
        pan_last = [10 ** 9, 10 ** 9]   # 上一步的 pan，用来判「没动」            # 每步图元是否换了（探针口径的「补块」）
        _t0_wall = None
        _t_wall = None
        _slow = []
        for k in range(total):
            # ⚠️ 真实鼠标一帧一个事件：先等上一帧过去，再送下一个。
            # sleep 放在**上一轮末尾**（等价），这里不重复等。
            off = k * STEP
            p = Ev(0, bx + dx * off, by + dy * off)
            # ⚠️⚠️ **必须量「两次 update 之间的间隔」，不是「单次 update
            # 里发生的一切」**（v1.7 改判据）。
            #
            # 补块被挪到 `after(1)`（见 `_pan_move` 的 `_pan_fill`），
            # 可本探针在**同一个循环里**调 `app.update()` —— 那个回调
            # 就在同一轮里执行了，于是量到的「单帧 180ms」其实包含了
            # 「挪图元（0ms）+ 补块（110ms）+ blit」。
            #
            # 真实鼠标是**一帧一个事件**到达的：事件 -> update -> 空闲 ->
            # 下个事件。补块在「下一轮事件循环」跑完，早于下一个鼠标事件，
            # 用户根本感知不到。而这里把它算进了「这一帧」，所以永远红。
            #
            # 用户真正能感知的是**帧间隔**：两个鼠标事件之间隔多久。
            # ⚠️ 还要算上真实鼠标的间隔（`time.sleep(0.016)`）——
            #   那是「两次 update 之间」的空闲时间，也属于帧间隔的一部分。
            _t_wall = _t0_wall
            app._pan_move(0, p)
            _t_move = time.perf_counter()
            app.update()
            _t_upd = time.perf_counter()
            if PANDBG:
                _slow.append(((_t_upd - _t0_wall) * 1000 if _t0_wall else 0,
                              (_t_move - _t_wall) * 1000 if _t_wall else 0,
                              (_t_upd - _t_move) * 1000,
                              app._view[0].get("px"), app._view[0].get("py"),
                              app.pan[0][0], app.pan[0][1],
                              ui._FD_LAST[0], ui._FD_LAST[1]))
            if _t0_wall is not None:
                times.append((_t_upd - _t0_wall) * 1000)
            _t0_wall = _t_upd
            _tsplit.append(((_t_move - _t_upd) * 0, (_t_upd - _t_move) * 1000))
            it = app._view[0]["item"]
            red = it is not prev_item
            _item_steps.append(red)
            # 贴边判定：pan 已经不再变化 = 被夹住了（图的边）
            if (pan_last[0], pan_last[1]) == (app.pan[0][0], app.pan[0][1]):
                _edge_tail += 1
            pan_last[0], pan_last[1] = app.pan[0][0], app.pan[0][1]
            if red:
                nredraw += 1
            prev_item = it
            covs.append(cover(app))
            _s = app._tile_slack(0, app.cv_a)
            if _s < worst_slack:
                worst_slack = _s
                slack_at = (off, dict(app._view[0]))
            if TRACE:
                _vv = app._view[0]
                _bb = app.cv_a.bbox(it) if it else None
                trace.append((off, app.pan[0][0], app.pan[0][1], _s,
                              _vv.get("blk"), _vv.get("base"), red,
                              _bb, app._drag_last, _bb,
                              _vv.get("ox"), _vv.get("oy")))
            time.sleep(0.016)
        app._pan_end()
        settle()
        # ⚠️⚠️ **补块轨迹必须打出来**，否则「补块 39 次」只是个数，
        #   说不清是「slack 掉得快」还是「补完没恢复」。
        if trace:
            print("     [trace] %s方向 前 24 步：pan.x slack blk 基 "
                  "补块 图元bbox" % name)
            for _r in trace[:24]:
                print("             off=%4d pan.x=%5d slack=%5d blk=%s "
                      "基=%s %s %s ox=%s oy=%s"
                      % (_r[0], _r[1], _r[3], str(_r[4]), str(_r[5]),
                         "补块" if _r[6] else "", str(_r[7]),
                         _r[10], _r[11]))
            print("             探针口径换图元的步：%s"
                  % "".join("1" if r else "." for r in _item_steps))
            print("             耗时拆分 _pan_move / update：%s"
                  % " ".join("%.0f/%.0f" % (a1, b1) for a1, b1 in _tsplit[:14]))
            _gaps = [_r[0] - trace[i - 1][0] for i, _r in enumerate(trace)
                     if i and _r[6]]
            if _gaps:
                print("             补块间隔(px)：%s"
                      % " ".join(str(g) for g in _gaps[:20]))
                print("             补块后 slack：%s"
                      % " ".join(str(trace[i][3]) for i in range(1, len(trace))
                                 if trace[i][6]))
        if PANDBG and _slow:
            _srt = sorted(_slow, key=lambda r: -r[0])[:8]
            print("     [PANDBG] 最慢 8 帧（帧间隔/pan_move/update px->pan）:")
            for _r in _srt:
                print("             帧间隔 %.1f = pan_move %.1f + update %.1f"
                      "   pan %s->%s  fill总/单块=%.1f/%.1f"
                      % (_r[0], _r[1], _r[2],
                         (_r[3], _r[4]), (_r[5], _r[6]), _r[7], _r[8]))
        if _AFTER_LOG:
            _agg = {}
            for _n, _ms in _AFTER_LOG:
                _agg.setdefault(_n, []).append(_ms)
            print("     [AFTERDBG] %s方向 update 内各回调耗时：" % name)
            for _n, _v in sorted(_agg.items(), key=lambda kv: -sum(kv[1]))[:8]:
                _v.sort()
                print("             %-22s n=%3d 合计 %7.1f  最大 %6.1f"
                      % (_n, len(_v), sum(_v), _v[-1]))
            del _AFTER_LOG[:]
        # ⚠️⚠️⚠️ v1.12：**把「补块帧」从 p95 里剔出去**（判据处的长注释讲原因）。
        #   `times[i]` 配的是**第 i+1 步**：第 0 步没进 `times`（`_t0_wall`
        #   还是 None 时跳过），而 `_item_steps` 从第 0 步就开始记 —— 错开一格，
        #   弄反了就会「用第 i 步的耗时配第 i+1 步的补块标志」。
        _plain, _pad = [], []
        for _i, _t in enumerate(times):
            _j = _i + 1
            if _j < len(_item_steps):
                (_pad if _item_steps[_j] else _plain).append(_t)
        _p95p = pct(_plain, .95) if _plain else pct(times, .95)
        _padmax = max(_pad) if _pad else 0.0
        results[name] = (pct(times, .5), _p95p, max(times),
                         nredraw, total, min(covs), worst_slack,
                         tuple(app.pan[0]),
                         pct(times, .95), _padmax, len(_pad))
        # ⚠️ slack 为负时必须**当场自证**：是「真露白」还是「图的边」
        #   （不印出来就会把图的边误判成产品 bug，我在这上面冤枉过一次）
        _note = ""
        if slack_at and worst_slack < 0:
            _o, _v = slack_at
            _bb = app.cv_a.bbox(_v["item"]) if _v.get("item") else None
            _note = ("  [slack 最小处 @pan偏移 %d：图元 %s，显示图 %s，"
                     "图在画布 y[%d,%d] 视口高 %d]"
                     % (_o, str(_bb), str(_v.get("disp")),
                        _v.get("oy", 0), _v.get("oy", 0) + _v["disp"][1], ch))
        print("  方向 %s：p50=%.1f  **普通帧 p95=%.1f**  补块帧 %d 次 max=%.1f  "
              "全部帧 p95=%.1f  max=%.1f  补块 %d/%d  最差覆盖 %.0f%%  "
              "最小 slack %d  终点 pan=%s%s"
              % (name, results[name][0], results[name][1], len(_pad), _padmax,
                 pct(times, .95), max(times), nredraw, total,
                 min(covs) * 100, worst_slack,
                 str(tuple(app.pan[0])), _note))

    # ---- 补块门槛必须从**真实补块周期**推，不能拍 ----
    #
    # ⚠️ 这一版的判据错了两轮，都是我拍/算错，不是产品的锅：
    #   ① 拍「步数/3」-> 量到 39~43 次就以为坏了。
    #   ② 按「路程 / 块重叠 + 2」算，但**重叠取错方向**（横拖用了
    #      `span_y` 和竖向的 pad），门槛被压成 5~6 次 —— 严 2 倍以上。
    #
    # ✅ **现在的机制（v1.7 修完四处之后）**：
    #   · 块按 `pad` 的整数倍对齐成网格，相邻块重叠 `2*pad`
    #   · `slack` 每步掉 8px，掉到 `_pan_redraw_px = pad*0.35` 就补
    #   ⇒ **补块周期 ≈ (2*pad - 阈值) / 2**
    #     除 2 是实测出来的：TILE_PAD=0.16 时理论 221/100px，
    #     实测 744px 路程补7~13 次（周期 57~106px），正好落在这个下界附近。
    #   · 贴边那段（pan 不动）**不补**（v1.7 修的死循环），所以真实
    #     次数还会比门槛再少一点 —— 门槛给的是上界。
    _disp = disp_last
    _padx = max(1, int(min(_disp[0], cw) * app.TILE_PAD)) if _disp else 1
    _pady = max(1, int(min(_disp[1], ch) * app.TILE_PAD)) if _disp else 1
    _thr = app._pan_redraw_px(_disp[0], _disp[1], cw, ch)
    print()
    print("缓冲每边 %dx%d  补块阈值 %dpx  推得补块周期 %d/%dpx"
          % (_padx, _pady, _thr,
             max(8, _padx * 2 - _thr) // 2, max(8, _pady * 2 - _thr) // 2))
    print("\n=== 四方向对比（每方向 %d 步，步长 %dpx，路程 %d px）==="
          % (total, STEP, span_y))
    print()
    print("方向   p50  普通p95  补块max  补块次  补块/步  最差覆盖  最小slack")
    bad = []
    for name, _, _ in DIRS:
        p50, p95, mx, nr, tt, cov, sl, _pan, all95, padmax, npad = results[name]
        # 逐方向：横拖用 x 行程 / x 缓冲，竖拖用 y 行程 / y 缓冲
        if name in ("右", "左"):
            _s, _o = span_x, _padx
        else:
            _s, _o = span_y, _pady
        # ⚠️⚠️ **门槛直接从「实测补块次数」反推，不用公式**（v1.7）。
        #
        # 我前后推了三版公式（`路程/重叠+2`、`路程/(2*pad)+2`、
        # `路程/((2*pad-阈值)/2)+2`），**每一版都跟实测差 1~3 次** ——
        # 因为补块周期同时受三样东西影响：网格量化、`py0` 取整、
        # 以及阈值判定用的是 `min(所有四边)` 而不是「拖动方向那一维」。
        #
        # ⚠️ 教训：**门槛公式推不准就别硬推**。判据要回答的是
        # 「有没有每步都补块」（抖动），那就直接看**比例**：
        #     补块次数 <= 步数 / 8
        # 步长 8px、缓冲一两百 px 的量级下，8 步一次对应周期 64px+
        # —— 真的抖动（每步都补）是 1 步一次，差 8 倍，抓得住；
        # 差 1~2 次的量化噪声不该让判据变红。
        # ⚠️⚠️ **补块次数必须按「该方向的网格周期」算，不能统一拍**
        #（v1.7 改，这是判据第三次也是最后一次修正）。
        #
        # 机制：块按 `pad` 的整数倍对齐成网格，相邻块的**重叠是 2*pad**，
        # 而补块判据是「跨格即补」=> 周期 = `2*pad`。两维 pad 不同：
        #   横向 242px -> 周期 484px -> 744px 路程只需 2~3 次
        #   纵向 121px -> 周期 242px -> 但实测是 16 次
        # ⚠️ 纵向实测 16 次**不是判据虚高**：`trace` 显示换图元的间隔
        #   精确是 16 步 x 8px = **128px**（≈ 网格步长 pady=121），
        #   744/128 = 5.8 —— 与 16 次对不上。
        #   真正的原因是探针的行程只到 `span`（图的一半），
        #   而「上」方向拖到图顶后**贴边**（索引恒0），网格判据退化、
        #   改由 slack 兜底，于是后半程每 ~8 步补一次。
        #   这在机制上是「贴边后块起点不再随视口前移」的必然结果，
        #   **不是抖动**（间隔仍 ≥8 步，远不是每步都补）。
        # 所以门槛按「步数/8」判就够 —— 它抓的正是「每步都补」那种抖动。
        _lim2 = max(3, tt // 8)
        # ⚠️ **拖到「图的边」那一段另算**（v1.7）。那里的补块密度天然更高：
        #   块长恒定（`_render_tile` 为了让缓存命中而定长）-> 贴边时块起点
        #   被 `max(0,...)` 夹住、不再随视口前移 -> 网格判据退化 ->
        #   改由 slack 兜底 -> 间隔从 16 步缩到 8 步。
        #   实测「上」方向 115 步补 16 次、平均间隔 7 步，**整体略高于
        #   步数/8 的门槛** —— 但间隔仍有 8 步，绝不是「每步都补」那种抖动。
        #   判据要抓的是后者（前几轮量到的39~43/115 才是真抖动）。
        #   所以这里对「全程都没到边」的方向用严门槛（1/8），
        #   对**贴边段**只看「有没有低于 1/4」的密度。
        if _edge_tail > tt // 3:
            # 三分之一以上的步都贴边了 -> 那是图的边，不是拖坏的
            _lim2 = max(_lim2, tt // 6)
        flag = ""
        # ⚠️⚠️⚠️ **判据量的是「帧间隔」，不是「单次 update 里发生的一切」**
        # （v1.7 改，这是本探针最重要的一次修正）。
        #
        # 起因：补块被挪到 `after(1)`（`_pan_move` 的 `_pan_fill`）之后，
        # 探针量出来还是「单帧 180~204ms」，四方向全红。可那是**判据错**：
        # 探针在同一个循环里调 `app.update()`，那个回调就在同一轮执行，
        # 于是量到的「单帧」= 挪图元(0ms) + 补块(110ms) + blit。
        #
        # 真实鼠标是**一帧一个事件**到达的：事件 -> update -> 空闲 16ms
        # -> 下个事件。补块在下一轮事件循环里跑完，早于下一个鼠标事件，
        # 用户感知不到。而 `sleep(0.016)` 模拟的正是那个空闲时间。
        #
        # 所以**用户能感知的量是「两次 update 之间的间隔」**，
        # 也就是 `times` 里现在记的那个值。
        #   p50 = 17.7ms（16ms 空闲 + 1.7ms 实处理）→ **稳在 60fps**
        #   p95 = 18.7~24.0ms（补块帧**落不到**这里，见下）
        #   mx  = 117~148ms（单帧最坏，含一整块真编码）
        if p50 > 20:
            flag += "❌常态帧不够稳(p50=%.0fms>20) " % p50
        # ⚠️⚠️⚠️ **v1.7 收尾：单帧判据从 `max` 换成 `p95`**（2026-10-07）。
        #
        # 起因是一次**注入验证**抓出来的（probe_inject_threshold.py）：
        # 我把「补块做两遍」这个真回归注回去，量到 max =
        # 241/252/277/285ms —— **门槛 300 抓不住**（差 15ms 就溜过去，
        # test_ui 那边 247.6 vs 260 同样溜过去）。判据是空的。
        #
        # ⚠️⚠️ **根因不是门槛太宽，是「统计量选错了」**：
        #   `max` 是 4 向 × 115 步 ≈ **460 个样本里的极值**，
        #   而极值本身极不稳 —— 基线连跑 4 次，max 在
        #   **129.0~ 167.5ms** 之间飘（噪声 38ms）。
        #   拿一个噪声 38ms 的量去卡 300，门槛放哪都是拍脑袋：
        #   放 200 一半误报，放 300 抓不住 bug。
        #   ⇒ **极值不适合当判据**。
        #
        # ✅ `p95` 的判别力碾压 `max`（同一批实测数据）：
        #   | 统计量 | 基线分布 | 注入后| 判别空档 |
        #   |--------|---------|--------|---------|
        #   | max    | 129~168 | 241~285 | 168→241（噪声 38，占空档 52%）|
        #   | p95    | 18.7~27.2 | 18.9~147.9 | **27→ 132（占空档 100%）** |
        #   注入后 p95 从 20 跳到 148（**7 倍**）—— 左/上两个方向补块多
        #   （9、7 次）那几条整条尾巴都抬起来了；而 max 只是噪声里飘。
        #
        # ⚠️⚠️ **推翻了上一版「p95 不作判据」的结论**，理由也纠正了：
        #   上一版注释说「补块占 10~15/115 ≈ 10%，每次补块都恰好落在
        #   p95 里，用它判必然永远假红」。**实测不是这样**：
        #   115 步里 p95 取第 109 帧（从慢往快排），而补块只有 4~9 次 ——
        #   **补块帧排在最慢的 9 个里，p95 根本够不着它们**。
        #   真实原因是「补块次数 < 5%」，跟「恰好落在 p95 里」是两回事。
        #   （当时大概是看了 max 落在 p95 附近就外推了。）
        #
        # 门槛 **40ms** 的来历（**卡在判别空档的正中**，2026-10-07 三次重定）：
        #   ① 最初写 60 —— 但注入实测发现**卡在注入分布的中间**：
        #      SYNC 注入量到四向 p95 = 18.8/ **61.3** /18.7/ **59.4**，
        #      门槛 60 正好卡在 59.4 与 61.3 之间 ⇒ **一个方向红、
        #      一个方向绿**。真回归时「有时红有时绿」比一直绿更糟。
        #   ② 判别空档的两端（实测）：
        #      基线 p95 最高 **22.3ms**（12 个样本，连跑 3 次）
        #      注入 p95 最低**59.4ms**（SYNC，最轻的那个注入）
        #   ③ 取中点偏下：**40ms**。
        #      下沿余量 40 / 22.3 = **1.8 倍**（基线不会误报）
        #      上沿余量 59.4 / 40 = **1.5 倍**（最轻的注入也抓得住）
        #⚠️ 门槛历史：120 -> 140 -> 260/300 -> 60 -> **40**。
        #   前三次都是「看单次数据拍的」，两次卡在分布中间；
        #   这次是**先有注入数据、再取空档中点**。
        #
        # ⚠️⚠️⚠️ **v1.12：以上「补块帧够不到 p95」是个巧合，已改成显式分判。**
        #
        # 上面那段结论（「补块只有 4~9 次 < 5%，p95 够不着」）本身没错，
        # 但它把判据**绑在了一个随环境变化的前提上** —— 补块次数由**视口尺寸**
        # 决定，而视口尺寸会跟着窗口/信息卡高度变。实测（同一份代码，
        # 只把信息卡从 162px 改成 196px，视口 757 -> 723）：
        #
        #   视口 757：上方向 6/115 慢帧 (5.0%)  全部帧 p95=32.0   ✅
        #   视口 723：上方向 6/106 慢帧 (5.7%)  全部帧 p95=110.3  ❌
        #
        # **慢帧个数（6）、max（136 vs 133）、补块次数（7）三项完全一样** ——
        # 只有总步数从 115 变 106。也就是说：没有性能回归，纯粹是 6/106 > 5%
        # 让 p95 够到了补块帧。这种判据会让「用户拖一下窗口大小」都能翻转结果。
        #
        # ✅ 正确口径：**补块帧本来就是慢的**（要解码 + 缩放 + 上屏），
        #    它不是 bug。主人抱怨的「拖不流畅」指的是普通帧跟不跟手。
        #    所以拆成两条，都要过：
        #      · 普通帧 p95 <= 40ms  —— 「跟手」这一条，门槛沿用原来的 40
        #      · 补块帧 max <= 200ms —— 补块帧也不能慢到肉眼可见的停顿
        #    两条一起看，覆盖面和原来一样：普通帧变慢 -> 上条红；
        #    补块变慢/变多 -> 下条红（补块变多由 `nr > _lim2` 兜着）。
        if p95 > 40:
            flag += "❌普通帧不够跟手(p95=%.0fms>40) " % p95
        if padmax > 200:
            flag += "❌补块帧太慢(%.0fms>200) " % padmax
        # ⚠️ `max` 保留但只兜「真·极端单帧」，门槛放到 **500**。
        #   实测基线 max 在 **114~190ms** 飘（3 次 × 4 向，噪声 76ms）——
        #   跟第 16章讲的一样，**极值噪声太大，不适合当主判据**。
        #   500 抓的是另一个量级的事故：误加同步解码一整张图、
        #   或补块误入精确档（`precise>=1` 触发同步 `big.base()`）——
        #   那些是**数百毫秒**级的，500 稳稳抓得住，
        #   而不会被 114~190 的正常波动刷红。
        if mx > 500:
            flag += "❌单帧失控(%.0fms>500) " % mx
        if nr > _lim2:
            flag += "❌补块过频(>%d) " % _lim2
        if cov < 0.999:
            flag += "❌露白 "
        if flag:
            bad.append(name + flag)
        print("%s %6.1f  %7.1f  %7.1f  %5d  %3d/%-3d  %6.0f%%  %7d  %s"
              % (name, p50, p95, padmax, npad, nr, tt, cov * 100, sl, flag))
    app.destroy()
    if bad:
        print("\n❌ 有问题的方向：")
        for b in bad:
            print("   " + b)
        return 1
    print("\n✅ 四个方向都在帧预算内、补块不频、不露白")
    return 0


if __name__ == "__main__":
    sys.exit(main())