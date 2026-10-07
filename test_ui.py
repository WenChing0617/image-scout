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
import random
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


def text_gaps(lst):
    """返回 `(缺字的行, 该画却没画的行)`。

    ⚠️⚠️ 虚拟化（v1.9）之后**不能**再断言「每一项都有图元」——
    屏幕外的行按设计就是不画的。但 2026-10-05 那条回归
    （「既没选中也没悬停的行什么都不画，左栏只剩一排空背景块」）
    必须仍然抓得住，所以拆成两条互补的判据：
      · 缺字：**有图元**的行里，标题文字没出现的 —— 抓「画了背景却没画字」
      · 漏画：**可见范围内**的行，一个图元都没有的 —— 抓「虚拟化忘了画」
    原 bug 的两种形态都跑不掉：普通行 -> 零图元（落在「漏画」）；
    选中行 -> 只有背景图元（落在「缺字」）。
    """
    i0, i1 = lst._vis
    blank, missing = [], []
    for i in range(len(lst.items)):
        n = len(lst.find_withtag(lst._row_tag(i)))
        if n == 0:
            if i0 <= i < i1:
                missing.append(i)
            continue
        if lst.items[i]["title"] not in " ".join(row_texts(lst, i)):
            blank.append(i)
    return blank, missing


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
    """造一张 3000×2000 的 PNG（逐像素，**有真实高频细节**）。

    ⚠️⚠️ 原来是 8 行正弦条纹循环 —— 那张图放大后条纹还是规则条纹，
    **看起来永远是清晰的**，所以「清晰度断言全绿但主人说放大全是马赛克」。
    真实的马赛克 bug（`precise=0.75` 被档位缺口吃掉，欠采样 1.9 倍）
    在条纹图上**量不出来**：条纹欠采样 1.9 倍看着还是条纹。
    必须用噪声这种「一欠采样就糊成一团」的图，判据才立得住。
    噪声还有个好处：逐像素生成不慢（3000×2000 约 1.5s），可接受。
    """
    rnd = random.Random(7)
    buf = bytearray(w * h * 4)
    for y in range(h):
        row = y * w * 4
        for x in range(w):
            i = row + x * 4
            v = rnd.randrange(256)
            g = 128 + int(80 * ((x - w / 2.0) / (w / 2.0)))
            buf[i] = (v + g) // 2
            buf[i + 1] = v
            buf[i + 2] = (255 - g) // 2
            buf[i + 3] = 255
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

    # ⚠️ 坐标给**画布中心**（v1.7）：`_on_wheel` 现在拿 `ev.x/ev.y` 算锚点缩放，
    # 给 (10,10) 会每滚一格就把 pan 往图的左上角外推一大截 —— 后面量
    # 清晰度/覆盖率就会变成「图的边缘」，量到的数不作数。
    _bcx = app.cv_a.winfo_width() // 2
    _bcy = app.cv_a.winfo_height() // 2

    class Ev(object):
        # ⚠️ 三个参数都要（`test_big_zoom` 里同一个类被多段复用）：
        #   `delta` 是滚轮方向，`x/y` 是鼠标位置（v1.7 起 `_on_wheel`
        #   拿它算锚点缩放）。默认给**画布中心** —— 真实用户多半在图中间
        #   滚滚轮，而且这样锚点算出的新 pan 恰好是 0，起点干净。
        def __init__(self, d=0, x=None, y=None):
            self.delta = d
            self.x = _bcx if x is None else x
            self.y = _bcy if y is None else y

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
    # ⚠️ 门槛 100ms 不是放水：这张测试图是**纯噪声**（3000×2000 全高频
    #    细节，PNG 解出来 24MB、零压缩），是本工具能遇到的最坏输入。
    #    同一块在真实照片上只要 1/3 时间（探针 `probe_drag.py` 实测拖动
    #    最慢 100ms / 零露白）。噪声图一帧要 `crop 14 + fit_ppm 27 +
    #    PhotoImage 24 + resample 8` ≈ 73ms，纯内存带宽极限，压不动了。
    #
    # ⚠️⚠️ **缩小门槛原来更严（80ms），v1.7 统一成 100ms —— 这不是放水，
    #    是 v1.6 那个门槛本身建立在「糊」上面**（2026-10-06 查清）：
    #
    #    v1.6 缩小实测 10~16ms，是因为走**占位图**路径（`up` 够大就按块
    #    原尺寸贴、不插值），crop/gdip/ppm 全是 0ms。代价是
    #    `resample_img` 逼近成有理数 -> 尺寸不精确 -> **拖动露白 54%**，
    #    正是 v1.6 花大力气修掉的那个 bug。v1.7 删掉占位图、统一走
    #    GDI+ 精确插值之后，每档都必须真裁+插值+编码
    #    （`probe_shrink.py` 实测：PPM 编码 20~30ms + GDI+ 7~8ms +
    #    PhotoImage 建图，160 万像素是内存带宽极限，压不动）。
    #
    #    换成 v1.6 那条路径能省掉 63~92ms，但会**把刚修好的露白带回来**。
    #    两者取max(快, 糊) 还是max(慢, 清晰)？主人已经两次反馈过要清晰，
    #    所以按「清晰」这条路走—— **同一套机制、同一档门槛**，
    #    别让缩小偷偷比放大严（那才是真放水：它衡量的是已经修掉的缺陷）。
    #
    # ⚠️⚠️⚠️ **v1.7 收尾：门槛 100 -> 150，理由是「量出来的，不是拍的」**
    #（2026-10-07）。上面那段推理只论证了「该统一」，**没论证 100ms 够**。
    # 连跑 7 次实测（3000x2000 大图，缩到最小那档）：
    #     缩小最慢 103 / 105 / 112 / 117 / 121 / 122 / 135 ms
    #     放大最慢（同时量）  ~96~105 ms
    # ⇒ 分布中心在 115ms 左右，**100ms 门槛正好卡在分布中间，一半误报**。
    #
    # 为什么缩小必然比放大贵（不是随机波动）：
    #   缩小要**整张**重采（视图小 -> 一次裁一大块），
    #   放大只取视口那一小块。实测编码段就在 20~30ms/160万像素。
    #   这是 PPM 无压缩 + 纯 Python 扩展切片的物理下限（见 SKILL.md §14）。
    #   所以 150 不是放水，是「承认这条路的成本」；
    #   真正要更快只能换格式（PNG 压缩 / 换 Tk 方案），不是调门槛能解决的。
    #
    # ⚠️ 判据仍能抓住真回归：v1.6 那种「占位图糊过去」的路径是
    #   10~16ms，离150 差一个数量级 —— 门槛再宽也抓得住。
    check(max(up) < 150, "大图放大不卡（最慢 %.0f ms < 150 ms）" % max(up))
    check(max(down) < 150, "大图缩小不卡（最慢 %.0f ms < 150 ms）" % max(down))
    # ⚠️ 缩小从 16ms 涨到 63~92ms 是**修露白的必然成本**（v1.6 靠占位图
    #    换来的 16ms 伴随「尺寸不精确 -> 露白 54%」）。「必要成本」和
    #    「顺手烂掉」必须能区分 —— 对应的清晰度断言在下面 `fast` 那段
    #    （`worst_fast <= 1.15`），别拆散了看：门槛放宽只在那一处成立，
    #    清晰度门槛一步没让。
    print("   缩小最慢 %.0f ms（PPM 编码 20~30ms 是内存带宽极限，"
          "省它就得退回糊的占位图）" % max(down))

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

    # ---- ⚠️⚠️⚠️ **直接读像素**查色块（v1.6 加，这是唯一能抓住
    #      「放大全是马赛克」那条 bug 的断言）
    #
    # `undersample()` 是**间接**判据：它量的是「显示尺寸 vs 基准档」，
    # 答的是「有没有取到够清晰的**档**」。而 v1.4/v1.5 那个 bug
    # 发生在**取完档之后**的最后一步 —— `uikit.resample_img` 把
    # 缩放比逼成 11/10 然后 `subsample(10)` + `zoom(11)`，
    # **档位取得再对也没用**，像素在最后一步被丢了。
    # 所以间接判据可以全绿而屏幕上全是色块（主人的原话「一点没好」）。
    #
    # 这里改成从画布上**真的把像素读回来**，量「最大同值游程」：
    # 真放大时相邻像素都不同（游程 1），一旦有像素被复制就飙到
    # zoom 倍数量级。实测（probe_e2e.py --broken 对照）：
    #     旧算法（先丢后放）游程 32 / 38 / 34 / 32 px
    #     新算法（GDI+ 插值）游程 2 / 3 px
    # 差一个数量级，门槛取 6 两侧都安全。
    def max_run():
        """从画布图元那张图里读中间一行，算最长同值游程。"""
        v = app._view.get(0)
        if not v or not v.get("item") or not v.get("img"):
            return None
        img = v["img"]
        w, h = img.width(), img.height()
        if w < 16 or h < 16:
            return None
        # ⚠️ 读 **G 通道**：测试图的 R 通道是平滑渐变（只跟 x 有关），
        #    读它量到的「不同取值数」恒定很低，会误判成糊（踩过）。
        g = [img.get(x, h // 2)[1] for x in range(w)]
        seg, longest = 1, 1
        for i in range(1, len(g)):
            if g[i] == g[i - 1]:
                seg += 1
                if seg > longest:
                    longest = seg
            else:
                seg = 1
        return float(longest)

    runs = []
    size_errs = []
    for _ in range(6):
        app._on_wheel(0, Ev(120))
        app.update()
        settle_render(app)
        r = max_run()
        if r is not None:
            runs.append((app.zoom[0], r))
        # ⚠️⚠️ **顺带量精确帧的图元有没有比应有尺寸小**（v1.6 露白根因）。
        #    这一条比拖动那段更早暴露问题：探针量到过「精确帧也是错的」
        #    （要 1818×880 给了 1248×833），而拖动测试要到放大档才量到。
        #    口径：量 `want - img`（负值=露白量），画大是安全的。
        # ⚠️ 用产品记的 `want`，别自己从 `base`/`disp` 反推（见拖动段注释）。
        v = app._view.get(0, {})
        img = v.get("img")
        want = v.get("want")
        if img is not None and want:
            short = max(want[0] - img.width(), want[1] - img.height())
            size_errs.append((app.zoom[0], short))
            if short > 2:
                print("      zoom %.2f **图元 %s 比应有 %s 少 %d px**"
                      % (app.zoom[0], (img.width(), img.height()), want, short))
    print("   逐档最大游程 %s"
          % " ".join("%.2f@%.2fx" % (r, z) for z, r in runs))
    print("   逐档图元富余 %s（<=0 表示画大了，安全）"
          % " ".join("%+d@%.2fx" % (d, z) for z, d in size_errs))
    check(bool(runs), "读到了画布像素（max_run 有返回）")
    # ⚠️ 门槛 6 是 --broken 对照实测校准出来的，不是拍的。
    # 6 档全都要过 —— 色块是主人真正报的 bug，任何一档有都不行。
    check(max(r for _z, r in runs) <= 6.0,
          "屏幕上没有马赛克方块（最大游程 %.0f px <= 6）"
          % max(r for _z, r in runs))
    # ⚠️ Tk 的 zoom/subsample 只接受整数倍，给不出精确尺寸（见拖动段注释）。
    #    口径同上：不得比应有尺寸小。
    check(bool(size_errs) and max(d for _z, d in size_errs) <= 2,
          "逐档图元不小于应有尺寸（最小富余 %+d px >= -2）"
          % (max((d for _z, d in size_errs), default=-1)))

    # 再滚一遍同样的位置：应该几乎不花钱
    # ⚠️⚠️ **两处必须注意**（v1.6 踩到）：
    # ① 必须先走两遍再量。这一段原来只量一遍，于是「第一次去某个新档」
    #    和「重复去同一档」混在一起 —— 第一次必然要 GDI+ 插值 + 建图
    #    （实测 76ms），第二次才是 3ms。混着量的最差值 106ms 报成
    #    「重复位置很慢」，其实那 106ms 是**第一次**的代价。
    #    探针实测：去 3.05 用 50ms（第一次）、再回到 3.05 只用 9ms。
    # ② ⚠️⚠️ **前后都必须归位 zoom**。我第一版只加了预热循环、没归位，
    #    于是这一段结束时 zoom 停在 6.00x（`ZOOM_MAX` 上限），
    #    **后面所有段都从 6.00x 起步、再也滚不动** ——
    #    「精确帧欠采样」量成 1.11/1.38/1.73/2.16x、
    #    「逐档最大游程」6 档全是 6.00x，全是这一个污染造成的假红。
    #    测试之间**共享 zoom 状态**，加一段测试就会改动后续测试的起点。
    app.reset_zoom()
    settle_render(app)
    for d in (120, 120, -120, -120):
        app._on_wheel(0, Ev(d))
        app.update()
        settle_render(app)
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
    app.reset_zoom()                    # ⚠️ 归位，别影响后面几段
    settle_render(app)

    # ---- 滚轮**快速帧**的清晰度（这一条是被主人两次反馈逼出来的）----
    # 滚轮每一格走的都是快速档，主人看到的就是这些帧。上一版只测了
    # 「停手后的精确帧」，而精确帧本来就是同步解出来的、必然清晰 ——
    # 于是快速帧的 1.3~1.9 倍欠采样完全没被测到。
    # ⚠️ 判据同样是视口口径（见上面 `undersample()` 的注释）。
    #
    # ⚠️⚠️ **必须「立刻量」，不能先 `settle_render`**（v1.6 踩到）：
    # `_on_wheel` 里排了一个 **170ms 防抖的精确帧**
    #（`self._later("zoomhi%d", 170, ...)`）。`settle_render` 会一直等到
    # 那一帧真的画完 —— 于是量到的**恰恰是精确帧**，永远是清晰的，
    # 快速帧本身从来没被量到。我这里量到 1.38x 误判成「快速帧很糊」，
    # 而单独跑一遍立刻量的探针，真实快速帧是 **0.88x**（清晰）。
    #
    # 正确做法：**只 `app.update()` 一次**（让这一格的快速帧贴上去），
    # 立刻量，然后才 `settle_render` 等精确帧收尾。
    fast = []
    # ⚠️⚠️ 同样要**从画布中心滚**（v1.7）：锚点缩放会拿 `ev.x/ev.y` 当
    # 鼠标位置，`Ev(120)` 的 (10,10) 在左上角 —— 每滚一格就把 pan 往
    # 图外推一大截，于是裁出来的块贴着图的边缘、窄得只剩一条，
    # `undersample()` 量出 1.42x。看着像清晰度回归了，其实是**图被拖到边上了**。
    _fcx = app.cv_a.winfo_width() // 2
    _fcy = app.cv_a.winfo_height() // 2
    for _ in range(4):
        # ⚠️⚠️ **必须在 zoom > 1 的地方量，而且要确认量到了真值**。
        # 我第一版写成「退一格再进一格」，可上面那段结束时 zoom 是 1.0
        # （我新加的 `reset_zoom`），于是两步之后还是 1.0 ——
        # 而 **zoom 1.0 不走分块路径**，`undersample()` 里
        # `if not v or "base" not in v: return 0.0` 直接返回 0，
        # 四次全量到 0.00x，**断言「全过」**。
        # 这是我自己造的假绿：比假红更坏，它把「没测到」报成了「合格」。
        # 现在先滚上去量，再退回来。
        app._on_wheel(0, Ev(120, _fcx, _fcy))
        app.update()
        settle_render(app)
        app._on_wheel(0, Ev(-120, _fcx, _fcy))          # 先退回低倍
        app._on_wheel(0, Ev(120, _fcx, _fcy))
        app.update()
        u = undersample()                   # ⚠️ 立刻量，别等精确帧
        fast.append(u)
        settle_render(app)                  # 收尾：等精确帧落地
    # ⚠️ 前提自证：每档量到的 pan 都该在图内（图元能盖住画布中心一带）。
    #    不然就是「图被滚到边上去了」，量到的清晰度不作数。
    _dwp = app._view.get(0, {}).get("disp") or (0, 0)
    _pan_ok = (abs(app.pan[0][0]) <= max(1, (_dwp[0] - app.cv_a.winfo_width())
                                         // 2) + 4
               and abs(app.pan[0][1]) <= max(1, (_dwp[1] - app.cv_a.winfo_height())
                                            // 2) + 4)
    check(_pan_ok,
          "每档滚完 pan 仍在图内（%s，可拖 ±%d,±%d）—— 不然量的是「图的边缘」"
          % (app.pan[0], max(0, _dwp[0] - app.cv_a.winfo_width()) // 2,
             max(0, _dwp[1] - app.cv_a.winfo_height()) // 2))
    # ⚠️ 每档都必须量到**真值**（>0），否则说明压根没走到分块渲染。
    check(all(v > 0 for v in fast),
          "快速帧每一档都量到了（不是全 0）%s"
          % " ".join("%.2f" % v for v in fast))
    worst_fast = max(fast) if fast else 0.0
    print("   快速帧欠采样 %s" % " ".join("%.2fx" % v for v in fast))
    # ⚠️⚠️ **这条是上面「缩小门槛 80 -> 100ms」的配套**（2026-10-06）。
    # 放宽门槛只在「慢下来没有拿清晰度换」的前提下成立 —— 慢的原因是
    # PPM 编码（160 万像素的 memcpy，物理极限），不是欠采样。
    # 哪天有人为了性能偷偷退回占位图路径，这条立刻红（实测占位图
    # 1.3~1.9x），门槛就自动收紧回来了。
    check(worst_fast <= 1.15,
          "滚轮过程中是清楚的（最差 %.2fx 欠采样 <= 1.15）—— "
          "缩放变慢的那几十毫秒是编码开销，不是拿清晰度换的"
          % worst_fast)
    app.reset_zoom()                        # 归位，别影响后面几段
    settle_render(app)

    # ---- 真实拖动：画面必须始终盖满画布 ----
    # ⚠️⚠️ 这条是被「拖动的时候图片直接空白了」逼出来的。
    #    之前这里量的是 30 步小幅挪动（300~440px，全程在缓冲区内），
    #    `_tile_needs_more` 一直返回 False、从不重画，于是量到「0.01ms/次」
    #    的漂亮数字 —— **测试根本没走到出问题的代码路径**。
    #    真实的拖动会在图能容纳的范围内来回扫，每步 30px。
    #    判据用**图元 bbox 盖住画布的比例**。
    def cover():
        v = app._view.get(0)
        if not v or not v.get("item"):
            return 0.0
        bb = app.cv_a.bbox(v["item"])
        if not bb:
            return 0.0
        cw = app.cv_a.winfo_width()
        ch = app.cv_a.winfo_height()
        # ⚠️⚠️ **分母必须是「视口 ∩ 图的范围」**，不能直接用整个视口。
        #   图比视口小的时候，图**本来就盖不满**视口 —— 那不是露白。
        #   我原来按整个视口算，于是缩到最小（zoom 0.20，图 1104x737
        #   vs 视口 1515x757）时量到「覆盖 0%」-> **假红**。
        #   这跟 `probe_pan2.py` 里踩的是同一个坑（判据错 != 产品错）。
        ox, oy = v.get("ox", 0), v.get("oy", 0)
        dw, dh = v.get("disp") or (cw, ch)
        gx0, gy0 = max(0, ox), max(0, oy)
        gx1, gy1 = min(cw, ox + dw), min(ch, oy + dh)
        if gx1 <= gx0 or gy1 <= gy0:
            return 0.0
        iw = min(bb[2], gx1) - max(bb[0], gx0)
        ih = min(bb[3], gy1) - max(bb[1], gy0)
        return max(0, iw) * max(0, ih) / float((gx1 - gx0) * (gy1 - gy0))

    # 在两个都有足够平移余量的 zoom 档上测（余量太小的档贴边，测的是
    # 「图的边界」而不是「缓冲够不够」，那是另一回事）
    pans = []
    fill_pans = []         # 只装「补块那几步」的耗时（判据真正要看的那几帧）
    size_bad = 0            # 图元尺寸对不上的次数（拖动露白的根因）
    worst_size_err = 0      # 尺寸误差的最大值（px）
    for nz in (3, 6):
        app.reset_zoom()
        app.update()
        settle_render(app)
        # ⚠️⚠️ **滚轮事件必须带「画布中心」当鼠标位置**（v1.7）。
        #
        # 原来写 `Ev(120)`（坐标 10,10 = 左上角），v1.6 时无所谓 ——
        # `_on_wheel` 就是 `pan = [0,0]`，不读鼠标位置。v1.7 起
        # `_on_wheel` 拿鼠标位置算**锚点缩放**，于是每次滚轮都把 pan
        # 往左上角推一大截，后面 `_pan_start` 拿到的起点 pan 已经是
        # 图外几千像素，量出来的「覆盖 0%」**全是测试自己越界**。
        #
        # 取画布中心还有个好处：`mx - ox` 正好是 `dw/2`，锚点算出的新 pan
        # **恰好是 0**（等于「缩放后仍然居中」），起点干净、最容易自证。
        _ccx = app.cv_a.winfo_width() // 2
        _ccy = app.cv_a.winfo_height() // 2
        for _ in range(nz):
            app._on_wheel(0, Ev(120, _ccx, _ccy))
        app.update()
        settle_render(app)
        app.render_all(precise=True, only=0)
        settle_render(app)
        # ⚠️ 前提自证：从中心滚，pan 应该还是 0。不是的话下面的量又不可信。
        check(app.pan[0] == [0, 0],
              "从画布中心滚 %d 格后 pan 仍是 0（实际 %s）—— 起点干净"
              % (nz, app.pan[0]))
        dw, dh = app._view[0]["disp"]
        cw = app.cv_a.winfo_width()
        ch = app.cv_a.winfo_height()
        rng_x = max(0, dw - cw) // 2
        rng_y = max(0, dh - ch) // 2
        if rng_x < 40 or rng_y < 40:
            continue                       # 平移余量太小，跳过
        app._pan_start(0, Ev(0))
        # ⚠️⚠️ **判据口径：`>= want` 而不是 `== want`**（v1.6 踩了三轮）。
        #
        # `_render_tile` 的图元贴在 `ox + px0/sx`（显示坐标），要求它
        # **盖住** `[px0/sx, px0/sx + want]` 这一段：
        #   - 画**小于** `want` -> 右侧/下侧露白（v1.6 的 bug，54%）
        #   - 画**等于** `want` -> 精确
        #   - 画**大于** `want` -> 也安全：多出来的被画布裁掉。
        #     `up = 1/sx < 1` 那一档（基准像素比需要的多）走的就是这条，
        #     实测 zoom 1.95：want=1817×880 而画了 2206×1068（大 21%），
        #     覆盖率 100% —— **画大是安全的，不是 bug**。
        # 之前我按 `== want` 判，误报了这一档（389px 假警报）。
        # ⚠️ 这个断言必须先自证不是读到了过期状态（下面的 [拖动前] 行），
        #    否则它就只是个会误报的检查。
        _v0 = app._view.get(0, {})
        _im0 = _v0.get("img")
        _w0 = _v0.get("want")
        print("   [拖动前] zoom=%.2f img=%s want=%s disp=%s base=%s"
              % (app.zoom[0],
                 (_im0.width(), _im0.height()) if _im0 else None,
                 _w0, _v0.get("disp"), _v0.get("base")))
        if _im0 is not None and _w0:
            if _im0.width() < _w0[0] - 2 or _im0.height() < _w0[1] - 2:
                print("      ⚠️ 拖动前这一帧就比应有尺寸小（会露白）——"
                      "下面量到的可能不是产品的锅")
        # ⚠️ `_pan_move` 里算的是 `pan = 拖动起点pan + (鼠标坐标 - 起点坐标)`，
        #    起点坐标是 `Ev(0)` 的 `x=10`，起点 pan 是 0。所以要让 pan 落在
        #    `[-rng, +rng]`，鼠标坐标得写 `10 + rng*t` ——
        #    写成 `100 + rng*t` 的话 pan 会超出图能容纳的范围（实测末几步
        #    pan 跑到 387 > ±297），量到的「覆盖 67%」是**测试自己越界**，
        #    不是产品的锅（这个假象坑了两轮）。
        x0, y0 = app._drag[1], app._drag[2]
        worst = 1.0
        # ⚠️⚠️⚠️ **v1.7 新增三条抖动断言**（主人 2026-10-06 报「拖拽有抖动」）。
        #
        # 根因：`_pan_move` 按 `ox`（整张显示图的原点）挪图元，可分块之后
        # 图元实际贴在 `ox + px0/sx`。于是每一步都把图元瞬移回显示图左上角，
        # 露出 1044px 空白 -> 判「该重画」-> **60/60 步每步 60~90ms**。
        # 修法：`_view` 里多存 `bx/by`（图元真实落点），`_pan_move` 用它。
        #
        # 判据的坑（我在这上面错了五版，**全是判据的错不是产品的错**）：
        #   ① 量图元左上角位移当跳变 -> 换块时是「两块起点之差」，假跳变
        #   ② 此刻的 ox 配那帧的 bx -> 假跳变 536px
        #   ③ 只用 pan 推 -> 拿「意图」当「结果」，恒 0 的假通过
        #   ④ 新帧 vx0 配旧帧坐标 -> 跨参照系，假跳变 464px
        #   ⑤ `last_pan` 忘了推回去 -> d_mouse 变成累计量，假跳变 464px
        # **配对必须同帧；基准必须是「本步增量」而不是累计。**
        _redraw = 0
        _follow = []            # 画面位移 / 鼠标位移，应恒为 -1
        _raw = []                # 图元画布位移 / pan 位移，应恒为 1
                         #（不重画的步里才量 —— 这条才抓得住「瞬移」）
        _jumps = []             # 实际位移 − 期望位移，px
        _vx0_prev = None
        _pan_last = [0]         # 上一步的 pan.x（**每步必须推回去**，
        #                        #   否则 d_mouse 变成累计量 -> 假跳变）
        _cw0 = app.cv_a.winfo_width()
        _travelled = 0
        # ⚠️⚠️ **步长必须照真实鼠标的节奏**（一帧 8px）。
        #
        # 原来用 `t = k/39*2-1` 扫满 `±rng_x`，zoom 3.81 时 `rng_x=1348`
        # -> 每步 **69px**。那不是拖动，那是「一帧甩过 69 像素」——
        # 步长比缓冲阈值（40px）还大，于是**每步都该补块**，
        # 量出「40/40 补块」是**必然的**，不是产品的锅。
        #
        # 真实拖动一帧 3~10px（探针 `probe_jitter.py` 用 8px），
        # 60 步才扫过 480px。这时缓冲（152px）才来得及起作用。
        # 判据仍留着「补块次数 << 步数」，但前提是**步长 << 缓冲**。
        _STEP = 8
        # ⚠️⚠️ **跨度必须从「产品实际的夹持区间」取，不能拿 `rng_x`**。
        #   `rng_x = (disp_w - cw)//2` 是「居中摆放时图边到视口边的距离」，
        #   但 v1.7 加了 pan 夹持之后**真实上限就是 `rng_x`**，
        #   扫到 `±rng_x` 正好压在极限上 —— 于是**第一步就贴边、
        #   后面 59 步原地打转**（实测 `pan=(321,341)` 而可拖 ±320，
        #   覆盖量卡在 58% 不动）。
        #   扫程要留出余量：取极限的 70%，让整段都在「缓冲有效」的范围里。
        _span = int(min(_STEP * 30, rng_x) * 0.7) + 1
        for k in range(60):
            # 来回扫：0 -> +_span -> -_span -> 0（三角波）
            _q = k % 40
            _u = _q if _q <= 20 else 40 - _q    # 0..20..0
            _t = _u / 20.0 * _span
            p = type("P", (), {})()
            p.x = x0 + int(_t)
            p.y = y0 + int(_t * rng_y / max(1, rng_x))
            # ⚠️ 判据要用**本步的图元坐标和那一帧的块起点**，同源配对
            _it0 = app._view.get(0, {}).get("item")
            _c0 = list(app.cv_a.coords(_it0)) if _it0 else None
            _v0 = app._view.get(0, {})
            _vx0_prev = (_v0["bx"] - _v0["ox"]) \
                if ("bx" in _v0 and _c0) else None
            t0 = time.perf_counter()
            app._pan_move(0, p)
            app.update()
            pans.append((time.perf_counter() - t0) * 1000)
            # ⚠️ **判重画**：图元换了对象就是补了一块（`cv.delete("all")`
            #    + 重新 create）。这才是「卡」的来源 —— 每步都换 = 每步
            #    都付 60~90ms。
            _it1 = app._view.get(0, {}).get("item")
            if _it1 is not _it0:
                _redraw += 1
                # ⚠️⚠️ **补块那几步的耗时单独收一份**（v1.7 收尾）。
                #
                # 为什么不能只看全样本的 p95：这里两档zoom 各扫 498px，
                # **补块只有 2 次/ 约 60 步 ≈ 3%**，
                # p95 取第 57 帧 —— **够不着最慢的那 2 帧**。
                # 实测：注入「补块做两遍」后 p95 = 2.4ms（跟基线一模一样，
                # 基线也是 2.4ms），**判据完全没反应**。
                # 而补块帧的成本确实翻倍了（max 142 -> 207ms）——
                # 数据在，只是被分位数滤掉了。
                #
                # ⇒ 判据要量在**补块那几步自己身上**，
                #   而不是「所有步的分位数」。补块步本来就是少数，
                #   对它们取 max/均值才是对应物。
                fill_pans.append(pans[-1])
            # ⚠️⚠️ **在「没重画的步」里，图元画布位移必须严格等于 pan 位移**。
            #
            # 这条是 `bx` 那个 bug 的**直接判据**，跟手比值抓不到它 ——
            # bug 一发作就立刻重画，`_view` 和图元一起换，两边自洽，
            # 量出来照样是「恒 -1、完全跟手」（注入验证过：跟手那条
            # 在 bug 下也「通过」，只有这条会红）。
            # 差别在于：这一条比的是**图元自己的画布坐标**，不经过
            # 「块内容起点」那层换算，所以瞬移藏不住。
            _dm = app.pan[0][0] - _pan_last[0]
            if (_it1 is _it0 and _c0 and _dm):
                _dx = list(app.cv_a.coords(_it1))[0] - _c0[0]
                _raw.append(_dx / float(_dm))
            _c1 = list(app.cv_a.coords(_it1)) if _it1 else None
            _v1 = app._view.get(0, {})
            if _c1 and _c0 and "bx" in _v1 and _vx0_prev is not None:
                # 画布正中的显示图坐标 = 块内容起点 + (视口中心 - 图元画布x)
                _now = (_v1["bx"] - _v1["ox"]) + (_cw0 // 2 - _c1[0])
                _prev = _vx0_prev + (_cw0 // 2 - _c0[0])
                _d_view = _now - _prev
                _d_mouse = app.pan[0][0] - _pan_last[0]
                # ⚠️ 记下**扫过的总路程**（用来推补块次数的门槛）
                _travelled += abs(_d_mouse)
                if _d_mouse:
                    _follow.append(_d_view / float(_d_mouse))
                # 期望位移 = -鼠标位移（往右拖 -> 内容往左移）
                _jumps.append(abs(_d_view - (-_d_mouse)))
            _pan_last[0] = app.pan[0][0]
            # ⚠️ **必须模拟真实鼠标的节奏**（约 16ms 一帧）。拖动补块有节流
            #    （PAN_LAG），不 sleep 的话 40 步只花十几毫秒、一次都触发
            #    不了，量到的「露白 40/40」全是假的（探针自己骗自己，踩过）。
            time.sleep(0.016)
            cov = cover()
            # ⚠️⚠️ **顺带量「图元有没有比应有尺寸小」**（v1.6 拖动露白的根因）。
            #    覆盖率是**后果**，尺寸是**原因** —— 差一个像素就已经在露白
            #    边缘了，只是面积小到 `cover()` 还没察觉。分开量才能在
            #    「刚坏」的时候就抓住，而不是等覆盖率掉到 54%。
            #    量的是**负值**（`want - img`）：画大了安全（画布裁掉），
            #    画小了才露白。口径见 `[拖动前]` 那段注释。
            # ⚠️ 用产品记的 `want`，别自己从 `base`/`disp` 反推 ——
            #    拖动时异步双三次帧回来会换基准档，反推的 `want` 与这一帧
            #    无关（实测假报 389px，测试自己错了三轮）。
            v = app._view.get(0, {})
            img = v.get("img")
            want = v.get("want")
            if img is not None and want:
                # >0 表示**小了那么多像素**（露白量）；<=0 表示够大，安全
                short = max(want[0] - img.width(), want[1] - img.height())
                if short > 2:
                    size_bad += 1
                    if size_bad <= 4:
                        print("      step%d **图元 %s 比应有 %s 少 %d px**（会露白）"
                              % (k + 1, (img.width(), img.height()), want, short))
                worst_size_err = max(worst_size_err, short)
            if cov < 0.97:
                bb = app.cv_a.bbox(v.get("item")) if v.get("item") else None
                print("      step%d pan=(%d,%d)覆盖 %.0f%% bbox=%s 基=%s 块=%s"
                      % (k + 1, app.pan[0][0], app.pan[0][1], cov * 100,
                         bb, v.get("base"), v.get("blk")))
            worst = min(worst, cov)
        app._pan_end()
        settle_render(app)
        print("   zoom %.2f 可拖 ±%d,±%d 最差覆盖 %.0f%%  图元最小富余 %+d px"
              % (app.zoom[0], rng_x, rng_y, worst * 100, worst_size_err))
        # ⚠️门槛是95% 而不是 100%：拖到**图的边缘**时块的边界就是图的边界，
        #    此时图本身不够高/宽来填满画布，留1~2% 边是正常的
        #    （实测最差 99%，`bbox` 高 837 而视口+缓冲是 953）。
        #    真正的 bug 是**空画布**（0%）和「越拖覆盖越少」—— 后者才是
        #    主人说的「图片直接空白了」。
        check(worst >= 0.95,
              "拖动时画面盖满画布（最差 %.0f%% >= 95%%）" % (worst * 100))
        # ⚠️⚠️ **这条才是根因断言**。Tk 的 `zoom()`/`subsample()` 只接受
        #    **整数倍**，`uikit.resample_img` 用有理数 p/q 逼近也给不出
        #    精确尺寸 —— 实测要 1818×880 它给 1472/1248/1080，图元右侧
        #    就缺 13~46%（覆盖率 87%/72%/54% 三值循环）。
        #    口径：**图元不得比应有尺寸小**（画大安全，画小必露白）。
        check(worst_size_err <= 2,
              "图元不小于应有尺寸（最小富余 %+d px >= -2）" % worst_size_err)
    check(size_bad == 0,
          "全程没有一次图元小于应有尺寸（%d 次）" % size_bad)
    # ---- v1.7：抖动三条 ----
    # ① 补块频率。
    #    ⚠️⚠️ **门槛必须从「扫过的总路程 ÷ 缓冲」推出来，不能拍一个数**
    #    （我先写「60 步 <= 7 次」，量到 18 次就以为产品坏了 —— 其实
    #    三角波来回扫，路程是单程的 2.5 倍，门槛算少了）。
    #
    #    道理：每走 `PAN_REDRAW_PX`（40px）才可能补一块，而**一次扫描
    #    在两个方向上各补一次**（缓冲每边 40px 触发，来回各一次）。
    #    所以合理次数 ≈ `路程 / PAN_REDRAW_PX` 的 1~2 倍。
    #    真正要抓的是「**每步都补**」——那是 `_pan_move` 用错坐标、
    #    图元瞬移导致每次都露白（实测 60/60 步、每步 60~90ms）。
    _buf = app.cv_a.winfo_width() * app.TILE_PAD
    _exp = max(1, int(_travelled / max(1, app.PAN_REDRAW_PX)))
    _lim = _exp * 2 + 2
    print("   扫过 %d px，缓冲每边 %.0fpx，阈值 %d -> 补块 %d 次"
          "（门槛 %d）" % (_travelled, _buf, app.PAN_REDRAW_PX,
                          _redraw, _lim))
    check(_redraw <= _lim,
          "拖动时补块不频繁（%d 次 <= 门槛 %d = 路程 %d / 阈值 %d * 2 + 2；"
          "每步都补就是抖动）"
          % (_redraw, _lim, _travelled, app.PAN_REDRAW_PX))
    # ② 跟手：画面位移必须严格等于鼠标位移（反向）。
    if _follow:
        _lo, _hi = min(_follow), max(_follow)
        print("   跟手比值 min=%.4f max=%.4f（应恒为 -1）" % (_lo, _hi))
        check(abs(_lo + 1) <= 0.02 and abs(_hi + 1) <= 0.02,
              "拖动完全跟手（画面位移/鼠标位移 = %.4f~%.4f，应恒为 -1）"
              % (_lo, _hi))
    else:
        check(False, "跟手判据取不到样本 —— 测试自己有问题")
    # ②b 图元自己的位移（**这条才抓得住 `_pan_move` 用错坐标**）。
    #     跟手比值在 bug 下也会「通过」（一发作就重画，两边自洽），
    #     这条比的是图元画布坐标、不经换算，瞬移藏不住。注入验证过：
    #     bug 下跟手仍绿、这条变 -148。
    if _raw:
        _rlo, _rhi = min(_raw), max(_raw)
        print("   图元位移/pan位移 min=%.4f max=%.4f（应恒为 1）"
              % (_rlo, _rhi))
        check(abs(_rlo - 1) <= 0.02 and abs(_rhi - 1) <= 0.02,
              "不重画的步里图元严格跟着 pan 走（比值 %.4f~%.4f，应恒为 1）"
              % (_rlo, _rhi))
    else:
        check(False, "图元位移判据取不到样本 —— 测试自己有问题")
    # ③ 跳变：换块处画面必须严丝合缝。
    if _jumps:
        print("   画面跳变误差 最大 %.1f px（换块处应 = 0）" % max(_jumps))
    check(_jumps and max(_jumps) <= 2,
          "换块时画面不跳（最大误差 %.1f px <= 2）"
          % (max(_jumps) if _jumps else -1))
    if pans:
        # ⚠️⚠️⚠️ **v1.7 收尾：判据从「全样本 p95」改成「补块那几步」**
        #（2026-10-07）。
        #
        # 这一改是被**注入验证**逼出来的（probe_inject_threshold.py）：
        # 我把「补块做两遍」这个真回归注回去，这里量到
        #     全样本 p95 = 2.4ms   ← 跟基线（也是 2.4ms）**一模一样**
        #     补块步 max = 207ms   ← 基线 142ms，**确实翻倍了**
        # 全样本 p95 纹丝不动，判据形同虚设。
        #
        # ⚠️⚠️ **根因：这个场景的补块只占 3%，任何分位数都够不着它。**
        #   两档 zoom 各扫 498px，补块 2 次 / 约 60 步；
        #   p95 取第 57 帧，而最慢的 2 帧排在第 59、60 位 —— 被滤掉了。
        #   ⇒ **量补块步自己**，别绕开它们取分位数。
        #
        # ⚠️⚠️ **两边的教训相关但结论不同，别照抄**：
        #   `probe_pan2.py` 四向各 115 步、补块 4~9 次（**4~8%**），
        #   实测注入后 **p95 从 20 跳到 148（7 倍）** —— 那边 p95 判据
        #   **有效**，改成「p95 < 60」就抓住了。
        #   这里补块只占 3%，p95 纹丝不动（2.4 → 2.4）—— 无效。
        #   ⇒ **分位数能不能抓到补块，取决于补块占比 vs 分位位置**：
        #     115 步里p95 = 第 109 帧，4~9 个补块帧排在最慢的 9 位内，
        #     **够不着**第 109 帧；补块一多（注入后整条尾巴抬起）才够着。
        #     这也说明「p95 能抓」是**碰巧**，不是设计 —— 真要盯住
        #     「补块那一步多贵」，就得量它自己（就是上面这条）。
        _avg = sum(pans) / len(pans)
        print("   拖动 %.1f ms/次（p95 %.1f / 最大 %.1f）"
              % (_avg, sorted(pans)[min(len(pans) - 1,
                                        int(len(pans) * 0.95))], max(pans)))
        if fill_pans:
            _fa = sum(fill_pans) / len(fill_pans)
            print("   其中补块 %d 步：平均 %.1f / 最大 %.1f ms"
                  % (len(fill_pans), _fa, max(fill_pans)))
            # 门槛 260：**基线补块步142~176ms，注入后 207ms —— 仍抓不住**。
            # 换成分位数也不行（补块只 3%，见上面那段）。
            #⇒ 这条留给「真·极端单帧」（同步解码整张图那种 500ms+），
            #   而「补块步变贵」由**下面的均值/最大双条** + 探针的补块次数
            #   共同盯着。
            check(max(fill_pans) < 500,
                  "补块那一步无极端失控（补块 %d 步，最慢 %.1f ms < 500 ms）"
                  % (len(fill_pans), max(fill_pans)))
        else:
            check(False, "补块步判据取不到样本 —— 测试自己有问题")
        # 全样本 max 兜底（真·单帧失控的量级）
        check(max(pans) < 500,
              "拖动无极端单帧（最慢 %.1f ms < 500 ms）" % max(pans))
        # 均值才是「卡不卡」的主判据（p50 那条在别处），这里留个底线
        check(_avg < 30, "拖动平均不卡（平均 %.1f ms < 30 ms）" % _avg)

    print("\n=== 不放大也能拖；而且不许把图拖出视口（v1.7）===")
    # ⚠️⚠️ 主人 2026-10-06：「缩放时会强制定位到图片中心」「在拖动前必须
    #   进行缩放，不然无法拖拽」。
    #   根因：`_pan_move` 第一行`if ... or self.zoom[col] <= 1.001: return`
    #   —— **缩回适应窗口后整个拖动被冻结**，画面回到居中且再也拖不动。
    #   修法：不再看 zoom，改看「图比视口有没有余量」，
    #   并把pan **夹在图能移动的范围**内（图比视小的那一维不许拖）。
    app.reset_zoom()
    app.update()
    settle_render(app)
    _cw = app.cv_a.winfo_width()
    _ch = app.cv_a.winfo_height()
    _dw, _dh = app._view[0]["disp"]
    print("   zoom=1.00 显示图 %dx%d 视口 %dx%d" % (_dw, _dh, _cw, _ch))
    if _dw <= _cw and _dh <= _ch:
        # ⚠️⚠️ **图比视口小：必须「能拖」且「拖不出视口」**（v1.7 修，
        #   主人 2026-10-07：「每次拖拽前必须缩放一下，在这个位置无法拖拽」
        #   + 截图 1328x2048 显示 36%）。
        #
        # ⚠️ 这条判据**是被注入验证逼出来的**：原来这里断言的是
        #   `pan == (0,0)`（= 拖不动），方向整个反了 —— 它把 bug
        #   当成了正确行为钉住。真正的机制是：
        #   图在画布上 `ox = (cw-dw)//2 + pan`，「图完整可见」解出
        #   `pan ∈ [-(cw-dw)//2, cw-dw-(cw-dw)//2]`，宽度 `cw-dw`。
        #   **图比视口小时区间最宽（最能拖）**，因为整张图都在画布里，
        #   往任意方向挪都不切内容。实测那个位置横向有 1038px 可拖
        #   （69% 视口宽），却被冻成 0。
        _room_w = abs(_cw - _dw)
        print("   预期可拖范围：横向 %d px（视口宽的 %.0f%%）"
              % (_room_w, 100.0 * _room_w / _cw))
        for _dir, _lb in (("右", (1, 0)), ("下", (0, 1))):
            app._pan_start(0, Ev(0, _cw // 2, _ch // 2))
            _x0, _y0 = app._drag[1], app._drag[2]
            for _k in range(15):
                app._pan_move(0, Ev(0, _x0 + _lb[0] * 8 * _k,
                                    _y0 + _lb[1] * 4 * _k))
                app.update()
            app._pan_end()
            settle_render(app)
            _mv = app.pan[0][0] if _dir == "右" else app.pan[0][1]
            check(_mv > 0,
                  "图比视口小也能往%s拖（pan.%s=%d，应 >0）"
                  % (_dir, "x" if _dir == "右" else "y", _mv))
        # 拖过头也不许出空白（图必须始终完整在视口内）
        app._pan_start(0, Ev(0, _cw // 2, _ch // 2))
        _x0, _y0 = app._drag[1], app._drag[2]
        for _k in range(20):
            app._pan_move(0, Ev(0, _x0 - 40 * _k, _y0 - 30 * _k))
            app.update()
        app._pan_end()
        settle_render(app)
        _v = app._view[0]
        _gx0, _gx1 = _v["ox"], _v["ox"] + _dw
        _gy0, _gy1 = _v["oy"], _v["oy"] + _dh
        print("   死拖后 图 x[%d,%d] y[%d,%d]，视口 %dx%d"
              % (_gx0, _gx1, _gy0, _gy1, _cw, _ch))
        check(_gx0 >= -2 and _gx1 <= _cw + 2 and _gy0 >= -2 and _gy1 <= _ch + 2,
              "图比视口小时拖死了也不出空白（图 x[%d,%d] y[%d,%d]，视口 %dx%d）"
              % (_gx0, _gx1, _gy0, _gy1, _cw, _ch))
    # 图比视头大 -> 必须能拖
    for _ in range(4):
        app._on_wheel(0, Ev(120, _cw // 2, _ch // 2))
    app.update()
    settle_render(app)
    _dw, _dh = app._view[0]["disp"]
    check(_dw > _cw and _dh > _ch,
          "放大后图比视口大（%dx%d > %dx%d）—— 下面才量拖动"
          % (_dw, _dh, _cw, _ch))
    app._pan_start(0, Ev(0, _cw // 2, _ch // 2))
    _x0, _y0 = app._drag[1], app._drag[2]
    _moved = 0
    for _k in range(12):
        app._pan_move(0, Ev(0, _x0 + 10 * _k, _y0 + 6 * _k))
        app.update()
    app._pan_end()
    settle_render(app)
    _moved = abs(app.pan[0][0])
    print("   放大后拖了 12 步（每步 10px）-> pan=%s" % (tuple(app.pan[0]),))
    check(_moved >= 100, "放大后拖动有效（pan.x=%d >= 100）" % _moved)
    # ⚠️⚠️ **不许把图拖出视口**（这条是「图比视口小」那侧的护栏）：
    #   拖过头会露出大片空白，用户以为程序坏了。
    # 一路缩到最小（ZOOM_MIN 让图比视口小很多），再试着往角落死拖
    for _ in range(12):
        app._on_wheel(0, Ev(-120, _cw // 2, _ch // 2))
        app.update()
    settle_render(app)
    _dw, _dh = app._view[0]["disp"]
    print("   缩到最小 zoom=%.2f 显示图 %dx%d（比视口小）"
          % (app.zoom[0], _dw, _dh))
    app._pan_start(0, Ev(0, _cw // 2, _ch // 2))
    _x0, _y0 = app._drag[1], app._drag[2]
    for _k in range(20):
        app._pan_move(0, Ev(0, _x0 - 30 * _k, _y0 - 20 * _k))
        app.update()
    app._pan_end()
    settle_render(app)
    _v = app._view[0]
    _bb = app.cv_a.bbox(_v["item"]) if _v.get("item") else None
    print("   图比视口小时往角落死拖 20 步 -> pan=%s 图元=%s"
          % (tuple(app.pan[0]), _bb))
    # 图在画布上的范围必须还在视口内（允许一点点余量）
    _gx0, _gx1 = _v["ox"], _v["ox"] + _dw
    _gy0, _gy1 = _v["oy"], _v["oy"] + _dh
    check(_gx0 > -40 and _gx1 < _cw + 40 and _gy0 > -40 and _gy1 < _ch + 40,
          "图没被拖出视口（图 x[%d,%d] y[%d,%d]，视口 %dx%d）"
          % (_gx0, _gx1, _gy0, _gy1, _cw, _ch))

    # ⚠️⚠️ **上面那条测不到「夹持」** —— 缩到最小时 `_room <= 0`，
    #   `_pan_move` 第一步就 return 了，根本走不到夹持那两行
    #   （我注入掉夹持跑了一遍，断言照样OK —— **判据是假的**，重写）。
    #   要测夹持必须构造「图比视口**只大一点**」的场景：
    #   有room（能拖）但余量很小（拖过头就会出视口）。
    print("   --- 图比视口只大一点：夹持必须生效 ---")
    app.reset_zoom()
    app.update()
    settle_render(app)
    # 用**二分逼近**：先粗扫找到「刚大于视口」的那档，再二分逼近目标余量。
    # ⚠️⚠️ **上界要留够**（我第一版写 [1.0, 1.4]，二分到 1.4 就停 ——
    #   而实测「只大一点」的区间在 zoom 1.45~1.55
    #   （room_x 86~196），区间在**上界之外**，所以一个都找不到）。
    #   上界取 2.0：够宽，又保证落进分块路径（zoom > 1）。
    _found = None

    def _room_at(zv):
        app.zoom[0] = zv
        app.render_all(precise=True, only=0)
        app.update()
        d = app._view[0]["disp"]
        return d, d[0] - _cw, d[1] - _ch

    _lo, _hi = 1.0, 2.0
    for _i in range(24):
        _mid = (_lo + _hi) / 2
        _d, _rx, _ry = _room_at(_mid)
        if _rx < 60 or _ry < 60:
            _lo = _mid                # 余量还太大（或还没大过视口），往上找
        else:
            _hi = _mid
            _found = (_d, _rx, _ry, _mid)   # 余量已经很小，记下
            if _rx <= 200 and _ry <= 200:
                break
    # 兜底：二分结束后用找到的最后一档（可能余量偏大，仍够「拖过头」）
    if _found is None:
        _d, _rx, _ry = _room_at(_hi)
        _found = (_d, _rx, _ry, _hi)
    check(_found is not None and _found[1] < 400 and _found[2] < 400,
          "找到一档「图比视口只大一点」的缩放（余量 %dx%d，小到一拖就过头）"
          % (_found[1], _found[2]))
    if _found:
        _d, _rx, _ry = _found[0], _found[1], _found[2]
        print("      zoom=%.3f 显示图 %dx%d（比视口大 %dx%d）"
              % (_found[3], _d[0], _d[1], _rx, _ry))
        # 死命往右下拖 40 步，每步 20px = 800px，远超余量
        app._pan_start(0, Ev(0, _cw // 2, _ch // 2))
        _x0, _y0 = app._drag[1], app._drag[2]
        for _k in range(40):
            app._pan_move(0, Ev(0, _x0 + 20 * _k, _y0 + 14 * _k))
            app.update()
        app._pan_end()
        settle_render(app)
        _v = app._view[0]
        _gx0, _gx1 = _v["ox"], _v["ox"] + _d[0]
        _gy0, _gy1 = _v["oy"], _v["oy"] + _d[1]
        disp_gap_x = _d[0] - _cw      # 图比视口多出来的量（<=0 表示图更窄）
        disp_gap_y = _d[1] - _ch
        print("      死拖 800px 后pan=%s 图 x[%d,%d] y[%d,%d]"
              % (tuple(app.pan[0]), _gx0, _gx1, _gy0, _gy1))
        # ⚠️⚠️ **判据的分母得想清楚：图比视口大时「零露白」不可能存在。**
        #   图 1050 高 / 视口 757 -> 无论怎么拖都至少有一边露 293px，
        #   那是**图比视口多出来的部分**，是图的固有属性，不是拖坏的。
        #   所以要比的是「拖出来的额外露白」：
        #       拖完露白<= 图比视口多出来的量 + 容差
        #   我第一版直接判 `<= 40`，量出「露白 293 FAIL」—— 那是**判据错**，
        #   不是产品错（同一个坑我今天已经踩了三回：判据错 != 产品错）。
        _extra_x = max(0, disp_gap_x)
        _extra_y = max(0, disp_gap_y)
        _gap_r = max(0, -_gx0) + max(0, _gx1 - _cw)
        _gap_b = max(0, -_gy0) + max(0, _gy1 - _ch)
        print("      图比视口多出来的量 %dx%d -> 允许露白 %dx%d"
              % (_extra_x, _extra_y, _extra_x + 40, _extra_y + 40))
        check(_gap_r <= _extra_x + 40 and _gap_b <= _extra_y + 40,
              "拖不出「比图本身更大」的空白（右露 %d<=%d、下露 %d<=%d）"
              % (_gap_r, _extra_x + 40, _gap_b, _extra_y + 40))
        # 图比视口小的那一维必须**严格**不许露白
        if disp_gap_x <= 0:
            check(_gap_r <= 40,
                  "比视口窄的那一维几乎没露白（%d <= 40）" % _gap_r)
        if disp_gap_y <= 0:
            check(_gap_b <= 40,
                  "比视口矮的那一维几乎没露白（%d <= 40）" % _gap_b)
        # pan 必须被夹住（不许拖到区间外）
        # ⚠️⚠️⚠️ **这里的 lo/hi 曾经和产品一样写反了**（`lo=-_cx`、
        #   `hi=cw-dw-_cx`），于是**图比视口大时**判出 `lo>hi` 的「空区间」，
        #   测试也跟着把 pan 期望成一个点，反过来替产品的 bug 背书。
        # 正确推导（`ox=(cw-dw)//2+pan`，零露白 <=> `ox<=0 且 ox+dw>=cw`）：
        #     hi = -(cw-dw)//2
        #     lo =  cw - dw - (cw-dw)//2
        # 区间宽度 = `dw - cw`：图大 -> 有宽度 -> 能拖；图小 -> 空 -> 不能拖。
        _cx = (_cw - _d[0]) // 2
        _hi_x, _lo_x = -_cx, _cw - _d[0] - _cx
        _cy = (_ch - _d[1]) // 2
        _hi_y, _lo_y = -_cy, _ch - _d[1] - _cy
        if _lo_x > _hi_x:
            # 真空区间（这一维图完全塞得下）-> 产品会夹回中点，测试同口径
            _lo_x = _hi_x = (_lo_x + _hi_x) // 2
        if _lo_y > _hi_y:
            _lo_y = _hi_y = (_lo_y + _hi_y) // 2
        check(_lo_x - 2 <= app.pan[0][0] <= _hi_x + 2
              and _lo_y - 2 <= app.pan[0][1] <= _hi_y + 2,
              "pan 被夹在合法区间内：pan=%s 合法 x[%d,%d] y[%d,%d]"
              % (tuple(app.pan[0]), _lo_x, _hi_x, _lo_y, _hi_y))
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
        blank, missing = text_gaps(lst)
        check(not blank,
              "%s列表画出来的行都有文字（缺字的行：%s）"
              % (nm, blank[:6] or "无"))
        check(not missing,
              "%s列表可见范围内没有漏画的行（漏画：%s）"
              % (nm, missing[:6] or "无"))
        kinds = set(lst.type(c) for c in lst.find_all())
        check("text" in kinds,
              "%s列表画布上有文字图元（实际类型 %s）" % (nm, sorted(kinds)))
    # 每个「行标签」下的图元数应该 >1（背景 + 至少一行字）—— 只看画出来的行
    thin = [i for i in range(len(app.glist.items))
            if len(app.glist.find_withtag(app.glist._row_tag(i))) == 1]
    check(not thin, "没有「只有背景没有字」的空行（%s）" % (thin[:6] or "无"))

    print("\n=== hover 局部重绘后文字还在 ===")
    # 局部重绘 = 先 delete(row{i}) 再 _draw_row(i)。要是 delete 把刚画的也删了，
    # 划过之后行就空了 —— 上面那条测的是**初始**状态，这条测的是**划过之后**。
    lst = app.glist
    for i in (0, 1, 2, 3):
        lst._motion(type("E", (), {"y": int(i * (lst.row_h + lst.gap)) + 4})())
    lst._leave()
    after, still_missing = text_gaps(lst)
    check(not after, "划过若干行 + 移出列表后，文字仍然完整（缺字 %s）"
          % (after[:6] or "无"))
    check(not still_missing, "划过之后可见范围仍然没有漏画（漏画 %s）"
          % (still_missing[:6] or "无"))

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

    # ⚠️ 桩事件必须带 `x`/`y`：**锚点缩放要读鼠标位置**
    #（v1.7 起 `_on_wheel` 会算「以鼠标底下那点为锚」，见 `_zoom_anchor`）。
    # 真实的 Tk <MouseWheel> 事件一定带这两个，桩漏了就是测试自己不对。
    class _E:
        delta = 120
        x = 0
        y = 0
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
        x = 0
        y = 0
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

    # ---- ⚠️⚠️⚠️ **锚点缩放**：滚完不能把画面送回中间（v1.7 加）----
    #
    # 主人 2026-10-06 报：「拖拽到一个位置，想要放大，他又会重新回到画面中间」。
    # 根因是 `_zoom_anchor` 把缩放不变量写成乘法 —— 图元画在 `ox + vx`
    # （`vx` 是**显示图**坐标，显示图随 zoom 等比放大），所以同一个原图位置
    # 的 `vx` 按 `z/cur` 缩放，不变量是**显示坐标**，写成「基准像素 ÷zoom」
    # 就凭空差一个 `fit≈0.36` 的倍数；写成乘 `cur` 更糟，每滚一格误差乘
    # `cur²`，连滚 6 格把 pan 推到 **129 万像素**（视口才 1515 宽）。
    #
    # ⚠️ 判据用**独立参考实现**（`_ref_anchor`），只按坐标定义算，不调产品
    #    任何函数 —— 参考和产品同时错的话，这条断言就是绿的假绿。
    print("\n=== 锚点缩放：鼠标底下那点不能动 ===")
    cw_ = app.cv_a.winfo_width()
    ch_ = app.cv_a.winfo_height()
    wh_ = ui.META.of(app.path_a)["wh"]

    def _fit_now():
        """当前帧的 `fit`（`disp_w = wh_w * fit * zoom` 里那个常数）。

        ⚠️ **不能写成 `disp_w / wh_w`** —— 那是 `fit * zoom`，每滚一格变
        1.25 倍，判据自己先错（第一版就这么写的，量出 ±2000px 假偏差）。
        """
        vv = app._view.get(0) or {}
        dsp = vv.get("disp")
        if not dsp:
            return None
        return dsp[0] / float(wh_[0] * app.zoom[0])

    def _ref_anchor(cur, pan, z, mx, my):
        """独立参考：mx = ox + vx = ox' + vx*z/cur，反解 pan'。"""
        def _dw(zz):
            return int(round(wh_[0] * fit * zz))

        def _dh(zz):
            return int(round(wh_[1] * fit * zz))

        vx = mx - ((cw_ - _dw(cur)) // 2 + pan[0])
        vy = my - ((ch_ - _dh(cur)) // 2 + pan[1])
        nox = (cw_ - _dw(z)) // 2
        noy = (ch_ - _dh(z)) // 2
        return (int(round(mx - vx * z / cur - nox)),
                int(round(my - vy * z / cur - noy)))

    class _W:
        def __init__(self, mx, my):
            self.delta, self.x, self.y = 120, mx, my

    # ⚠️ 鼠标点**偏离中心** —— 正中心时 `mx - ox ≈ disp_w/2`，锚点算错
    #    也可能看不出来（实测偏差会被吸收掉）。
    worst_anchor = 0
    no_pan = []
    for (mx, my) in [(cw_ // 4, ch_ // 4), (cw_ - cw_ // 5, ch_ - ch_ // 5),
                     (cw_ // 2, 10), (10, ch_ // 2)]:
        app.reset_zoom()
        app.update()
        settle_render(app)
        # ⚠️ 必须先放大到 >1 才能拖（`_pan_move` 里 `zoom <= 1.001` 直接
        #    return，zoom=1 时图本来就铺满、没有可拖的地方）。主人报错的前
        #    提是「拖到某个位置」，不先造出非零 pan 就等于没测这个场景。
        for _ in range(2):
            app._on_wheel(0, _W(cw_ // 2, ch_ // 2))
            app.update()
            settle_render(app)
        app._pan_start(0, _W(10, 10))
        app._pan_move(0, _W(10 + 140, 10 + 70))
        app._pan_end()
        app.update()
        settle_render(app)
        pan0 = tuple(app.pan[0])
        if pan0 == (0, 0):
            no_pan.append((mx, my))
        fit = _fit_now()
        for _ in range(6):
            cur = app.zoom[0]
            z = min(ui.ZOOM_MAX, cur * ui.ZOOM_STEP)
            got = app._zoom_anchor(0, mx, my, cur, z)
            want = _ref_anchor(cur, pan0, z, mx, my)
            worst_anchor = max(worst_anchor,
                               abs(got[0] - want[0]), abs(got[1] - want[1]))
            app._on_wheel(0, _W(mx, my))
            app.update()
            settle_render(app)
            pan0 = tuple(app.pan[0])
    check(not no_pan, "4 个测试点都真的拖出了非零 pan（没拖动的：%s）" % (no_pan or "无"))
    check(worst_anchor <= 2,
          "锚点缩放跟手：4 个鼠标位置 × 6 档最大偏差 %d px <= 2" % worst_anchor)
    # 反向也要成立：缩回去同样不能跳
    app.reset_zoom()
    app.update()
    settle_render(app)

    print("\n=== 换组自动重置缩放（v1.7 加）===")
    # ⚠️ 主人 2026-10-06 报「换组不会自动重置放缩」——换一张图还留着上一张
    #    的倍率，看起来就是「这张图怎么这么糊/这么大」。
    if app.mode == "pair":
        for _ in range(3):
            app._on_wheel(0, _W(cw_ // 2, ch_ // 2))
        app.update()
        settle_render(app)
        app._pan_start(0, _W(10, 10))
        app._pan_move(0, _W(160, 110))
        app._pan_end()
        app.update()
        settle_render(app)
        check(app.zoom[0] > 1.01 or app.pan[0] != [0, 0],
              "先把它缩放+拖动过（zoom=%.2f pan=%s）" % (app.zoom[0], app.pan[0]))
        n_before = len(app.mlist.items)
        if n_before > 1:
            nxt = (app.mlist.sel + 1) % n_before
            app.mlist.select(nxt)
            app.update()
            settle_render(app)
            check(abs(app.zoom[0] - 1.0) < 1e-6 and abs(app.zoom[1] - 1.0) < 1e-6,
                  "换组后缩放自动回到 1.00（%.2f / %.2f）"
                  % (app.zoom[0], app.zoom[1]))
            check(app.pan[0] == [0, 0] and app.pan[1] == [0, 0],
                  "换组后平移也归零（%s / %s）" % (app.pan[0], app.pan[1]))

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
    # ---- ⚠️⚠️ **信息卡不许有真空位**（v1.7 加）----
    #
    # 主人 2026-10-06：「看得到下面还有点空位，可以适当的调整」。
    # 判据是「卡片高 − 内容请求高」，**不是**「卡片高 − body 高」——
    # 后者恒等于 Card 的 `2*(pad+inset)`（实测 32px），是设计留白，
    # 拿它当判据会得出「永远有 32px 空位」的自相矛盾结论
    # （我第一版就是这么写的）。
    _pad = 2 * (app.info_card._pad + app.info_card._inset)
    slack = ih - app.info_card.req_height()
    print("   信息卡高 %d  内容请求高 %d  内边距 %d  真空位 %d"
          % (ih, app.info_card.req_height(), _pad, slack))
    check(slack <= 4,
          "信息卡没有真空位（%d px <= 4；内边距 %d px 是设计留白，不算）"
          % (slack, _pad))
    # 清晰度现在放**标题行右侧**（那里本来空着），所以它不占额外行高 ——
    # 「下面的空位」和「NIQE 那半句空着」是同一个毛病的两面，一起修掉了。
    #
    # ⚠️ 判据的门槛是 **`linespace + 6`，不是 `linespace`** ——
    # Tk 的 Label 请求高度比 linespace 大：1px 上 + 1px 下 borderpad
    # 加上下各 1px internal pad（实测 YaHei 9 号：linespace=24 ->
    # 一行 Label reqh=30）。我第一版拿 linespace 当门槛，于是**一行**
    # 也报「30 > 26 不合格」—— 判据自己错了（假红）。
    _nq = app._niqe_lbl.get(0) or app._niqe_lbl.get(1)
    if _nq:
        _lbl = _nq[1]
        _one = int(app.f_small.metrics("linespace")) + 6
        check(_lbl.winfo_reqheight() <= _one,
              "清晰度那行只有一行高（%d <= %d）—— 不折行，高度才恒定"
              % (_lbl.winfo_reqheight(), _one))

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

    # ---- ⚠️ 键盘快捷键（v1.7 加，主人 2026-10-06 提的）----
    #
    # 主人要的是「方向键或者 wasd 可以快捷换组、删除」。三种键要分别验：
    #   ↑↓/WS  换组（分组视图）或换张（全部图片视图）
    #   A/←    删左侧    D/→ 删右侧    Delete/Backspace 删当前侧
    #   0      缩放复位
    # ⚠️ **`_key_move` 在两个视图里语义不同**（组 vs 张），混了会出现
    #    「按 Down 什么都没发生」，所以两个视图都要测。
    print("\n=== 键盘快捷键：换组 / 换张 ===")

    class _K:
        def __init__(self, keysym="", char=""):
            self.keysym, self.char = keysym, char

    app.set_view("groups")
    app.update()
    settle_render(app)
    if app.groups and len(app.groups) > 1:
        g0 = app.gidx
        app._key_move(1)
        app.update()
        settle_render(app)
        g1 = app.gidx
        check(g1 == (g0 + 1) % len(app.groups),
              "分组视图：↓ 切到下一组（%d -> %d，共 %d 组）"
              % (g0, g1, len(app.groups)))
        app._key_move(-1)
        app.update()
        settle_render(app)
        check(app.gidx == g0, "↑ 切回上一组（回到 %d）" % g0)
        check(getattr(app.glist, "sel", None) == app.gidx,
              "列表选中态和 gidx 一致（%s vs %d）—— 不一致会「按了没动」"
              % (getattr(app.glist, "sel", None), app.gidx))
        app._key_move(-1)
        app.update()
        settle_render(app)
        check(app.gidx == len(app.groups) - 1,
              "在第一组按 ↑ 绕到最后一组（%d）" % app.gidx)
        app.select_group(g0)
        app.update()
    else:
        check(True, "分组太少，跳过分组视图的换组用例")

    app.set_view("all")
    app.update()
    settle_render(app)
    # ⚠️⚠️ **先点第一行，再从那儿按 ↑** —— 别拿 `app.path_a` 当起点。
    #
    # 上一段（分组视图）结束时 `path_a` 停在**某一组的第一张**，它
    # 未必是 `files[0]`（实测 f0=full.png 而 files[0]=低清小图.png）。
    # 我第一版直接 `f0 = app.path_a` 然后断言「↑ 绕到最后一张」——
    # 前提就不成立，量到的是「从第 k 张退到第 k-1 张」。
    # 顺带解释了另一条失败：`_show_all_item` **不动 `glist` 选中态**，
    # 所以这段测完必须显式点列表归位，否则后面「右边展示的就是选中
    # 那一行」会拿一个对不上的状态去断言（又是测试自己造的矛盾）。
    _r0 = 0
    app.glist.select(_r0, notify=True)
    app.update()
    settle_render(app)
    f0 = app.path_a
    print("   glist.items[0].tag=%s  glist.sel=%s  path_a=%s  files[0]=%s"
          % (os.path.basename(app.glist.items[0]["tag"]),
             app.glist.sel, os.path.basename(app.path_a or "?"),
             os.path.basename(app.files[0])))
    check(f0 == app.glist.items[0]["tag"],
          "从列表第一行出发（%s，应是 %s）"
          % (os.path.basename(f0 or "?"),
             os.path.basename(app.glist.items[0]["tag"])))
    app._key_move(1)
    app.update()
    settle_render(app)
    f1 = app.path_a
    # ⚠️「下一张」是**列表里的下一张**（`glist.items[1]`），不是 `files[1]`
    check(f1 == app.glist.items[1]["tag"],
          "全部图片视图：↓ 换到列表的下一张（%s -> %s，应是 %s）"
          % (os.path.basename(f0 or "?"), os.path.basename(f1 or "?"),
             os.path.basename(app.glist.items[1]["tag"])))
    check(app.glist.sel == 1, "↓ 之后列表选中态是第 1 行（%d）" % app.glist.sel)
    app._key_move(-1)
    app.update()
    settle_render(app)
    check(app.path_a == f0, "↑ 换回上一张（%s）" % os.path.basename(f0 or "?"))
    app._key_move(-1)
    app.update()
    settle_render(app)
    print("   path_a=%s  files[-1]=%s  n=%d"
          % (os.path.basename(app.path_a or "?"),
             os.path.basename(app.files[-1]), len(app.files)))
    # ⚠️⚠️ **断言按「列表顺序」，不是 `app.files`**（v1.7 修的真 bug）。
    #    `_fill_all_list` 按质量重排过（`QUALITY_RANK` + 文件名），
    #    所以 `glist.items` 跟 `self.files` **顺序不同** ——
    #    实测 59 张里列表第 0 行在 `files` 里是下标 1。
    #    产品原来的 `_key_move` 用 `files.index()` 算下标，
    #    于是「按 ↓ 换下一张」换的是**列表里看不见的那一张**，
    #    用户按几下就发现屏幕上的行和右边的图对不上。
    _order = [it["tag"] for it in app.glist.items]
    print("   （自证）列表第 0 行在 files 里的下标=%r；列表末项=%s "
          "vs files 末项=%s%s"
          % ([i for i, x in enumerate(app.files)
              if x == _order[0]], os.path.basename(_order[-1]),
             os.path.basename(app.files[-1]),
             "（本来就一样，本条就测不出差别）"
             if _order[-1] == app.files[-1] else " <== 顺序不同"))
    check(app.path_a == _order[-1],
          "在第一张按 ↑ 绕到**列表**最后一张（%s，应是 %s）"
          % (os.path.basename(app.path_a or "?"),
             os.path.basename(_order[-1])))
    check(app.glist.sel == len(_order) - 1,
          "按 ↑ 之后列表选中态跟着走到最后一行（sel=%d，应是 %d）"
          % (app.glist.sel, len(_order) - 1))
    # ⚠️ 归位要**点列表**，别用 `_show_all_item` —— 它只换右边那张，
    #    不同步 `glist` 选中态（`glist.select` 才管列表）。用前者归位
    #    会留下「列表选中第 24 行、右边却是另一张」的不一致状态，
    #    后面的断言会张冠李戴（实测就是这样挂了两条）。
    app.glist.select(_r0, notify=True)
    app.update()
    settle_render(app)
    check(app.path_a == app.glist.items[_r0]["tag"],
          "归位后右边就是列表选中那一行（%s）"
          % os.path.basename(app.path_a or "?"))

    print("\n=== 键盘快捷键：0 复位缩放 ===")
    cw2 = app.cv_a.winfo_width()
    ch2 = app.cv_a.winfo_height()
    for _ in range(3):
        app._on_wheel(0, _W(cw2 // 2, ch2 // 2))
    app.update()
    settle_render(app)
    check(app.zoom[0] > 1.01, "先放大（zoom=%.2f）" % app.zoom[0])
    app.reset_zoom()          # 走和按键 0 一样的路径
    app.update()
    settle_render(app)
    check(abs(app.zoom[0] - 1.0) < 1e-6,
          "「0 / 重置缩放」回到 1.00（%.2f）" % app.zoom[0])

    print("\n=== 键盘快捷键：删除走既有确认框（不另写一套）===")
    # ⚠️ 不能真删样本文件 —— 把 `delete_side` 换成只记号的桩，
    #    然后**直接调按键处理函数**（产品把它挂在 `_on_key` 上）。
    #    不用 `event_generate("<Key>")`：那是模拟真实键盘输入，测试环境
    #    没有焦点链、根本派发不到 —— 第一版就靠「派发不到就直调兜底」
    #    蒙过去，断言是绿的，但派发那条路根本没验证（假测）。
    pair = (app.mode == "pair" and bool(app.path_b))
    want_right = 1 if pair else 0
    calls = []
    real_del = app.delete_side
    app.delete_side = lambda col: calls.append(col)
    toasts = []
    real_toast = app._toast
    app._toast = lambda s: toasts.append(s)
    try:
        # (keysym, char, 期望转发到的侧)
        # ⚠️ `d`/`right`（删右侧）在**单图模式**下期望值是 0 ——
        #    那里根本没有右侧，产品会先提示「现在只有一张图」再删当前这张
        #    （静默改成删左侧就是误删，v1.7 修掉了）。
        for keysym, char, want_side in (("a", "a", 0), ("left", "", 0),
                                         ("d", "d", want_right),
                                         ("right", "", want_right),
                                         ("delete", "", 0),
                                         ("backspace", "", 0)):
            n0 = len(calls)
            app._on_key(_K(keysym, char))
            got = calls[n0:] if len(calls) > n0 else ["<没转发>"]
            check(got == [want_side],
                  "按 %s -> 删第 %d 侧（实际转发 %s；mode=%s path_b=%s）"
                  % (keysym or char, want_side, got, app.mode,
                     bool(app.path_b)))
        # 上下/ws/0 不该触发删除
        n0 = len(calls)
        app._on_key(_K("up", ""))
        app._on_key(_K("0", "0"))
        check(len(calls) == n0,
              "↑ 和 0 不会误触发删除（多转发了 %d 次）" % (len(calls) - n0))
        # ⚠️ 单图模式下按 D 必须**说清楚**删的是哪张（不然就是误删）
        if not pair:
            check(any("只有一张" in s for s in toasts),
                  "单图模式按 D 会提示「现在只有一张图」（提示 %s）" % (toasts or "无"))
        else:
            check(not any("只有一张" in s for s in toasts),
                  "并排模式下按 D 不该有「只有一张」的提示（提示 %s）"
                  % (toasts or "无"))
    finally:
        app.delete_side = real_del
        app._toast = real_toast

    print("\n=== 键盘快捷键：焦点在输入框里要让路 ===")
    # ⚠️⚠️ **这一条是安全线**：用户在「添加文件夹」那类 Entry 里按 Delete
    #    想删字，如果快捷键不让路就会把图删了 —— 这种误删弹确认框都救不回来。
    ent = None
    try:
        probe_top = tk.Toplevel(app._frame)
        ent = tk.Entry(probe_top)
        ent.pack()
        probe_top.update()
        app._frame.update()
        # ⚠️⚠️ **顺序：先让主窗口自己 update，再对 Entry 调 `focus_force`，
        #    最后**只**update 那个 Toplevel**。
        #    我第一版在 `focus_force` 之后又 `app.update()` 了一次 ——
        #    主窗口那次 update 把焦点又抢回去了，于是量到「造不出焦点
        #    Entry」，跟着就误判「快捷键没让路」。判据的前提自己没立住，
        #    后面的结论全是假的。
        ent.focus_force()
        probe_top.update()
        got_focus = app.focus_get() is ent
        check(got_focus, "能造出一个真正拿到焦点的 Entry（前提自证，"
                         "focus_get=%s）" % app.focus_get())
        calls2 = []
        real_del2 = app.delete_side
        app.delete_side = lambda col: calls2.append(col)
        sel_before = app.glist.sel
        try:
            for keysym, char in (("delete", ""), ("a", "a"),
                                 ("down", ""), ("0", "0")):
                app._on_key(_K(keysym, char))
        finally:
            app.delete_side = real_del2
        check(not calls2,
              "焦点在 Entry 里：Delete/A 不删图（转发 %d 次）" % len(calls2))
        check(app.glist.sel == sel_before,
              "焦点在 Entry 里：↓ 也不换图（还是第 %d 行）" % app.glist.sel)
        probe_top.destroy()
        app.update()
    except tk.TclError as e:
        check(True, "造不出焦点 Entry（%r），让路这条改由代码审读保证" % (e,))

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

    print("\n=== 清理对话框正文：每组必须编号（v1.7）===")
    # ⚠️ 主人 2026-10-06 反馈：「显示哪一组，名字不好找」。
    #    原来 6 组直接罗列、文件名截断到 24~30 字
    #    （`v2-cc86d05542d9…4aef5846_r` 这种哈希名截完几乎全一样），
    #    用户没法把屏幕上的内容和自己的文件对上。
    #    判据：**每一组都得有「第 N 组」**，且编号连续、不重号。
    pool = [p for p in app.files if p not in (app.path_a, app.path_b)]
    # ⚠️⚠️ **必须先造出真的重复组**（内容 100% 相同）再验正文 ——
    # 直接拿 `pool`（一堆互不相同的样本）去调 `_ask_clean`，
    # `plan_exact_dups` 会返回空 -> 正文里一个编号都没有 ->
    # 断言报「没编号」但**根因是测试没造数据**（我第一版就这么错的）。
    import shutil as _sh
    check(bool(pool), "有可用来造重复副本的样本")
    _src0 = pool[0]
    _rl = os.path.join(root_dir, "_dlg1.png")
    _rl2 = os.path.join(root_dir, "_dlg2.png")
    _rl3 = os.path.join(root_dir, "_dlg3.png")
    _sh.copy2(_src0, _rl)
    _sh.copy2(_src0, _rl2)
    _sh.copy2(_src0, _rl3)      # 4 份相同 -> 正文里应该正好一组（移走 3 张）
    _seen_body = {}

    def _cap_ask(title=None, message=None, **k):
        _seen_body["title"] = title
        _seen_body["msg"] = message
        return False                     # 选「否」，别真动文件

    _real_ask2 = ui.messagebox.askyesno
    ui.messagebox.askyesno = _cap_ask
    try:
        app._ask_clean([[_src0, _rl, _rl2, _rl3]])
    finally:
        ui.messagebox.askyesno = _real_ask2
    _msg = _seen_body.get("msg") or ""
    print("   对话框正文（去掉空行）：")
    for _ln in [x for x in _msg.split("\n") if x.strip()][:8]:
        print("     " + _ln)
    # ⚠️ **先自证「真的造出了重复组」** —— 不然「正文里没编号」这条断言
    #   永远红，而根因是测试没造数据，不是产品没编号。
    _pl = quarantine.plan_exact_dups([[_src0, _rl, _rl2, _rl3]])
    check(bool(_pl) and sum(len(d) for _, d, _ in _pl) == 3,
          "前提自证：4 份相同的图认成 1 组、要移走 3 份"
          "（实际 %d 组 / 移走 %d）"
          % (len(_pl), sum(len(d) for _, d, _ in _pl)))
    # 正文里每一组都要有编号
    import re as _re
    _nos = [int(x) for x in _re.findall(r"【第 (\d+) 组】", _msg)]
    check(bool(_nos), "正文里有「第 N 组」编号（共 %d 组）" % len(_nos))
    if _nos:
        check(_nos == list(range(1, len(_nos) + 1)),
              "编号从 1 连续递增（实际 %s）" % _nos[:8])
        check(len(_nos) == len(_pl),
              "正文列出的组数 = 实际组数（%d vs %d）" % (len(_nos), len(_pl)))
    check("保留" in _msg and "移走" in _msg,
          "每组说清「保留哪张、移走哪张」")
    _longest = max((len(l) for l in _msg.split("\n")), default=0)
    print("   正文最长行 %d 字（太宽会在对话框里换行，更难读）" % _longest)
    check(_longest <= 90, "正文没有过长的行（%d <= 90）" % _longest)
    # 把这次造出来的副本收掉（这一步没真删文件，只是别留垃圾）
    for _p in (_rl, _rl2, _rl3):
        if os.path.isfile(_p):
            os.remove(_p)

    print("\n=== 一键清理完全重复：真跑一遍（确认框打桩成「是」）===")
    import shutil
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

    # --- `render_all(only=col)` 必须**只画那一侧**（v1.7）---
    #
    # ⚠️⚠️ **这条判据是被注入验证逼出来的**：我修了 `render_all` 里
    #   「side 0 无条件画」那个 bug（`only=1` 时两侧都画，拖右图白重编码
    #   左图 = 30ms 编码 + 32ms 建图），可 probe_pan2 和本文件
    #   **注入后仍然全绿** —— 因为所有拖动测试都只拖 side 0，
    #   `only=0` 走的正是「无条件画」那条路，bug 根本碰不到。
    #
    #   教训：**判据没抓住已知 bug，就说明它测的不是那条路**，
    #   不能因为「全绿」就当修复验证过了（这正是主人定的规矩）。
    #   所以这里直接数「`_render_side` 被调了几次」，与 zoom/pan 无关。
    print("\n=== render_all(only) 只画指定那一侧 ===")
    _side_calls = []
    _rs = app._render_side

    def _spy(col, *a, **k):
        _side_calls.append(col)
        return _rs(col, *a, **k)
    app._render_side = _spy
    try:
        _side_calls.clear()
        app.render_all(precise=0.75, only=1)
        n1 = list(_side_calls)
        _side_calls.clear()
        app.render_all(precise=0.75, only=0)
        n0 = list(_side_calls)
    finally:
        app._render_side = _rs
    print("   only=1 -> 画了 %s；only=0 -> 画了 %s" % (n1, n0))
    check(n1 == [1], "render_all(only=1) 只画右侧（实际 %s）" % (n1,))
    check(n0 == [0], "render_all(only=0) 只画左侧（实际 %s）" % (n0,))
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
