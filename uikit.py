# -*- coding: utf-8 -*-
"""界面小件：配色 + 圆角按钮 + 圆角卡片。零依赖。

Tkinter 给控件做不了圆角，所以这套东西自己画：

  * 先画到一块 **BGRA 像素缓冲**（`bytearray`，见 `Surface`），再用现成的
    PNG 编码器转成 `tk.PhotoImage`。比 `PhotoImage.put` 逐条带填快，而且
    **角上的抗锯齿可以直接按「该像素原本的颜色」混合** —— 不需要手工传底色，
    也就不会出现「圆角外那一圈涂错色」的经典问题。
  * 文字交给 Tk 的 `create_text`（用系统字体渲染，比自己画字好得多）。
  * 按钮 = 圆角底图（竖向渐变 + 顶部高光 + 描边）+ 居中文字；三态各一张，
    鼠标进出/按下时换图。

配色是**白底 + 干净通透的天蓝强调**：页面底纯白（2026-10-05 主人定稿），
强调色色相 200°（不是靛蓝的 215°），圆角一律偏大，描边淡但有彩度，
层次全靠描边和阴影表态。
改色只改 `Palette`，别处一律从它派生 —— 具体规矩见 `Palette` 的 docstring
（里面记着「白字天花板」和「浅 ≠ 去饱和」两次踩坑）。

⚠️ **DPI 缩放**：主程序启动时会开进程 DPI 感知，之后「逻辑像素 == 物理像素」，
所以写死的像素值（行高 54、缩略图 40、内边距 14…）在 150% 屏上会缩成原来的
2/3。这些值一律过 `sc()` 换算；**字号不用管**（用的是点，Tk 按 `tk scaling`
自己换算，开感知后自动变大且锐利）。

调试小技巧：`Surface` 可以直接导出 PNG 用 Read 工具看图 ——
本环境截不了屏，这是唯一能「肉眼验收」视觉的办法（见 `uishot.py`）。
"""

from __future__ import annotations

import math
import sys
import tkinter as tk
from tkinter import font as tkfont

import thumbs

__all__ = ["Palette", "Surface", "pick_family", "ui_font", "rounded_image",
           "rounded_photo", "round_off_corners", "rounded_png", "fit_photo",
           "draw_rounded", "RoundButton", "Pill", "Card", "NiceList", "mix",
           "set_scale", "scale", "sc", "enable_dpi_awareness"]


# ---------------------------------------------------------------------------
# 配色
# ---------------------------------------------------------------------------

class Palette:
    """白底 + 低饱和度「清新通透」的**浅天蓝**配色。

    取色原则（改配色时请守住这几条）：
      * **页面底是纯白**（`PAGE=#ffffff`）—— 2026-10-05 主人定稿
        「背景的蓝色改回白色」。层次不靠底色反差，靠描边（BORDER）+ 阴影。
      * **主色是「天蓝」而不是「靛蓝」**：色相落在 **201°** 上下（`#7cc8f1`）。
        这是这几轮改动的**核心** —— 最早的主色 `#3b74c6` 色相 **215°**，
        它的绿通道太低，看过去偏紫、偏靛，就是用户说的「不够天蓝」。
        天蓝的判据是**绿通道要抬起来**（青味），不是简单「加蓝」。
      * **正文用带蓝的深灰**（`#193752`）而不是石板灰或纯黑，整屏色调统一。
      * 深字压在主蓝上的对比度要 ≥ 4.5（三个状态、渐变上下端都算），
        其余文字与背景的对比度都不低于 3.0 —— **好看不能拿看不清换**。
        具体数值见 README「配色与对比度」那张表。

    下表括号里是**实测对比度**，不是估的 —— 换色后请重跑
    `test_ui.py` 核对（它会逐条算，见 `test_palette`）。

    ⚠️⚠️ **两次踩坑的最终结论（2026-10-05，改了三轮才走到这儿）**：

    **坑一：白字天花板。** 主色原本是「实心深蓝 + 白字」，白字必须 ≥4.0，
    于是主色的亮度被**钉死**在中深蓝（实测最多只能比上一版亮 5%）：

        #1179bd  本色 4.67 / 上渐 4.23   ← 可用的最后一档
        #127dc2  本色 4.43 / 上渐 4.06   ← 贴线
        #1380c4  本色 4.27 / 上渐 3.92   ← 白字开始看不清

    **坑二：把「浅」做成了「去饱和」，结果是一片灰白。** 用户第三次说
    「颜色灰不溜秋的好恶心」。量出来一看：页面底彩度只有 **21**（纯白是 0），
    蓝色被我洗没了 —— **「浅」不等于「淡到没颜色」，那是灰；真正的清新通透
    是「颜色干净 + 亮度高」**。现在页面底彩度 49。

    最终解法：**主色换成「亮天蓝底 + 深字」**（`PRIMARY` 做底、`PRIMARY_INK` 做字）。
    白字约束**根本不存在了**，底色一路亮到 `#4fb6ec`（第四轮）→ `#7cc8f1`（第五轮，
    主人：「这个颜色太重了」）；而且深字压亮底的对比度有 **8.2+**，比白字的 4.2~4.7
    还更好读。

    → 以后想要「更亮」：改的是**底色 + 用深字**，别再拿白字去压中等深度的蓝。
    """

    PAGE = "#ffffff"          # 页面底：**纯白**（2026-10-05 主人定稿：背景的蓝改回白色）
    PAGE_D = "#e3f1fc"        # 凹陷/禁用底：极浅天蓝（白页面上做「凹进去」的感觉）
    CARD = "#ffffff"          # 卡片也是白 —— 与页面的区分靠描边（BORDER）+ 阴影
    CARD_SOFT = "#ecf7ff"
    CARD_TINT = "#c9e6fd"
    BORDER = "#aed7f7"
    BORDER_HI = "#74b9ea"     # 描边也加彩度（与白底 2.13）
    LINE = "#e4f3fe"

    TEXT = "#193752"          # 带蓝的深灰（12.3 / 9.7）
    TEXT_2 = "#2b6395"        # 次要字（6.3 / 5.0）
    TEXT_3 = "#4a83a9"        # 提示/次要字（压白底 4.7；页面改白后它不再是最紧的一档）

    # ⚠️ 主色是「亮天蓝底 + 深字」，白字约束根本不存在，所以可以真正做到「亮」。
    #    2026-10-05 主人看实物说「这个颜色太重了」→ 再提亮一档：#4fb6ec → #7cc8f1
    #    （色相 201° 不变、彩度 117、与白底 1.69）。边界靠深描边（PRIMARY_D 系），
    #    字压底 8.2，比之前还清楚。
    PRIMARY = "#7cc8f1"       # 亮天蓝：CTA 的底（与白底 1.69，比上一版轻一档）
    PRIMARY_D = "#0b5f96"     # 深一档：soft 按钮的**字色**（白底 7.0）
    PRIMARY_L = "#a3dcf7"
    PRIMARY_INK = "#08283f"   # 压在亮天蓝底上的深字（压 CTA 底 7.5~8.2）
    ON_PRIMARY = "#ffffff"

    TEAL = "#2b7f9c"          # 次要强调：偏青的蓝（白字压得住，界面上暂未用）
    TEAL_D = "#1d647f"
    WARN = "#c8894a"          # 琥珀（提示）
    WARN_D = "#9c6530"
    DANGER = "#bd5a53"        # 砖红（删除类操作用）
    DANGER_D = "#96403a"
    POOR = "#a9723c"          # 低质量强调（比 WARN 更沉）
    MATCH = "#bd5a53"         # 匹配区域框（已按使用者要求不再画，留着备用）

    SHADOW = "#d6e8f6"        # 阴影也调浅了：太重会显得「压着一块」，不通透
    ROW_SEL = "#c4e4fc"


