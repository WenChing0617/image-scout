# -*- coding: utf-8 -*-
"""测试「一键交给网页」这条链路。

分三层，因为可信度不一样：
  A. **纯函数层**（永远要过）：bgra_to_dib 造出来的 CF_DIB 字节对不对。
     不碰剪贴板，所以任何环境都能真验证 —— 这也是当初发现红蓝互换的地方。
  B. **剪贴板层**（能过就过）：写进去再读回来，核对尺寸 / 位深 / 行序。
     受限环境（非交互进程）系统会直接拒绝访问剪贴板，此时如实跳过。
  C. **兜底层**（永远要过）：剪贴板不可用时的降级 —— 导出临时副本，
     并且副本必须能被 imgsize 读出正确尺寸。
"""
import ctypes
import os
import sys
import tempfile
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import imgsize        # noqa: E402
import makeset        # noqa: E402
import net            # noqa: E402
import winimg         # noqa: E402

# 临时图放 %TEMP%，跑测试不会把工程目录弄脏
WORK = os.path.join(tempfile.gettempdir(), "ImageScout-nettest")

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalSize.restype = ctypes.c_size_t

FAIL = []
SKIP = []


def check(cond, msg):
    print("   %s %s" % ("OK  " if cond else "FAIL", msg))
    if not cond:
        FAIL.append(msg)


def skip(msg):
    print("   SKIP %s" % msg)
    SKIP.append(msg)


def read_fmt(fmt):
    if not user32.IsClipboardFormatAvailable(fmt):
        return None
    if not user32.OpenClipboard(None):
        return None
    try:
        h = user32.GetClipboardData(fmt)
        if not h:
            return None
        p = kernel32.GlobalLock(h)
        if not p:
            return None
        try:
            return ctypes.string_at(p, kernel32.GlobalSize(h))
        finally:
            kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


def make_bgra(w, h):
    """造一张 B G R A 内存序的图，每个像素都带位置信息，方便逐位核对。

    像素 (x, y) 的内存字节 = (B=1+y, G=100+x, R=200, A=255)。
    这样行列颠倒、通道颠倒都躲不过。
    """
    buf = bytearray()
    for y in range(h):
        for x in range(w):
            buf += bytes((1 + y, 100 + x, 200, 255))
    return bytes(buf)


# ---------------------------------------------------------------------------
# A. 纯函数：CF_DIB 字节布局
# ---------------------------------------------------------------------------

