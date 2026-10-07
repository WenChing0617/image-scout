# -*- coding: utf-8 -*-
"""视觉自检：把界面抓成 PNG，好让「肉眼」验收。零第三方依赖。

抓法是 GDI 的 `PrintWindow` —— 把窗口内容画进一块内存位图，再 `GetDIBits` 取像素。
之所以抓「自己进程的窗口」而不是抓整屏：截图更干净（不带桌面其它东西），
而不是因为整屏抓不了 —— 整屏（`GetDC(NULL)` + `BitBlt`）在本环境同样可行，实测能拿到真实画面。

⚠️ 64 位下最容易踩的坑：`CreateCompatibleDC` / `CreateCompatibleBitmap` /
`GetWindowDC` / `PrintWindow` 返回或接收的是**句柄（指针）**，不声明
`restype = c_void_p` 的话 ctypes 默认按 `c_int` 截成 32 位，句柄被砍掉高半截 ——
传给下一个 API 就是野指针，进程直接被杀（还是不给任何输出的那种）。
⚠️ 反过来也别被吓着：句柄打印出来是 `18446744073407904076` 这种天文数字很正常
（高位补了符号位），只要 `argtypes` 声明成 `c_void_p` 就能原样传回去，不用管它好不好看。
所以下面每个 API 都把 argtypes / restype 写全。

用法：
    python uishot.py              造几张测试图 → 扫一遍 → 抓「并排对比」和
                                  「只看匹配区域」两张截图存到当前目录
"""

from __future__ import annotations

import ctypes
import os
import sys
import tempfile
import time
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import tkinter as tk                                    # noqa: E402

import thumbs                                           # noqa: E402
import uikit                                            # noqa: E402
from uikit import Palette as P                          # noqa: E402

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.GetWindowDC.argtypes = [wintypes.HWND]
user32.GetWindowDC.restype = ctypes.c_void_p
user32.ReleaseDC.argtypes = [wintypes.HWND, ctypes.c_void_p]
user32.PrintWindow.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.UINT]
user32.PrintWindow.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.c_void_p]
user32.GetWindowRect.restype = wintypes.BOOL

gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                         ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
gdi32.SelectObject.restype = ctypes.c_void_p
gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
gdi32.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT,
                            wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p,
                            wintypes.UINT]
gdi32.GetDIBits.restype = ctypes.c_int


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class RGBQUAD(ctypes.Structure):
    _fields_ = [("rgbBlue", ctypes.c_ubyte), ("rgbGreen", ctypes.c_ubyte),
                ("rgbRed", ctypes.c_ubyte), ("rgbReserved", ctypes.c_ubyte)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER),
                ("bmiColors", RGBQUAD * 1)]


def grab(hwnd, path, box=None):
    """把窗口 hwnd 抓成 PNG 写到 path。返回 (宽, 高)。

    `box=(x, y, w, h)` 可选：只保留这一块（坐标是**根窗口相对**的物理像素）。

    ⚠️⚠️ 想抓「工具栏那一条」**只能走 box**：`GetAncestor(GA_ROOT)` 会把
       任何子控件的句柄换成根窗口 —— 直接 `grab(line2.winfo_id(), ...)`
       抓出来还是整窗（2150×1346），子控件坐标全被丢掉。
        裁之前先把整窗抓下来，再按 `控件.winfo_rootx() - 根.winfo_rootx()`
       算偏移切行。别指望 PrintWindow 支持「只画一块」。
    """
    hwnd = user32.GetAncestor(hwnd, 2)          # GA_ROOT
    rc = (ctypes.c_long * 4)()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rc)):
        raise OSError("GetWindowRect 失败")
    w, h = rc[2] - rc[0], rc[3] - rc[1]
    if w <= 0 or h <= 0:
        raise OSError("窗口尺寸不对：%dx%d" % (w, h))

    hdc_win = user32.GetWindowDC(hwnd)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_win)
    hbmp = gdi32.CreateCompatibleBitmap(hdc_win, w, h)
    gdi32.SelectObject(hdc_mem, hbmp)

    bi = BITMAPINFO()
    bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.bmiHeader.biWidth = w
    bi.bmiHeader.biHeight = -h                  # 负数 = 请求自上而下的行序
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 32
    bi.bmiHeader.biCompression = 0              # BI_RGB

    buf = ctypes.create_string_buffer(w * h * 4)
    try:
        user32.PrintWindow(hwnd, hdc_mem, 2)    # 2 = PW_RENDERFULLCONTENT
        got = gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf, ctypes.byref(bi), 0)
        if got != h:
            raise OSError("GetDIBits 只拿到 %d / %d 行" % (got, h))
    finally:
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(hwnd, hdc_win)

    data = bytearray(buf.raw)
    for i in range(3, len(data), 4):            # GDI 留下的 alpha 全是 0，补满
        data[i] = 255

    if box is not None:
        bx, by, bw, bh = (int(v) for v in box)
        bx = max(0, min(w - 1, bx))
        by = max(0, min(h - 1, by))
        bw = max(1, min(bw, w - bx))
        bh = max(1, min(bh, h - by))
        out = bytearray()
        for yy in range(by, by + bh):
            s = yy * w * 4 + bx * 4
            out += data[s:s + bw * 4]
        data, w, h = out, bw, bh

    with open(path, "wb") as f:
        f.write(thumbs.bgra_to_png(w, h, bytes(data)))
    return w, h


