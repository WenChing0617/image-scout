# -*- coding: utf-8 -*-
"""配色的**离线标定脚本**（可选，改色系的时候用）。

作用：把「想换的那个色系」先在这儿试，**别直接改 `uikit.py`** —— 因为它会
逐条算出 `test_ui.test_palette` 的全部硬指标（色相 / 对比度 / 按钮白字），
一眼就能看出候选方案栽在哪一条、要往哪个方向调。

跑法（**必须用带 tkinter 的那支 Python**，`uikit` 要 import tkinter）：

      D:\\python\\python-3.13.5\\python.exe calib_palette.py

本文**不建窗口**，只是 import；主程序与五个 `test_*.py` 都不 import 它。
对比度的算法直接复用 `test_ui.contrast` / `test_ui.hue` —— 不抄第二份，
否则测试和标定会各说各话（口径漂移比算错更难查）。

--- 这份文件记着「为什么是现在这套色」---

用户提过**四次**配色要求：「颜色有些灰，个人喜欢蓝色系」→「换浅天蓝」→
「颜色浅一些，亮一些，清新通透一些」→「**颜色灰不溜秋的好恶心，开始扫描的按键为什么不改**」。
三次关键结论：

**① 靛蓝与天蓝都是「B 通道最大」**，光看蓝通道分不出来，必须看**色相**：

    靛蓝  #3b74c6  色相 215.4°   ← 被用户否掉（「不够天蓝」）
    天蓝  #4fb6ec  色相 200.6°   ← 现在这套

**② 「浅」≠「去饱和」。** 第三轮我把每个色都调浅了，页面底彩度掉到 **21**
（纯白是 0），蓝色被洗没了 —— 用户原话「灰不溜秋」，而**所有对比度断言都还合格**。
对比度测不出「灰」，所以现在测试里有**彩度下限**（页面底 ≥ 35 等）。

**③ 主色的「白字天花板」靠换形态破，不靠再压暗。** 白字必须 ≥ 4.0 把主色
钉死在中深蓝（`#1179bd` 就是尽头，见下面的阶梯）。第四轮把「开始扫描」改成
**亮天蓝底 `#4fb6ec` + 深字 `PRIMARY_INK`** —— 白字约束消失，
深字压亮底 6.7~7.5，比原来白字的 4.2~4.7 还好读。

`REJECTED` 里留着试过但没过的方案，标了各自栽在哪 —— 下次再调色，先看这儿，能省一轮。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import test_ui          # noqa: E402  （只为复用 contrast / hue，不建窗口）
import uikit            # noqa: E402

contrast = test_ui.contrast
hue = test_ui.hue


# ---------------------------------------------------------------------------
# 硬指标：与 test_ui.test_palette 一一对应（改了那边记得同步这里）
# ---------------------------------------------------------------------------

BLUE_NAMES = ("PAGE", "PAGE_D", "CARD_TINT", "TEXT", "TEXT_2", "TEXT_3",
              "PRIMARY", "PRIMARY_D", "PRIMARY_L", "BORDER", "BORDER_HI")

HUE_LO, HUE_HI = 195.0, 212.0        # 「天蓝」的色相带（靛蓝 ≈ 215°）
PRIMARY_HUE_LO, PRIMARY_HUE_HI = 198.0, 208.0


def text_reqs(P):
    """(标签, 前景, 背景, 门槛) —— 全部来自 test_palette。"""
    return [
        ("正文 on 卡片", P.TEXT, P.CARD, 7.0),
        ("正文 on 页面底", P.TEXT, P.PAGE, 7.0),
        ("正文 on 卡片浅底", P.TEXT, P.CARD_SOFT, 7.0),
        ("正文 on 选中行", P.TEXT, P.ROW_SEL, 7.0),
        ("次要字 on 卡片", P.TEXT_2, P.CARD, 4.5),
        ("次要字 on 页面底", P.TEXT_2, P.PAGE, 4.5),
        ("提示字 on 卡片", P.TEXT_3, P.CARD, 3.5),
        ("提示字 on 页面底", P.TEXT_3, P.PAGE, 3.0),
        # ⚠️ 主色 2026-10-05 起是「亮天蓝底 + 深字」，
        #    旧的「主色 on 卡片 >= 4.5」作废（亮底跟白底比 4.5 毫无意义）。
        ("CTA 深字压亮底", P.PRIMARY_INK, P.PRIMARY, 4.5),
        ("soft 按钮字", P.PRIMARY_D, P.CARD_TINT, 4.5),
        ("次要强调字", P.TEAL_D, P.CARD, 4.5),
        ("提示色字", P.WARN_D, P.CARD, 4.5),
        ("低质量强调字", P.POOR, P.CARD, 3.5),
    ]


def audit(P, verbose=True):
    """把一套配色按全部硬指标算一遍，返回 (失败项列表, 最低富余)。"""
    bad = []

    # 1) 蓝调族 + 天蓝色相
    for n in BLUE_NAMES:
        c = getattr(P, n)
        r, g, b = [int(c.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
        if not (b > r and b > g):
            bad.append("%s 非蓝调" % n)
        hu = hue(c)
        if not (HUE_LO <= hu <= HUE_HI):
            bad.append("%s 色相 %.1f° 出了天蓝带" % (n, hu))
    if not (PRIMARY_HUE_LO <= hue(P.PRIMARY) <= PRIMARY_HUE_HI):
        bad.append("主色色相 %.1f° 不在天蓝正中" % hue(P.PRIMARY))

    # 2) 文字对比度
    worst = 99.0
    for tag, fg, bg, need in text_reqs(P):
        v = contrast(fg, bg)
        worst = min(worst, v - need)
        if verbose:
            print("  %s %-16s %5.2f / %.1f" % ("  " if v >= need else "!!",
                                               tag, v, need))
        if v < need:
            bad.append("%s %.2f<%.1f" % (tag, v, need))

    # 3) 三种实心按钮 x 三个状态：按钮字压底（danger/teal 白字，primary 深字）
    old, uikit.Palette = uikit.Palette, P
    try:
        for kind in ("primary", "teal", "danger"):
            for state in ("normal", "hover", "press"):
                top, bot, _bd, fg = uikit._spec(kind, state)
                a, b = contrast(fg, top), contrast(fg, bot)
                if verbose:
                    print("  %s %s/%-6s 按钮字 上%.2f 下%.2f"
                          % ("  " if min(a, b) >= 4.0 else "!!", kind, state, a, b))
                if min(a, b) < 4.0:
                    bad.append("%s/%s 按钮字 %.2f" % (kind, state, min(a, b)))
    finally:
        uikit.Palette = old

    # 4) 分层与描边
    lay, bd = contrast(P.CARD, P.PAGE), contrast(P.BORDER_HI, P.CARD)
    if lay < 1.05:
        bad.append("卡片与页面底没分层 %.3f" % lay)
    if bd < 1.3:
        bad.append("描边看不见 %.3f" % bd)
    if verbose:
        print("  卡片/页面底分层 %.3f（>=1.05）；描边 %.3f（>=1.3）" % (lay, bd))
        print("  主色色相 %.1f°（天蓝 198~208°，靛蓝 ≈215°）" % hue(P.PRIMARY))
    return bad, worst


# ---------------------------------------------------------------------------
# 试过但没过的方案 —— 下次调色先看这儿
# ---------------------------------------------------------------------------

class _P:
    """一个只有色值的空壳，假装自己是 Palette。"""

    def __init__(self, **kw):
        self.ON_PRIMARY = "#ffffff"
        for k, v in kw.items():
            setattr(self, k, v)


COMMON = dict(TEAL="#2b7f9c", TEAL_D="#1d647f", WARN="#c8894a", WARN_D="#9c6530",
              DANGER="#bd5a53", DANGER_D="#96403a", POOR="#a9723c",
              MATCH="#bd5a53", LINE="#e3f2fd", CARD="#ffffff",
              PRIMARY_INK="#08283f")   # CTA 深字（2026-10-05 起主色是亮底）

REJECTED = [
    ("A：天蓝但主色没压暗",
     "#0f7fc4 色相 202.9°",
     "白字压主色 4.33 < 4.5，按钮上渐变 3.97 < 4.0 —— "
     "光换色相不压亮度就是这个下场",
     _P(PAGE="#e8f4fe", PAGE_D="#d8ebfc", CARD_SOFT="#f4fafe", CARD_TINT="#e1f0fd",
        BORDER="#d2e6f8", BORDER_HI="#b0d2f0", TEXT="#1b3348", TEXT_2="#3c6a90",
        TEXT_3="#5a84a8", PRIMARY="#0f7fc4", PRIMARY_D="#0b649e",
        PRIMARY_L="#9dcced", SHADOW="#b3cbe4", ROW_SEL="#dceefc", **COMMON)),

    ("B：更偏青",
     "#0b81c0 色相 200.9°",
     "同上（4.27 / 3.92）；色相也更靠近青，离「天」蓝反而远了点",
     _P(PAGE="#e6f6fd", PAGE_D="#d5edfb", CARD_SOFT="#f3fbfe", CARD_TINT="#ddf1fd",
        BORDER="#cfe7f7", BORDER_HI="#a9d4ef", TEXT="#173044", TEXT_2="#356b93",
        TEXT_3="#5484a9", PRIMARY="#0b81c0", PRIMARY_D="#08689c",
        PRIMARY_L="#98cdec", SHADOW="#a9cbe6", ROW_SEL="#d8eefb", **COMMON)),

    ("C：页面底最淡的一版",
     "#1a86c6 色相 202.3°",
     "三项不过（3.99 / 3.66 / 3.99）；淡底还会让「提示字 on 页面底」只剩 0 富余",
     _P(PAGE="#eaf6fe", PAGE_D="#daeeff", CARD_SOFT="#f5fbff", CARD_TINT="#e2f2fe",
        BORDER="#d4e9fa", BORDER_HI="#aed6f4", TEXT="#1d3549", TEXT_2="#3f7093",
        TEXT_3="#5d8aad", PRIMARY="#1a86c6", PRIMARY_D="#0f6aa4",
        PRIMARY_L="#a3d3f1", SHADOW="#b0cfe8", ROW_SEL="#def0fd", **COMMON)),

    ("D：「再浅一档」的底 + 更亮的主色",
     "PAGE #edf8ff，PRIMARY #1380c4",
     "主色本色 4.27 / 白字上渐 3.92 —— **就是主色天花板那一档**；"
     "顺带提示字也掉到 3.41。说明「页面底还能淡一点点，但主色一步都不能再亮」",
     _P(PAGE="#edf8ff", PAGE_D="#dff1fe", CARD_SOFT="#f9fdff", CARD_TINT="#e9f6ff",
        BORDER="#e1f0fc", BORDER_HI="#c2def5", TEXT="#2a4864", TEXT_2="#4a759b",
        TEXT_3="#6390b1", PRIMARY="#1380c4", PRIMARY_D="#106fa9",
        PRIMARY_L="#b5ddf7", SHADOW="#cadff3", ROW_SEL="#edf8ff", **COMMON)),
]


PRIMARY_LADDER = ("#0d78b8", "#1077bb", "#1179bd", "#127dc2", "#1380c4", "#1785c9")
"""主色亮度阶梯：找「还能再亮到哪」。