def test_dib_layout():
    print("=== A. CF_DIB 字节布局（纯函数，不碰剪贴板）===")
    w, h = 3, 2
    bgra = make_bgra(w, h)
    blob = net.bgra_to_dib(w, h, bgra, 24)

    hdr = net.BITMAPINFOHEADER.from_buffer_copy(blob[:40])
    stride = ((w * 3 + 3) // 4) * 4
    print("   头: %dx%d 位深%d 压缩%d 声明%d字节"
          % (hdr.biWidth, hdr.biHeight, hdr.biBitCount, hdr.biCompression,
             hdr.biSizeImage))

    check(hdr.biSize == 40, "头长度 40")
    check(hdr.biWidth == w and hdr.biHeight == h, "尺寸 %dx%d" % (w, h))
    check(hdr.biHeight > 0, "biHeight 为正数 -> 自下而上")
    check(hdr.biBitCount == 24, "位深 24")
    check(hdr.biCompression == 0, "BI_RGB 无压缩")
    check(hdr.biPlanes == 1, "biPlanes = 1")
    check(len(blob) - 40 == stride * h,
          "像素区 = 行距%d x %d = %d（实际 %d）"
          % (stride, h, stride * h, len(blob) - 40))
    check(hdr.biSizeImage == stride * h, "biSizeImage 与像素区一致")

    # 第一行必须是源图的**最后一行**（y = h-1）
    p = blob[40:43]
    check(p == bytes((h, 100, 200)),
          "首像素 = 源图 (0,%d) 的 B,G,R = (B=%d,G=100,R=200)，实际 %s"
          % (h - 1, h, tuple(p)))

    # 第二行应当是 y = h-2
    p2 = blob[40 + stride:40 + stride + 3]
    check(p2 == bytes((h - 1, 100, 200)),
          "第二行 = 源图 (0,%d)（实际 %s）" % (h - 2, tuple(p2)))

    # 行距补的字节必须是 0
    pad = blob[40 + 9:40 + stride]
    check(bytes(pad) == b"\0" * (stride - 9), "行尾补零（%d 字节）" % len(pad))

    # 换一个「3w 本来就是 4 的倍数」的宽度，验证没有多余补位
    w2, h2 = 4, 1
    b2 = net.bgra_to_dib(w2, h2, make_bgra(w2, h2), 24)
    check(len(b2) - 40 == 12, "宽 4 时行距 12 且无补位（实际 %d）" % (len(b2) - 40))

    # 32 位分支：alpha 必须被填成 255，否则会被当成全透明
    b3 = net.bgra_to_dib(2, 1, bytes((1, 2, 3, 0, 4, 5, 6, 0)), 32)
    px = b3[40:]
    check(len(px) == 8, "32 位像素区 = 2 x 4 字节")
    check(px[0:3] == bytes((1, 2, 3)) and px[3] == 255,
          "32 位首像素 B,G,R,255（实际 %s）" % (tuple(px[0:4]),))
    check(px[7] == 255, "32 位 alpha 全部填 255")


# ---------------------------------------------------------------------------
# B. 剪贴板往返
# ---------------------------------------------------------------------------

def test_clipboard_roundtrip(p1, p2):
    print("\n=== B. 剪贴板往返（依赖系统许可）===")
    if not net.clipboard_available():
        skip("本环境不允许访问剪贴板（OpenClipboard 被拒），"
             "CF_DIB / CF_UNICODETEXT / CF_HDROP 的写入验证无法进行")
        return
    check(True, "剪贴板可用")

    ok, info = net.copy_image(p1)
    check(ok, "copy_image 返回成功（%s）" % info)
    blob = read_fmt(net.CF_DIB)
    check(blob is not None, "剪贴板里确实有 CF_DIB")
    if blob:
        hdr = net.BITMAPINFOHEADER.from_buffer_copy(blob[:40])
        check(hdr.biWidth == 240 and hdr.biHeight == 180,
              "尺寸 240x180（实际 %dx%d）" % (hdr.biWidth, hdr.biHeight))
        check(hdr.biBitCount == 24, "位深 24（实际 %d）" % hdr.biBitCount)
        check(hdr.biCompression == 0, "无压缩")
        stride = ((240 * 3 + 3) // 4) * 4
        check(len(blob) - 40 == stride * 180, "像素字节数正确")
        # 第一行 = 源图最后一行，顺序 B,G,R
        w, h, bgra = winimg.load_pixels(p1, 1024)
        i = ((h - 1) * w) * 4
        src = (bgra[i], bgra[i + 1], bgra[i + 2])
        check(tuple(blob[40:43]) == src,
              "首像素顺序与源一致 B,G,R %s（实际 %s）"
              % (src, tuple(blob[40:43])))

    check(net.copy_text("你好，ImageScout"), "copy_text 成功")
    raw = read_fmt(net.CF_UNICODETEXT)
    if raw:
        txt = raw.decode("utf-16-le").rstrip("\0")
        check(txt == "你好，ImageScout", "读回文本一致（%r）" % txt)
    else:
        check(False, "读不到 CF_UNICODETEXT")

    check(net.copy_files([p1, p2]), "copy_files 成功")
    raw = read_fmt(net.CF_HDROP)
    check(raw is not None, "剪贴板里确实有 CF_HDROP")
    if raw:
        df = net.DROPFILES.from_buffer_copy(raw[:20])
        check(df.fWide, "是宽字符列表")
        items = [x for x in raw[df.pFiles:].decode("utf-16-le").split("\0") if x]
        check(len(items) == 2, "列表里有 2 个路径（实际 %d）" % len(items))
        check(items and os.path.normcase(items[0]) == os.path.normcase(p1),
              "第一个路径就是 p1")


# ---------------------------------------------------------------------------
# C. 兜底：导出临时副本 + 降级文案
# ---------------------------------------------------------------------------

def test_fallback(p1):
    print("\n=== C. 兜底路径（导出副本 + 降级文案）===")
    out, msg = net.export_copy(p1)
    check(out is not None and os.path.isfile(out), "导出了临时副本：%s" % out)
    if out:
        wh = imgsize.size_of_file(out)
        check(wh == (240, 180), "副本是 240x180 的 PNG（实际 %s）" % (wh,))
        check(imgsize.format_of(out) == "PNG", "副本格式是 PNG")
        check(os.path.dirname(out).endswith("ImageScout"),
              "副本放在 %TEMP%/ImageScout 下")
        print("   位置：%s" % os.path.dirname(out))
        check(imgsize.format_of(p1) == "PNG" and imgsize.size_of_file(p1) == (240, 180),
              "源图本身可被解析（对照组）")


# ---------------------------------------------------------------------------
# D. 「一键找高清」确实删干净了，但复制 / 定位 / 打开还要在
# ---------------------------------------------------------------------------

def test_online_removed():
    print("\n=== D. 「一键找高清」已按要求删掉 ===")
    for name in ("SITES", "SITE_MAP", "PROMPT", "search_online", "open_url"):
        check(not hasattr(net, name), "net 里没有 %s 了" % name)
    check(all(callable(getattr(net, n)) for n in
              ("copy_image", "copy_text", "copy_files")),
          "复制相关的三个函数都还在")
    check(all(callable(getattr(net, n)) for n in
              ("reveal_in_explorer", "open_path")),
          "定位 / 打开相关的函数都还在")
    check(all(callable(getattr(net, n)) for n in
              ("clipboard_available", "export_copy")),
          "剪贴板探测 / 降级导出都还在")
    src = open(os.path.join(HERE, "net.py"), encoding="utf-8").read()
    # docstring 里留着一段「当初为什么走不通上传接口」的踩坑记录（含几个域名），
    # 那是有意保留的注释；这里要确认的是**没有可点的站点清单**了
    check('"https://' not in src and "'https://" not in src,
          "源码里没有残留的站点网址字面量")


def main():
    root = WORK
    os.makedirs(root, exist_ok=True)
    p1 = os.path.join(root, "a.png")
    p2 = os.path.join(root, "b.png")
    makeset.write_png(p1, 240, 180, makeset.render("rings", 240, 180, seed=3))
    makeset.write_png(p2, 160, 120, makeset.render("wave", 160, 120, seed=1))

    try:
        test_dib_layout()
        test_clipboard_roundtrip(p1, p2)
        test_fallback(p1)
        test_online_removed()
    finally:
        import glob
        for p in glob.glob(os.path.join(root, "*.png")):
            try:
                os.remove(p)
            except OSError:
                pass
        try:
            os.rmdir(root)
        except OSError:
            pass

    print("\n=== 汇总 ===")
    if SKIP:
        print("   跳过 %d 项（环境限制）：" % len(SKIP))
        for m in SKIP:
            print("     - %s" % m)
    if FAIL:
        print("   %d 项失败：" % len(FAIL))
        for m in FAIL:
            print("     - %s" % m)
        return 1
    print("   全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
