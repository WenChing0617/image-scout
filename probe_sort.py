# -*- coding: utf-8 -*-
"""排列功能的探针。

主人 2026-10-07 的需求原文：

> 「图片分组可以根据每组的图片数量来排序，数量多的放上面，
>    每组和全部图片按照图片质量来排序，同时支持倒序。」

## 为什么不能只用一批「一样的图」测

第一版我用的是 400 张同规格合成图 —— **质量档位全是同一档**，
于是「按质量排序」这条判据无论排没排都通过，等于没测。
所以这里专门造一批**质量分层**的图（低清 / 一般 / 清晰），
让「质量排序」真的有东西可排。

## 判据

S1  分组列表按**张数降序**（第一行的张数 >= 第二行，以此类推）
S2  按「排列」变升序、再按回来逐项还原（可逆）
S3  「全部图片」按**质量档位**排（默认最差的在前），倒序后反过来
S4  组内成员：代表图**固定第一**，其余按质量档位排
S5  切排列之后**选中的东西不丢**（分组视图保住组号，全部视图保住那张图）
S6  质量排序确实**有效**：数据里至少有两种档位（否则 S3 是空判据）
S7  同张数 + 同质量档 -> 按**组号**升序（v1.11，第三键不能是文件名）
S8  同张数 + **质量档不同** -> 也要按组号升序（v1.11，质量档不许插在中间）

用法：python probe_sort.py
"""
from __future__ import annotations

import os
import random
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import image_scout as ui            # noqa: E402
import scan                         # noqa: E402
import thumbs                       # noqa: E402
import uikit                        # noqa: E402

QROOT = os.path.join(os.environ.get("TEMP", "."), "ImageScout-qual")

FAIL = []
OK = []


def check(cond, label, extra=""):
    (OK if cond else FAIL).append(label)
    print("   %s %s%s" % ("[OK]" if cond else "[XX]", label,
                          ("  " + extra) if extra else ""))


def make_photo(path, w, h, seed=1):
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


# 三个质量档各来几张（档位由 `quality_of` 的阈值决定）
TIERS = {"poor": (300, 200), "fair": (800, 600), "good": (1600, 1200)}


def build_dir():
    shutil.rmtree(QROOT, ignore_errors=True)
    os.makedirs(QROOT, exist_ok=True)
    out = {}
    k = 0
    for tier, (w, h) in TIERS.items():
        for _ in range(4):
            p = os.path.join(QROOT, "%s-%02d.png" % (tier, k))
            make_photo(p, w, h, seed=100 + k)
            out.setdefault(tier, []).append(p)
            k += 1
    return out


def fake_group(members):
    """形状跟 `scan.build_groups` 一致的组（只有 members 有意义）。"""
    return {"members": list(members), "strong_links": [], "weak_links": [],
            "kind": scan.UI_LOOSE, "best": 0.5, "best_strong": 0.0}


