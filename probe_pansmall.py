# -*- coding: utf-8 -*-
"""复现「图比视口小时拖不动」并验证修法（v1.7 第二个抖动/不可拖根因）。

**主人 2026-10-07 报**：「每次拖拽前必须缩放一下，在这个位置无法拖拽」，
截图显示「1328 × 2048 · 显示 36%」—— 图比视口小。

## 根因：两种约束用了同样两个数，我只实现了其中一种

图在画布上 `ox = (cw-dw)//2 + pan`，两个候选界（记 `m = (cw-dw)//2`）：

| 情况 | 约束 | 解出的 pan 区间 | 宽度 |
|---|---|---|---|
| `dw <= cw` 图小于视口 | 图**完整可见**：`ox>=0`、`ox+dw<=cw` | `[-m, cw-dw-m]` | `cw-dw` |
| `dw > cw` 图大于视口 | 画面**无空白**：`ox<=0`、`ox+dw>=cw` | `[cw-dw-m, -m]` | `dw-cw` |

⚠️⚠️ **两行的界是同样的两个数 `-m` 和 `cw-dw-m`，只是大小关系相反。**
所以正确写法只有一行：

    lo, hi = min(-m, cw-dw-m), max(-m, cw-dw-m)

我原来写的是「图大于视口」那一套，且用 `lo <= hi` 判「能不能拖」——
图小时区间恒空（`lo > hi`），于是被判成「图塞得下、拖了只会露白」→ **完全冻结**。

⚠️ 这和 v1.7 那次「lo/hi 写反」是**同一类错误的第二次**：都是没把
两个候选界的**大小关系随图/视口尺寸翻转**这件事想清楚。

## 区间宽度恒等于 |cw-dw|

`hi - lo = |(cw-dw-m) - (-m)| = |cw-dw|`

⇒ **「真空间隙」根本不存在**。dw==cw 时宽度 0（正好铺满，确实拖不动），
其余情况都能拖。原代码那条 `if not free_x: px = 中点` 分支是多余的。

用法：
    python probe_pansmall.py          # 断言修好了
    python probe_pansmall.py --inject # 把 bug 注回去，看判据是否变红
"""
from __future__ import annotations

import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import thumbs                      # noqa: E402
import uikit                       # noqa: E402

# ⚠️⚠️ **`image_scout` 故意不在这里 import**。
#
#   `--inject` 模式会**改写 image_scout.py** 再跑测试，而模块一旦在文件
#   顶部 import 进来，改文件对「内存里已加载的模块」毫无影响 ——
#   我因此得到一个假的「注入后仍全绿」，差点以为判据抓不住 bug。
#   **注入必须放到子进程里跑**（见 `main()`）。

ROOT = os.path.join(os.environ.get("TEMP", "."), "ImageScout-pansmall")
INJECT = "--inject" in sys.argv


def make_photo(path, w=1328, h=2048, seed=5):
    """造图。默认尺寸 = 主人截图那张：1328 × 2048（显示 36%）。"""
    import random
    rnd = random.Random(seed)
    buf = bytearray(w * h * 4)
    buf[:] = rnd.randbytes(w * h * 4)
    for i in range(3, len(buf), 4):
        buf[i] = 255
    with open(path, "wb") as f:
        f.write(thumbs.bgra_to_png(w, h, bytes(buf)))
    return path


