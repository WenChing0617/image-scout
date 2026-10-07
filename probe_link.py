# -*- coding: utf-8 -*-
"""双图联动（v1.8，主人要的功能）专项验证。

## 为什么单独一个探针，不塞进 test_ui

联动是**两侧**交互，而 `test_ui` 里绝大多数用例只拖 `side 0`
⇒ 「另一侧有没有跟着动」它压根测不到。
v1.7 那次`render_all(only=)` 的 bug 就是这么漏掉的
（所有拖动测试都只拖 side 0，判据数 `_render_side` 次数才抓得住）。
**判据量不到的那一侧，就是没有判据。**

## ⚠️⚠️ 造图必须用**构图完全不同**的两张，否则测不出真 bug

如果两张图同尺寸（都是 3000x2000），那么「直接抄 `pan` 数值」
和「按归一化位置换算」的结果**完全一样** ⇒ 判据会全绿，而代码是错的。

⇒ 本探针用 **3000x2000（横）+ 1328x2048（竖）**：
长宽比 1.5 vs 0.65，显示尺寸差2.3 倍，抄pan 必错。

    用法：
        python probe_link.py
        python probe_link.py --inject    # 把 bug 放回源码，看判据红不红
"""
from __future__ import annotations

import io
import os
import random
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import image_scout as ui  # noqa: E402
import thumbs  # noqa: E402
import uikit  # noqa: E402

ROOT = os.path.join(os.environ.get("TEMP", "."), "ImageScout-link")
FAILS = []


def check(ok, msg):
    print("   %s   %s" % ("OK  " if ok else "FAIL", msg))
    if not ok:
        FAILS.append(msg)
    return ok


def make_photo(path, w, h, seed):
    """造一张纯噪声图（最坏输入：GDI+ 缩放最慢的那种）。"""
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


def pct(vals, p):
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * p))]


def norm_of(app, col):
    return app._link_norm(col)