# ---------------------------------------------------------------------------
# 像素缩放：进程开了 DPI 感知之后，逻辑像素 == 物理像素，
# 所以「330 宽的侧栏」在 150% 屏上会缩成原来物理尺寸的 2/3。
# 所有写死的像素值都过一遍 sc()，字不用管（用的是点，Tk 自己换算）。
# ---------------------------------------------------------------------------

_SCALE = 1.0


def set_scale(s) -> float:
    """由主程序在拿到真实 DPI 后调用一次（`dpi / 96`）。"""
    global _SCALE
    try:
        s = float(s)
    except (TypeError, ValueError):
        return _SCALE
    _SCALE = max(0.75, min(4.0, s))
    return _SCALE


def scale() -> float:
    return _SCALE


def sc(v):
    """把设计稿上的像素值换算成当前 DPI 下的像素值。"""
    return int(round(float(v) * _SCALE))


def enable_dpi_awareness() -> int:
    """开进程级 DPI 感知，返回真实系统 DPI（非 Windows 或失败时返回 96）。

    **为什么一定要开**：不开的话进程是 `unaware`，Windows 会把整个窗口的位图
    按缩放比拉伸 —— 150% 屏上文字先按 96 DPI 渲染成 13px，再被放大到 19.5px，
    边缘全是插值糊的。实测（150% 屏）：

        开之前：winfo_fpixels('1i')=95.9，屏幕报 1707x960，10pt 雅黑 13px → 拉伸
        开之后：winfo_fpixels('1i')=143.9，屏幕报 2560x1440，10pt 雅黑 27px → 直接渲染

    ⚠️ 必须在**创建任何窗口之前**调用，否则无效（窗口建好后再改，Tk 不认）。
    调用之后所有写死的像素值都要过 `sc()`，见本模块开头那段说明。
    """
    if not sys.platform.startswith("win"):
        return 96
    try:
        import ctypes
        user32 = ctypes.windll.user32
        shcore = ctypes.windll.shcore
    except Exception:
        return 96

    # 三级降级：per-monitor v2 最好（多屏不同缩放也不会被系统拉伸），
    # 老系统退到 per-monitor，再退到 system aware。
    done = False
    try:
        user32.SetProcessDpiAwarenessContext.restype = ctypes.c_bool
        done = bool(user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)))
    except Exception:
        done = False
    if not done:
        try:
            shcore.SetProcessDpiAwareness(2)
            done = True
        except Exception:
            try:
                user32.SetProcessDPIAware()
            except Exception:
                pass

    try:
        return int(user32.GetDpiForSystem())
    except Exception:
        pass
    try:
        hdc = user32.GetDC(None)
        dpi = int(ctypes.windll.gdi32.GetDeviceCaps(hdc, 88))     # LOGPIXELSX
        user32.ReleaseDC(None, hdc)
        return dpi or 96
    except Exception:
        return 96



def _hx(s: str):
    s = s.lstrip("#")
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


def _clamp8(v):
    return 0 if v < 0 else (255 if v > 255 else int(v))


def _xh(t) -> str:
    return "#%02x%02x%02x" % (_clamp8(t[0]), _clamp8(t[1]), _clamp8(t[2]))


def mix(c1, c2, t: float) -> str:
    """两色插值。c1/c2 可以是 16 进制串，也可以是 (r,g,b)，返回 16 进制串。"""
    a = _hx(c1) if isinstance(c1, str) else c1
    b = _hx(c2) if isinstance(c2, str) else c2
    return _xh(tuple(a[i] + (b[i] - a[i]) * t for i in range(3)))


# ---------------------------------------------------------------------------
# 像素缓冲
# ---------------------------------------------------------------------------