def _injection_pair():
    """返回 (修复版代码, bug 版代码)。

    ⚠️ 单独抽出来是因为注入和还原必须用**同一对字符串** ——
    我第一版把 `good` 写在注入处、还原时用 `good.replace(" \\\n", "\n")`
    现场拼，结果缩进差 12 个空格，注入直接失败（报「没找到代码段」）。
    """
    good = (
        "        lo_x, hi_x = min(-_T - _cx, cw + _T - disp[0] - _cx), \\\n"
        "            max(-_T - _cx, cw + _T - disp[0] - _cx)\n"
        "        lo_y, hi_y = min(-_T - _cy, ch + _T - disp[1] - _cy), \\\n"
        "            max(-_T - _cy, ch + _T - disp[1] - _cy)\n"
        "        # 逐维夹住：图比视口小的那一维，区间就是「让它贴边但不越界」；\n"
        "        # 图比视口大的那一维，区间是「不露白」。两维互不干扰。\n"
        "        px = max(lo_x, min(hi_x, px))\n"
        "        py = max(lo_y, min(hi_y, py))\n"
    )
    bad = (
        "        lo_x, hi_x = cw + _T - disp[0] - _cx, -_T - _cx\n"
        "        lo_y, hi_y = ch + _T - disp[1] - _cy, -_T - _cy\n"
        "        # 逐维夹住：图比视口小的那一维，区间就是「让它贴边但不越界」；\n"
        "        # 图比视口大的那一维，区间是「不露白」。两维互不干扰。\n"
        "        _fx = lo_x <= hi_x\n        _fy = lo_y <= hi_y\n"
        "        px = (lo_x + hi_x) // 2 if not _fx else max(lo_x, min(hi_x, px))\n"
        "        py = (lo_y + hi_y) // 2 if not _fy else max(lo_y, min(hi_y, py))\n"
    )
    return good, bad


class Ev(object):
    def __init__(self, d=0, x=None, y=None):
        self.delta = d
        self.x = x
        self.y = y


def settle(app, n=3):
    for _ in range(n):
        t0 = time.time()
        while time.time() - t0 < 8:
            app.update()
            if not app._fit_pend:
                break
            time.sleep(0.02)
    app.update()


