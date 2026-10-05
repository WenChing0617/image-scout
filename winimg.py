"""Windows 自带图像解码 —— ctypes 调 shell32/gdi32/gdiplus，零第三方依赖。

**两条读取路径，别用混**（这是本文件最重要的一条）：

* `load_pixels()` —— 走 **Shell 缩略图**（`IShellItemImageFactory`）。
  优点是支持系统装了编解码器的所有格式（含 HEIC/AVIF/WEBP），
  适合算指纹、生成缩略图 —— 反正只要「同一张图每次拿到的完全一样」就行。
  ⚠️ **它返回的不是原图**：实测同一张 PNG，Shell 回过来的像素有
  **70% 的字节和原图不同**（均值差 4.4、最大 37），因为这条路会串色/重采样。
  更要命的是**具体偏差取决于请求尺寸命中了哪条缓存分支** —— 同一张 480×480 的图，
  请求 480 / 512 / 1024 / 2048 全都被悄悄改过，只有 1500 才逐字节等于原图；
  另一个 300×300 的图则反过来（1024 精确、400 不精确）。
  **所以说它「稳定」也只是同尺寸下稳定**，横向比不同图时并不可靠。
* `load_pixels_exact()` —— 走 **GDI+**（`GdipCreateBitmapFromFile`）。
  实测 PNG / JPEG 与原图**逐字节相同**（和 Pillow 对过）。需要「像素就是原图」的
  场合（比如 NIQE 这种对像素级差异极敏感的质量分）必须用它。
  代价：GDI+ 只认 PNG/JPEG/BMP/GIF/TIFF/ICO，没有 WEBP/HEIC。

另外三个必须记住的坑（都踩过）：
  1. 所有涉及句柄的函数都要显式声明 argtypes，否则 64 位下
     OverflowError: int too long to convert。
  2. 算指纹必须用 SIIGBF_RESIZETOFIT 固定尺寸。带 SIIGBF_BIGGERSIZEOK 时
     系统会按自己的缓存状态回一张更大的图 → 同一张图两次算出不同指纹 →
     本该同组的重复图被拆散（表现为「第一次扫描分组对的、第二次不对」）。
  3. **缩略图缓存有冷 / 热两条路径，返回的像素不一样**（实测：同一文件连读 4 遍，
     第 1 遍和第 2 遍 58/58 全不同，第 2/3/4 遍彼此完全相同；请求尺寸 256 时
     恰好走的是同一条路径所以看不出问题，128 / 512 都会暴露）。
     所以这里**固定走热路径**：先预热一次（丢弃结果），再带
     SIIGBF_INCACHEONLY 只读缓存。不这么做的话，同一次会话里
     「批量扫描」和「后来单独读一张图」拿到的指纹会不一致，缓存库反而变成错的来源。
"""

from __future__ import annotations

import ctypes
import os
import threading
import uuid
from ctypes import POINTER, byref, c_int, c_long, c_uint, c_uint32, c_ulong, c_void_p
from ctypes import wintypes

shell32 = ctypes.windll.shell32
gdi32 = ctypes.windll.gdi32
ole32 = ctypes.windll.ole32
gdiplus = ctypes.windll.gdiplus


# ---------------------------------------------------------------------------
# 结构
# ---------------------------------------------------------------------------

class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    def __init__(self, text: str | None = None):
        super().__init__()
        if text:
            u = uuid.UUID(text)
            self.Data1 = u.time_low
            self.Data2 = u.time_mid
            self.Data3 = u.time_hi_version
            self.Data4 = (ctypes.c_ubyte * 8)(*u.bytes[8:])


class SIZE(ctypes.Structure):
    _fields_ = [("cx", c_long), ("cy", c_long)]