判据两条 —— 本色在白底上 ≥ 4.5（它在卡片上要看得见），
**按钮上渐的白字 ≥ 4.0**（`_spec` 的顶部高光往白里混 6%，所以上渐比本色更险）。
后者先塌，它就是天花板。
"""


def primary_ceiling():
    """【历史记录】白字时代主色的天花板。

    现在的主色已经改成「亮天蓝底 + 深字」（`#4fb6ec` + `PRIMARY_INK`），
    白字约束**不再存在**，这个阶梯只留着说明「当初为什么被钉在中深蓝」——
    免得以后有人看到旧文档里的 `#1179bd` 又想回去。
    """
    print("\n=== 【历史】白字时代主色能亮到哪（白字 ≥ 4.0 就是天花板）===")
    last_ok = None
    for c in PRIMARY_LADDER:
        base = contrast(c, "#ffffff")
        top = contrast(uikit.mix(c, "#ffffff", 0.06), "#ffffff")
        ok = top >= 4.0 and base >= 4.5
        if ok:
            last_ok = c
        print("  %s %s  本色 %.2f / 上渐白字 %.2f  色相 %.1f°"
              % ("OK " if ok else "!! ", c, base, top, hue(c)))
    print("  → 白字时代最亮的可用主色：%s" % (last_ok or "（一个都不过）"))
    print("  → 现在的主色 %s 与白底 %.2f —— 因为换了**深字**，约束没了"
          % (uikit.Palette.PRIMARY, contrast(uikit.Palette.PRIMARY, "#ffffff")))
    print("  ⚠️ 教训：主色被白字钉死时，出路是**换形态**（亮底 + 深字），别再压暗。")


def main():
    print("=== 当前配色（uikit.Palette）===")
    bad, worst = audit(uikit.Palette)
    print("  最低富余 %.2f；%s" % (worst, "全部通过" if not bad
                                 else "失败 %d 项：%s" % (len(bad), "; ".join(bad))))

    primary_ceiling()

    print("\n=== 被否掉的方案（留个记录，别再试第二遍）===")
    for name, anchor, why, P in REJECTED:
        bad, worst = audit(P, verbose=False)
        print("  %-22s %-18s -> %s" % (name, anchor,
                                       "通过" if not bad else "不过 %d 项" % len(bad)))
        print("        %s" % why)

    print("\n=== 结论 ===")
    print("  1. 是「天蓝」不是「靛蓝」：色相要落在 195~212°（主色 198~208°）。")
    print("  2. 「浅」≠「去饱和」：页面底彩度掉到 21 时整屏灰白，而对比度全部合格。")
    print("     所以现在有彩度下限（页面底 ≥35 等）。")
    print("  3. 主色现在是「亮天蓝底 + 深字」，白字天花板已经不存在 ——")
    print("     想更亮就调高 PRIMARY 的亮度，别再走回白字压深底的老路。")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
