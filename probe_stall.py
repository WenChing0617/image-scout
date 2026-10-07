# -*- coding: utf-8 -*-
"""诊断：拖动补块那 86ms 到底花在哪（v1.8，主人 2026-10-07 报「拖拽卡顿」）。

## 实测结论（v1.8 修之前）

1328×2048、zoom 3.815、拖 150 步：

    帧耗时中位 87.1 / p95 103.9 ms
    `_render_tile` 129 次调用，**缓存命中 0 次**
    块高733 -> 747 -> 750 -> 753 … **每拖 4px 变 3px**

⇒ 根因不是「编码慢」，是 **`pkey` 每步都变**（纵向块长跟视口滑动），
所以每帧都在真编码同一块内容。块两维都定长之后 pkey 只随网格变，
第二次起就是缓存命中。

## ⚠️⚠️ 判据必须能抓住这两个回归

只量「帧耗时」抓不住「露白」——而**为了不卡而把块定长，最容易踩的坑
就是露白**（块不随视口前移，缓冲被消耗完却不恢复）。所以两条都要量。

用法：python probe_stall.py
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

ROOT = os.path.join(os.environ.get("TEMP", "."), "ImageScout-stall")
FAILS = []


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


class Ev(object):
    def __init__(self, d=0, x=0, y=0):
        self.delta = d
        self.x = x
        self.y = y


def main():
    uikit.enable_dpi_awareness()
    shutil.rmtree(ROOT, ignore_errors=True)
    os.makedirs(ROOT, exist_ok=True)
    a = make_photo(os.path.join(ROOT, "s.png"), 1328, 2048, 11)
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
    app.set_pair(a, None)
    app.update()
    for _ in range(4):
        t0 = time.time()
        while time.time() - t0 < 8:
            app.update()
            if not app._fit_pend:
                break
            time.sleep(0.02)
        app.render_all(precise=True)
        app.update()
    cw = app.cv_a.winfo_width()
    ch = app.cv_a.winfo_height()
    for _ in range(6):
        app._on_wheel(0, Ev(120, cw // 2, ch // 2))
        app.update()
    app.render_all(precise=True)
    app.update()
    v = app._view.get(0) or {}
    print("zoom %.3f disp %s 画布 %dx%d 基准 %s" % (
        app.zoom[0], v.get("disp"), cw, ch, v.get("base")))
    print("缓冲 pad=%s" % (v.get("pad"),))

    # ---- 包一层，量每一次 _render_tile 的 pkey 与命中情况 -----------
    stat = {"calls": 0, "hit": 0, "miss": 0, "keys": [], "ms": []}
    orig = app._render_tile

    def wrapped(col, cv, cap, path, box, zoom, precise=True):
        stat["calls"] += 1
        t0 = time.time()
        r = orig(col, cv, cap, path, box, zoom, precise)
        stat["ms"].append((time.time() - t0) * 1000)
        vv = app._view.get(col) or {}
        k = vv.get("blk")
        stat["keys"].append((tuple(app.pan[col]), k, vv.get("grid")))
        return r

    app._render_tile = wrapped
    #同时统计 big 缓存命中
    phits = {"hit": 0, "miss": 0}
    ophoto = app.big.photo

    def p2(key):
        r = ophoto(key)
        if isinstance(key, tuple) and key and key[0] == "tile":
            phits["hit" if r is not None else "miss"] += 1
        return r

    app.big.photo = p2

    # ================================================================
    # ⚠️⚠️⚠️ **判据 1 必须在「往返拖」上量，单程量不到缓存命中**
    #（v1.8 改判据时踩的坑：判据报错，先怀疑判据）。
    #
    # 单程拖只会**一路跨新格**（grid 0,7 -> 8 -> 9 -> 10 -> 11），
    # 每格的 `py0` 都不同 => 每格都是新内容 => **pkey 必然 miss**。
    # 所以「命中率」在单程里恒等于 0%，而那**完全正常**。
    #
    # v1.7 的判据（「命中 > 0、命中率 > 50%」）能过，是因为那时候
    # **每一步都补块**（判据 bug），同一步反复查同一个 key 当然命中 ——
    # 它量到的是「判据有多频繁」，不是「缓存有没有效」。
    # 那个 bug 修掉之后这条判据就**自动失效**了（不是产品变差）。
    #
    # ✅ 正确口径分两条：
    #   ① 单程：`_render_tile` 调用数（= 跨格数）必须**远小于步数**
    #      —— 这是「没有每步都补块」的独立证据（v1.7 修的正是这个）。
    #   ② 往返：**回拖到走过的格子必须命中缓存** ——
    #      这是「pkey 稳定」的直接证明，也是主人真正在意的
    #      「来回拖不卡」（同一块内容第二次该几毫秒就几毫秒）。
    #
    # ⚠️⚠️ **往返段必须放在单程之前**（判据自己的坑，第三次了）。
    #   往返要「拖出去再原路回来」，前提是**起点有富余**。
    #   单程拖完已经贴边（pan -600 到头），此时无论怎么复位都量不到：
    #     · `reset_zoom()`-> zoom 退回 1.0，`_render_tile` 整条路都不走
    #       （grid=None，tile 缓存一次都查不到）；
    #     · 只改 `pan[0]` 不重画 -> `_drag` 起点与 `_view` 不一致，
    #       `_pan_move` 的夹持算出空区间 -> **120 步pan 一动不动**。
    #   放在最前面时 pan 刚居中（±（disp-cw)/2 的余量全在），不用任何复位。
    # ================================================================
    phits["hit"] = phits["miss"] = 0
    _keys_b = []
    _all_b = []
    obig = app.big.photo

    def p3(key):
        r = obig(key)
        if isinstance(key, tuple) and key and key[0] == "tile":
            # ⚠️⚠️ **必须区分「拖动补块」与「松手精确帧」**（判据自己的坑，
            #   第五次）。`_pad_scale = 1.0 if _pan_redraw_active else 0.55`
            #   ——松手那一帧的精确档 pad **更小**，于是 `_gridy`/`py0`
            #   算出来就是另一组 key。那是**设计如此**（精确帧要的分辨率
            #   和缓冲都不同），不是「pkey 还在滑」。
            #   混在一起统计 => 每次松手都贡献 1~2 次必然的 miss，
            #   命中率被凭空拉低（实测 46%，而补块本身全命中）。
            _tag = "pan" if app._pan_redraw_active else "exact"
            _rec = (key[5:7], _tag)
            _all_b.append(_rec)
            if _tag == "pan":
                phits["hit" if r is not None else "miss"] += 1
        return r

    app.big.photo = p3
    _v0 = app._view.get(0) or {}
    check(_v0.get("grid") is not None,
          "往返量之前仍在分块路径（grid=%s）—— 若为 None 说明缩放被退回 1.0，"
          "整条 tile 缓存都不会被查" % (_v0.get("grid"),))
    # ① 先往一个方向拖出若干格
    # ⚠️⚠️ **`_pan_start` 不能漏**（判据自己的坑，第四次了）。
    #   `_pan_move` 头一句就是 `if not d or d[0] != col: return`——
    #   没有 `_drag` 就**一步都不动**。我第一版把这段写在
    #   单程段之后、只顾着复位pan，把 `_pan_start` 顺手漏了，
    #   量到 `pan (0,0) -> (0,0)`、120 步全废。
    _pan0 = tuple(app.pan[0])
    app._pan_start(0, Ev(0, cw // 2, ch // 2))
    for k in range(1, 121):
        app._pan_move(0, Ev(0, cw // 2 + k * 6, ch // 2 + k * 5))
        app.update()
    app._pan_end()
    app.update()
    _pan1 = tuple(app.pan[0])
    check(_pan1 != _pan0,
          "拖出阶段真的动了（%s -> %s）—— 没动就是 `_pan_start` 漏了或贴边了"
          % (_pan0, _pan1))
    # ② 原路拖回来 —— 这段每一步都踩在**已经画过的格子**上
    #
    # ⚠️⚠️⚠️ **回拖前必须清零计数**（判据自己的坑，第六次）。
    # 缓存的**语义**就是「同一块内容第二次起才命中」——
    # 每一格**第一次**渲染必然 miss，那不是浪费，那是缓存**正在工作**。
    # 拿全程当分母的话：拖出 6 次必然 miss + 回拖 12 次命中
    # => 命中率被算成 12/25 = 48%，明明「回拖 12 步 12 次全中」。
    # 所以**只量回拖段**，那时候每一格都是第二次访问。
    _n_out = phits["hit"] + phits["miss"]
    _out_keys = [k for k, t in _all_b]
    phits["hit"] = phits["miss"] = 0
    del _all_b[:]
    app._pan_start(0, Ev(0, cw // 2 + 121 * 6, ch // 2 + 121 * 5))
    for k in range(120, 0, -1):
        app._pan_move(0, Ev(0, cw // 2 + k * 6, ch // 2 + k * 5))
        app.update()
    app._pan_end()
    app.update()
    _pan2 = tuple(app.pan[0])
    app.big.photo = obig
    back_hits = phits["hit"]
    back_all = phits["hit"] + phits["miss"]
    back_rate = back_hits / float(max(1, back_all))
    print("   往返：pan %s -> %s -> %s" % (_pan0, _pan1, _pan2))
    print("   拖出段 tile 查询 %d 次（每格首次必然 miss，缓存正在工作）" % _n_out)
    print("   拖出 pkey：%s" % " ".join(str(k) for k in _out_keys[:10]))
    print("   回拖 pkey：%s" % " ".join(
        "%s/%s" % (k, "P" if t == "pan" else "E") for k, t in _all_b[:20]))
    print("   回拖段：tile 查缓存 命中 %d / 未命中 %d（命中率 %.0f%%）"
          % (back_hits, back_all - back_hits, back_rate * 100))
    check(back_hits > 0,
          "回拖走过的格子必须命中缓存（命中 %d / 共 %d）—— 全 0 说明 pkey "
          "还在滑，来回拖每次都在真编码" % (back_hits, back_all))
    check(back_rate > 0.9,
          "回拖段缓存命中率 > 90%%（实测 %.0f%%，%d/%d）—— 拖动补块"
          "本该次次命中" % (back_rate * 100, back_hits, back_all))
    # 回到居中，好让后面的单程/露白段从同一个状态出发
    app._zoom_anchor(0, cw // 2, ch // 2, app.zoom[0], app.zoom[0])
    app.pan[0][0] = 0
    app.pan[0][1] = 0
    app.render_all(precise=True)
    app.update()

    times = []
    phits["hit"] = phits["miss"] = 0
    app._pan_start(0, Ev(0, cw // 2, ch // 2))
    for k in range(1, 151):
        t0 = time.time()
        app._pan_move(0, Ev(0, cw // 2 - k * 7, ch // 2 - k * 4))
        app.update()
        times.append((time.time() - t0) * 1000)
    app._pan_end()
    app.update()
    app._render_tile = orig

    s = sorted(times)
    p50 = s[len(s) // 2]
    p95 = s[int(len(s) * .95)]
    print("\n拖 150 步：帧 中位 %.1f / p95 %.1f / 最大 %.1f ms" % (
        p50, p95, s[-1]))
    print("_render_tile 被调%d 次：命中 %d / 未命中 %d" % (
        phits["hit"] + phits["miss"], phits["hit"], phits["miss"]))
    if stat["ms"]:
        r = sorted(stat["ms"])
        print("   每次耗时 中位 %.1f / 最大 %.1f ms" % (
            r[len(r) // 2], r[-1]))
    # pkey 变化点：块尺寸/起点变了几次 = 理论上必须重编码几次
    prev = None
    changes = []
    for pan, blk, grid in stat["keys"]:
        sig = (blk, grid)
        if prev is not None and sig != prev:
            changes.append((pan, prev, sig))
        prev = sig
    print("块(尺寸/网格)变化次数 = %d" % len(changes))
    for c in changes[:4]:
        print("    pan%s  %s -> %s" % (c[0], c[1], c[2]))

    # ⚠️⚠️⚠️ **块长必须逐帧恒定，判据直接钉这个**（v1.8 加）。
    #
    # 起因：块长原来是「终点 ceil - 起点 int」，两个取整各自独立、
    # 截断误差互不相关 => `blkH` 在 **730 / 729** 之间抖。
    # 抖了会怎样？**危害比想象中小**：pkey 里含 `py0`（按格变），
    # 所以每格仍是新 key、缓存仍会命中回拖 —— 实测注入后命中率还是 92%。
    # 但它是**隐患**：一旦哪天有别的路径按 blk 尺寸算显示尺寸，
    # 就会变成「同一个格子两次算出不同尺寸」-> 抖动。
    # 所以直接钉死：**同一次拖动里，块尺寸集合必须只有 1 种**。
    _blks = set()
    _bk = (app._view.get(0) or {}).get("base") or (0, 0)
    for _pan, blk, _g in stat["keys"]:
        # ⚠️ **贴图边时块会被 `min(bw, ...)` 截断**，那是正常的：
        #   `vx1 = min(disp_w, vx0 + _nx)`，拖到最右时 `vx1 = disp_w`
        #   -> `px1 = bw` -> 块宽就是整张基准宽（实测 1328 = bw）。
        #   那种尺寸不参与「抖动」判定，只看**没贴边**的那些。
        if blk and blk[0] < _bk[0] and blk[1] < _bk[1]:
            _blks.add(tuple(blk))
    print("   基准 %s；拖动中出现过的块尺寸（已排除贴边截断）：%s"
          % (tuple(_bk), sorted(_blks)))
    check(len(_blks) <= 1,
          "拖动全程块尺寸恒定（实测 %d 种：%s）—— 出现多种说明"
          "「终点取整 - 起点」那种写法回来了，blkH 会 729/730 抖"
          % (len(_blks), sorted(_blks)))

    single_calls = len(stat["keys"])
    print("   单程 %d 步 -> `_render_tile` 调用 %d 次" % (150, single_calls))
    check(single_calls <= 150 // 4,
          "单程 150 步只补 %d 次块（<=37）—— 接近每步一次说明补块判据退化"
          % single_calls)

    # ================================================================
    # ⚠️⚠️ **判据 2：全程不许露白**（这是「定长」最危险的副作用）
    # ================================================================
    app._pan_start(0, Ev(0, cw // 2, ch // 2))
    worst = 0
    worst_at = None
    trace = []
    v0 = app._view.get(0) or {}
    ox0, oy0 = v0.get("ox", 0), v0.get("oy", 0)
    dw0, dh0 = v0.get("disp") or (0, 0)
    # ⚠️⚠️ **露白必须扣掉「图的边界」**（判据自己的坑，v1.7 在
    # `_tile_slack` 里踩过一次，这里是同一个错的另一个出口）。
    #
    # 实测：图 1821×2808 在 1515×757 的视口里，**纵向图比视口高得多**，
    # 而这一步正在往「图的上边」拖 —— 于是图的下沿根本到不了视口下沿，
    # 露白 306px 那是**图的下边界**，不是拖坏了。
    #
    # 图在画布上的真实范围是 `[ox, ox+dw] x [oy, oy+dh]`（含pan），
    # 只有超出这个范围的部分才算「该盖住却没盖住」。
    def gap_of(bb):
        gx = 0
        if ox0 > 0:                       # 图左沿在视口内-> 左边缘算露白
            gx = max(gx, -bb[0])
        if ox0 + dw0 < cw:                # 图右沿在视口内 -> 右边缘算
            gx = max(gx, bb[2] - cw)
        gy = 0
        if oy0 > 0:
            gy = max(gy, -bb[1])
        if oy0 + dh0 < ch:
            gy = max(gy, bb[3] - ch)
        # 两维都不在视口内（图那一边够不到）-> 说明图正被拖出视口，
        # 那是 `_pan_move` 的夹持该管的，不算渲染露白
        return max(gx, gy)

    for k in range(1, 201):
        app._pan_move(0, Ev(0, cw // 2 + k * 6, ch // 2 + k * 4))
        app.update()
        it = (app._view.get(0) or {}).get("item")
        if it is None:
            worst = 10 ** 6
            worst_at = k
            break
        bb = app.cv_a.bbox(it)
        if not bb:
            worst = 10 ** 6
            worst_at = k
            break
        gap = gap_of(bb)
        trace.append((k, gap, bb, (app._view.get(0) or {}).get("grid")))
        if gap > worst:
            worst = gap
            worst_at = k
    app._pan_end()
    app.update()
    print("反向拖 200 步：最大露白 %d px（第 %s 步）" % (worst, worst_at))
    print("   图在画布上范围 x[%d,%d] y[%d,%d]，视口 %dx%d" % (
        ox0, ox0 + dw0, oy0, oy0 + dh0, cw, ch))
    print("   前 4 步露白轨迹：")
    for k, gap, bb, g in trace[:4]:
        print("      步%-4d 露白%-6d bbox=%s grid=%s" % (k, gap, bb, g))
    settled = max([g for _, g, _, _ in trace] or [-1])
    check(settled <= 0,
          "扣掉图的边界后，拖动全程不露白（最大 %d px，第 %s 步）"
          % (worst, worst_at))

    # ================================================================
    # 判据 3：帧耗时（这次的痛点）
    # ================================================================
    check(p50 < 20, "拖动中位帧耗时 %.1f ms < 20 ms（修前 87.1）" % p50)
    check(p95 < 60, "拖动 p95 %.1f ms < 60 ms（修前 103.9）" % p95)

    app.destroy()
    print("\n" + "=" * 60)
    if FAILS:
        print("❌ %d 项不通过：%s" % (len(FAILS), FAILS[0]))
        sys.exit(1)
    print("✅ 拖动不卡、且不露白")


if __name__ == "__main__":
    main()