def main():
    uikit.enable_dpi_awareness()
    shutil.rmtree(ROOT, ignore_errors=True)
    os.makedirs(ROOT, exist_ok=True)
    # ⚠️⚠️ **两张图构图必须不同**（见模块 docstring）
    a = make_photo(os.path.join(ROOT, "wide.png"), 3000, 2000, 7)
    b = make_photo(os.path.join(ROOT, "tall.png"), 1328, 2048, 9)

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
    # 并排对比
    app.set_pair(a, b)
    app.update()
    for _ in range(3):
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
    print("画布 %dx%d" % (cw, ch))
    check(app.mode == "pair", "进入并排对比模式")
    check(app.cell_b.winfo_ismapped(), "右侧格子在")

    # ---- 前置自证：两张图显示尺寸必须真的不同 ----------------------
    dw_a, dh_a = app._view[0]["disp"]
    dw_b, dh_b = app._view[1]["disp"]
    print("左图显示 %dx%d   右图显示 %dx%d" % (dw_a, dh_a, dw_b, dh_b))
    check(dw_a > 0 and dw_b > 0, "两侧都出图了")
    check(dw_a != dw_b or dh_a != dh_b,
          "两张图显示尺寸不同（构图不同）—— 否则测不出「抄 pan」的错误")

    # ================================================================
    print("\n=== A. 关闭联动时：两侧互不影响（回归）===")
    check(app.link_view.get() is False, "联动默认是关的")
    app._on_wheel(0, Ev(120, cw // 2, ch // 2))
    app.update()
    z0, z1 = app.zoom[0], app.zoom[1]
    print("   滚左图后 zoom 左 %.3f右 %.3f" % (z0, z1))
    check(abs(z0 - z1) > 1e-6, "不联动时只有左侧缩放了（两图倍率不同）")
    app.set_pair(a, b)
    app.update()
    for _ in range(3):
        t0 = time.time()
        while time.time() - t0 < 8:
            app.update()
            if not app._fit_pend:
                break
            time.sleep(0.02)
    app.render_all(precise=True)
    app.update()

    # ================================================================
    print("\n=== B. 打开联动：滚轮两侧同步 ===")
    app.toggle_link()
    app.update()
    check(app.link_view.get() is True, "联动已打开")
    check(app.btn_link.get_kind() == "on", "按钮是选中态")

    for _ in range(4):
        app._on_wheel(0, Ev(120, cw // 2, ch // 2))
        app.update()
    z0, z1 = app.zoom[0], app.zoom[1]
    print("滚 4 格后 zoom 左 %.4f 右 %.4f" % (z0, z1))
    check(abs(z0 - z1) < 1e-9, "两侧倍率完全一致（%.4f vs %.4f）" % (z0, z1))
    check(z0 > 1.05, "确实放大了（%.4f）" % z0)

    # ⚠️⚠️ **位置必须按归一化换算，不能直接抄 pan**
    n0, n1 = norm_of(app, 0), norm_of(app, 1)
    print("归一化位置 左 (%.4f, %.4f) 右 (%.4f, %.4f)"
          % (n0[0], n0[1], n1[0], n1[1]))
    if n0 and n1:
        check(abs(n0[0] - n1[0]) < 0.02 and abs(n0[1] - n1[1]) < 0.02,
              "两侧「注视的归一化位置」一致（构图不同也对得上同一处）")
        # 抄 pan 的 bug 在这里会露馅：两张图显示尺寸差 2.3 倍，
        # 直接抄 pan 的话归一化位置会差很远。
        check(not (app.pan[0] == app.pan[1] and dw_a != dw_b),
              "两侧 pan 数值不同（说明是按归一化算的，不是照抄）")

    # ================================================================
    print("\n=== C. 放大到图比视口大：拖动两侧同步 ===")
    for _ in range(5):
        app._on_wheel(0, Ev(120, cw // 2, ch // 2))
        app.update()
    app.render_all(precise=True)
    app.update()
    dwa, dha = app._view[0]["disp"]
    dwb, dhb = app._view[1]["disp"]
    check(dwa > cw, "左图已比视口大（%d > %d）" % (dwa, cw))
    check(dwb > cw, "右图已比视口大（%d > %d）" % (dwb, cw))
    check(abs(app.zoom[0] - app.zoom[1]) < 1e-9, "两侧倍率仍然一致")

    # 记下拖动前的归一化位置
    bn0 = norm_of(app, 0)
    # ⚠️ 也要记下**拖动前的图元位置** —— 「画面到底动没动」那条判据要用。
    #   只看pan 数字抓不到「pan 对、画面没动」那种 bug。
    _i0 = app._view[0].get("item")
    _i1 = app._view[1].get("item")
    pan_first_bbox = (app.cv_a.bbox(_i0) if _i0 else None,
                      app.cv_b.bbox(_i1) if _i1 else None)
    times = []
    # ⚠️⚠️⚠️ **逐帧要量，而且只能量「纯挪图元」的步**（v1.8，两次修正）。
    #
    # ①  为什么必须逐帧：只在拖完时量一次 bbox 是**测不出来的** ——
    #   `render_all(only=None)` 会在拖动结束时把两侧画面纠正回来，
    #   中间帧哪怕全错，终态也对。
    #   实测：只看终态时，注入「去掉挪另一侧图元」仍然 **exit=0 全绿**。
    #
    # ②  为什么只算「没重画」的步：**分块渲染下图元贴在 `ox + px0/sx`，
    #   而块起点是对齐到固定网格的** ⇒ 图元的画布坐标**按网格跳变**，
    #   不是 pan 的连续函数。实测重画那几步的位移比是
    #   0.00/0.12/-0.12 这种**跳变**，拿它跟「显示尺寸比」比就是错的
    #   ⇒ **判据自己错了，不是代码错了**。
    #
    #   而「两侧都没重画、纯挪图元」那几步，位移必须严格按比例 ——
    #   **那才是用户眼里「联动跟手」的真实场景**，
    #   也正是「另一侧没被挪」这个 bug 会露馅的地方。
    seq = []                # 每步 (左图元x, 右图元x, 左item, 右item, 左pan, 右pan)
    app._pan_start(0, Ev(0, cw // 2, ch // 2))
    bx, by = app._drag[1], app._drag[2]
    pan_seen = []
    for k in range(14):
        off = k * 8
        t = time.perf_counter()
        app._pan_move(0, Ev(0, bx + off, by))
        app.update()
        times.append((time.perf_counter() - t) * 1000)
        pan_seen.append((tuple(app.pan[0]), tuple(app.pan[1])))
        # ⚠️ 每步都记两侧图元的画布x（见上面那段：终态会被 render_all 纠正，
        #   只有逐帧量才抓得住「中间帧画面没跟上」）
        _it0 = app._view[0].get("item")
        _it1 = app._view[1].get("item")
        _bi0 = app.cv_a.bbox(_it0) if _it0 else None
        _bi1 = app.cv_b.bbox(_it1) if _it1 else None
        # ⚠️ 连**图元对象**一起记：只有「两侧都是同一个对象」（=没重画）
        #   那些步，位移比才是有意义的（见上面那段）
        seq.append((_bi0[0] if _bi0 else None,
                    _bi1[0] if _bi1 else None,
                    _it0, _it1,
                    tuple(app.pan[0]), tuple(app.pan[1])))
        time.sleep(0.016)
    app._pan_end()
    app.update()
    print("   拖动 14 步：左 pan %s  右 pan %s"
          % (pan_seen[-1][0], pan_seen[-1][1]))
    print("   帧耗时 中位 %.1f / p95 %.1f / 最大 %.1f ms"
          % (pct(times, .5), pct(times, .95), max(times)))
    check(pan_seen[-1][0] != pan_seen[0][0], "左侧确实被拖动了")

    # ⚠️⚠️ **核心判据：另一侧也必须跟着动，且停在对应位置**
    moved = [p for p in pan_seen if p[1] != pan_seen[0][1]]
    check(len(moved) >= 10,
          "右侧跟着动了（%d/14 步 pan 变化）" % len(moved))
    an0 = norm_of(app, 0)
    an1 = norm_of(app, 1)
    if an0 and an1:
        print("   拖后归一化 左 (%.4f, %.4f) 右 (%.4f, %.4f)"
              % (an0[0], an0[1], an1[0], an1[1]))
        check(abs(an0[0] - an1[0]) < 0.03 and abs(an0[1] - an1[1]) < 0.03,
              "拖完两侧注视同一处（归一化一致）")
    # 图元也要真的挪了（不是只改了 pan 数字、画面没动）
    it0 = app._view[0].get("item")
    it1 = app._view[1].get("item")
    check(it0 is not None and it1 is not None, "两侧图元都在")
    # ⚠️⚠️ **画面位移判据**：本侧拖了 dx 像素，另一侧图元也必须挪动。
    #   这条专抓「pan 数字对了、画面没动」那种 bug
    #   （--inject-off 注入的就是它）——
    #   那种 bug 下上面所有 pan/归一化判据**全部照过**，
    #   唯一能露馅的就是「图元到底动没动」。
    #   注入验证过：--inject-off 下这条会红。
    b0 = app.cv_a.bbox(it0) if it0 else None
    b1 = app.cv_b.bbox(it1) if it1 else None
    # ⚠️⚠️⚠️ **逐帧判据（本功能最核心的一条）**
    #
    # 「画面跟不跟手」是**逐帧**的性质。只在拖完时量一次 bbox 是**测不出来的**
    # —— `render_all(only=None)` 会在拖动结束时把两侧画面纠正回正确位置，
    # 中间帧哪怕全错，终态也是对的。
    # 实测：判据只看终态时，注入「去掉挪另一侧图元」**仍然全绿（exit=0）**。
    #
    # ⇒ 每一步都比「两侧图元位移之比 ≈ 显示尺寸之比」，
    #   偏差超容差就说明那一帧另一侧画面没跟上（用户看到的就是「一侧在动、
    #   另一侧卡住」）。
    if len(seq) >= 3:
        ratio = (app._view[1]["disp"][0] / float(app._view[0]["disp"][0]))
        bad_frames = []
        ratios = []
        for i in range(1, len(seq)):
            x0a, x1a, ia, ib = seq[i - 1][:4]
            x0b, x1b, ic, id_ = seq[i][:4]
            if None in (x0a, x1a, x0b, x1b):
                continue
            # ⚠️⚠️ **只算「两侧图元都没换过」的步**（纯挪图元）。
            #   只要有一侧重画了，块起点按网格跳变，位移比就没有意义。
            if not (ia is ic and ib is id_):
                continue
            d0, d1 = x0b - x0a, x1b - x1a
            if abs(d0) < 1:
                continue                       # 本侧没动，不比
            got = d1 / float(d0)
            ratios.append(got)
            if abs(got - ratio) > 0.35:
                bad_frames.append((i, d0, d1, round(got, 3)))
        print("   纯挪图元的 %d 步，位移比（应约 %.3f）：%s"
              % (len(ratios), ratio, " ".join("%.2f" % g for g in ratios)))
        check(len(ratios) >= 3,
              "「纯挪图元」的样本够不够（%d 步，需>=3）—— 不够说明测的不是"
              "「跟手」这个场景" % len(ratios))
        check(not bad_frames,
              "纯挪图元时两侧画面都跟手（%d 步里 %d 步偏离 %.2f±0.35）%s"
              % (len(ratios), len(bad_frames), ratio,
                 bad_frames[:3] if bad_frames else ""))
    else:
        check(False, "逐帧样本太少，判据自己有问题")
    if b0 and b1 and pan_first_bbox[0] and pan_first_bbox[1]:
        #两侧显示尺寸比 = disp1/disp0，同一位移在两侧的画布位移之比就是它
        ratio = (app._view[1]["disp"][0] / float(app._view[0]["disp"][0]))
        # ⚠️⚠️ **别写成 `pan_first_bbox[0]`** —— 那是**整个 bbox 四元组**，
        # 不是 x 坐标。正确是 `[0][0]`（先取第0 侧、再取 x）。
        #   我第一版写成前者，直接 TypeError炸在这儿。
        moved0 = b0[0] - pan_first_bbox[0][0]
        moved1 = b1[0] - pan_first_bbox[1][0]
        print("   图元水平位移：左 %.1fpx 右 %.1fpx（两侧显示尺寸比 %.3f）"
              % (moved0, moved1, ratio))
        check(abs(moved0) > 5, "左侧图元确实动了（%.1f px）" % moved0)
        # ⚠️ 这条是**唯一**能抓「pan 数字对、画面没动」的判据
        check(abs(moved1) > 2,
              "右侧图元也跟着动了（%.1f px）—— 专抓「pan 对、画面不动」"
              % moved1)
        # 两侧画布位移之比应约等于显示尺寸比（同一份归一化位移）
        if abs(moved0) > 5:
            got = moved1 / moved0
            check(abs(got - ratio) <= max(0.12, ratio * 0.25),
                  "右图元位移与理论比一致（实测比 %.3f，理论 %.3f）"
                  % (got, ratio))
    else:
        check(False, "取不到图元 bbox，判据自己有问题")
    # 联动不能引入卡顿
    check(pct(times, .95) < 120, "联动拖动 p95 %.1f ms < 120 ms"
          % pct(times, .95))

    # ================================================================
    print("\n=== D. 两侧都不许露白 ===")
    for col, cv in ((0, app.cv_a), (1, app.cv_b)):
        v = app._view.get(col)
        if not v or v.get("item") is None:
            continue
        bb = cv.bbox(v["item"])
        if not bb:
            check(False, "第 %d 侧图元丢了" % col)
            continue
        cwv, chv = cv.winfo_width(), cv.winfo_height()
        d = app._view[col]["disp"]
        rx = max(0, d[0] - cwv)
        ry = max(0, d[1] - chv)
        slack = app._tile_slack(col, cv)
        print("   第 %d 侧 图 x[%d,%d] y[%d,%d] 视口 %dx%d  余量 %dx%d slack %d"
              % (col, bb[0], bb[2], bb[1], bb[3], cwv, chv, rx, ry, slack))
        out_x = max(0, -bb[0]) + max(0, bb[2] - cwv)
        out_y = max(0, -bb[1]) + max(0, bb[3] - chv)
        check(out_x <= rx + 4 and out_y <= ry + 4,
              "第 %d 侧拖动中没露白（露出 %dx%d <= 余量 %dx%d）"
              % (col, out_x, out_y, rx, ry))

    # ================================================================
    print("\n=== E. 切回单图：按钮藏起来、联动自动关掉 ===")
    app.toggle_link()          # 先关掉联动，避免带状态切
    app.update()
    app.set_pair(a, None)
    app.update()
    check(app.mode == "single", "切到单图")
    check(app.link_view.get() is False, "单图下联动自动关掉")
    check(not app.btn_link.winfo_ismapped(), "单图下按钮藏起来了")

    print("\n=== F. 单图下按联动键要提示，不能默默不动 ===")
    app.toggle_link()
    app.update()
    check(app.link_view.get() is False, "单图下按了不会误开")

    app.destroy()

    print("\n" + "=" * 60)
    if FAILS:
        print("❌ %d 项失败：" % len(FAILS))
        for m in FAILS:
            print("   - %s" % m)
        return 1
    print("✅ 双图联动全部通过")
    return 0


if __name__ == "__main__":
    if "--inject" in sys.argv or "--inject-off" in sys.argv:
        # ---- 两个注入，各模拟一类真 bug ----
        #
        # ① `--inject`：把「按归一化换算」退化成「直接抄 pan 数值」。
        #    本功能**最容易写错**的地方 —— 探针造图刻意用
        #    3000x2000（横）+ 1328x2048（竖），显示尺寸差 2.3 倍，
        #    抄 pan 必错。同尺寸的话两种写法结果一样，判据就白写了。
        #
        # ② `--inject-off`：去掉「另一侧图元也挪」这一步。
        #    模拟「联动只改了 pan 数字、画面没动」——
        #    那种 bug 下 **pan 全对、归一化全对，只有画面不对**。
        #    ⇒ 判据必须能抓到它，否则「看着生效了、实际另一侧是呆的」。
        SRC = os.path.join(HERE, "image_scout.py")
        BAK = os.path.join(HERE, "_link_bak.py")
        INJ = {
            "--inject": (
                "        self.pan[other] = [int(round(m[0] + norm[0] * disp[0])),\n"
                "                           int(round(m[1] + norm[1] * disp[1]))]",
                "        self.pan[other] = list(self.pan[col])   # INJECT",
                "「按归一化换算」→「直接抄 pan」",
            ),
            "--inject-off": (
                # ⚠️⚠️ **只去掉 `_link_move_item` 这一行**。
                #   我第一版把两行一起 `pass` 掉了 —— 结果 `pan` 也不更新，
                #   于是「右侧 pan 0/14 步变化」那条先红了，
                #   **「pan 对、画面不动」那个真正的场景压根没被构造出来**
                #   （画面其实也被 render_all 重画了、也动了）。
                #   ⇒注入必须**精确对准要模拟的那一个 bug**，
                #     一次改多了就等于测了别的东西。
                "            self._link_move_item(1 - col)    # 只挪图元，出图交给 _pan_fill",
                "            pass    # INJECT: 只去掉挪图元那行",
                "只去掉「另一侧图元也挪」（pan 仍更新）",
            ),
        }
        which = "--inject" if "--inject" in sys.argv else "--inject-off"
        good, bad, desc = INJ[which]
        s = io.open(SRC, encoding="utf-8").read()
        c = s.count(good)
        print("注入 [%s] %s" % (which, desc))
        print("锚点匹配 %d 处%s" % (c, "" if c == 1 else "  <- 不是 1 处！"))
        if c != 1:
            print("X 锚点对不上，注入作废（源码结构变了？）")
            sys.exit(2)
        shutil.copyfile(SRC, BAK)
        bak = io.open(BAK, encoding="utf-8").read()
        io.open(SRC, "w", encoding="utf-8", newline="\n").write(
            s.replace(good, bad))
        print("已注入，子进程重跑…\n")
        rc = subprocess.call([sys.executable, __file__])
        #还原必须做**字节级**校验，不能只 grep 某个片段
        #   （v1.8 踩到：断言 count("norm[0]*disp[0]")==1 因换行写法不同
        #     而误报「还原失败」，其实源码干净 —— **断言自己错了**）。
        # => 跟备份逐字节比，那才是唯一可靠判据。
        shutil.copyfile(BAK, SRC)
        now = io.open(SRC, encoding="utf-8").read()
        assert now == bak, "还原后与备份不一致 —— 停下，别继续跑！"
        assert "INJECT" not in now, "还原后仍有注入残留"
        os.remove(BAK)
        print("\n[restore] 已还原并校验（%d 字节，与备份逐字节一致）"
              % len(now.encode("utf-8")))
        sys.exit(rc)
    sys.exit(main())
