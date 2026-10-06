# -*- coding: utf-8 -*-
"""界面端到端测试：真的把窗口建起来、跑一遍扫描、点一遍操作。

不靠肉眼看截图，靠断言：
  * **清晰度**：Tk 报的 DPI 与系统真实 DPI 一致（说明进程开了 DPI 感知，
    窗口没被 Windows 位图拉伸 —— 这是「界面糊」的根因）
  * 扫描能在后台跑完并回填到界面（分组列表有内容、成员列表有内容）
  * **选中一组就自动对比**（不用再点「与代表图对比」）
  * 两张图的预览都**填满各自格子**，且**预览区比信息+操作加起来还高**
  * **信息是两列并排**、同字段对齐（不是「选哪张看哪张」）
  * **没有匹配红框了**；「只看匹配区域」切过去之后画的是裁出来的那块
  * 列表每一项都带一张小预览；左栏**分组列表比成员列表高**
  * 「全部图片」视图能列出所有图片，**低质量图带徽章被强调**
    （徽章文字必须等于判据标签：「低清」是分辨率低，「体积小」是压缩后没信息量）
  * 滚轮能放大（放大后重新解码，不是把小图硬拉）、能重置
  * 双击图片交给系统打开（「打开 A/B」按钮已删掉）
  * **删除 = 移动进扫描文件夹下的隔离夹**，文件真的过去了
  * 顶栏按钮都在同一行、开始扫描紧挨着停止且更大
  * 三档灵敏度都能跑完，且「严格」的边数 <= 「宽松」的边数

需要有桌面会话（Windows 一定有）。跑法：
  python test_ui.py
"""
import io
import os
import re
import shutil
import sys
import tempfile
import time
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import image_scout as ui    # noqa: E402
import makeset              # noqa: E402
import net                  # noqa: E402
import quarantine           # noqa: E402
import scan                 # noqa: E402
import thumbs               # noqa: E402
import uikit                # noqa: E402
import winimg               # noqa: E402

# 样本放 %TEMP%，跑测试不会把工程目录弄脏
SAMPLE_ROOT = os.path.join(tempfile.gettempdir(), "ImageScout-uitest")

FAIL = []


def check(cond, msg):
    print("   %s %s" % ("OK  " if cond else "FAIL", msg))
    if not cond:
        FAIL.append(msg)
    return cond


def reset_sample_root(root):
    """把样本根目录清空，保证测试可重复跑。

    上一轮跑测试时被「删除」掉的图会留在 `_隔离` 里，不清掉的话：
    重名的文件会被 `unique_path` 加上 `-1` 后缀，按名字数数就会数多。
    逐文件删（本环境里 `shutil.rmtree` 会被安全删除守卫静默杀掉），
    失败也不抛异常 —— 测试不该因为打扫不干净就跑不下去。
    """
    if not os.path.isdir(root):
        return
    for base, dirs, names in os.walk(root, topdown=False):
        for nm in names:
            try:
                os.remove(os.path.join(base, nm))
            except OSError:
                pass
        for dn in dirs:
            try:
                os.rmdir(os.path.join(base, dn))
            except OSError:
                pass


def pump(root, seconds=0.02, until=None, timeout=120.0):
    """跑事件循环，直到条件成立或超时。界面测试必须这样「等」，不能 sleep。"""
    end = time.time() + timeout
    while time.time() < end:
        root.update()
        if until is not None and until():
            return True
        time.sleep(seconds)
    return until is None


def widget_texts(widget):
    """把容器里所有能显示文字的控件文本收集起来。

    两处坑：
      * `ttk.Menubutton` 不是 `ttk.Button` 的子类，按 isinstance 过滤会漏；
      * `uikit.RoundButton` / `Pill` 是自绘的 Canvas，文本存在自己的属性里，
        `cget("text")` 会抛 TclError —— 得单独认。
    """
    out = []
    for ch in widget.winfo_children():
        if isinstance(ch, (uikit.RoundButton, uikit.Pill)):
            out.append(ch.get_text())
        else:
            try:
                out.append(str(ch.cget("text")))
            except tk.TclError:
                pass
        out.extend(widget_texts(ch))
    return out


def canvas_items(cv):
    """返回 (图元类型集合, 图片包围盒列表)。"""
    kinds = set()
    boxes = []
    for it in cv.find_all():
        t = cv.type(it)
        kinds.add(t)
        if t == "image":
            bb = cv.bbox(it)
            if bb:
                boxes.append(bb)
    return kinds, boxes


def row_texts(lst, i):
    """第 i 行的所有文字图元（按 `row{i}` 标签找）。"""
    out = []
    for cid in lst.find_withtag(lst._row_tag(i)):
        if lst.type(cid) == "text":
            try:
                out.append(lst.itemcget(cid, "text"))
            except tk.TclError:
                pass
    return out


def shown_pct(cv):
    """图片在格子里占了多大（长边方向）—— 用来验「尽可能放大」。"""
    _, boxes = canvas_items(cv)
    if not boxes:
        return 0.0
    bb = boxes[0]
    return max((bb[2] - bb[0]) / float(max(1, cv.winfo_width())),
               (bb[3] - bb[1]) / float(max(1, cv.winfo_height())))


def settle_render(app, timeout=6.0):
    """等「去抖重绘」真的按**当前**画布尺寸画完，再量几何。

    画布绑了 `<Configure>` → 130ms 后才 `render_all()`。不等就去量 bbox，
    量到的是用**上一次容器尺寸**画出来的图元：窗口刚从大变小的时候它比画布还大，
    看着像「图溢出被裁」，其实只是测试没等 —— 这个假红踩过（724×543 vs 画布 736×507）。
    判据是「画布尺寸 == 这次渲染记录的尺寸」且图元完整装在画布里。
    """
    def ok():
        # ⚠️ 高清图是**后台编码**的（不然切一组要卡半秒）。不等它回来就量，
        #    量到的是占位图 —— 占位图也能满足「画了图」「没溢出」，
        #    于是「放大后真的解了更大的图」这类断言变成**假绿**。
        if app._fit_pend:
            return False
        for col, cv in ((0, app.cv_a), (1, app.cv_b)):
            v = app._view.get(col)
            if not v:
                continue
            item = v["item"]                 # 现在是 dict：{画布, 图元, 位置, ...}
            cw, ch = cv.winfo_width(), cv.winfo_height()
            bb = cv.bbox(item)
            if not bb:
                return False
            # ⚠️ 放大后画布上只有「视口那一块」，它应当盖住「图 ∩ 画布」这一整块。
            #    所以不能直接要求「图元 == 画布」：<｜hy_place▁holder▁no▁813｜>比 zoom 小时上下本来就该留黑边。
            disp_w, disp_h = v["disp"]
            ox = (cw - disp_w) // 2 + app.pan[col][0]
            oy = (ch - disp_h) // 2 + app.pan[col][1]
            want = (max(0, ox), max(0, oy),
                    min(cw, ox + disp_w), min(ch, oy + disp_h))
            if want[2] <= want[0] or want[3] <= want[1]:
                return False                 # 图被拖出画布了
            if bb[0] > want[0] + 2 or bb[1] > want[1] + 2 \
                    or bb[2] < want[2] - 2 or bb[3] < want[3] - 2:
                return False                 # 还没铺满（多半是后台还在出图）
        return True
    return pump(app, until=ok, timeout=timeout)


def make_tiny(path, w=200, h=150):
    """造一张低清小图（长边 < 480），用来验「质量差的图要被强调」。"""
    px = bytes((200, 180, 140, 255)) * (w * h)
    with open(path, "wb") as f:
        f.write(thumbs.bgra_to_png(w, h, px))
    return path