class Surface:
    """一块 BGRA 像素缓冲 + 圆角矩形绘制。坐标一律 0 起始，右下角**包含**。"""

    def __init__(self, w: int, h: int, bg=None):
        self.w = max(1, int(w))
        self.h = max(1, int(h))
        rgb = _hx(bg or Palette.PAGE)
        self.buf = bytearray(bytes((rgb[2], rgb[1], rgb[0], 255))
                             * (self.w * self.h))

    @classmethod
    def from_bgra(cls, w, h, bgra):
        """直接接管一段现成的 BGRA 像素（和 `winimg` 的输出同序，不用转换）。"""
        s = cls.__new__(cls)
        s.w, s.h = int(w), int(h)
        s.buf = bytearray(bgra[:int(w) * int(h) * 4])
        return s

    # -- 基本操作 --------------------------------------------------------
    def px(self, x, y):
        b = (y * self.w + x) * 4
        return (self.buf[b + 2], self.buf[b + 1], self.buf[b])

    def set_px(self, x, y, rgb):
        if x < 0 or y < 0 or x >= self.w or y >= self.h:
            return
        b = (y * self.w + x) * 4
        self.buf[b] = _clamp8(rgb[2])
        self.buf[b + 1] = _clamp8(rgb[1])
        self.buf[b + 2] = _clamp8(rgb[0])

    def blend_px(self, x, y, rgb, t: float):
        if t <= 0.0 or x < 0 or y < 0 or x >= self.w or y >= self.h:
            return
        if t >= 1.0:
            self.set_px(x, y, rgb)
            return
        cur = self.px(x, y)
        self.set_px(x, y, (cur[0] + (rgb[0] - cur[0]) * t,
                           cur[1] + (rgb[1] - cur[1]) * t,
                           cur[2] + (rgb[2] - cur[2]) * t))

    def fill_rect(self, x0, y0, x1, y1, rgb):
        """逐行整段赋值 —— 切片赋值走 C 层，比逐像素快得多。"""
        x0 = max(0, int(x0))
        y0 = max(0, int(y0))
        x1 = min(self.w - 1, int(x1))
        y1 = min(self.h - 1, int(y1))
        if x1 < x0 or y1 < y0:
            return
        seg = bytes((_clamp8(rgb[2]), _clamp8(rgb[1]), _clamp8(rgb[0]), 255)) \
            * (x1 - x0 + 1)
        n = (x1 - x0 + 1) * 4
        for y in range(y0, y1 + 1):
            b = (y * self.w + x0) * 4
            self.buf[b:b + n] = seg

    # -- 圆角 ------------------------------------------------------------
    def fill_round_rect(self, x0, y0, x1, y1, r, rgb, aa=True, ss=4):
        x0, y0 = int(x0), int(y0)
        x1, y1 = int(x1), int(y1)
        if x1 < x0 or y1 < y0:
            return
        r = int(max(0, min(r, (x1 - x0 + 1) // 2, (y1 - y0 + 1) // 2)))
        if r == 0:
            self.fill_rect(x0, y0, x1, y1, rgb)
            return
        for y in range(y0, y1 + 1):
            a, b = _row_span(x0, x1, y0, y1, r, y)
            if b >= a:
                self.fill_rect(a, y, b, y, rgb)
        if not aa:
            return
        # 抗锯齿：只重画 4 个 r×r 的角框，按覆盖率把新色混到**已有像素**上。
        # ⚠️ 右/下侧角框的圆心要按「距右边界 r-0.5」算：像素 x1 覆盖 [x1, x1+1)，
        # 所以右边界是 x1+1，圆心 = (x1+1) - (r-0.5) = x1-r+1.5。
        # 写成 x1-r+0.5 的话左右能差出 1 个像素（圆角看着一边胖一边瘦）。
        for bx0, by0, ccx, ccy in (
                (x0, y0, x0 + r - 0.5, y0 + r - 0.5),
                (x1 - r + 1, y0, x1 - r + 1.5, y0 + r - 0.5),
                (x0, y1 - r + 1, x0 + r - 0.5, y1 - r + 1.5),
                (x1 - r + 1, y1 - r + 1, x1 - r + 1.5, y1 - r + 1.5)):
            for py in range(by0, by0 + r):
                for px in range(bx0, bx0 + r):
                    self.blend_px(px, py, rgb,
                                  _circle_cov(px, py, ccx, ccy, r, ss))

    def png(self) -> bytes:
        return thumbs.bgra_to_png(self.w, self.h, bytes(self.buf))


def _row_span(x0, x1, y0, y1, r, y):
    """圆角矩形在第 y 行上 x 的跨度（含两端）。"""
    if r <= 0:
        return x0, x1
    if y < y0 + r:
        dy = r - 0.5 - (y - y0)
    elif y > y1 - r:
        dy = r - 0.5 - (y1 - y)
    else:
        return x0, x1
    dx = r - math.sqrt(max(0.0, r * r - dy * dy))
    return x0 + int(round(dx)), x1 - int(round(dx))


def _circle_cov(px, py, cx, cy, r, ss):
    """像素 (px,py) 落在圆心 (cx,cy) 半径 r 的圆内的面积占比（超采样）。"""
    if r <= 0:
        return 0.0
    step = 1.0 / ss
    off = step / 2.0
    hit = 0
    rr = r * r
    for i in range(ss):
        dy = (py + off + i * step) - cy
        base = dy * dy
        for j in range(ss):
            dx = (px + off + j * step) - cx
            if dx * dx + base <= rr:
                hit += 1
    return hit / float(ss * ss)


def draw_rounded(surf: Surface, x, y, w, h, r, *, fill=None, grad=None,
                 border=None, hi=None, shadow=None, sd=3, inset=2, shade=None):
    """在 surf 的 (x, y) 画一个圆角矩形。

    grad=(上色, 下色) 竖向渐变；hi 顶部 1px 高光；shadow 阴影色，
    会在下方多出 sd 像素（`shade` 是底部内阴影色，用于更立体的观感）。
    """
    fill = fill or Palette.CARD
    sd, inset = sc(sd), sc(inset)
    w, h = max(4, int(w)), max(4, int(h))
    x0, y0 = x + inset, y + inset
    x1 = x + w - 1 - inset
    y1 = y + h - 1 - inset - (sd if shadow else 0)
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None

    if shadow:
        for extra, alpha in ((sd + 1, 0.16), (sd, 0.30)):
            surf.fill_round_rect(x0, y0, x1, min(y + h - 1, y1 + extra), r,
                                 _hx(mix(Palette.PAGE, shadow, alpha)), ss=3)

    edge = border or Palette.BORDER
    surf.fill_round_rect(x0, y0, x1, y1, r, _hx(edge))

    # 描边宽度也跟着 DPI 走：150% 屏上 1px 的边看着比 100% 屏细一半。
    bw = max(1, sc(1))
    ix0, iy0, ix1, iy1 = x0 + bw, y0 + bw, x1 - bw, y1 - bw
    ir = max(0, r - bw)
    if grad:
        top, bot = _hx(grad[0]), _hx(grad[1])
        span = max(1, iy1 - iy0)
        for yy in range(iy0, iy1 + 1):
            t = (yy - iy0) / float(span)
            c = top if yy == iy0 else tuple(top[i] + (bot[i] - top[i]) * t
                                            for i in range(3))
            if shade and yy == iy1:
                c = _hx(mix(_xh(c), shade, 0.35))
            a, b = _row_span(ix0, ix1, iy0, iy1, ir, yy)
            if b >= a:
                surf.fill_rect(a, yy, b, yy, c)
    else:
        surf.fill_round_rect(ix0, iy0, ix1, iy1, ir, _hx(fill), ss=3)

    if hi:
        a, b = _row_span(ix0, ix1, iy0, iy1, ir, iy0)
        if b >= a:
            surf.fill_rect(a, iy0, b, iy0, _hx(hi))
    return (x0, y0, x1, y1)


def rounded_photo(root, w, h, r, **kw):
    """造一张独立的圆角图（tk.PhotoImage）。"""
    surf = Surface(w, h, kw.pop("page", None))
    draw_rounded(surf, 0, 0, w, h, r, **kw)
    return tk.PhotoImage(data=surf.png(), master=root)


rounded_image = rounded_photo          # 兼容旧名字


def round_off_corners(surf: Surface, r, page=None, ss=4):
    """把 `surf` 的四个角削圆 —— 圆角外的像素混成 `page` 底色。

    和 `draw_rounded` 的区别：这个**不动内容**，只改四个角，所以可以直接
    套在系统解码出来的照片上（缩略图、大预览都靠它变圆角）。
    代价只有 4×r² 个像素，跟图片本身多大无关。
    """
    r = int(r)
    if r <= 0:
        return surf
    w, h = surf.w, surf.h
    r = min(r, w // 2, h // 2)
    if r <= 0:
        return surf
    pb = _hx(page or Palette.PAGE)
    for bx0, by0, ccx, ccy in (
            (0, 0, r - 0.5, r - 0.5),
            (w - r, 0, w - r + 0.5, r - 0.5),
            (0, h - r, r - 0.5, h - r + 0.5),
            (w - r, h - r, w - r + 0.5, h - r + 0.5)):
        for py in range(by0, min(h, by0 + r)):
            for px in range(bx0, min(w, bx0 + r)):
                cov = _circle_cov(px, py, ccx, ccy, r, ss)
                if cov >= 0.999:
                    continue
                cur = surf.px(px, py)
                k = 1.0 - cov
                surf.set_px(px, py, (cur[0] + (pb[0] - cur[0]) * k,
                                     cur[1] + (pb[1] - cur[1]) * k,
                                     cur[2] + (pb[2] - cur[2]) * k))
    return surf


def rounded_png(w, h, bgra, r, page=None):
    """BGRA 照片 -> 四角削圆的 PNG 字节。"""
    return round_off_corners(Surface.from_bgra(w, h, bgra), r, page).png()


def fit_img(img, cw, ch, max_zoom=4.0):
    """把**已经存在的** Tk 图按容器缩放，返回 (图, 显示宽, 显示高)。

    和 `fit_photo` 的区别：这里手上只有 Tk 图对象（拿不回像素），所以只能
    用 C 层的 `zoom` / `subsample`（都是整数倍）。用途是**占位** ——
    高清图还在后台编码时，先把上一档的图/缩略图按正确尺寸顶上去，
    界面立刻有画面，位置大小都对（糊一点而已）。
    """
    if img is None:
        return None, 0, 0
    w, h = img.width(), img.height()
    cw, ch = int(max(16, cw)), int(max(16, ch))
    s = min(cw / float(max(1, w)), ch / float(max(1, h)))
    s = max(1e-3, min(s, float(max_zoom)))
    if 0.985 <= s <= 1.015:
        return img, w, h
    out = img
    if s > 1.0:
        k = max(1, int(s + 0.5))
        while k > 1 and (w * k > cw or h * k > ch):
            k -= 1
        out = out.zoom(k)
        m = int(round(k / s))
        if m > 1:
            out = out.subsample(m)
    else:
        m = max(1, int(round(1.0 / s)))
        out = out.subsample(m)
        while (out.width() > cw or out.height() > ch) and m < 64:
            m += 1
            out = img.subsample(m)
    return out, out.width(), out.height()


def fit_ppm(w, h, bgra, radius=0, page=None):
    """`fit_photo` 的前半段：**削圆角 + 编码成给 Tk 的字节**，返回 (字节, 宽, 高)。

    单独拆出来是为了**能在后台线程跑**：这一段是纯计算（实测 3600×2400
    要 145ms，是整个渲染里最贵的一环），放在 UI 线程就是「切一组卡半秒」。
    剩下来的 `PhotoImage(...)` + 缩放必须回主线程（Tk 只认主线程，而且
    `zoom`/`subsample` 动的是 Tk 自己的图对象）。
    """
    if radius:
        bgra = bytes(round_off_corners(
            Surface.from_bgra(w, h, bgra), radius, page).buf)
    # PPM 快速通道（无 zlib 压缩）：PNG 编码同尺寸要 212ms，PPM 只要 145ms。
    # 圆角已经混进 page 底色，不需要 alpha。
    return thumbs.bgra_to_ppm(w, h, bgra), w, h


def fit_photo(root, w, h, bgra, cw, ch, max_zoom=4.0, radius=0, page=None):
    """把一张解码好的图缩放着用，返回 (PhotoImage, 显示宽, 显示高, 比例)。

    * 系统解码器**可以放大**，但要显式带 `SIIGBF_SCALEUP`；不带的话它只缩不放
      （实测：请求长边 1200、源图只有 120，拿回来还是 120）。所以正常路径下
      这里几乎不需要缩放 —— 解码时就已经按容器尺寸要好了。
    * 这里的 `zoom` / `subsample` 只是兜底（解码器没给出足够大的图时），
      两者都是 C 层整数倍操作，够快。`max_zoom` 是保险：200×150 的小图
      扔进 900 宽的格子，真拉 6 倍就是一屏马赛克，限制在 4 倍以内。
    * `radius` 不是 0 时会先把四个角削圆（圆角外混成 `page` 底色）再缩放 ——
      只动 4×r² 个像素，跟图多大无关。
    """
    try:
        ppm, w, h = fit_ppm(w, h, bgra, radius, page)
        img = tk.PhotoImage(data=ppm, master=root)
    except Exception:
        return None, 0, 0, 1.0
    cw, ch = int(cw), int(ch)
    if cw < 16 or ch < 16:
        return img, w, h, 1.0
    s = min(cw / float(w), ch / float(h))
    s = max(1e-3, min(s, float(max_zoom)))
    if 0.985 <= s <= 1.015:
        return img, w, h, 1.0
    if s > 1.0:
        k = max(1, int(s + 0.5))
        # Tk 的 `zoom` 只能整数倍，四舍五入会把图撑出容器：
        # s=1.58 → k=2 → 大了 26%，超出的部分被画布裁掉。
        # 「只看匹配区域」就是这么被裁掉上下两截、看不到完整匹配区的。
        # 退到装得下为止 —— 宁可小一点留白，跟下面缩小分支同一个原则。
        # （s > 1 时必然 w < cw 且 h < ch，所以 k 至少能取到 1，一定装得下。）
        while k > 1 and (w * k > cw or h * k > ch):
            k -= 1
        out = img.zoom(k)
        m = int(round(k / s))
        if m > 1:
            out = out.subsample(m)
    else:
        m = max(1, int(round(1.0 / s)))
        out = img.subsample(m)
        # 兜底：万一解码器给回来的图远大于容器（例如它不支持 SIIGBF_SCALEUP，
        # 只能把原图整张塞过来），取整出来的倍数可能仍然不够。这时宁可再缩
        # 一档 —— 稍微小一点只是留白，溢出的代价是红框被裁到画面外，
        # 那才叫看不出所以然。容差给 2px：整数取整带来的 1px 误差不算溢出，
        # 否则「只超 8 像素 → 直接缩成一半」这种蠢事就会发生。
        if out.width() > cw + 2 or out.height() > ch + 2:
            m2 = max(1, int(math.ceil(max(w / float(cw + 2),
                                           h / float(ch + 2)))))
            if m2 > m:
                out = img.subsample(m2)
    return out, out.width(), out.height(), out.width() / float(w)


# ---------------------------------------------------------------------------
# 字体
# ---------------------------------------------------------------------------

_FAMILY = None


def pick_family(root) -> str:
    global _FAMILY
    if _FAMILY:
        return _FAMILY
    try:
        have = set(tkfont.families(root))
    except tk.TclError:
        have = set()
    for f in ("Microsoft YaHei UI", "Microsoft YaHei", "微软雅黑",
              "PingFang SC", "Noto Sans CJK SC", "SimHei"):
        if f in have:
            _FAMILY = f
            return f
    _FAMILY = tkfont.nametofont("TkDefaultFont").cget("family")
    return _FAMILY


def ui_font(root, size=10, bold=False):
    """用**点**作单位：Tk 会按屏幕 DPI 换算，天生适配高分屏。"""
    return tkfont.Font(root=root, family=pick_family(root), size=size,
                       weight="bold" if bold else "normal")


# ---------------------------------------------------------------------------
# 按钮外观
# ---------------------------------------------------------------------------

def _spec(kind, state):
    """一种 kind 的四态配色：(渐变上色, 渐变下色, 描边, 文字)。

    颜色**全部从 Palette 推**（不再写死十六进制）—— 早先这里散着一堆
    `#a3c4d3`、`#e2ecef` 这种手工调的色，换一次主色就得逐个找出来改，
    漏一个就会出现「按钮是蓝的、悬停变青」的错位。
    """
    P = Palette
    on = P.ON_PRIMARY
    white = "#ffffff"
    black = "#000000"

    def pair(base, lt, dk):
        """(上渐, 下渐) —— lt/dk 是往白/黑混的比例。"""
        return mix(base, white, lt), mix(base, black, dk)

    table = {
        # ⚠️ 白色文字压在按钮上，所以渐变**只能往暗里做**。
        # 早先是「悬停变亮」，白字压上去对比度掉到 2.6，等于看不清 ——
        # 现在 hover / press 都是加深（更饱和），既读得出「按下去/指上来」，
        # 白字也一直有 4.2 以上。实测值见 README 的对比度表。
        # ⚠️ primary = 主行动按钮（「开始扫描」）。**2026-10-05 从「实心深蓝 + 白字」
        #    改成「亮天蓝底 + 深字」** —— 用户第三次说「灰不溜秋、为什么不改」之后。
        #    原因：白字压主色必须 ≥4.0，把主色死死钉在中深蓝（最多只能比上一版亮 5%），
        #    那支蓝怎么看都闷。换成**深字压亮底**，约束直接消失，底色可以亮到 `#4fb6ec`。
        #    现在三态的深字压底 6.7~7.5，比原来白字的 4.2~4.7 还**更好读**。
        "primary": {
            "normal": (mix(P.PRIMARY, white, 0.14), P.PRIMARY,
                       mix(P.PRIMARY_D, black, 0.18), P.PRIMARY_INK),
            "hover": (P.PRIMARY, mix(P.PRIMARY, black, 0.08),
                      mix(P.PRIMARY_D, black, 0.30), P.PRIMARY_INK),
            "press": (mix(P.PRIMARY, black, 0.05), mix(P.PRIMARY, black, 0.10),
                      mix(P.PRIMARY_D, black, 0.42), P.PRIMARY_INK),
            "disabled": (P.PAGE_D, P.PAGE, P.BORDER, P.TEXT_3),
        },
        "soft": {
            "normal": (P.CARD_TINT, mix(P.CARD_TINT, P.PRIMARY, 0.10), P.BORDER, P.PRIMARY_D),
            "hover": (mix(P.CARD_TINT, P.PRIMARY, 0.06), mix(P.CARD_TINT, P.PRIMARY, 0.20), P.BORDER_HI, P.PRIMARY_D),
            "press": (mix(P.CARD_TINT, P.PRIMARY, 0.16), mix(P.CARD_TINT, P.PRIMARY, 0.26), P.BORDER_HI, P.PRIMARY_D),
            "disabled": (P.CARD_SOFT, P.PAGE, P.BORDER, P.TEXT_3),
        },
        # ⚠️ 选中态（开关/分段控件的「开」）—— 单独一种，别拿 primary 顶。
        #
        # 早先「选中的那个」直接用 `primary`（实心深蓝 + 白字）。顶栏上三处分段控件
        # （含子文件夹 / 灵敏度 / 视图切换）外加「只看匹配区域」，一屏就有四个实心深块，
        # 观感很沉 —— 用户连着提了两次「浅一些、亮一些、通透一些」，
        # 最后一句「已选中的按钮颜色同理」，说的就是这个。
        #
        # 但**换成浅底必须解决一件事：跟未选中（ghost：白底 + 极淡描边）分得开**，
        # 否则用户看不出哪个是开着的。所以这一版靠**三样一起**表态：
        #   ① 底色明显更蓝（与白底 1.47~2.07，而 ghost 是纯白）
        #   ② 描边换成**主色**（而不是 ghost 那种近乎看不见的淡边）
        #   ③ 字用正文级深色（`TEXT`），读起来比 ghost 的蓝字更实
        # 实测（README 配色表）：底 0.64~0.72、字压底 6.3~6.9、描边压底 3.0 上下。
        #
        # ⚠️ 字色为什么不用 `PRIMARY_D`（别的按钮的字色）：
        #    实测深蓝字压在这么浅的底上只有 **3.7~4.4**，不到 4.5 —— 读不清。
        #    换 `TEXT` 立刻有 6.9。**别顺手改回去**，那是拿可读性换「看起来统一」。
        # ⚠️ 描边**不能三态都用 PRIMARY**：hover / press 的底会加深，主色描边压在上面
        #    只剩 2.51 / 2.21，描边就糊了（实测）。所以跟着**逐态加深**。
        # ⚠️ 混白比例跟着 PRIMARY 的亮度走：2026-10-05 主色提到 #7cc8f1 后，
        #    混白 0.46 的选中底与白底只剩 1.37（贴线）→ 降成 0.34 / 0.26 / 0.18
        #    （与白底 1.49 / 1.57 / 1.63）。
        "on": {
            "normal": (mix(P.PRIMARY, white, 0.34), mix(P.PRIMARY, white, 0.26),
                       mix(P.PRIMARY_D, white, 0.04), P.TEXT),
            "hover": (mix(P.PRIMARY, white, 0.26), mix(P.PRIMARY, white, 0.18),
                      P.PRIMARY_D, P.TEXT),
            "press": (mix(P.PRIMARY, white, 0.18), mix(P.PRIMARY, white, 0.10),
                      mix(P.PRIMARY_D, black, 0.10), P.TEXT),
            "disabled": (P.PAGE_D, P.PAGE, P.BORDER, P.TEXT_3),
        },
        "teal": {
            "normal": (pair(P.TEAL, 0.06, 0.00) + (P.TEAL_D, on)),
            "hover": (pair(P.TEAL, 0.00, 0.10) + (P.TEAL_D, on)),
            "press": (pair(P.TEAL_D, 0.00, 0.16) + (mix(P.TEAL_D, black, 0.28), on)),
            "disabled": (P.PAGE_D, P.PAGE, P.BORDER, P.TEXT_3),
        },
        "danger": {
            "normal": (pair(P.DANGER, 0.06, 0.00) + (P.DANGER_D, on)),
            "hover": (pair(P.DANGER, 0.00, 0.10) + (P.DANGER_D, on)),
            "press": (pair(P.DANGER_D, 0.00, 0.16) + (mix(P.DANGER_D, black, 0.28), on)),
            "disabled": (P.PAGE_D, P.PAGE, P.BORDER, P.TEXT_3),
        },
        "ghost": {
            "normal": (P.CARD, mix(P.CARD, P.PRIMARY, 0.05), P.BORDER, P.PRIMARY_D),
            "hover": (white, mix(P.CARD, P.PRIMARY, 0.11), P.BORDER_HI, P.PRIMARY_D),
            "press": (mix(P.CARD, P.PRIMARY, 0.08), mix(P.CARD, P.PRIMARY, 0.15), P.BORDER_HI, P.PRIMARY_D),
            "disabled": (P.CARD_SOFT, P.PAGE, P.BORDER, P.TEXT_3),
        },
    }
    return (table.get(kind) or table["ghost"]).get(state) or \
        (table.get(kind) or table["ghost"])["normal"]


_BTN_CACHE = {}


class RoundButton(tk.Canvas):
    """圆角按钮：预渲染三态底图 + canvas 文字。

    用法和普通按钮差不多：`RoundButton(parent, text, command, kind=...)`，
    另外可以用 `configure_state('disabled')` 禁用。

    ⚠️ 别用 `self._w` / `self._h` 存自己的尺寸：`_w` 是 **Tkinter 内部属性**，
    存的是这个控件的 Tcl 路径名（`.!frame.!card.!roundbutton` 这种），
    `super().__init__()` 一执行就把你的数字覆盖成字符串 —— 于是后面
    `int(self._w)` 报 `invalid literal for int() with base 10: '.!frame...'`。
    这里一律叫 `_bw` / `_bh`。
    """

    def __init__(self, master, text="", command=None, kind="ghost",
                 padx=15, pady=7, radius=13, size=10, bold=False,
                 page=None, width=None, **kw):
        self.font = ui_font(master, size, bold)
        self.kind = kind
        # 尺寸类参数过 sc()，字体不过（点是物理无关单位，Tk 自己按 DPI 换算）
        padx, pady, radius = sc(padx), sc(pady), sc(radius)
        self.radius = radius
        self.page = page or Palette.PAGE
        self._cmd = command
        self._state = "normal"
        self._text = text
        w = int(sc(width) if width else (self.font.measure(text) + padx * 2))
        h = int(self.font.metrics("linespace") + pady * 2)
        self._bw, self._bh = w, h
        super().__init__(master, width=w, height=h, bg=self.page,
                         highlightthickness=0, bd=0, **kw)
        self._bg = self.create_image(0, 0, anchor="nw")
        self._fg = self.create_text(w // 2, h // 2, text=text,
                                    font=self.font, fill=Palette.TEXT)
        self._render()
        self.bind("<Enter>", lambda e: self._set("hover"))
        self.bind("<Leave>", lambda e: self._set("normal"))
        self.bind("<ButtonPress-1>", lambda e: self._set("press"))
        self.bind("<ButtonRelease-1>", self._release)

    def _pair(self, state):
        key = (self.kind, state, self._bw, self._bh, self.page)
        got = _BTN_CACHE.get(key)
        if got is None:
            top, bot, edge, fg = _spec(self.kind, state)
            img = rounded_photo(self, self._bw, self._bh, self.radius,
                                grad=(top, bot), border=edge, page=self.page,
                                hi=mix(top, "#ffffff", 0.5),
                                shade=mix(bot, "#000000", 0.25), inset=2)
            _BTN_CACHE[key] = got = (img, fg)
        return got

    def _render(self):
        img, fg = self._pair(self._state)
        self._img = img
        self.itemconfigure(self._bg, image=img)
        self.itemconfigure(self._fg, fill=fg)

    def _set(self, state):
        if self._state == "disabled" or self._state == state:
            return
        self._state = state
        self._render()

    def _release(self, e):
        if self._state == "disabled":
            return
        self._set("hover")
        if 0 <= e.x <= self._bw and 0 <= e.y <= self._bh and self._cmd:
            self._cmd()

    def configure_state(self, state):
        self._state = state
        self._render()

    def set_kind(self, kind):
        """换外观（做分段控件用：选中的那个换成 `on`，不是 `primary`）。"""
        if kind != self.kind:
            self.kind = kind
            self._render()

    def get_kind(self):
        return self.kind

    def get_state(self):
        return self._state

    def set_text(self, text):
        self._text = text
        self.itemconfigure(self._fg, text=text)

    def get_text(self):
        return self._text


class Pill(tk.Canvas):
    """小圆角标签（相似度 / 类型徽章）。"""

    def __init__(self, master, text="", color=None, bgcolor=None, size=9,
                 padx=11, pady=4, page=None):
        self.font = ui_font(master, size, True)
        self.page = page or Palette.PAGE
        self._color = color or Palette.PRIMARY_D
        self._bgcolor = bgcolor or mix(Palette.PRIMARY, "#ffffff", 0.87)
        self._text = text
        padx, pady = sc(padx), sc(pady)
        w, h = self._measure(padx, pady)
        super().__init__(master, width=w, height=h, bg=self.page,
                         highlightthickness=0, bd=0)
        self._img = None
        self._imgitem = None
        self._txtitem = None
        self._paint(w, h)
        self._padx, self._pady = padx, pady

    def _measure(self, padx, pady):
        return (self.font.measure(self._text) + padx * 2,
                self.font.metrics("linespace") + pady * 2)

    def _paint(self, w, h):
        img = rounded_photo(self, w, h, h // 2, fill=self._bgcolor,
                            border=mix(self._bgcolor, self._color, 0.26),
                            page=self.page, inset=1)
        if self._imgitem is None:
            self._img = img
            self._imgitem = self.create_image(0, 0, anchor="nw", image=img)
            self._txtitem = self.create_text(w // 2, h // 2, text=self._text,
                                             font=self.font, fill=self._color)
        else:
            self._img = img
            self.itemconfigure(self._imgitem, image=img)
            self.coords(self._txtitem, w // 2, h // 2)

    def set(self, text, color=None, bgcolor=None):
        self._text = text
        if color:
            self._color = color
        if bgcolor:
            self._bgcolor = bgcolor
        w, h = self._measure(self._padx, self._pady)
        self.configure(width=w, height=h)
        self.delete("all")
        self._imgitem = self._txtitem = None
        self._paint(w, h)
        self.itemconfigure(self._txtitem, fill=self._color)

    def get_text(self):
        return self._text


# ---------------------------------------------------------------------------
# 圆角卡片
# ---------------------------------------------------------------------------

_CARD_CACHE = {}


class Card(tk.Canvas):
    """圆角卡片。内容放进 `card.body`（一个 Frame）。

    尺寸变化时重渲染底图（按尺寸缓存，来回拉不会反复算）。

    ⚠️ `tk.Canvas` 的**默认高度是 7cm**（≈264px）。只往里塞一行字的话，
    卡片会白白多出两百来像素的空白 —— 界面上一眼就是「上面一大片空、
    内容贴底」。所以默认 `fit="height"`：跟着 `body` 的**请求高度**
    (`winfo_reqheight`，只由内容决定，不受拉伸影响) 调自己的高度。
    需要撑满父容器的卡片（比如放图片的那张）传 `fit=None`。
    """

    def __init__(self, master, radius=15, fill=None, border=None,
                 page=None, shadow=True, pad=0, fit="height", **kw):
        self.radius = sc(radius)
        self.fill = fill or Palette.CARD
        self.border = border or Palette.BORDER
        self.page = page or Palette.PAGE
        self.shadow = shadow
        self.fit = fit
        # 只增不减的**最低高度**（见 App._fit_card）：
        # 卡片高度一变，它下面的预览画布就跟着变，解码尺寸跟着变，
        # 成品图缓存就整批作废。钉住之后布局才稳定。
        self.min_h = 0
        self._pad = sc(pad)
        self._inset = sc(2)
        self._last = None
        self._bgitem = None
        super().__init__(master, bg=self.page, highlightthickness=0, bd=0, **kw)
        self.body = tk.Frame(self, bg=self.fill)
        self._win = self.create_window(self._pad + self._inset,
                                       self._pad + self._inset,
                                       window=self.body, anchor="nw")
        self.bind("<Configure>", self._on_conf)
        if fit == "height":
            self.body.bind("<Configure>", self._on_body)

    def req_height(self):
        """**内容本身**请求的高度（不含 `min_h`）。

        必须分开：App 要在「内容高度」上加预留（NIQE 那半句还没落地），
        再跟 `min_h` 取 max。直接在「已取过 max 的高度」上加预留会**逐次累加**
        —— 实测一路加到 306px，信息卡吃掉半个窗口。
        """
        return self.body.winfo_reqheight() + 2 * (self._pad + self._inset)

    def _on_body(self, _e=None):
        req = self.req_height()
        if self.min_h:
            req = max(req, self.min_h)
        if req < 12:
            return
        try:
            cur = int(self.cget("height"))
        except (ValueError, tk.TclError):
            cur = -1
        if abs(cur - req) > 1:
            self.configure(height=req)

    def fit_to_content(self):
        """内容换过之后**显式**叫一声。

        ⚠️ 光靠 `body` 的 `<Configure>` 不够：如果 `body` 的实际尺寸早被
        固定住（`create_window` 给了 height），往里增删控件只会改它的
        **请求尺寸**，实际尺寸没动 → 压根不触发 `<Configure>` → 卡片高度
        停在旧值上，新内容就被裁掉（表现是「卡片里一片空白」）。

        ⚠️ 也不能图省事写成 `after_idle(self._on_body)` —— `after_idle` 和
        Tk 自己的几何计算都在 idle 队列里，谁先谁后不保证，实测量到的还是旧的
        请求高度（卡片照样停在 25px）。这里先 `update_idletasks()` 把几何算完
        再量，稳定。
        """
        if self.fit != "height":
            return
        try:
            self.update_idletasks()
        except tk.TclError:
            return
        self._on_body()

    def _on_conf(self, e):
        if e.width < 10 or e.height < 10 or self._last == (e.width, e.height):
            return
        self._last = (e.width, e.height)
        key = (e.width, e.height, self.radius, self.fill, self.border,
               self.page, self.shadow)
        img = _CARD_CACHE.get(key)
        if img is None:
            img = rounded_photo(self, e.width, e.height, self.radius,
                                fill=self.fill, border=self.border,
                                page=self.page,
                                shadow=Palette.SHADOW if self.shadow else None,
                                sd=3, inset=2)
            _CARD_CACHE[key] = img
        self._img = img
        if self._bgitem is None:
            self._bgitem = self.create_image(0, 0, anchor="nw", image=img)
            self.tag_lower(self._bgitem)
        else:
            self.itemconfigure(self._bgitem, image=img)
        self.itemconfigure(self._win,
                           width=max(1, e.width - 2 * (self._pad + self._inset)),
                           height=max(1, e.height - 2 * (self._pad + self._inset)))
        self.coords(self._win, self._pad + self._inset,
                    self._pad + self._inset)


# ---------------------------------------------------------------------------
# 自绘圆角列表
# ---------------------------------------------------------------------------

_ROW_CACHE = {}


class NiceList(tk.Canvas):
    """自绘的圆角列表：每行 = 小预览 + 标题 + 副标题，选中行是圆角高亮块。

    为什么不用 `ttk.Treeview`：Treeview 的行高/配色/选中色都改不动，
    做不出「圆滑通透」的样子；这里全部自己画，顺手还能在行左边塞一张
    圆角小预览 —— 用户明确要「每个文件后面加个小型预览方便确认」。

    `items` 是字典列表，可用键：
        title   主文字
        sub     副文字
        photo   小预览（tk.PhotoImage，可 None）
        tone    "normal" / "strong" / "loose" / "poor"，决定标题颜色
                （"poor" 用来**强调质量差的图**）
        badge   行尾的小徽章文字（如「低清」「体积小」），None 就不画
        tag     任意数据，回调时原样带回
    """

    GAP = 2

    def __init__(self, master, on_pick=None, on_activate=None, row_h=54,
                 thumb=40, page=None, radius=11, size=9, empty_text="",
                 **kw):
        self.page = page or Palette.PAGE
        self.row_h = sc(row_h)
        self.thumb = sc(thumb)
        self.radius = sc(radius)
        self.gap = sc(self.GAP)
        self.on_pick = on_pick
        self.on_activate = on_activate
        self.empty_text = empty_text
        self.items = []
        self.sel = -1
        self.hover = -1
        self._refs = []
        self.f_t = ui_font(master, size)
        self.f_tb = ui_font(master, size, True)
        self.f_s = ui_font(master, max(7, size - 1))
        super().__init__(master, bg=self.page, highlightthickness=0, bd=0, **kw)
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<Button-1>", self._click)
        self.bind("<Double-Button-1>", self._dbl)
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", self._leave)
        self.bind("<MouseWheel>", self._wheel)

    # -- 数据 ------------------------------------------------------------
    def set_items(self, items, keep_sel=False):
        self.items = list(items)
        if not keep_sel or self.sel >= len(self.items):
            self.sel = -1
        self.redraw()

    def select(self, idx, notify=True):
        if idx == self.sel:
            return
        old, self.sel = self.sel, idx
        # ⚠️ 只重画「旧选中行 + 新选中行」，不是全表重画。
        #    换选中是**最频繁**的操作（点组、点成员、键盘上下），
        #    全表重画在几十上百项时是纯浪费。
        if self.items:
            self._redraw_rows([old, idx])
        else:
            self.redraw()
        if notify and self.on_pick and 0 <= idx < len(self.items):
            self.on_pick(self.items[idx], idx)

    def current(self):
        if 0 <= self.sel < len(self.items):
            return self.items[self.sel]
        return None

    # -- 交互 ------------------------------------------------------------
    def _at(self, ev):
        y = self.canvasy(ev.y)
        idx = int(y // (self.row_h + self.gap))
        return idx if 0 <= idx < len(self.items) else -1

    def _click(self, ev):
        idx = self._at(ev)
        if idx >= 0:
            self.select(idx)

    def _dbl(self, ev):
        idx = self._at(ev)
        if idx >= 0 and self.on_activate:
            self.on_activate(self.items[idx], idx)

    def _motion(self, ev):
        idx = self._at(ev)
        if idx != self.hover:
            old, self.hover = self.hover, idx
            # ⚠️ 只重画换掉的那两行。原来这里调 `redraw()` 全量重画 ——
            #    200 项要 10~16ms，而鼠标划过列表一秒能触发几十次，
            #    实测下来就是「左边窗口上下滑动卡顿」。
            self._redraw_rows([old, idx])

    def _leave(self, _e=None):
        if self.hover != -1:
            old, self.hover = self.hover, -1
            self._redraw_rows([old])

    def _redraw_rows(self, idxs):
        """只重画指定的几行（删掉它们的图元再画一遍）。"""
        for i in set(idxs):
            if 0 <= i < len(self.items):
                self.delete(self._row_tag(i))
                self._draw_row(i)
        # 局部重画不会像 `redraw()` 那样重置 `_refs`，鼠标长时间在列表上划
        # 会让它无限增长。图本体都在 `_ROW_CACHE` / items 里有强引用，
        # 这里只丢尾部的冗余引用，不会把正在显示的图回收掉。
        if len(self._refs) > 4000:
            self._refs = self._refs[-1000:]

    def _wheel(self, ev):
        total = len(self.items) * (self.row_h + self.gap)
        if total > self.winfo_height():
            self.yview_scroll(-1 if ev.delta > 0 else 1, "units")

    # -- 绘制 ------------------------------------------------------------
    def _rowbg(self, w, state):
        key = (w, self.row_h, self.radius, self.page, state)
        got = _ROW_CACHE.get(key)
        if got is None:
            if state == "sel":
                got = rounded_photo(self, w, self.row_h, self.radius,
                                    fill=Palette.ROW_SEL,
                                    border=Palette.BORDER_HI, page=self.page,
                                    inset=1)
            elif state == "hover":
                got = rounded_photo(self, w, self.row_h, self.radius,
                                    fill=Palette.CARD_SOFT,
                                    border=Palette.LINE, page=self.page,
                                    inset=1)
            _ROW_CACHE[key] = got
        return got

    def _badge(self, text, color, fill):
        """行尾小徽章 -> (PhotoImage, 宽, 高)，按 (文字, 配色) 缓存。"""
        key = ("badge", text, color, fill, self.page, self.radius)
        got = _ROW_CACHE.get(key)
        if got is None:
            w = self.f_s.measure(text) + sc(14)
            h = self.f_s.metrics("linespace") + sc(4)
            img = rounded_photo(self, w, h, h // 2, fill=fill,
                                border=mix(fill, color, 0.35),
                                page=self.page, inset=1)
            got = (img, w, h)
            _ROW_CACHE[key] = got
        return got

    def redraw(self):
        self.delete("all")
        self._refs = []
        W = max(40, self.winfo_width())
        if not self.items:
            if self.empty_text:
                self.create_text(W // 2, sc(30), text=self.empty_text,
                                 fill=Palette.TEXT_3, font=self.f_s,
                                 width=max(sc(80), W - sc(24)))
            self.configure(scrollregion=(0, 0, W, sc(60)))
            return
        pitch = self.row_h + self.gap
        pad = sc(8)
        mid = self.row_h // 2
        for i in range(len(self.items)):
            self._draw_row(i, W, pitch, pad, mid)
        self.configure(scrollregion=(0, 0, W, len(self.items) * pitch))

    def _row_tag(self, i):
        return "row%d" % i

    def _draw_row(self, i, W=None, pitch=None, pad=None, mid=None):
        """画第 i 行（图元都带 `row{i}` 标签，好单独删）。

        拆出来是为了 hover：鼠标在列表上划过时，原来每换一行就**全部重画**
        （200 项实测 10~16ms，而鼠标移动事件一秒几十次 → 界面掉帧）。
        现在只删掉受影响的那两行、重画那两行。
        """
        if W is None:
            W = max(40, self.winfo_width())
        if pitch is None:
            pitch = self.row_h + self.gap
        if pad is None:
            pad = sc(8)
        if mid is None:
            mid = self.row_h // 2
        it = self.items[i]
        tag = self._row_tag(i)
        y = i * pitch
        # ⚠️ 背景和内容必须是两个**独立**的 if。
        #    之前把内容整段写进了 `elif i == self.hover:` 里，
        #    结果「既没选中也没悬停」的普通行什么都不画 —— 左栏只剩一排空背景块。
        if i == self.sel:
            bg = self._rowbg(W - sc(2), "sel")
            self._refs.append(bg)
            self.create_image(sc(1), y, anchor="nw", image=bg, tags=tag)
        elif i == self.hover:
            bg = self._rowbg(W - sc(2), "hover")
            self._refs.append(bg)
            self.create_image(sc(1), y, anchor="nw", image=bg, tags=tag)
        # ---- 文字/预览/徽章：每一行都要画 ----
        tx = pad + sc(2)
        ph = it.get("photo")
        if ph is not None:
            self._refs.append(ph)
            self.create_image(pad + sc(2), y + mid, anchor="w", image=ph,
                              tags=tag)
            tx = pad + sc(2) + self.thumb + sc(10)
        # 行尾徽章先量出来，好给标题留出宽度，免得字压到徽章上
        badge_w = 0
        badge = it.get("badge")
        if badge:
            tone_b = it.get("badge_tone") or it.get("tone", "normal")
            col = {"poor": Palette.POOR, "loose": Palette.WARN_D,
                   "strong": Palette.PRIMARY_D}.get(tone_b, Palette.TEXT_2)
            bimg, bw, bh = self._badge(badge, col, mix(col, "#ffffff", 0.86))
            self._refs.append(bimg)
            self.create_image(W - pad - bw, y + mid - bh // 2,
                              anchor="nw", image=bimg, tags=tag)
            self.create_text(W - pad - bw // 2, y + mid, text=badge,
                             fill=col, font=self.f_s, tags=tag)
            badge_w = bw + sc(10)
        tone = it.get("tone", "normal")
        fg = {"strong": Palette.PRIMARY_D, "loose": Palette.WARN_D,
              "poor": Palette.POOR, "match": Palette.MATCH}.get(
                  tone, Palette.TEXT)
        self.create_text(tx, y + mid - sc(9), text=it["title"],
                         anchor="w", fill=fg,
                         font=self.f_tb if i == self.sel else self.f_t,
                         width=max(sc(40), W - tx - badge_w - pad),
                         tags=tag)
        sub = it.get("sub") or ""
        if sub:
            self.create_text(tx, y + mid + sc(9), text=sub,
                             anchor="w", fill=Palette.TEXT_3,
                             font=self.f_s,
                             width=max(sc(40), W - tx - badge_w - pad),
                             tags=tag)
        self.configure(scrollregion=(0, 0, W, len(self.items) * pitch))