class BITMAP(ctypes.Structure):
    _fields_ = [("bmType", c_long), ("bmWidth", c_long), ("bmHeight", c_long),
                ("bmWidthBytes", c_long), ("bmPlanes", wintypes.WORD),
                ("bmBitsPixel", wintypes.WORD), ("bmBits", c_void_p)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", c_long),
                ("biHeight", c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", c_long),
                ("biYPelsPerMeter", c_long), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


IID_IShellItemImageFactory = "{BCC18B79-BA16-442F-80C4-8A59C30C463B}"

SIIGBF_RESIZETOFIT = 0x00
SIIGBF_BIGGERSIZEOK = 0x01
SIIGBF_MEMORYONLY = 0x02
SIIGBF_THUMBNAILONLY = 0x08
SIIGBF_INCACHEONLY = 0x10
SIIGBF_SCALEUP = 0x100

HRESULT = c_long

# ---------------------------------------------------------------------------
# 函数签名
# ---------------------------------------------------------------------------

shell32.SHCreateItemFromParsingName.argtypes = [
    wintypes.LPCWSTR, c_void_p, POINTER(GUID), POINTER(c_void_p)]
shell32.SHCreateItemFromParsingName.restype = HRESULT

ole32.CoInitializeEx.argtypes = [c_void_p, wintypes.DWORD]
ole32.CoInitializeEx.restype = HRESULT

gdi32.GetObjectW.argtypes = [wintypes.HGDIOBJ, c_int, c_void_p]
gdi32.GetObjectW.restype = c_int

gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC

gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.restype = wintypes.BOOL

gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteObject.restype = wintypes.BOOL

gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT,
                            wintypes.UINT, c_void_p, POINTER(BITMAPINFO),
                            wintypes.UINT]
gdi32.GetDIBits.restype = c_int

_GetImageProto = ctypes.WINFUNCTYPE(HRESULT, c_void_p, SIZE, c_int,
                                    POINTER(wintypes.HBITMAP))
_ReleaseProto = ctypes.WINFUNCTYPE(c_ulong, c_void_p)

_com_tids = set()
_com_lock = threading.Lock()


def _ensure_com():
    """每个线程都要初始化一次 COM（线程池里每个 worker 都得过这一关）。

    注意用**集合**记已初始化的线程：早先写成单个 `_com_tid`，
    多线程时后一个线程会把前一个顶掉，于是老线程以为初始化过了、
    其实没有 —— 一上线程池就随机出错。
    """
    tid = threading.get_ident()
    if tid in _com_tids:
        return
    with _com_lock:
        if tid in _com_tids:
            return
        try:
            ole32.CoInitializeEx(None, 0x2)      # COINIT_APARTMENTTHREADED
        except Exception:
            pass
        _com_tids.add(tid)


def _hbmp_to_bgra(hbmp):
    bm = BITMAP()
    if not gdi32.GetObjectW(hbmp, ctypes.sizeof(BITMAP), byref(bm)):
        return None
    w, h = int(bm.bmWidth), int(bm.bmHeight)
    if w <= 0 or h <= 0:
        return None

    hdc = gdi32.CreateCompatibleDC(None)
    if not hdc:
        return None
    try:
        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = w
        info.bmiHeader.biHeight = -h          # 负数 = 自上而下
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0      # BI_RGB
        buf = ctypes.create_string_buffer(w * h * 4)
        got = gdi32.GetDIBits(hdc, hbmp, 0, h, buf, byref(info), 0)
        if not got:
            return None
        return w, h, buf.raw
    finally:
        gdi32.DeleteDC(hdc)


def _get_image(path: str, size: int, flags: int):
    """真正去要一张图。返回 (宽, 高, BGRA 字节)，失败返回 None。"""
    p = c_void_p()
    iid = GUID(IID_IShellItemImageFactory)
    hr = shell32.SHCreateItemFromParsingName(path, None, byref(iid), byref(p))
    if hr != 0 or not p:
        return None

    try:
        vtbl = ctypes.cast(p, POINTER(POINTER(c_void_p))).contents
        hbmp = wintypes.HBITMAP()
        hr = _GetImageProto(vtbl[3])(p, SIZE(size, size), flags, byref(hbmp))
        if hr != 0 or not hbmp:
            return None
        try:
            return _hbmp_to_bgra(hbmp)
        finally:
            gdi32.DeleteObject(hbmp)
    finally:
        try:
            _ReleaseProto(vtbl[2])(p)
        except Exception:
            pass


def load_pixels(path: str, size: int = 128, flags: int = SIIGBF_RESIZETOFIT,
                raw: bool = False):
    """读一张图，返回 (宽, 高, BGRA 字节)。失败返回 None。

    返回尺寸不超过 size x size，但**保持原始长宽比**（不会拉伸）。
    path 必须是绝对路径 —— SHCreateItemFromParsingName 对相对路径静默失败。

    ⚠️ 这里**故意读两次**：系统缩略图缓存的冷路径和热路径返回的像素不同
    （冷路径是现渲染的，热路径是缓存里的缩略图）。为了让指纹稳定，
    统一走热路径 —— 先预热一次丢掉结果，再带 SIIGBF_INCACHEONLY 取缓存里的那张。
    如果该 provider 根本不缓存（第二次仍失败），就退回普通读，
    这种情况本身没有冷热之分，也是稳定的。

    `raw=True` 是给「只看图、不参与指纹」的调用用的（缩略图、大预览、
    复制到剪贴板前的那次解码），走单次读取，两个好处：
      * 省掉一次多余的解码（预览快一倍）；
      * **尺寸是精确的**。带 `SIIGBF_INCACHEONLY` 那次读会命中系统缓存里
        另一档的缩略图，拿回来的尺寸可能比请求的大不少 —— 实测请 389 拿到 431，
        于是预览溢出画布、红框被裁到画面外（这个坑找了半天）。
    """
    if not path:
        return None
    path = os.path.abspath(path)
    _ensure_com()

    if raw:
        return _get_image(path, size, flags)

    _get_image(path, size, flags)                       # 预热（结果丢弃）
    got = _get_image(path, size, flags | SIIGBF_INCACHEONLY)
    if got:
        return got
    return _get_image(path, size, flags)


def gray_fit(w: int, h: int, bgra: bytes, gw: int, gh: int):
    """把 BGRA 面积平均采样成 gw x gh 灰度（0-255）。

    面积平均比最近邻稳得多 —— 缩略图被缩放时最近邻会丢细节、指纹抖动。
    """
    out = []
    for ty in range(gh):
        y0 = ty * h // gh
        y1 = max(y0 + 1, (ty + 1) * h // gh)
        row = []
        for tx in range(gw):
            x0 = tx * w // gw
            x1 = max(x0 + 1, (tx + 1) * w // gw)
            total = 0
            n = 0
            for y in range(y0, y1):
                base = y * w * 4
                for x in range(x0, x1):
                    i = base + x * 4
                    b, g, r = bgra[i], bgra[i + 1], bgra[i + 2]
                    total += (r * 299 + g * 587 + b * 114) // 1000
                    n += 1
            row.append(total // n if n else 0)
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# GDI+ 精确解码（需要「像素就是原图」时用，别拿去算指纹）
# ---------------------------------------------------------------------------

class _GdipStartupInput(ctypes.Structure):
    _fields_ = [("GdiplusVersion", c_uint32),
                ("DebugEventCallback", c_void_p),
                ("SuppressBackgroundThread", c_int),
                ("SuppressExternalCodecs", c_int)]


class _GdipBitmapData(ctypes.Structure):
    _fields_ = [("Width", c_uint), ("Height", c_uint), ("Stride", c_int),
                ("PixelFormat", c_int), ("Scan0", c_void_p),
                ("Reserved", ctypes.c_size_t)]


# PixelFormat32bppARGB：32 位值 0xAARRGGBB，小端存下来正好是 B,G,R,A 四字节
_PIXFMT_32BPP_ARGB = 0x0026200A
_LOCK_READ = 1
_INTERP_HQ_BICUBIC = 7          # InterpolationModeHighQualityBicubic
_OFFSET_HQ = 2                  # PixelOffsetModeHighQuality

gdiplus.GdiplusStartup.argtypes = [POINTER(ctypes.c_size_t),
                                   POINTER(_GdipStartupInput), c_void_p]
gdiplus.GdiplusStartup.restype = c_int
gdiplus.GdiplusShutdown.argtypes = [ctypes.c_size_t]
gdiplus.GdiplusShutdown.restype = None
gdiplus.GdipCreateBitmapFromFile.argtypes = [wintypes.LPCWSTR, POINTER(c_void_p)]
gdiplus.GdipCreateBitmapFromFile.restype = c_int
gdiplus.GdipCreateBitmapFromScan0.argtypes = [c_int, c_int, c_int, c_int,
                                              c_void_p, POINTER(c_void_p)]
gdiplus.GdipCreateBitmapFromScan0.restype = c_int
gdiplus.GdipGetImageWidth.argtypes = [c_void_p, POINTER(c_uint)]
gdiplus.GdipGetImageWidth.restype = c_int
gdiplus.GdipGetImageHeight.argtypes = [c_void_p, POINTER(c_uint)]
gdiplus.GdipGetImageHeight.restype = c_int
gdiplus.GdipBitmapLockBits.argtypes = [c_void_p, c_void_p, c_uint, c_int,
                                       POINTER(_GdipBitmapData)]
gdiplus.GdipBitmapLockBits.restype = c_int
gdiplus.GdipBitmapUnlockBits.argtypes = [c_void_p, POINTER(_GdipBitmapData)]
gdiplus.GdipBitmapUnlockBits.restype = c_int
gdiplus.GdipDisposeImage.argtypes = [c_void_p]
gdiplus.GdipDisposeImage.restype = c_int
gdiplus.GdipGetImageGraphicsContext.argtypes = [c_void_p, POINTER(c_void_p)]
gdiplus.GdipGetImageGraphicsContext.restype = c_int
gdiplus.GdipSetInterpolationMode.argtypes = [c_void_p, c_int]
gdiplus.GdipSetInterpolationMode.restype = c_int
gdiplus.GdipSetPixelOffsetMode.argtypes = [c_void_p, c_int]
gdiplus.GdipSetPixelOffsetMode.restype = c_int
gdiplus.GdipDrawImageRectI.argtypes = [c_void_p, c_void_p, c_int, c_int, c_int, c_int]
gdiplus.GdipDrawImageRectI.restype = c_int
gdiplus.GdipDeleteGraphics.argtypes = [c_void_p]
gdiplus.GdipDeleteGraphics.restype = c_int

_gdip_token = None
_gdip_lock = threading.Lock()


def _ensure_gdiplus():
    """GDI+ 每进程只要启动一次；多线程下加锁。失败返回 False。"""
    global _gdip_token
    if _gdip_token is not None:
        return True
    with _gdip_lock:
        if _gdip_token is not None:
            return True
        tok = ctypes.c_size_t()
        si = _GdipStartupInput(1, None, 0, 0)
        if gdiplus.GdiplusStartup(byref(tok), byref(si), None) != 0:
            return False
        _gdip_token = tok
        return True


def _gdip_measure(bmp):
    w, h = c_uint(), c_uint()
    if gdiplus.GdipGetImageWidth(bmp, byref(w)) or gdiplus.GdipGetImageHeight(bmp, byref(h)):
        return None
    return int(w.value), int(h.value)


def _gdip_pixels(bmp):
    """把一张 GpBitmap 读成 (宽, 高, BGRA 字节)。"""
    wh = _gdip_measure(bmp)
    if not wh or wh[0] <= 0 or wh[1] <= 0:
        return None
    w, h = wh
    d = _GdipBitmapData()
    rect = (c_int * 4)(0, 0, w, h)
    if gdiplus.GdipBitmapLockBits(bmp, byref(rect), _LOCK_READ,
                                  _PIXFMT_32BPP_ARGB, byref(d)):
        return None
    try:
        if d.Stride == w * 4:
            return w, h, ctypes.string_at(d.Scan0, w * h * 4)
        rows = [ctypes.string_at(d.Scan0 + y * d.Stride, w * 4) for y in range(h)]
        return w, h, b"".join(rows)
    finally:
        gdiplus.GdipBitmapUnlockBits(bmp, byref(d))


def _gdip_scale(bmp, out_w, out_h):
    """用 GDI+ 自己缩（HighQualityBicubic）—— 比在 Python 里遍历像素快几个数量级。"""
    dst = c_void_p()
    if gdiplus.GdipCreateBitmapFromScan0(out_w, out_h, 0, _PIXFMT_32BPP_ARGB,
                                         None, byref(dst)):
        return None
    g = c_void_p()
    if gdiplus.GdipGetImageGraphicsContext(dst, byref(g)):
        gdiplus.GdipDisposeImage(dst)
        return None
    try:
        gdiplus.GdipSetInterpolationMode(g, _INTERP_HQ_BICUBIC)
        gdiplus.GdipSetPixelOffsetMode(g, _OFFSET_HQ)
        if gdiplus.GdipDrawImageRectI(g, bmp, 0, 0, out_w, out_h):
            return None
        return _gdip_pixels(dst)
    finally:
        gdiplus.GdipDeleteGraphics(g)
        gdiplus.GdipDisposeImage(dst)


def load_pixels_exact(path: str, max_side: int | None = None):
    """用 GDI+ 精确解码，返回 (宽, 高, BGRA 字节)，失败返回 None。

    **「精确」是指什么**：不走缩略图缓存、不做色彩管理，
    像素和原图逐字节相同（PNG / JPEG 都验过，和 Pillow 对过）。

    `max_side` 给定时，长边超过它就先用 GDI+ 的高质量双三次缩下来再返回 ——
    这是给「开销随像素数线性涨」的调用方（比如 NIQE）用的，
    否则一张 1200 万像素的照片能算到天荒地老。**缩小会削弱对轻微模糊的灵敏度**，
    所以能不开就不开；但统一缩到同一档至少保证横向可比。

    ⚠️ 不支持 WEBP / HEIC / AVIF（GDI+ 不带这些编解码器）——
    那种文件会返回 None，调用方自己决定要不要退回 `load_pixels()`。
    """
    if not path:
        return None
    path = os.path.abspath(path)
    if not _ensure_gdiplus():
        return None
    bmp = c_void_p()
    if gdiplus.GdipCreateBitmapFromFile(path, byref(bmp)):
        return None
    try:
        wh = _gdip_measure(bmp)
        if not wh:
            return None
        w, h = wh
        if max_side and max(w, h) > max_side:
            if w >= h:
                ow, oh = max_side, max(1, int(round(h * max_side / float(w))))
            else:
                ow, oh = max(1, int(round(w * max_side / float(h)))), max_side
            got = _gdip_scale(bmp, ow, oh)
            if got:
                return got
        return _gdip_pixels(bmp)
    finally:
        gdiplus.GdipDisposeImage(bmp)


def paint_to_png(w: int, h: int, bgra: bytes) -> bytes:
    """BGRA -> PNG 字节（真彩 RGB，不带 alpha）。转交 thumbs 实现。"""
    import thumbs
    return thumbs.bgra_to_png(w, h, bgra)