def grab_widget(root, widget, path, margin=8, span=0, extra_w=0):
    """抓 `root` 窗口里某个**子控件**那一块（含四周 margin），返回 (宽, 高)。

    ⚠️⚠️ 偏移必须自己算，`grab` 的默认行为在这里是错的：
      · `PrintWindow` 只能整窗抓，图像起点是**窗口左上角**（含标题栏和边框）；
      · 而 `widget.winfo_rootx/rooty` 是**客户区相对**坐标（Tk 报的）。
      两者差一圈非客户区 —— 实测 2150×1346 的图 vs 2128×1290 的客户区，
      横 11、纵 45。直接用 winfo 坐标当 box 会整体上移一个标题栏高，
      **裁到上一行去**（第一次就裁到了「停止 / 开始扫描」）。

    `span` 往左多框一点（把左边的兄弟按钮一起带进来）；
    `extra_w` 往右多框一点（防自绘圆角被切边）。
    """
    w0, h0 = root.winfo_width(), root.winfo_height()
    full = path + ".fullt.tmp.png"
    gw, gh = grab(root.winfo_id(), full)
    try:
        os.remove(full)
    except OSError:
        pass
    dx = max(0, (gw - w0) // 2)
    dy = max(0, gh - h0 - dx)
    x = dx + (widget.winfo_rootx() - root.winfo_rootx()) - margin - span
    y = dy + (widget.winfo_rooty() - root.winfo_rooty()) - margin
    bw = widget.winfo_width() + margin * 2 + span + extra_w
    bh = widget.winfo_height() + margin * 2
    return grab(root.winfo_id(), path, box=(x, y, bw, bh))


def settle(win, n=25, pause=0.035):
    """把事件队列和排队中的 after 回调都跑完，并给系统一点绘制时间。"""
    win.update_idletasks()
    for _ in range(n):
        try:
            win.update()
        except tk.TclError:
            break
        time.sleep(pause)


# ---------------------------------------------------------------------------
# 造测试图：3 张同源裁剪 + 3 张互不相同的
# ---------------------------------------------------------------------------

def _clamp(v):
    return 0 if v < 0 else (255 if v > 255 else int(v))


def _paint(w, h, kind=0):
    """画一张有结构的图。

    ⚠️ 别让不同 kind 只差一点点「种子」：早先版本每张都是「渐变 + 棋盘 + 大白圆」，
    只是颜色和相位微调 —— 结果查重工具（正确地）把它们全判成了同一张图的不同裁剪，
    截图里看着像程序出错，其实是我的样本造错了。
    """
    buf = bytearray()
    cx, cy = w / 2.0, h / 2.0
    for y in range(h):
        for x in range(w):
            if kind == 0:                # 渐变 + 棋盘 + 圆 + 斜线
                r = x * 255 // w
                g = y * 255 // h
                b = 128 + 90 * ((x // 37 + y // 29) % 2)
                if (x - w // 3) ** 2 + (y - h // 3) ** 2 < (min(w, h) // 4) ** 2:
                    r, g, b = 250, 245, 230
                if (x + y) % 97 < 3:
                    r = g = b = 30
            elif kind == 1:              # 横向条纹 + 黄色斜线
                band = y // 24 % 2
                r, g, b = 30 + 60 * band, 200 - 120 * band, 90
                if (x * 3 + y * 2) % 71 < 4:
                    r, g, b = 245, 240, 60
            elif kind == 2:              # 径向渐变 + 深色斑点
                d = int(255 * min(1.0, (((x - cx) ** 2 + (y - cy) ** 2) ** 0.5)
                                  / (w * 0.6)))
                r, g, b = 250 - d, 140 - d // 2, 200 - d
                if (x // 30 + y // 19) % 5 == 0:
                    r, g, b = 20, 60, 90
            else:                        # 纯网格
                r = 255 if (x // 28) % 2 else 60
                g = 255 if (y // 28) % 2 else 60
                b = 120 if (x // 28 + y // 28) % 2 else 30
            buf += bytes((_clamp(b), _clamp(g), _clamp(r), 255))
    return bytes(buf)


def make_samples(d):
    # 先清空 —— 不然上一轮留下的 other_blob.png 会被一起扫进来，
    # 截图里出现一堆没见过的文件名（这个坑我踩过一次）
    if os.path.isdir(d):
        for f in os.listdir(d):
            try:
                os.remove(os.path.join(d, f))
            except OSError:
                pass
    os.makedirs(d, exist_ok=True)
    out = []
    W, H = 560, 380
    base = _paint(W, H, 0)
    whole = os.path.join(d, "poster_full.png")
    with open(whole, "wb") as f:
        f.write(thumbs.bgra_to_png(W, H, base))
    out.append(whole)

    for name, box in (("poster_crop_right.png", (0, 0, 372, 380)),
                      ("poster_crop_bottom.png", (60, 40, 560, 300))):
        cw = box[2] - box[0]
        ch = box[3] - box[1]
        w, h, sub = thumbs.crop_bgra(W, H, base, box[0], box[1], cw, ch)
        p = os.path.join(d, name)
        with open(p, "wb") as f:
            f.write(thumbs.bgra_to_png(w, h, sub))
        out.append(p)

    for name, kind in (("other_stripes.png", 1), ("other_radial.png", 2),
                       ("other_grid.png", 3)):
        p = os.path.join(d, name)
        with open(p, "wb") as f:
            f.write(thumbs.bgra_to_png(W, H, _paint(W, H, kind)))
        out.append(p)

    # 一张低清小图：从海报里裁一小块（160x120，长边 < 480 会被判「低清」）。
    # 故意做成海报的裁剪变体 —— 这样它会跟海报分到同一组，
    # 截图里就能看到「低清副本混在组里被徽章强调」这个真实场景。
    box = (150, 100, 310, 220)
    w, h, sub = thumbs.crop_bgra(W, H, base, box[0], box[1],
                                 box[2] - box[0], box[3] - box[1])
    low = os.path.join(d, "poster_lowres.png")
    with open(low, "wb") as f:
        f.write(thumbs.bgra_to_png(w, h, sub))
    out.append(low)
    return out


# ---------------------------------------------------------------------------
# 真实程序截图
# ---------------------------------------------------------------------------

def shot_app(out_dir):
    import image_scout as core

    d = os.path.join(tempfile.gettempdir(), "ImageScout-shot")
    files = make_samples(d)
    print("测试图 %d 张 -> %s" % (len(files), d))

    app = core.App([])
    # 尺寸按 DPI 换算（设计稿像素 -> 物理像素），不然在高分屏上会被 minsize 顶住
    app.geometry("%dx%d+0+0" % (uikit.sc(1400), uikit.sc(830)))
    app.roots = [d]
    app._refresh_roots()
    settle(app, 8)
    app.start_scan()

    t0 = time.time()
    while app.busy and time.time() - t0 < 120:
        app.update()
        time.sleep(0.05)
    settle(app, 30)
    print("扫描结束：%d 张 -> %d 组  状态：%s"
          % (len(app.descs), len(app.groups), app.stat.cget("text")))
    for tag, cv, cap in (("A", app.cv_a, app.cap_a),
                         ("B", app.cv_b, app.cap_b)):
        print("  %s 格 %dx%d   %s | %s"
              % (tag, cv.winfo_width(), cv.winfo_height(),
                 cap.name_lbl.cget("text"), cap.dim_lbl.cget("text")))
    print("  结论卡高 %s  信息卡高 %s"
          % (app.verdict_card.cget("height"), app.info_card.cget("height")))

    shots = []
    p1 = os.path.join(out_dir, "screenshot.png")
    shots.append(grab(app.winfo_id(), p1))
    shots.append(p1)

    # 再截一张「只看匹配区域」的状态
    app.toggle_crop()
    settle(app, 25)
    p2 = os.path.join(out_dir, "shot-crop.png")
    shots.append(grab(app.winfo_id(), p2))
    shots.append(p2)

    # 再截一张「全部图片」视图：连没配对上的图也列出来，低质量图带徽章
    app.set_view("all")
    settle(app, 25)
    # 「全部图片」填的是上面那个大列表（glist）；下面 mlist 是「与选中图相似的」
    badged = [it for it in app.glist.items if it.get("badge")]
    print("  全部图片视图 %d 项，其中带徽章的 %d 项：%s"
          % (len(app.glist.items), len(badged),
             [(it.get("title"), it.get("badge")) for it in badged]))
    p3 = os.path.join(out_dir, "shot-all.png")
    shots.append(grab(app.winfo_id(), p3))
    shots.append(p3)

    app.destroy()
    return shots


# ---------------------------------------------------------------------------

def main():
    # 抓任何窗口之前先开 DPI 感知：不开的话抓到的窗口本身就是被系统
    # 位图拉伸过的，字全是糊的 —— 拿它当验收依据会得出错误结论。
    uikit.enable_dpi_awareness()
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    out_dir = os.path.abspath(args[0]) if args else HERE
    for size, p in zip(*[iter(shot_app(out_dir))] * 2):
        print("OK %dx%d -> %s (%d 字节)" % (size[0], size[1], p,
                                            os.path.getsize(p)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
