# -*- coding: utf-8 -*-
"""「组内 >=3 张时自己点选两张来对比」的判据。

主人 2026-10-07 原话：
    「本组成员里面如果 3 个及以上最好是自定义组员图片来对比」

所以规则是：
  · 组内 **>= 3** 张 —— 单击成员行是「选进来」，选满两张立刻并排；
    再点一下同一行 = 取消那一张；只选一张时拉代表图陪它。
  · 组内 **< 3** 张 —— 保持原来自动语义（代表图 vs 那一张），
    因为两张时「比这两张」是唯一解，让用户点两下纯属多事。

⚠️⚠️ 这里必须走**真实的 `_click`**（用假事件算出行号），不能直接调
    `_on_pick_member` —— 那样会绕过 `select()` 的「同一行不重复通知」
    以及新加的 `on_repick` 通道，而「再点一下取消」**恰恰挂在那个通道
    上**。第一版探针就是直接调回调写的，注入 `on_repick=None` 照样全绿。

用法：
    python probe_pick.py
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

QROOT = os.path.join(os.environ.get("TEMP", "."), "ImageScout-pick")
OK = []
BAD = []


def check(cond, label, extra=""):
    (OK if cond else BAD).append(label)
    print("   [%s] %s%s" % ("OK" if cond else "XX", label,
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
    return {"members": list(members), "strong_links": [], "weak_links": [],
            "kind": scan.UI_LOOSE, "best": 0.5, "best_strong": 0.0}


class Ev(object):
    def __init__(self, x, y):
        self.x = x
        self.y = y


def click(lst, row):
    """**真实点击**第 row 行（走 `_click` -> `select` / `on_repick`）。"""
    p = lst._pitch()
    y = int(row * p + p // 2 - lst._top())
    lst._click(Ev(30, y))


def main():
    uikit.enable_dpi_awareness()
    T = build_dir()
    app = ui.App([QROOT])
    app.store = scan.FingerStore(os.path.join(
        os.environ["TEMP"], "_pick_cache.json"))
    app.update()
    app.update()

    plans = [
        ([T["fair"][2], T["good"][3]], 2),                       # 只有 2 张
        (T["poor"][:2] + [T["fair"][0]] + T["good"][:2], 5),     # 5 张
        ([T["poor"][3], T["poor"][0]], 2),                       # 只有 2 张
        ([T["good"][2], T["fair"][1], T["poor"][2]], 3),         # 3 张
    ]
    app.groups_raw = [fake_group(m) for m, _ in plans]
    app.weak = []
    app._make_view_groups()
    app.view = "groups"
    app._fill_group_list(select=0)
    app.update()

    rows = [it["tag"] for it in app.glist.items]
    big = next((i for i, (m, _) in enumerate(plans) if len(m) == 5), None)
    small = next((i for i, (m, _) in enumerate(plans) if len(m) == 2), None)
    check(big is not None and small is not None, "P0 造出 5 张 / 2 张两种组")
    if big is None or small is None:
        return 1

    # 切到 5 张那一组
    app.glist.select(rows.index(big))
    app.update()
    order = app._member_order()
    check(len(order) == 5, "P1a 5 张组已载入", "order=%d" % len(order))
    check(app.pick_pair == [], "P1b 刚进组时没有自定义选择")

    # ---- P2 点第 2 行 -> 选进来一张，拉代表图陪它 ---------------------
    click(app.mlist, 1)
    app.update()
    check(app.pick_pair == [order[1]], "P2a 点第 2 行后 pick_pair = [那一张]",
          "pick_pair=%s" % [os.path.basename(p) for p in app.pick_pair])
    check(app.path_a == order[1] and app.path_b == order[0],
          "P2b 对比区 = 点的那张 vs 代表图",
          "a=%s b=%s" % (os.path.basename(app.path_a or "-"),
                         os.path.basename(app.path_b or "-")))

    # ---- P3 再点第 3 行 -> 两张都选好了 -------------------------------
    click(app.mlist, 2)
    app.update()
    check(app.pick_pair == [order[1], order[2]],
          "P3a 点第二张后 pick_pair 有两项")
    check(app.path_a == order[1] and app.path_b == order[2],
          "P3b 对比区 = 自己选的那两张")

    # ---- P4 行上有 A / B 徽章 -----------------------------------------
    by_tag = {it["tag"]: it for it in app.mlist.items}
    got_a = by_tag.get(order[1], {}).get("badge")
    got_b = by_tag.get(order[2], {}).get("badge")
    check(got_a == "A" and got_b == "B", "P4 两行分别挂 A / B 徽章",
          "A行=%s B行=%s" % (got_a, got_b))

    # ---- P5 **再点一下**取消那一张 ------------------------------------
    click(app.mlist, 1)
    app.update()
    check(app.pick_pair == [order[2]],
          "P5a 点已选中的那一行 = 取消它（此时 sel 在第 3 行）",
          "pick_pair=%s" % [os.path.basename(p) for p in app.pick_pair])
    check(app.path_b == order[2] or app.path_a == order[2],
          "P5b 剩下那张还在画面上")

    # ---- P5c **连点同一行两次** —— 只有这一下才走 `on_repick` --------
    # ⚠️⚠️ 上面 P5a 点的是**另一行**（`sel` 当时在第 3 行），走的还是
    #     `select()` -> `on_pick`，**根本没碰到 `on_repick`**。
    #     第一版就是栽在这儿：注入 `on_repick=None` 后 P5a 照样全绿，
    #     因为判据跑的场景不是它声称的场景（和 P12 那次同一个坑）。
    app.pick_pair = []
    app.update()
    click(app.mlist, 2)                 # 一下：sel 变成第 3 行，选进来
    app.update()
    was = list(app.pick_pair)
    click(app.mlist, 2)                 # 再造一下：`idx == self.sel` -> on_repick
    app.update()
    check(len(was) == 1 and app.pick_pair == [],
          "P5c 连点同一行：第二下能取消（走 on_repick 通道）",
          "第一下 %d 项 -> 第二下 %d 项" % (len(was), len(app.pick_pair)))

    # ---- P6 全取消 -> 交回自动配对 ------------------------------------
    # ⚠️ 这一条测的是「空列表要交回 `_auto_pair()`」这个**分支**，
    #    点击通道已经由 P5c 单独覆盖了，所以这里直接调函数更可控 ——
    #    用点击去凑「恰好取消到空」要连点好几下，读数也不好定位。
    app.pick_pair = list(order[1:3])
    app._fill_member_list()
    app.update()
    app._pick_pair_click(order[1], order)
    app.update()
    app._pick_pair_click(order[2], order)
    app.update()
    check(app.pick_pair == [], "P6a 两张都取消后 pick_pair 空")
    check(app.path_a == order[0],
          "P6b 自动配对回到「代表图打头」",
          "a=%s" % os.path.basename(app.path_a or "-"))

    # ---- P7 连点三张 -> 滑动窗口，只留最近两张 ------------------------
    for r in (1, 2, 3):
        click(app.mlist, r)
        app.update()
    check(app.pick_pair == [order[2], order[3]],
          "P7 连点三张后只留最近两张（滑动窗口）",
          "pick_pair=%s" % [os.path.basename(p) for p in app.pick_pair])

    # ---- P10 `_auto_pair` 不许冲掉用户的选择 --------------------------
    app._auto_pair()
    app.update()
    check(app.path_a == order[2] and app.path_b == order[3],
          "P10 重跑自动配对也不会冲掉自己点的两张")

    # ---- P8 只有 2 张的组：保持自动语义 -------------------------------
    app.glist.select(rows.index(small))
    app.update()
    o2 = app._member_order()
    check(len(o2) == 2, "P8a 2 张组已载入")
    click(app.mlist, 1)
    app.update()
    check(app.pick_pair == [], "P8b 2 张的组不进自定义模式（pick_pair 仍空）")
    check(app.path_a == o2[0] and app.path_b == o2[1],
          "P8c 2 张组仍是「代表图 vs 那一张」")

    # ---- P9 切组后陈旧选择被清掉 --------------------------------------
    app.glist.select(rows.index(big))
    app.update()
    click(app.mlist, 1)
    click(app.mlist, 2)
    app.update()
    n_pick = len(app.pick_pair)
    app.glist.select(rows.index(small))
    app.update()
    check(n_pick == 2 and app.pick_pair == [],
          "P9 切组后陈旧的自定义选择被剔除",
          "切前 %d 项 -> 切后 %d 项" % (n_pick, len(app.pick_pair)))

    app.destroy()
    print("\n== 结果：%d 通过 / %d 失败 ==" % (len(OK), len(BAD)))
    for b in BAD:
        print("   XX " + b)
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