def main():
    uikit.enable_dpi_awareness()
    T = build_dir()
    app = ui.App([QROOT])
    app.store = scan.FingerStore(os.path.join(
        os.environ["TEMP"], "_sort_cache.json"))
    app.update()
    app.update()

    # ---- 造 4 个大小/质量都不同的组 ----------------------------------
    # ⚠️⚠️ **组的原始顺序必须是「乱的」**。第一版我按 5/3/2/2 的顺序摆，
    #     结果注入「不排序」时 S1 照样通过 —— 因为不排也恰好是降序，
    #     判据根本没测到东西。这里刻意摆成 2/5/2/3。
    plans = [
        ([T["fair"][2], T["good"][3]], 2),
        (T["poor"][:2] + [T["fair"][0]] + T["good"][:2], 5),
        ([T["poor"][3], T["poor"][0]], 2),
        ([T["good"][2], T["fair"][1], T["poor"][2]], 3),
    ]
    groups = [fake_group(m) for m, _ in plans]
    app.groups_raw = list(groups)
    app.weak = []
    app._make_view_groups()
    app.view = "groups"
    app._fill_group_list(select=0)
    app.update()

    def rows():
        return [it["tag"] for it in app.glist.items]

    def counts(order):
        return [len(app.groups[gi]["members"]) for gi in order]

    # ---- S6 先证明数据本身有多种质量档（否则 S3 是空判据）-----------
    ranks = sorted({app._quality_key(p)[0] for p in app.files})
    print("\n-- 数据 --")
    print("   出现的质量档位：%s（0=低清 1=一般 2=未知 3=清晰）" % ranks)
    check(len(ranks) >= 2, "S6 测试数据含多种质量档（S3 才有意义）",
          "%s" % ranks)

    print("\n-- 分组顺序 --")
    seq = counts(rows())
    print("   当前行序的张数：%s" % seq)
    check(seq == sorted(seq, reverse=True), "S1 分组按张数降序", "%s" % seq)

    app.toggle_sort()
    app.update()
    seq2 = counts(rows())
    print("   按「排列」之后：%s" % seq2)
    check(seq2 == sorted(seq2), "S2a 再按一次变升序", "%s" % seq2)
    check(sorted(seq2, reverse=True) == seq, "S2b 内容没变，只是顺序翻了")
    app.toggle_sort()
    app.update()
    check(counts(rows()) == seq, "S2c 再按一次逐项还原", "%s" % counts(rows()))

    # ---- S3 「全部图片」按质量排 --------------------------------------
    print("\n-- 「全部图片」的质量顺序 --")
    app.set_view("all")
    app.update()
    q_all = [app._quality_key(it["tag"]) for it in app.glist.items]
    print("   档位序列：%s" % [x[0] for x in q_all])
    check(q_all == sorted(q_all), "S3a 默认：质量差的在前")
    app.toggle_sort()
    app.update()
    q_rev = [app._quality_key(it["tag"]) for it in app.glist.items]
    print("   倒序后：%s" % [x[0] for x in q_rev])
    check(q_rev == sorted(q_rev, reverse=True), "S3b 倒序：质量好的在前")
    check(sorted(x[0] for x in q_all) == sorted(x[0] for x in q_rev),
          "S3c 倒序只改顺序，不改内容")
    app.toggle_sort()
    app.update()
    check([x[0] for x in q_all]
          == [x[0] for x in (app._quality_key(it["tag"])
                             for it in app.glist.items)],
          "S3d 再按回来与原来一致")
    app.set_view("groups")
    app.update()

    # ---- S4 组内成员 --------------------------------------------------
    big = max(range(len(app.groups)),
              key=lambda gi: len(app.groups[gi]["members"]))
    app.select_group(big)
    app.update()
    order = app._member_order()
    rep = app._representative(app.groups[big])
    print("\n-- 组内成员（最大的一组 %d 张）--" % len(order))
    print("   代表图 %s，其余档位 %s"
          % (os.path.basename(rep), [app._quality_key(p)[0]
                                     for p in order[1:]]))
    check(order[0] == rep, "S4a 代表图固定第一")
    rest = [app._quality_key(p) for p in order[1:]]
    check(rest == sorted(rest), "S4b 代表图之后按质量（差的前）",
          "%s" % [x[0] for x in rest])
    check([it["tag"] for it in app.mlist.items] == order,
          "S4c 成员列表显示顺序 = `_member_order()`")
    app.toggle_sort()
    app.update()
    rest2 = [app._quality_key(p) for p in app._member_order()[1:]]
    check(rest2 == sorted(rest2, reverse=True), "S4d 倒序后成员反过来",
          "%s" % [x[0] for x in rest2])
    app.toggle_sort()
    app.update()

    # ---- S5 切排列不丢选中 --------------------------------------------
    print("\n-- 切换排列后的选中 --")
    app.select_group(big)
    app.update()
    before = app.gidx
    app.toggle_sort()
    app.update()
    check(app.gidx == before, "S5a 分组视图：组号不变",
          "切前 %s 切后 %s" % (before, app.gidx))
    cur = app.glist.current()
    check(cur is not None and cur["tag"] == before,
          "S5b 选中的那一行就是原来那组")
    app.toggle_sort()
    app.update()

    app.set_view("all")
    app.update()
    path0 = app.path_a
    app.toggle_sort()
    app.update()
    check(app.path_a == path0, "S5c 全部图片视图：看的那张不变",
          "%s" % os.path.basename(path0 or ""))
    app.set_view("groups")
    app.update()

    # ---- S7 同张数 + 同质量档 -> 必须按**组号**升序（v1.11）-----------
    # ⚠️⚠️ 场景必须让「按文件名排」和「按组号排」**结果不同**，否则
    #     注入回旧的第三键（文件名）照样通过 —— 判据就是空的。
    #     这里刻意把「组号顺序」与「代表图文件名顺序」错开：
    #         组号  0      1      2      3
    #         rep   -03    -00    -02    -01
    #     按文件名排 = [1,3,2,0]（乱）；按组号排 = [0,1,2,3] ✓
    print("\n-- 同张数同质量档时按组号升序 --")
    pp = T["poor"]
    ties = [([pp[3], pp[0]], 0), ([pp[0], pp[1]], 1),
            ([pp[2], pp[3]], 2), ([pp[1], pp[2]], 3)]
    app.groups_raw = [fake_group(m) for m, _ in ties]
    app.weak = []
    app._make_view_groups()
    app.view = "groups"
    # ⚠️⚠️ **先把「排列」复位**：前面 S5 反复按过那个开关，`sort_rev`
    #     停在哪一面是不确定的（第一次跑就撞上了 —— 量到 [3,2,1,0]，
    #     看着像代码错，其实是探针**继承了上一个用例的状态**）。
    #     判据必须显式声明白己要的前置条件，不能指望前一个用例收尾干净。
    if app.sort_rev:
        app.toggle_sort()
        app.update()
    app._fill_group_list(select=0)
    app.update()
    got = rows()
    want_gi = list(range(len(ties)))
    reps = [os.path.basename(app._representative(app.groups[gi]) or "")
            for gi in want_gi]
    by_name = sorted(want_gi, key=lambda gi: reps[gi].lower())
    print("   各组代表图：%s" % reps)
    print("   显示组号：%s" % [g + 1 for g in got])
    check(len({len(app.groups[gi]["members"]) for gi in want_gi}) == 1,
          "S7a 四组张数相同（前置条件）")
    check(len({app._quality_key(app._representative(app.groups[gi]))[0]
               for gi in want_gi}) == 1,
          "S7b 四组代表图质量档相同（前置条件）",
          "%s" % [app._quality_key(app._representative(app.groups[gi]))[0]
                  for gi in want_gi])
    check(by_name != want_gi, "S7c 场景有效：按文件名排 ≠ 按组号排",
          "按文件名 %s / 按组号 %s" % (by_name, want_gi))
    check(got == want_gi, "S7d 同分时按组号升序", "实际 %s" % got)

    # ---- S8 同张数但**质量档不同** -> 仍然按组号升序（v1.11）----------
    # ⚠️⚠️ S7 只覆盖「同张数 + 同质量档」，那就默认「质量档」这一层无害 ——
    #     而真实数据恰恰不是这样。18 张样本扫出来的三组是各 6 张、
    #     质量档 poor / good / poor，排序键 `(-6, rank, gi)` 让质量档插在
    #     中间，屏幕上是「第 1 / 3 / 2 组」（主人截图报的就是这个形态）。
    #     所以判据必须覆盖「质量档不同的并列」，光测同档那次是白测。
    print("\n-- 同张数但质量档不同时，仍按组号升序 --")
    plan8 = [T["good"][:2], T["poor"][:2], T["fair"][:2], T["poor"][2:4]]
    app.groups_raw = [fake_group(m) for m in plan8]
    app.weak = []
    app._make_view_groups()
    app.view = "groups"
    if app.sort_rev:                    # 同 S7：先复位，别继承上个用例的状态
        app.toggle_sort()
        app.update()
    app._fill_group_list(select=0)
    app.update()
    got8 = rows()
    ranks = [app._quality_key(app._representative(app.groups[gi]))[0]
             for gi in range(len(plan8))]
    by_rank = sorted(range(len(plan8)), key=lambda gi: (ranks[gi], gi))
    want8 = list(range(len(plan8)))
    print("   各组质量档 rank：%s" % ranks)
    print("   显示组号：%s" % [g + 1 for g in got8])
    check(len({len(app.groups[gi]["members"]) for gi in want8}) == 1,
          "S8a 四组张数相同（前置条件）")
    check(len(set(ranks)) > 1,
          "S8b 四组质量档**不全相同**（前置条件 —— 全同的话这条判据是空的）",
          "rank=%s" % ranks)
    check(by_rank != want8, "S8c 场景有效：按质量档排 ≠ 按组号排",
          "按质量 %s / 按组号 %s" % (by_rank, want8))
    check(got8 == want8, "S8d 质量档不同的并列组也按组号升序",
          "实际 %s" % got8)

    app.destroy()
    print("\n== 结果：%d 通过 / %d 失败 ==" % (len(OK), len(FAIL)))
    for f in FAIL:
        print("   FAIL %s" % f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