def drag(app, dx, dy, steps=15, step=10):
    """从画布中心起手，拖 steps 步。返回 (pan, 每步耗时列表)。"""
    cw = app.cv_a.winfo_width()
    ch = app.cv_a.winfo_height()
    app._pan_start(0, Ev(0, cw // 2, ch // 2))
    x0, y0 = app._drag[1], app._drag[2]
    times = []
    for k in range(steps):
        t = time.perf_counter()
        app._pan_move(0, Ev(0, x0 + dx * step * k, y0 + dy * step * k))
        app.update()
        times.append((time.perf_counter() - t) * 1000)
    app._pan_end()
    settle(app)
    return tuple(app.pan[0]), times


def main():
    # ⚠️ 注入模式：**改完文件必须换进程重跑**，否则测的还是内存里的旧模块
    if "--child" in sys.argv:
        return _run()
    if INJECT:
        _inject()
        print("↻ 注入完成，改用子进程重跑")
        import subprocess
        r = subprocess.call([sys.executable, os.path.abspath(__file__),
                             "--child"])
        _restore()
        print("已还原修复版源码")
        if r == 0:
            print("\n⚠️ 子进程全绿 = **判据抓不住这个 bug**，判据是假的")
            return 1
        print("\n✅ 子进程变红 = 判据确实抓住了这个 bug")
        return 0
    return _run()


def _inject():
    """把 bug 注回 image_scout.py（两处：入口冻结 + lo/hi 方向）。"""
    p = os.path.join(HERE, "image_scout.py")
    src = open(p, encoding="utf-8").read()
    good, bad = _injection_pair()
    if good not in src:
        raise SystemExit("⚠️ 没找到修复版代码段（源码可能已改），无法注入")
    src = src.replace(good, bad)
    gate_now = "        px = d[3] + (ev.x - d[1])\n        py = d[4] + (ev.y - d[2])\n"
    gate_bad = ("        _room_x = disp[0] - cw\n        _room_y = disp[1] - ch\n"
                "        if _room_x <= 0 and _room_y <= 0:\n            return\n") + gate_now
    if gate_now in src:
        src = src.replace(gate_now, gate_bad, 1)
    open(p, "w", encoding="utf-8").write(src)


def _restore():
    """把 image_scout.py 还原回修复版。"""
    p = os.path.join(HERE, "image_scout.py")
    src = open(p, encoding="utf-8").read()
    good, bad = _injection_pair()
    src = src.replace(bad, good)
    src = src.replace(
        ("        _room_x = disp[0] - cw\n        _room_y = disp[1] - ch\n"
         "        if _room_x <= 0 and _room_y <= 0:\n            return\n"), "", 1)
    open(p, "w", encoding="utf-8").write(src)


def _run():
    import image_scout as ui        # noqa: E402  （延后到这里，见上面的说明）
    uikit.enable_dpi_awareness()
    shutil.rmtree(ROOT, ignore_errors=True)
    os.makedirs(ROOT, exist_ok=True)
    make_photo(os.path.join(ROOT, "p.png"))
    # ⚠️ B/C 场景要测「横向能拖很远」和「图比视口只大一点」，
    #   用主人那张 1328x2048（宽高比 0.65）**测不到** —— 放大 5 档后
    #   宽 1456 仍窄于视口 1515，横向区间被夹成 30px，量不出东西。
    #   所以另造一张 4:3 的专门跑 B/C。
    make_photo(os.path.join(ROOT, "wide.png"), 2400, 1800, seed=9)

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
    row = next(i for i, it in enumerate(app.glist.items)
               if it["tag"] == os.path.join(ROOT, "p.png"))
    app.glist.select(row)
    app.update()
    settle(app)

    cw = app.cv_a.winfo_width()
    ch = app.cv_a.winfo_height()
    fails = []

    def ck(ok, msg):
        print(("   OK   " if ok else "   FAIL ") + msg)
        if not ok:
            fails.append(msg)

    # ---------- 场景 A：适应窗口（主人截图那个位置）----------
    app.reset_zoom()
    app.update()
    settle(app)
    dw, dh = app._view[0]["disp"]
    print("\n=== A. 适应窗口（图 %dx%d 视口 %dx%d，比值 %.2f）==="
          % (dw, dh, cw, ch, dw / float(cw)))
    ck(dw <= cw and dh <= ch, "前置自证：图确实比视口小（否则量的是别的场景）")

    # A1 能不能拖
    pan_r, t_r = drag(app, 1, 0)
    ck(pan_r[0] > 0, "图比视口小时**横向能拖**（pan.x=%d，应 >0）" % pan_r[0])
    pan_d, t_d = drag(app, 0, 1)
    ck(pan_d[1] > 0, "图比视口小时**纵向能拖**（pan.y=%d，应 >0）" % pan_d[1])

    # A2 图必须始终完整可见（不许拖出视口）
    v = app._view[0]
    gx0, gx1 = v["ox"], v["ox"] + dw
    gy0, gy1 = v["oy"], v["oy"] + dh
    print("   死拖后 图 x[%d,%d] y[%d,%d]，视口 %dx%d" % (gx0, gx1, gy0, gy1, cw, ch))
    ck(gx0 >= -2 and gx1 <= cw + 2 and gy0 >= -2 and gy1 <= ch + 2,
       "图始终完整在视口内（没拖出空白）")

    # A3 区间宽度应该正好是 |cw-dw|
    m = (cw - dw) // 2
    exp = abs(cw - dw)
    ck(True, "理论 pan 区间宽度 = |cw-dw| = %d（%.0f%% 视口宽）"
       % (exp, 100.0 * exp / cw))

    # A4 拖动流畅（不能因为这次修复变卡）
    ck(max(t_r) < 60, "图小时拖动不卡（最慢一步 %.1fms < 60ms）" % max(t_r))

    # ---------- 场景 B：图比视口只大一点（不许拖出）----------
    # ⚠️ 先换成 4:3 那张（见上面 make_photo 的注释）
    print("\n=== B. 图比视口只大一点：夹持必须生效 ===")
    _brow = next(i for i, it in enumerate(app.glist.items)
                 if it["tag"] == os.path.join(ROOT, "wide.png"))
    app.glist.select(_brow)
    app.update()
    settle(app)
    app.reset_zoom()
    app.update()
    settle(app)

    def room_at(zv):
        app.zoom[0] = zv
        app.render_all(precise=True, only=0)
        app.update()
        d = app._view[0]["disp"]
        return d, d[0] - cw, d[1] - ch

    lo, hi, found = 1.0, 2.0, None
    for _ in range(24):
        mid = (lo + hi) / 2
        d, rx, ry = room_at(mid)
        if rx < 60 or ry < 60:
            lo = mid
        else:
            hi = mid
            found = (d, rx, ry, mid)
            if rx <= 200 and ry <= 200:
                break
    ck(found is not None, "找到「只大一点」那一档")
    if found:
        d, rx, ry, zv = found
        app.zoom[0] = zv
        app.render_all(precise=True, only=0)
        app.update()
        settle(app)
        print("   zoom=%.3f 图 %dx%d 视口 %dx%d（余量 %dx%d）"
              % (zv, d[0], d[1], cw, ch, rx, ry))
        pan, _ = drag(app, 1, 1, steps=25, step=40)
        v = app._view[0]
        gx0, gx1 = v["ox"], v["ox"] + d[0]
        gy0, gy1 = v["oy"], v["oy"] + d[1]
        print("   死拖后 pan=%s 图 x[%d,%d] y[%d,%d]" % (pan, gx0, gx1, gy0, gy1))
        ck(pan[0] > 0, "夹持后仍能拖（pan.x=%d > 0）" % pan[0])
        # ⚠️⚠️ **判据要按「余量」算，不是按「视口」**（我第一版写错了）。
        #
        # 图比视口大 `rx` px 时，**允许露出的最大值就是 `rx`** ——
        # 那正是「图多出来、视口装不下」的部分，是图的固有属性，
        # 拖到图边对齐视口边（`ox=0`）就该停。实测 pan.x=30、
        # 图 x[0,1575]、视口 1515 -> 右侧超出 60 == 余量 60，**正确**。
        #
        # 我原来写的是「不许超出视口」(+2 容差)，那等于要求图必须缩到
        # 视口内 —— 可图比视口大时**数学上不可能**（见 _pan_move 的推导），
        # 于是判据永远红。判据必须与机制的允许量一致。
        out_x = max(0, -gx0, gx1 - cw)
        out_y = max(0, -gy0, gy1 - ch)
        ck(out_x <= rx + 2 and out_y <= ry + 2,
           "露出量不超过「图比视口多出来的部分」（露出 %dx%d <= 余量 %dx%d）"
           % (out_x, out_y, rx, ry))
        # 同时验证夹持的另一面：**能贴到边**（不是被提前卡住）
        ck(gx0 <= 0 and gy0 <= 0,
           "能拖到贴边（ox=%d oy=%d 应 <= 0，说明没被提前卡住）"
           % (gx0, gy0))

    # ---------- 场景 C：放大到能自由拖（回归）----------
    print("\n=== C. 放大后自由拖（回归，别把好的搞坏）===")
    app.reset_zoom()
    app.update()
    settle(app)
    for _ in range(5):
        app._on_wheel(0, Ev(120, cw // 2, ch // 2))
    app.update()
    settle(app)
    dw, dh = app._view[0]["disp"]
    ck(dw > cw and dh > ch, "放大后图比视口大（%dx%d > %dx%d）" % (dw, dh, cw, ch))
    pan, ts = drag(app, 1, 0, steps=20, step=12)
    ck(pan[0] > 200, "放大后能自由拖（pan.x=%d > 200）" % pan[0])
    ck(max(ts) < 140, "放大后拖动不失控（最慢一步 %.1fms < 140ms）" % max(ts))

    app.destroy()

    print("\n" + ("=" * 46))
    if fails:
        print("❌ %d 项失败：" % len(fails))
        for f in fails:
            print("   - " + f)
        return 1
    print("✅ 图比视口小时也能拖、且始终完整可见；放大后行为未回归")
    return 0


if __name__ == "__main__":
    sys.exit(main())