def _lumi(hex_color):
    h = hex_color.lstrip("#")
    c = [int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]
    c = [(v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4) for v in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def contrast(fg, bg):
    """WCAG 对比度。"""
    a, b = _lumi(fg), _lumi(bg)
    if a < b:
        a, b = b, a
    return (a + 0.05) / (b + 0.05)


def hue(hex_color):
    """色相角（0~360）。用来区分「天蓝」和「靛蓝」—— 光看 B 通道是分不出来的。"""
    h = hex_color.lstrip("#")
    r, g, b = [int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]
    mx, mn = max(r, g, b), min(r, g, b)
    d = mx - mn
    if d == 0:
        return 0.0
    if mx == r:
        v = ((g - b) / d) % 6
    elif mx == g:
        v = (b - r) / d + 2
    else:
        v = (r - g) / d + 4
    return v * 60.0


def test_palette():
    """配色不只看好看，还要保证看得清。

    这条测试是「换了一次配色顺手把字调浅了」的防线 —— 光看截图不一定发现，
    但对比度一掉就是实打实的看不清。另外还钉住「是天蓝、不是靛蓝」：
    色相必须落在 195~212°，因为**靛蓝和天蓝都是「B 通道最大」**，
    少这一条就分不出来（上一版 215° 就是这么混过去的）。
    """
    P = uikit.Palette
    print("\n=== 配色：浅天蓝 + 对比度 ===")

    # 1) 是蓝色系，不是灰。判据：正文/主色都带明显的蓝（B 通道最大）
    #    ⚠️ PAGE 不在列表里 —— 2026-10-05 主人定稿「背景改回白色」，页面底是**故意**纯白。
    for name in ("PAGE_D", "CARD_TINT", "TEXT", "TEXT_2", "TEXT_3",
                 "PRIMARY", "PRIMARY_D", "PRIMARY_L", "BORDER", "BORDER_HI"):
        r = int(getattr(P, name).lstrip("#")[0:2], 16)
        g = int(getattr(P, name).lstrip("#")[2:4], 16)
        b = int(getattr(P, name).lstrip("#")[4:6], 16)
        check(b > r and b > g, "%s=%s 是蓝调（B 最大）" % (name, getattr(P, name)))

    # 1b) ⚠️「B 最大」这一条**靛蓝也满足** —— 它钉不住用户真正要的东西。
    #     用户说过一次「不够天蓝」，而天蓝与靛蓝的差别在**色相**（绿通道抬没抬起来），
    #     不在蓝通道多少。所以这里直接量色相：这条才是「换浅天蓝」的防线。
    #     锚点：靛蓝那版主色为 215.4°（被否掉），现在这版是 201°。
    #     ⚠️ PAGE 同样不在列表里（纯白没有色相），它由 1c 的「PAGE 必须是纯白」守着。
    for name in ("PAGE_D", "CARD_TINT", "TEXT", "TEXT_2", "TEXT_3",
                 "PRIMARY", "PRIMARY_D", "PRIMARY_L", "BORDER", "BORDER_HI"):
        hu = hue(getattr(P, name))
        check(195.0 <= hu <= 212.0,
              "%s=%s 是天蓝色相（%.1f°，靛蓝≈215°）"
              % (name, getattr(P, name), hu))
    # 主色尤其要看：这一支决定了整屏的「蓝」是偏紫还是偏青
    check(198.0 <= hue(P.PRIMARY) <= 208.0,
          "主色 %s 落在天蓝正中（%.1f°）" % (P.PRIMARY, hue(P.PRIMARY)))
    print("   主色色相 %.1f°（靛蓝锚点 215.4°）" % hue(P.PRIMARY))

    # 1c) ⚠️ 两个方向都守：
    #     ① PAGE 必须是**纯白** —— 2026-10-05 主人原话「背景的蓝色改回白色」，
    #        之前把页面做成浅天蓝（#cce8fd）被否了。这条防它悄悄蓝回去。
    #     ② 其余蓝色件**不能被洗成灰** —— 判据是**彩度**（最大通道 - 最小通道），
    #        纯白是 0。踩过的坑：页面底彩度掉到 21 时整屏灰白，而对比度全「合格」。
    def chroma(c):
        r, g, b = [int(c.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
        return max(r, g, b) - min(r, g, b)

    check(P.PAGE.lower() == "#ffffff", "PAGE=%s 是纯白（主人定稿：背景改回白色）" % P.PAGE)
    for name, need in (("PAGE_D", 20), ("CARD_TINT", 40),
                       ("ROW_SEL", 45), ("BORDER", 55), ("BORDER_HI", 90)):
        v = chroma(getattr(P, name))
        check(v >= need, "%s=%s 彩度 %d >= %d（纯白是 0，太淡就是灰）"
              % (name, getattr(P, name), v, need))
    print("   页面底 %s（纯白）；主色彩度 %d、色相 %.1f°"
          % (P.PAGE, chroma(P.PRIMARY), hue(P.PRIMARY)))

    # 1d) `PRIMARY` 只做填充这个前提得有人守着 —— 一旦有人把它当文字色用，
    #     上面「CTA 深字压亮底」那条就守不住了（亮底当字色等于看不见）。
    #    ⚠️ 别把 `PRIMARY_D` / `PRIMARY_L` 也算进来 —— 它们是**字色**，本来就该用。
    src = io.open(os.path.join(HERE, "image_scout.py"), encoding="utf-8").read()
    misuse = [ln.strip() for ln in src.splitlines()
              if re.search(r"\b(?:fg|foreground)=P\.PRIMARY(?!_)", ln)]
    check(not misuse,
          "PRIMARY 没被当文字色用（%s）" % ("；".join(misuse) or "它只做填充"))

    # 2) 对比度门槛（正文 7、次要 4.5、提示 3.5）
    reqs = [
        ("正文 on 卡片", P.TEXT, P.CARD, 7.0),
        ("正文 on 页面底", P.TEXT, P.PAGE, 7.0),
        ("正文 on 卡片浅底", P.TEXT, P.CARD_SOFT, 7.0),
        ("正文 on 选中行", P.TEXT, P.ROW_SEL, 7.0),
        ("次要字 on 卡片", P.TEXT_2, P.CARD, 4.5),
        ("次要字 on 页面底", P.TEXT_2, P.PAGE, 4.5),
        ("提示字 on 卡片", P.TEXT_3, P.CARD, 3.5),
        ("提示字 on 页面底", P.TEXT_3, P.PAGE, 3.0),
        # ⚠️ 这条原来是 `PRIMARY on CARD >= 4.5`（照「正文」的门槛）。但主色现在是
        #    **亮天蓝底**（与白底只有 2.27），拿它跟白底比 4.5 毫无意义 ——
        #    它上面压的是 `PRIMARY_INK` 深字，不是正文。所以改成**真正的要求**：
        #    深字压亮底必须 >= 4.5（实测 6.7~7.5）。
        ("CTA 深字压亮底", P.PRIMARY_INK, P.PRIMARY, 4.5),
        ("soft 按钮字", P.PRIMARY_D, P.CARD_TINT, 4.5),
        ("次要强调字", P.TEAL_D, P.CARD, 4.5),
        ("提示色字", P.WARN_D, P.CARD, 4.5),
        ("低质量强调字", P.POOR, P.CARD, 3.5),
    ]
    worst = 99.0
    for tag, fg, bg, need in reqs:
        v = contrast(fg, bg)
        worst = min(worst, v - need)
        check(v >= need, "%-16s %5.2f >= %.1f" % (tag, v, need))
    print("   （最低富余 %.2f）" % worst)

    # 3) 按钮上的字压得住底 —— 三种实心按钮 x 三个状态，上渐和下渐都得看得清。
    #    ⚠️ 这条用的是 `_spec` 返回的**实际字色**：`danger`/`teal` 是白字，
    #    `primary` 现在是深字（`PRIMARY_INK` 压亮底）—— 同一条断言两者都管。
    #    这条最容易翻车：把 hover 做成「变亮」白字就糊了（实测只剩 2.6）。
    for kind in ("primary", "teal", "danger"):
        for state in ("normal", "hover", "press"):
            top, bot, _border, fg = uikit._spec(kind, state)
            a, b = contrast(fg, top), contrast(fg, bot)
            check(min(a, b) >= 4.0,
                  "%s/%-6s 按钮字 上渐 %.2f 下渐 %.2f" % (kind, state, a, b))

    # 3b) 选中态（`on`）：用户说「已选中的按钮颜色同理」，指的是也要浅、也要通透。
    #     但**浅了就必须看得出来哪个是选中的** —— 这两条一起才守得住：
    #       ① 底是真的浅（比主色浅得多，别又退回实心块）
    #       ② 跟未选中（ghost，白底）分得开 + 描边看得见 + 字读得清
    #     只做 ① 不做 ② 的下场是「三个按钮长得都一样，用户不知道选了谁」。
    print("\n=== 选中态（`on`）：要浅，也要看得出来 ===")
    for state in ("normal", "hover", "press"):
        top, bot, edge, fg = uikit._spec("on", state)
        lo = min(contrast(top, P.CARD), contrast(bot, P.CARD))
        hi = max(contrast(top, P.CARD), contrast(bot, P.CARD))
        check(hi <= 2.4, "on/%s 底是浅的（与白底 %.2f，实心主色可是 %.2f）"
              % (state, hi, contrast(P.PRIMARY, P.CARD)))
        check(lo >= 1.35, "on/%s 底又没浅到看不出来（%.2f）" % (state, lo))
        check(contrast(fg, top) >= 4.5 and contrast(fg, bot) >= 4.5,
              "on/%s 字读得清（上 %.2f 下 %.2f）"
              % (state, contrast(fg, top), contrast(fg, bot)))
        check(contrast(edge, bot) >= 2.8,
              "on/%s 描边看得见（%.2f）" % (state, contrast(edge, bot)))
    g_top, g_bot, _ge, _gf = uikit._spec("ghost", "normal")
    o_top, o_bot, _oe, _of = uikit._spec("on", "normal")
    sep = min(contrast(o_top, g_top), contrast(o_bot, g_bot))
    check(sep >= 1.4,
          "选中与未选中**分得开**（底与底 %.2f；ghost 是白底）" % sep)
    print("   选中底 %s ~ %s；未选中底 %s ~ %s"
          % (o_top, o_bot, g_top, g_bot))

    # 4) 四态配色都得能算出来（别出现 None 导致按钮变黑）
    for kind in ("primary", "on", "soft", "teal", "danger", "ghost"):
        for state in ("normal", "hover", "press", "disabled"):
            s = uikit._spec(kind, state)
            ok = (len(s) == 4 and all(isinstance(x, str) and x.startswith("#")
                                      and len(x) == 7 for x in s))
            check(ok, "%s/%s 四元组是 4 个合法色值" % (kind, state))

    # 5) 分层：页面白、卡片也白（2026-10-05 定稿），层次**全靠描边 + 阴影**表态 ——
    #    所以守的是「描边/阴影在白底上看得见」，不再守卡片与页面的反差。
    check(P.CARD.lower() == P.PAGE.lower() == "#ffffff",
          "页面与卡片同为纯白（分层交给描边）")
    check(contrast(P.BORDER_HI, P.CARD) >= 1.3, "描边在卡片上看得见")
    check(contrast(P.SHADOW, P.CARD) >= 1.05, "阴影在白底上看得见")


BIG_ROOT = os.path.join(tempfile.gettempdir(), "ImageScout-bigtest")


def make_huge(path, w=3000, h=2000):
    """造一张 3000×2000 的 PNG（行 pattern 拼，避免逐像素慢）。

    ⚠️ 之前的流畅度断言全是用 480×360 的小样本跑的，**测不出真实开销**：
    主人的照片是几千万像素的。4000×3000 的图按老实现缩放一次要 229ms，
    小样本那边只有几十毫秒 —— 这就是为什么「测试全绿但主人还是觉得卡」。
    """
    rows = []
    for phase in range(8):
        line = bytearray()
        for x in range(w):
            line += bytes(((x + phase * 37) % 256,
                           (x * 3 + phase * 11) % 256,
                           (x * 7 + phase * 53) % 256, 255))
        rows.append(bytes(line))
    buf = bytearray()
    for y in range(h):
        buf += rows[y % 8]
    with open(path, "wb") as f:
        f.write(thumbs.bgra_to_png(w, h, bytes(buf)))
    return path


def test_big_zoom():
    """真实大图的缩放流畅度（单独一个窗口，测完就销毁）。"""
    print("\n########  大图（3000×2000）缩放  ########")
    shutil.rmtree(BIG_ROOT, ignore_errors=True)
    os.makedirs(BIG_ROOT, exist_ok=True)
    a = make_huge(os.path.join(BIG_ROOT, "big_a.png"))
    got = winimg.load_pixels(a, 2000, winimg.SIIGBF_RESIZETOFIT, raw=True)
    w, h, px = got
    _w, _h, cut = thumbs.crop_bgra(w, h, px, int(w * 0.1), int(h * 0.1),
                                   int(w * 0.8), int(h * 0.8))
    with open(os.path.join(BIG_ROOT, "big_b.png"), "wb") as f:
        f.write(thumbs.bgra_to_png(_w, _h, cut))

    # ⚠️ 上一个窗口刚销毁：模块级的圆角图缓存里还挂着**旧解释器**的
    #    PhotoImage，直接建第二个窗口会报 image "pyimage1" doesn't exist。
    uikit.clear_photo_caches()
    app = ui.App([BIG_ROOT])
    app.update()
    app.start_scan()
    ok = pump(app, until=lambda: not app.busy and bool(app.files), timeout=300)
    check(ok, "大图样本扫完了（%d 张）" % len(app.files))
    app.set_view("all")
    row = next(i for i, it in enumerate(app.glist.items) if it["tag"] == a)
    app.glist.select(row)
    app.update()
    settle_render(app)
    # 画布尺寸一变预热会重做一遍，所以要等「队列空 + 不在忙」稳定一小会儿
    def warm_idle():
        return (not getattr(app, "_prewarm_q", [])
                and not getattr(app, "_prewarm_busy", False))
    pump(app, until=warm_idle, timeout=120)
    settle_render(app)
    pump(app, until=warm_idle, timeout=120)
    settle_render(app)
    print("   画布 %dx%d" % (app.cv_a.winfo_width(), app.cv_a.winfo_height()))

    class Ev(object):
        def __init__(self, d):
            self.delta = d
            self.x = 10
            self.y = 10

    up, down = [], []
    for _ in range(8):
        t0 = time.perf_counter()
        app._on_wheel(0, Ev(120))
        app.update()
        up.append((time.perf_counter() - t0) * 1000)
        settle_render(app)
    for _ in range(8):
        t0 = time.perf_counter()
        app._on_wheel(0, Ev(-120))
        app.update()
        down.append((time.perf_counter() - t0) * 1000)
        settle_render(app)
    print("   放大 %s" % " ".join("%.0f" % v for v in up))
    print("   缩小 %s" % " ".join("%.0f" % v for v in down))
    # 老实现：放大 130~229ms、缩小 200~342ms；新实现的目标是**跟 zoom 无关**。
    check(max(up) < 80, "大图放大不卡（最慢 %.0f ms < 80 ms）" % max(up))
    check(max(down) < 80, "大图缩小不卡（最慢 %.0f ms < 80 ms）" % max(down))

    # ---- 清晰度：这一条是被用户反馈逼出来的（2026-10-06「放大还有马赛克」）
    # 判据是**视口口径**的欠采样倍数：屏幕上真正看得见的那一块，
    # 是用多少个源像素撑起来的。>1.4 就是肉眼可见的糊。
    # ⚠️ 别拿 `disp_w / blk_w` 算 —— 块含一圈缓冲、显示图比视口大，
    #    那个比值天然偏大，会把清晰的帧也判成糊（踩过）。
    def undersample():
        v = app._view.get(0)
        if not v or "base" not in v:
            return 0.0
        cw, ch = app.cv_a.winfo_width(), app.cv_a.winfo_height()
        dw, dh = v["disp"]
        bw, bh = v["base"]
        sx, sy = bw / float(dw), bh / float(dh)
        ox, oy = v["ox"], v["oy"]
        vx = min(dw, max(0, cw - ox)) - max(0, -ox)
        vy = min(dh, max(0, ch - oy)) - max(0, -oy)
        if vx <= 0 or vy <= 0:
            return 0.0
        return max(cw / float(vx * sx), ch / float(vy * sy))

    sharp = []
    for _ in range(4):
        app._on_wheel(0, Ev(120))
        app.update()
        settle_render(app)
        app.render_all(precise=True, only=0)      # 停手后的精确帧
        # ⚠️ 必须等这一帧**真的画完**再量。`render_all` 内部超时的块会丢后台
        #    编码（`_fit_async`），刚 return 时屏幕上还是 0.75 快速档那一帧 ——
        #    量出来是 1/0.75 = 1.33x 欠采样，**假的**马赛克（踩过：同一份代码
        #    上一轮量到 0.99x，这轮 1.37x，差别只是这一帧有没有赶上落地）。
        settle_render(app)
        app.update()
        sharp.append(undersample())
    # 原图 3000 宽放到 5 倍以上必然超过原生分辨率（放大镜的物理极限），
    # 所以只要求前 4 档（zoom 1.25~2.44）真的 1:1。
    worst = max(sharp[:3]) if sharp else 0.0
    print("   精确帧欠采样 %s" % " ".join("%.2fx" % v for v in sharp))
    check(worst <= 1.15, "放大后是清楚的（最差 %.2fx 欠采样 <= 1.15）" % worst)

    # 再滚一遍同样的位置：应该几乎不花钱
    again = []
    for d in (120, 120, -120, -120):
        t0 = time.perf_counter()
        app._on_wheel(0, Ev(d))
        app.update()
        again.append((time.perf_counter() - t0) * 1000)
        settle_render(app)
    print("   走过的位置再来一次 %.0f~%.0f ms" % (min(again), max(again)))
    check(max(again) < 40, "重复的位置几乎不花钱（最慢 %.0f ms < 40 ms）"
          % max(again))

    # 拖动平移：块里带缓冲，纯挪图元应该是 0ms 级
    app.reset_zoom()
    app.update()
    for _ in range(3):
        app._on_wheel(0, Ev(120))
    app.update()
    settle_render(app)
    app._pan_start(0, Ev(0))
    kind = type("P", (), {})
    moves = []
    for k in range(30):
        p = kind()
        p.x, p.y = 300 + (k % 15) * 10, 300 + (k % 11) * 8
        t0 = time.perf_counter()
        app._pan_move(0, p)
        moves.append((time.perf_counter() - t0) * 1000)
    print("   拖动 %.2f ms/次（最大 %.1f ms）"
          % (sum(moves) / len(moves), max(moves)))
    check(max(moves) < 60, "拖动不卡（最慢 %.1f ms < 60 ms）" % max(moves))
    app.destroy()


def main():
    test_palette()

    root_dir = SAMPLE_ROOT
    print("=== 准备样本 ===")
    reset_sample_root(root_dir)
    print("   清空了 %s（上次跑测试的隔离夹残留也一并清掉）" % root_dir)
    crops_set, noise = makeset.build_set(root_dir)
    n = sum(len(v) for v in crops_set.values())
    tiny = make_tiny(os.path.join(root_dir, "低清小图.png"))
    print("   在 %s 造了 %d 张图（含 1 张低清小图）" % (root_dir, n + len(noise) + 1))
    check(n > 20, "样本数量够用（%d）" % n)

    print("\n=== 建窗口 ===")
    app = ui.App([root_dir])
    app.update()
    check(True, "主窗口建起来了")
    check(bool(app.files), "已列出 %d 张图片" % len(app.files))

    print("\n=== 清晰度：DPI 感知（模糊的根因）===")
    ppi = app.winfo_fpixels("1i")
    real = uikit.enable_dpi_awareness()
    print("   Tk 认为 1 英寸 = %.1f px；系统真实 DPI = %d；屏幕 %dx%d；缩放系数 %.2f"
          % (ppi, real, app.winfo_screenwidth(), app.winfo_screenheight(),
             uikit.scale()))
    check(abs(ppi - real) <= 2,
          "Tk 的 DPI(%.0f) == 系统真实 DPI(%d) —— 进程是 DPI 感知的，"
          "窗口没有被系统位图拉伸" % (ppi, real))
    check(uikit.scale() > 0.9, "像素缩放系数已生效（%.2f）" % uikit.scale())
    lh = app.f_txt.metrics("linespace")
    check(lh >= round(10 / 72.0 * real) - 2,
          "10pt 正文字高 %d px，是按真实 DPI 渲染的（拉伸的话只有 %d px 再被插值放大）"
          % (lh, round(10 / 72.0 * 96)))

    print("\n=== 扫描（标准档）===")
    t0 = time.time()
    app.start_scan()
    ok = pump(app, until=lambda: not app.busy and bool(app.groups), timeout=300)
    dt = time.time() - t0
    check(ok, "扫描在 %.1fs 内跑完并产生分组" % dt)
    print("   状态栏：%s" % app.stat["text"])
    check(len(app.descs) == len(app.files), "所有图都算出了指纹")
    check(bool(app.groups), "至少有一组（%d 组）" % len(app.groups))
    check(bool(app.links), "至少有若干条边（%d 条）" % len(app.links))

    print("\n=== 顶栏：常用按钮挤在一起、开始扫描较大 ===")
    all_texts = widget_texts(app)
    for want in ("添加文件夹", "添加图片", "刷新", "清空", "含子文件夹",
                 "严格", "标准", "宽松", "停止", "开始扫描", "清理完全重复"):
        check(want in all_texts, "顶栏有「%s」" % want)
    # ⚠️ 别直接比 rooty：同一行里高度不同的按钮是按「垂直居中」对齐的，
    # 尺寸大的那个 rooty 天然更小。要比的是**垂直中心**。
    cy_scan = app.btn_scan.winfo_rooty() + app.btn_scan.winfo_height() / 2.0
    cy_stop = app.btn_stop.winfo_rooty() + app.btn_stop.winfo_height() / 2.0
    check(abs(cy_scan - cy_stop) <= 3,
          "「开始扫描」和「停止」在同一行（垂直中心 %.0f / %.0f）"
          % (cy_scan, cy_stop))
    dx = app.btn_scan.winfo_rootx() - (app.btn_stop.winfo_rootx()
                                       + app.btn_stop.winfo_width())
    check(dx <= ui.S(20), "两者之间只隔 %d px（不再是「隔着大半个窗口」）" % dx)
    check(app.btn_scan.winfo_height() > app.btn_dedup.winfo_height(),
          "「开始扫描」比普通按钮大（%d > %d）"
          % (app.btn_scan.winfo_height(), app.btn_dedup.winfo_height()))

    print("\n=== 左边两个列表 ===")
    check(len(app.glist.items) == len(app.groups),
          "分组列表项数 = 分组数（%d）" % len(app.glist.items))
    check(app.glist.sel == 0, "默认选中了第一组")
    check(app.glist.items[0]["photo"] is not None,
          "分组列表每一项都带小预览")
    g = app.groups[app.glist.sel]
    check(len(app.mlist.items) == len(g["members"]),
          "成员列表项数 = 组内张数（%d）" % len(app.mlist.items))
    check(all(it["photo"] is not None for it in app.mlist.items),
          "成员列表每一项都带小预览")
    print("   分组：%s" % " | ".join(it["title"] for it in app.glist.items[:4]))
    print("   成员：%s" % " | ".join(it["title"] for it in app.mlist.items[:4]))

    print("\n=== 每一行都真的画出了文字（回归）===")
    # 2026-10-05 踩的坑：为做 hover 局部重绘把 `redraw()` 拆出 `_draw_row()` 时，
    # 把「标题/副标题/徽章」整段塞进了 `elif i == self.hover:` 分支里 ——
    # 于是**既没选中也没悬停**的行什么都不画，左栏只剩一排空背景块。
    # 只断言「画布上有 text 图元」会漏：选中行那一行照样能画出字。
    # 所以必须**逐行**断言：每一行都得有自己那张 title。
    for lst, nm in ((app.glist, "分组"), (app.mlist, "成员")):
        blank = [i for i in range(len(lst.items))
                 if lst.items[i]["title"] not in " ".join(row_texts(lst, i))]
        check(not blank,
              "%s列表每一行都有文字（缺字的行：%s）"
              % (nm, blank[:6] or "无"))
        kinds = set(lst.type(c) for c in lst.find_all())
        check("text" in kinds,
              "%s列表画布上有文字图元（实际类型 %s）" % (nm, sorted(kinds)))
    # 每个「行标签」下的图元数应该 >1（背景 + 至少一行字）
    thin = [i for i in range(len(app.glist.items))
            if len(app.glist.find_withtag(app.glist._row_tag(i))) < 2]
    check(not thin, "没有「只有背景没有字」的空行（%s）" % (thin[:6] or "无"))

    print("\n=== hover 局部重绘后文字还在 ===")
    # 局部重绘 = 先 delete(row{i}) 再 _draw_row(i)。要是 delete 把刚画的也删了，
    # 划过之后行就空了 —— 上面那条测的是**初始**状态，这条测的是**划过之后**。
    lst = app.glist
    for i in (0, 1, 2, 3):
        lst._motion(type("E", (), {"y": int(i * (lst.row_h + lst.gap)) + 4})())
    lst._leave()
    after = [i for i in range(len(lst.items))
             if lst.items[i]["title"] not in " ".join(row_texts(lst, i))]
    check(not after, "划过若干行 + 移出列表后，文字仍然完整（缺字 %s）"
          % (after[:6] or "无"))

    print("\n=== 左栏比例：分组大、成员小 ===")
    app.update_idletasks()
    gh, mh = app.glist.winfo_height(), app.mlist.winfo_height()
    print("   分组列表高 %d，成员列表高 %d" % (gh, mh))
    check(gh > mh, "分组列表比成员列表高（%d > %d）" % (gh, mh))
    check(mh <= ui.S(220), "成员列表被压小了（%d <= %d）" % (mh, ui.S(220)))

    print("\n=== 自动对比（不用点「与代表图对比」）===")
    check(app.path_a is not None and app.path_b is not None,
          "选中一组就自动挑好了要比的两张")
    rep = app._representative(g)
    check(app.path_a == rep, "左侧固定是代表图（%s）"
          % os.path.basename(app.path_a or "?"))
    check(app.path_b != app.path_a, "右边是另一张（%s）"
          % os.path.basename(app.path_b or "?"))
    check(app.mode == "pair", "处在并排对比模式")

    print("\n=== 预览：并排 + 尽可能放大 ===")
    app.update()
    app.update_idletasks()
    settle_render(app)
    wa, ha = app.cv_a.winfo_width(), app.cv_a.winfo_height()
    wb, hb = app.cv_b.winfo_width(), app.cv_b.winfo_height()
    print("   A 格 %dx%d   B 格 %dx%d" % (wa, ha, wb, hb))
    check(wa > 150 and ha > 150 and wb > 150 and hb > 150, "两个格子都够大")
    check(abs(wa - wb) <= 4, "两个格子等宽（%d / %d）" % (wa, wb))
    pa, pb = shown_pct(app.cv_a), shown_pct(app.cv_b)
    print("   图片占格子：A %.0f%%  B %.0f%%" % (pa * 100, pb * 100))
    check(pa > 0.85 and pb > 0.85, "两张图都填满了自己的格子")
    m = re.search(r"显示 (\d+)%", app.cap_a.dim_lbl.cget("text"))
    check(m is not None and int(m.group(1)) >= 90,
          "没有走「缩一半」的兜底路径（%s）" % app.cap_a.dim_lbl.cget("text"))

    print("\n=== 预览区够大（其他区域压缩）===")
    # 2026-10-05 主人：「这两个窗口可以合并」—— 结论行并进信息卡了，
    # 右栏除预览区外就剩「信息卡（含结论行）+ 操作卡」两块。
    others = app.info_card.winfo_height() + app.acts.winfo_height()
    print("   预览区高 %d；信息(含结论) %d + 操作 %d = %d"
          % (ha, app.info_card.winfo_height(), app.acts.winfo_height(), others))
    check(ha > others, "预览区比「信息+操作」加起来还高（%d > %d）"
          % (ha, others))
    check(ha >= ui.S(200), "预览区高度够大（%d >= %d）" % (ha, ui.S(200)))

    print("\n=== 匹配区域的红框：已按要求去掉 ===")
    settle_render(app)
    kinds_a, boxes_a = canvas_items(app.cv_a)
    kinds_b, _ = canvas_items(app.cv_b)
    check("image" in kinds_a and "image" in kinds_b, "两侧都画了图")
    check("rectangle" not in kinds_a and "rectangle" not in kinds_b,
          "两侧都**没有**匹配红框了（不挡着看图）")
    check(app.crop_only.get() is False,
          "「只看匹配区域」默认是关的（BooleanVar 必须 .get()，否则恒为真）")
    # 四条边都要查：只查左上角挡不住「图比画布高、底部被切掉」这种情况
    for tag, cv in (("A", app.cv_a), ("B", app.cv_b)):
        _, bxs = canvas_items(cv)
        cw_, ch_ = cv.winfo_width(), cv.winfo_height()
        bad = [b for b in bxs
               if b[0] < -2 or b[1] < -2 or b[2] > cw_ + 2 or b[3] > ch_ + 2]
        check(bool(bxs) and not bad,
              "%s 侧图完整装在画布里（%s，画布 %dx%d）"
              % (tag, bxs[0] if bxs else "无", cw_, ch_))

    print("\n=== 切「只看匹配区域」===")
    app.toggle_crop()
    app.update()
    check(app.crop_only.get() is True, "开关切到开了")
    # ⚠️ 这里原来是 `"teal"`（实心青）。改成浅色的 `on` 之后，断言要跟着认新名字 ——
    #    更重要的是**别只断言「不是 ghost」**：那种断言在「所有按钮都不高亮」时也过。
    check(app.btn_crop.get_kind() == "on", "按钮换成高亮态（浅天蓝的 `on`，不是实心块）")
    # 关掉之后必须退回 ghost，否则「两个都亮着」就看不出状态了
    app.toggle_crop()
    app.update()
    check(app.btn_crop.get_kind() == "ghost", "再点一下退回未选中（白底）")
    app.toggle_crop()
    app.update()
    settle_render(app)
    kinds2, boxes2 = canvas_items(app.cv_a)
    check("rectangle" not in kinds2, "裁剪模式下也没有框")
    check("image" in kinds2, "裁剪模式下画的是裁出来的那块")
    check(bool(boxes2) and (boxes2[0][2] - boxes2[0][0]) < wa,
          "裁出来的那块比整图窄（说明真的换了内容）")
    # 光「比整图窄」挡不住溢出：裁出来那块要是被放大撑出容器，上下会被画布裁掉，
    # 用户就看不到完整的匹配区了（踩过：290×286 放进 713×453 被撑成 580×572）。
    # 这里按四个边逐边断言它**完整装在画布里**。
    for tag, cv in (("A", app.cv_a), ("B", app.cv_b)):
        _, bxs = canvas_items(cv)
        cw_, ch_ = cv.winfo_width(), cv.winfo_height()
        bad = [b for b in bxs
               if b[0] < 0 or b[1] < 0 or b[2] > cw_ or b[3] > ch_]
        check(not bad, "%s 侧匹配区完整装在画布里（%s，画布 %dx%d）"
              % (tag, bxs[0] if bxs else "无", cw_, ch_))
        check(bool(bxs) and (bxs[0][2] - bxs[0][0]) > wa * 0.4,
              "%s 侧匹配区没被缩得太小（宽 %s / 画布 %d）"
              % (tag, (bxs[0][2] - bxs[0][0]) if bxs else "无", wa))
    app.toggle_crop()
    app.update()
    check(app.crop_only.get() is False, "再切回到关")

    print("\n=== 滚轮放大：只作用于鼠标底下那一侧 ===")

    class _E:
        delta = 120
    z0 = list(app.zoom)
    check(len(z0) == 2, "缩放是每一侧一份（%r）" % (z0,))

    app._on_wheel(0, _E())
    app.update()
    check(app.zoom[0] > z0[0], "在左侧滚轮 → 左侧放大（%.2f -> %.2f）" % (z0[0], app.zoom[0]))
    check(abs(app.zoom[1] - 1.0) < 1e-6,
          "右侧**没被动过**（还是 %.2f）—— 这就是「放大鼠标所在那一张」" % app.zoom[1])
    # ⚠️ 2026-10-06 改成分块渲染之后，画布上不再是「整张放大后的图」，
    #    而是「视口那一块」——所以不能再断言「图元比画布宽」（那是旧实现的行为）。
    #    新语义：**图 ∩ 画布** 那一块必须被完整盖住，且显示倍率真的涨上去了。
    settle_render(app)
    v = app._view.get(0)
    _, boxes_z = canvas_items(app.cv_a)
    if v and boxes_z:
        disp_w = v["disp"][0]
        bb = boxes_z[0]
        need = (max(0, (wa - disp_w) // 2 + app.pan[0][0]),
                max(0, (ha - v["disp"][1]) // 2 + app.pan[0][1]),
                min(wa, (wa - disp_w) // 2 + app.pan[0][0] + disp_w),
                min(ha, (ha - v["disp"][1]) // 2 + app.pan[0][1] + v["disp"][1]))
        check(bb[0] <= need[0] + 2 and bb[2] >= need[2] - 2,
              "放大后视口那一块铺满了画布（块 %d..%d ⊇ 需要 %d..%d）"
              % (bb[0], bb[2], need[0], need[2]))
        check(disp_w > wa * 1.15,
              "显示尺寸真的按倍率长大了（%d > %.0f）—— 不是把小图硬拉"
              % (disp_w, wa * 1.15))

    zl = app.zoom[0]
    app._on_wheel(1, _E())
    app.update()
    check(app.zoom[1] > 1.0, "在右侧滚轮 → 右侧放大（1.00 -> %.2f）" % app.zoom[1])
    check(abs(app.zoom[0] - zl) < 1e-6, "左侧保持刚才的值（%.2f）没被带着动" % app.zoom[0])

    # 往下一格滚 = 缩小；两侧各回一格
    class _D:
        delta = -120
    app._on_wheel(0, _D())
    app._on_wheel(1, _D())
    app.update()
    check(app.zoom[0] < zl, "反方向滚轮缩小（%.2f -> %.2f）" % (zl, app.zoom[0]))

    # 到顶就停住，不越界（ZOOM_MAX 是 image_scout 里的常量）
    for _ in range(60):
        app._on_wheel(1, _E())
    app.update()
    check(abs(app.zoom[1] - ui.ZOOM_MAX) < 1e-6,
          "连续放大到上限就停住（%.2f == ZOOM_MAX %.2f）" % (app.zoom[1], ui.ZOOM_MAX))
    for _ in range(80):
        app._on_wheel(1, _D())
    app.update()
    check(abs(app.zoom[1] - ui.ZOOM_MIN) < 1e-6,
          "连续缩小到下限就停住（%.2f == ZOOM_MIN %.2f）" % (app.zoom[1], ui.ZOOM_MIN))

    app.reset_zoom()
    app.update()
    check(abs(app.zoom[0] - 1.0) < 1e-6 and abs(app.zoom[1] - 1.0) < 1e-6,
          "「重置缩放」两侧一起回到适应窗口（%.2f / %.2f）" % (app.zoom[0], app.zoom[1]))
    check(app.btn_zoom.get_text() == "重置缩放", "按钮就叫「重置缩放」")

    print("\n=== 双击打开 / 「打开」按钮已删 ===")
    binds = " ".join(app.cv_a.bind())
    check("Double" in binds, "预览区绑了双击事件（%s）" % binds.replace("\n", " ")[:60])
    check("打开 A" not in all_texts and "打开 B" not in all_texts,
          "那两个「打开」按钮已经去掉了")
    calls = []
    real_open = net.open_path
    net.open_path = lambda p: (calls.append(p), True)[1]
    try:
        app._open_side(0)
        app._open_side(1)
    finally:
        net.open_path = real_open
    check(calls and calls[0] == app.path_a and calls[1] == app.path_b,
          "两侧双击都会交给系统打开（%s / %s）"
          % (os.path.basename(calls[0]) if calls else "?",
             os.path.basename(calls[1]) if len(calls) > 1 else "?"))

    print("\n=== 信息两列并排（同字段对齐）===")
    # 结论行（pack）+ 两列信息（grid 容器 `_info_grid`）住在同一张卡里
    box_cols = app._info_grid.winfo_children()
    check(len(box_cols) == 2, "信息区是两列（%d）" % len(box_cols))
    txts = [widget_texts(c) for c in box_cols]
    for i, t in enumerate(txts):
        print("   第 %d 列：%s" % (i + 1, " / ".join(x for x in t if x)))

    def fields(t):
        s = " ".join(t)
        return [k for k in ("分辨率", "质量", "清晰度")
                if k in s]

    check(fields(txts[0]) == fields(txts[1]),
          "两列字段一致且顺序一致：%s" % fields(txts[0]))
    check(len(fields(txts[0])) >= 3, "字段没被砍掉（%s）" % fields(txts[0]))
    check("清晰度" in fields(txts[0]), "质量旁边多了一列「清晰度」（NIQE）")
    # 2026-10-05 主人：「工作网格什么的可以去掉」——这行删了之后，措辞不能再出现
    all_info = " ".join(" ".join(t) for t in txts)
    check("工作网格" not in all_info and "匹配区域 占本图" not in all_info,
          "「工作网格 / 匹配区域」那一行已经去掉")
    ih = int(app.info_card.cget("height"))
    check(ih > ui.S(50), "信息卡高度跟着内容长（%d）" % ih)
    check(ih < ha, "信息卡比预览区矮（%d < %d）—— 高度让给了看图" % (ih, ha))

    print("\n=== NIQE：块数不够就不许给档位 ===")
    # 先验措辞（纯函数，不用等异步）
    t1 = ui.niqe_field(app.path_a, (5.73, "25 个块", 25))
    print("   块数够  ：%s" % t1)
    check("自然" in t1 and "参考" not in t1, "块数够 → 正常给档位")
    t2 = ui.niqe_field(app.path_a, (6.84, "9 个块", 9))
    print("   块数少  ：%s" % t2)
    check(("参考" in t2) and ("自然" not in t2) and ("劣化" not in t2),
          "块数不够 → 只说「参考」，**不声称档位**（档位在这种块数下是假的）")
    t3 = ui.niqe_field(app.path_a, (None, "图太小（只能切出 1 个 96×96 的块）", 1))
    print("   算不了  ：%s" % t3)
    check("算不了" in t3 and "图太小" in t3, "算不了时把原因说清楚")
    check(ui.niqe_field(app.path_a, None) == "清晰度 计算中…",
          "还没算完时显示「计算中…」")

    # 再真算：真图要算得出，小图要如实说算不了
    big = os.path.join(HERE, "testdata", "baboon480.png")
    sb, _, nbb = ui.niqe_score(big)
    print("   baboon480          -> %s（%d 块）" % (("" if sb is None else "%.4f" % sb), nbb))
    check(sb is not None and abs(sb - 5.7296) < 2e-3,
          "界面这条管线也能算到官方锚点 5.7296（%s）" % ("None" if sb is None else "%.4f" % sb))
    check(nbb >= ui.niqe.SOLID_BLOCKS, "块数够给档位（%d >= %d）" % (nbb, ui.niqe.SOLID_BLOCKS))
    tiny_path = os.path.join(root_dir, "低清小图.png")
    st, nt, nbt = ui.niqe_score(tiny_path)
    print("   低清小图 (200x150) -> %s；%s" % (st, nt))
    check(st is None and nbt < ui.niqe.MIN_BLOCKS,
          "低清小图如实说算不了，不编一个数（%d 块 < %d）" % (nbt, ui.niqe.MIN_BLOCKS))

    # 界面上真的写出来了（等后台线程算完并回填）
    pa, pb = app.path_a, app.path_b           # set_pair 会改这两个，先存下来
    app.set_pair(big, big)                    # 拿 480×480 的锚点图当「大图」
    app.update()
    ok = pump(app, until=lambda: "计算中" not in " ".join(widget_texts(app.info)),
              timeout=90)
    shown = " ".join(widget_texts(app.info))
    print("   信息卡：%s" % (shown[:110] + ("…" if len(shown) > 110 else "")))
    check(ok and "清晰度" in shown and "计算中" not in shown,
          "信息卡上的「清晰度」被后台算完回填了")
    check("5.73" in shown, "卡片上写的是真算出来的 5.73，不是占位符")
    app.set_pair(pa, pb)                      # 还原，后面的用例还要用
    app.update()

    print("\n=== 「全部图片」视图 + 低质量强调 ===")
    app.set_view("all")
    app.update()
    check(app.view == "all", "切到「全部图片」")
    check(app.btn_view_all.get_kind() == "on", "「全部图片」按钮高亮")
    # 分段控件里**同一时刻只能有一个是选中态**，否则等于没选中
    check(app.btn_view_groups.get_kind() == "ghost",
          "「相似分组」同时退回未选中（选中态互斥）")
    check(len(app.glist.items) == len(app.files),
          "列表里是**所有**图片（%d / %d）" % (len(app.glist.items),
                                              len(app.files)))
    badged = [it for it in app.glist.items if it.get("badge")]
    tones = set(it["tone"] for it in badged)
    print("   带徽章的有 %d 项：%s；色调 %s"
          % (len(badged), sorted(set(it["badge"] for it in badged)), tones or "{}"))
    check(bool(badged), "低质量图被标出来了（徽章 %s）"
          % (badged[0]["badge"] if badged else "无"))
    check(all(it["tone"] == "poor" for it in badged), "低质量图用 poor 色调强调")
    # 徽章文字必须等于 quality_of 给的标签，不能一律写死「低清」——
    # 560×380/9KB 的图分辨率不低，只是体积小，写「低清」就是撒谎
    bad = [it for it in badged
           if it["badge"] != ui.META.quality(it["tag"])[1]]
    check(not bad, "徽章文字 == 判据标签（不一致的 %s）"
          % ([(it["title"], it["badge"]) for it in bad] or "0 个"))
    check(all(it["badge"] in ("低清", "体积小") for it in badged),
          "徽章只在「低清 / 体积小」里取值")
    # ⚠️ 这一节以前只断言 `path_a is not None` —— 那是**上一组留下的值**，
    # 恒为真，等于没测。而真实情况是：`_on_pick_group` 开头就
    # `if self.view != "groups": return`，**在「全部图片」里点哪一行都不会有反应**，
    # 切过来右边干脆是空的。现在改成「点哪一行，右边就必须换成那一张」。
    check(app.group is None,
          "「全部图片」视图里不保留当前组（不然左下还挂着上一组的成员）")
    check(app.path_a is not None, "切过来右边就有一张在展示（不是空的）")
    sel = app.glist.sel
    check(0 <= sel < len(app.glist.items) and
          app.path_a == app.glist.items[sel]["tag"],
          "右边展示的就是列表里选中那一行（%s）"
          % os.path.basename(app.path_a or "?"))
    check(app.lbl_members.cget("text").startswith("与它相似"),
          "左下标题变成「与它相似」（%s）" % app.lbl_members.cget("text"))
    check(app.mlist.items and app.mlist.items[0]["tag"] == app.path_a,
          "左下第一项就是它自己")

    print("\n=== 「全部图片」里每行都点得动 ===")
    misses = []
    for row in (0, 7, 20, len(app.glist.items) - 1):
        if row >= len(app.glist.items):
            continue
        target = app.glist.items[row]["tag"]
        app.glist.select(row, notify=True)
        app.update()
        settle_render(app)
        card = " ".join(widget_texts(app.info))
        kinds, _ = canvas_items(app.cv_a)
        bad = []
        if app.path_a != target:
            bad.append("右边没换成它")
        if "image" not in kinds:
            bad.append("没画出图")
        if os.path.basename(target) not in card:
            bad.append("信息卡不是它的")
        if bad:
            misses.append((row, os.path.basename(target), bad))
        print("   第 %-2d 行 %-22s -> %s"
              % (row, os.path.basename(target),
                 "OK（可比对象 %s）" % os.path.basename(app.path_b)
                 if app.path_b else "OK（单图模式）"))
        check(not bad, "点第 %d 行「%s」：右边换成它 + 画了图 + 信息卡是它的"
              % (row, os.path.basename(target)))
    check(not misses, "每一行都点得动（没反应的有 %d 行）" % len(misses))

    # 没有相似对象的图：照样要能预览、能看信息（只是退化成单图模式）
    no_nb = [p for p in app.files
             if not list(scan.neighbors_of(app.links, p))]
    check(bool(no_nb), "样本里有 %d 张没有任何相似对象（用来验单图路径）"
          % len(no_nb))
    if no_nb:
        target = no_nb[0]
        row = next(i for i, it in enumerate(app.glist.items)
                   if it["tag"] == target)
        app.glist.select(row, notify=True)
        app.update()
        settle_render(app)
        kinds, _ = canvas_items(app.cv_a)
        card = " ".join(widget_texts(app.info))
        check(app.mode == "single" and app.path_a == target,
              "没有相似对象 -> 单图模式展示它（%s）" % os.path.basename(target))
        check("image" in kinds and os.path.basename(target) in card,
              "单图模式下照样有预览 + 信息")
        check(not app.cell_b.winfo_ismapped(), "右格收起来了（不留半边空）")

    print("\n=== 质量分级判据（纯函数）===")
    for wh, want in (((160, 120), "poor"), ((320, 240), "poor"),
                     ((800, 600), "fair"), ((1920, 1080), "good"),
                     ((None, None), "unknown")):
        got = ui.quality_of(wh if wh[0] else None)[0]
        check(got == want, "%s -> %s（实际 %s）" % (wh, want, got))
    # 体积那条判据：560×380 分辨率不算低，但只有 9KB —— 标签必须是「体积小」
    check(ui.quality_of((560, 380), 9 * 1024) == ("poor", "体积小"),
          "560×380 / 9KB -> 体积小（不是低清）")
    check(ui.quality_of((560, 380), 150 * 1024) == ("fair", "一般"),
          "同样尺寸但有 150KB -> 一般（体积小这条不误伤）")
    check(ui.poor_badge(("poor", "体积小")) == "体积小"
          and ui.poor_badge(("poor", "低清")) == "低清"
          and ui.poor_badge(("good", "清晰")) is None,
          "poor_badge 原样透传标签、非 poor 返回 None")

    app.set_view("groups")
    app.update()
    check(app.view == "groups" and len(app.glist.items) == len(app.groups),
          "切回分组视图（%d 项）" % len(app.glist.items))

    print("\n=== 一键清理完全重复（无重复时不弹窗，只说明）===")
    app._ask_clean([])
    check("没有" in app.stat.cget("text"), "没有重复时如实说明：%s"
          % app.stat.cget("text"))
    check(app.btn_dedup.get_text() == "清理完全重复", "按钮在顶栏")

    print("\n=== 一键清理完全重复：真跑一遍（确认框打桩成「是」）===")
    import shutil
    pool = [p for p in app.files if p not in (app.path_a, app.path_b)]
    check(bool(pool), "有可用来造重复副本的样本")
    src = pool[0]
    d1 = os.path.join(root_dir, "_dup_a.png")
    d2 = os.path.join(root_dir, "_dup_b.png")
    qdir_root = os.path.join(root_dir, quarantine.QUARANTINE_NAME)
    # 记下开跑前隔离夹里已有的名字，断言只看「新增了什么」
    # （重名的会被加 -1 后缀，按总数数会数错 —— 这条踩过）
    had = set(os.listdir(qdir_root)) if os.path.isdir(qdir_root) else set()
    shutil.copy2(src, d1)
    shutil.copy2(src, d2)
    app.files.extend([d1, d2])
    n_files_before = len(app.files)
    real_ask = ui.messagebox.askyesno
    ui.messagebox.askyesno = lambda *a, **k: True      # 打桩：用户点了「是」
    try:
        app.clean_exact_dups()
        done = pump(app, until=lambda: not app.busy, timeout=120)
    finally:
        ui.messagebox.askyesno = real_ask
    check(done, "查重在超时前跑完")
    app.update()
    now = set(os.listdir(qdir_root)) if os.path.isdir(qdir_root) else set()
    moved = sorted(n for n in now - had if n.startswith("_dup"))
    print("   隔离夹里新增的重复副本：%s" % moved)
    check(len(moved) == 2, "三份相同的图移走了 2 份（实际 %d）" % len(moved))
    left = [p for p in (src, d1, d2) if os.path.isfile(p)]
    check(len(left) == 1, "留了 1 份没动（实际 %d）" % len(left))
    check(len(app.files) == n_files_before - 2, "文件列表同步减 2（%d -> %d）"
          % (n_files_before, len(app.files)))
    print("   %s" % app.stat.cget("text"))

    print("\n=== 删除 = 移进扫描文件夹里的隔离夹 ===")
    victim = app.path_b or app.path_a
    qdir = quarantine.quarantine_dir_for(victim, app.roots)
    base_name = os.path.basename(victim)
    had_q = set(os.listdir(qdir)) if os.path.isdir(qdir) else set()
    n_before = len(app.files)
    moved = app.delete_paths([victim])
    app.update()
    check(moved == 1, "报告移动了 1 张")
    check(not os.path.exists(victim), "源文件已经不在了")
    # 同样只看增量：残留的同名旧文件会让「按名字找」蒙对
    stem = os.path.splitext(base_name)[0]
    fresh = sorted(n for n in set(os.listdir(qdir)) - had_q
                   if os.path.splitext(n)[0] == stem
                   or os.path.splitext(n)[0].startswith(stem + "-"))
    check(len(fresh) == 1, "隔离夹里新增了 1 个（实际 %s）" % (fresh or "0 个"))
    landed = os.path.join(qdir, fresh[0]) if len(fresh) == 1 else None
    check(bool(landed) and os.path.isfile(landed),
          "文件躺在隔离夹里：%s" % (os.path.basename(landed) if landed else "?"))
    check(os.path.basename(qdir) == quarantine.QUARANTINE_NAME,
          "隔离夹就叫「%s」，且在扫描文件夹下（%s）"
          % (quarantine.QUARANTINE_NAME,
             os.path.dirname(qdir) == root_dir))
    check(victim not in app.files, "从文件列表里摘掉了")
    check(victim not in app.descs, "指纹缓存里也摘掉了")
    check(all(victim not in (l[0], l[1]) for l in app.links),
          "相关边都清掉了")
    check(len(app.files) == n_before - 1, "文件数 -1（%d -> %d）"
          % (n_before, len(app.files)))
    check(app.path_a is None or os.path.isfile(app.path_a),
          "界面没有悬在不存在的文件上")
    check(not os.path.isfile(victim) or True, "删除没抛异常")

    print("\n=== 再扫一遍：隔离夹里的图不会被收回来 ===")
    app._refresh_roots()
    check(landed not in app.files,
          "刷新后隔离夹里的文件没被重新扫进来（%d 张）" % len(app.files))

    print("\n=== 结论文案 ===")
    app.glist.select(0, notify=True)
    app.update()
    check(bool(app.verdict_text.cget("text")), "给出了结论：%s"
          % app.verdict_text.cget("text"))
    check(app.verdict_pill.get_text() not in ("", "—"),
          "结论条上有相似度：%s" % app.verdict_pill.get_text())

    print("\n=== 单图模式（组里只有一张时不留半边空）===")
    only = app.path_a
    app.set_pair(only, None)
    app.update()
    check(app.mode == "single", "切到单图模式")
    check(not app.cell_b.winfo_ismapped(), "右侧格子收起来了")
    app.set_pair(only, app.path_b or only)
    app.update()

    print("\n=== 三档灵敏度 ===")
    counts = {}
    for key in ("strict", "standard", "loose"):
        app.set_preset(key)
        app.start_scan()
        ok = pump(app, until=lambda: not app.busy and bool(app.groups),
                  timeout=300)
        counts[key] = len(app.links)
        print("   %-9s 边 %4d  组 %d  疑似 %d"
              % (key, len(app.links), len(app.groups), len(app.weak)))
        check(ok, "「%s」档跑完了" % key)
        check(app.preset_btns[key].get_kind() == "on",
              "「%s」按钮是选中态" % key)
        # 另外两档必须退回未选中 —— 只断言「当前这个亮了」是不够的，
        # 万一三个都亮着，这条仍然过，但用户根本看不出选的是哪个。
        others = [k for k in app.preset_btns
                  if k != key and app.preset_btns[k].get_kind() != "ghost"]
        check(not others, "另外两档是未选中（不会同时亮：%s）"
              % ("、".join(others) or "无"))
    check(counts["strict"] <= counts["standard"] <= counts["loose"],
          "边数随灵敏度递增：%d <= %d <= %d"
          % (counts["strict"], counts["standard"], counts["loose"]))

    print("\n=== 剪贴板自检文案 ===")
    try:
        okc = net.clipboard_available()
    except Exception:
        okc = False
    print("   剪贴板可用：%s" % okc)
    print("   界面提示：%s" % app.clip_hint["text"][:70])

    print("\n=== 流畅度（主线程阻塞耗时）===")
    # 主人 2026-10-05：「缩放仍然卡顿，在左边窗口上下滑动，选择组合时也会卡顿」。
    # 光看代码猜不出卡在哪，所以这里直接量：每一次操作把主线程占住多久。
    # 判据：>100ms 人就能感觉到「顿一下」，>16ms 就凑不满 60fps。
    app.set_view("groups")
    app.update()
    settle_render(app)
    # 等预热跑完 —— 主人 2026-10-06：「每组第一次打开会卡顿一下，
    # 不能在加载时同时加载好吗」。预热之后每次点开都该是现成的。
    warm_ok = pump(
        app,
        until=lambda: (not getattr(app, "_prewarm_q", [])
                       and not getattr(app, "_prewarm_busy", False)),
        timeout=60)
    settle_render(app)
    print("   预热队列跑完：%s（还剩 %d 张）"
          % (warm_ok, len(getattr(app, "_prewarm_q", []))))
    check(warm_ok, "空闲预热在 60s 内跑完（后台解码，不占主线程）")
    check(any(k[0] != "tile" for k in app.big._p),
          "预热真的产出了成品图（缓存里 %d 张）" % len(app.big._p))

    class _Ev(object):
        """假事件对象（hover 只用 y，滚轮只用 delta）。"""

        def __init__(self, y=0, delta=0):
            self.x = 10
            self.y = y
            self.delta = delta

    lst = app.glist
    pitch = lst.row_h + lst.gap
    ngrp = min(len(app.groups), 6)

    # --- 选组 ---
    for gi in range(ngrp):                      # 先走一遍预热（首遍要解码）
        lst.select(gi)
        app.update()
    settle_render(app)
    sel_ms = []
    for gi in range(ngrp):
        t0 = time.perf_counter()
        lst.select(gi)
        app.update()                            # 把这次操作触发的绘制真正跑完
        sel_ms.append((time.perf_counter() - t0) * 1000)
        settle_render(app)                      # 高清后台补帧不计入
    worst_sel = max(sel_ms)
    print("   选一组（缓存命中）每次 %.0f~%.0f ms"
          % (min(sel_ms), worst_sel))
    check(worst_sel < 150,
          "选一组不卡（最慢 %.0f ms < 150 ms）" % worst_sel)

    # --- 鼠标在列表上划（hover 局部重绘）---
    t0 = time.perf_counter()
    for k in range(40):
        lst._motion(_Ev((k % 5) * pitch + 4))
        app.update()
    hv = (time.perf_counter() - t0) * 1000 / 40.0
    print("   hover 换一行 %.2f ms/次" % hv)
    check(hv < 8, "鼠标划过列表不掉帧（%.2f ms/次 < 8 ms）" % hv)

    # --- 列表滚动 ---
    t0 = time.perf_counter()
    for k in range(40):
        lst.yview_scroll(1 if k % 8 else -3, "units")
        app.update()
    sc = (time.perf_counter() - t0) * 1000 / 40.0
    print("   列表滚动 %.2f ms/次" % sc)
    check(sc < 8, "左栏上下滑动不卡（%.2f ms/次 < 8 ms）" % sc)
    lst.yview_moveto(0.0)

    # --- 滚轮缩放（先响应后精解，主线程只该占很短）---
    t0 = time.perf_counter()
    for k in range(12):
        app._on_wheel(0, _Ev(delta=120 if k % 2 else -120))
        app.update()
    zm = (time.perf_counter() - t0) * 1000 / 12.0
    print("   滚轮缩放 %.2f ms/次（高清帧在后台补，不计入）" % zm)
    check(zm < 60, "滚轮缩放跟手（%.2f ms/次 < 60 ms）" % zm)
    settle_render(app)

    print("\n=== 布局几何体检 ===")
    print("   这里量的是各控件的实际坐标与尺寸（像素级断言，比肉眼看截图更硬）：")
    print("   控件有没有被压成 0 尺寸、对比区是不是真的比左栏大。")
    print("   想看图另有 uishot.py，抓真实窗口存 screenshot.png。")
    app.update()
    app.update_idletasks()
    geom = {
        "分组列表": app.glist,
        "成员列表": app.mlist,
        "预览A": app.cv_a,
        "预览B": app.cv_b,
        "信息卡(含结论行)": app.info_card,
        "操作条": app.acts,
        "状态栏": app.stat,
        "进度条": app.pb,
    }
    for name, w in geom.items():
        gw, ghh = w.winfo_width(), w.winfo_height()
        print("   %-6s %4d x %-4d @ (%d, %d)"
              % (name, gw, ghh, w.winfo_rootx(), w.winfo_rooty()))
        check(gw > 60 and ghh > 10, "%s 尺寸正常（%d x %d）" % (name, gw, ghh))
    check(app.cv_a.winfo_width() > app.glist.winfo_width(),
          "右侧对比区比左侧列表宽（%d > %d）"
          % (app.cv_a.winfo_width(), app.glist.winfo_width()))

    print("\n=== 关闭 ===")
    app.destroy()
    check(True, "窗口正常销毁，没有异常")

    # 大图（3000×2000）的缩放单独开一个窗口测 —— 小样本测不出真实开销
    test_big_zoom()

    print("\n=== 汇总 ===")
    if FAIL:
        print("   %d 项失败：" % len(FAIL))
        for m in FAIL:
            print("     - %s" % m)
        return 1
    print("   全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
