# -*- coding: utf-8 -*-
"""图片查重（ImageScout）—— 零第三方依赖的本地工具，Tkinter 界面。

用法：python image_scout.py [目录或图片 ...]

界面一句话概括：**右边那块就是对比区**。

  左  两个视图可切：相似分组 / 全部图片（每项都带小预览）
      下面是当前组的成员（或「与它相似」）
  右  结论条 + 两张图**并排、可滚轮放大** + 两图信息**并排对照**
      + 一排操作按钮

几条刻意为之的地方：

* **选中一组就自动对比**，用的是「代表图 ↔ 与它最像的那张」。想换另一张比，
  在左边点一下就行 —— 不再需要先选图、再点「与代表图对比」。
* **两图信息并排**，同一字段左右对齐，竖着扫一眼就能看出差异。
* **预览尽可能大**：图片按容器尺寸（再乘缩放倍率）向系统解码器要像素
  （带 `SIIGBF_SCALEUP`）。滚轮放大时**重新按放大后的尺寸要一次像素**，
  而不是把已解码的小图硬拉 —— 后者是最近邻插值，越放越糊。
* **删除 = 移动到扫描文件夹下的 `_隔离`**（见 `quarantine.py`），
  扫描时自动跳过它，所以删完再扫不会又冒出来。
* 匹配区域的红框**已按使用者要求去掉**（挡着看图）；要看对应部位用
  「只看匹配区域」直接把那块裁出来看。
* 外观走「低饱和清新通透」：所有控件都来自 `uikit`（自己画的圆角底图），
  配色统一在 `uikit.Palette` 里。
"""

from __future__ import annotations

import math
import os
import queue
import shutil
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def resource_path(name):
    """数据文件的运行时路径：源码跑 = 同目录；打包后 = 解包临时目录。"""
    base = getattr(sys, "_MEIPASS", None)      # PyInstaller onefile 解包目录
    if base:
        cand = os.path.join(base, name)
        if os.path.exists(cand):
            return cand
    return os.path.join(HERE, name)

import imgsize                                    # noqa: E402
import net                                        # noqa: E402
import niqe                                       # noqa: E402
import quarantine                                 # noqa: E402
import scan                                       # noqa: E402
import thumbs                                     # noqa: E402
import uikit                                      # noqa: E402
import winimg                                     # noqa: E402
from uikit import Palette as P                    # noqa: E402

PRESET_ORDER = ("strict", "standard", "loose")

PIC_RADIUS = 11               # 图片预览的圆角（设计稿像素，会过 sc()）
MAX_ZOOM = 8.0                # 解码尺寸 = 适应容器的尺寸 × zoom，上限还是靠 4096 截
ZOOM_MIN, ZOOM_MAX = 0.2, 6.0
ZOOM_STEP = 1.25

# 设 IS_DEBUG=1 时会打印每侧的解码尺寸 / 显示尺寸。
DEBUG = bool(os.environ.get("IS_DEBUG"))

S = uikit.sc                  # 简写（调用时才读当前缩放，所以在这里取没问题）


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def human_size(n) -> str:
    if n is None:
        return "?"
    v = float(n)
    for u in ("B", "KB", "MB", "GB"):
        if v < 1024 or u == "GB":
            return ("%d %s" % (v, u)) if u == "B" else ("%.1f %s" % (v, u))
        v /= 1024.0
    return "%.1f GB" % v


def human_time(ts) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    except (ValueError, OSError, TypeError):
        return "?"


def short_name(name: str, keep: int = 26) -> str:
    if len(name) <= keep:
        return name
    a = keep // 2
    return name[:a] + "…" + name[-(keep - a - 1):]


def preset_short(key: str) -> str:
    lab = scan.PRESETS[key]["label"]
    return lab.split("（")[0].split("(")[0].strip() or key


def quality_of(wh, size=None):
    """图片质量分级 -> (级别, 中文标签)。

    判据只看**能确定的东西**：像素总量、长边、文件体积。不去猜「模糊」——
    没有可信的模糊度指标就不装作有，宁可只标「低清」「一般」。

    级别顺序（差不差）：poor < fair < unknown < good。

    ⚠️ 标签要**如实描述是哪条判据命中的**，别一律叫「低清」：
    一条 560×380 的图分辨率并不低，只是压缩后只剩 9 KB（信息量极少），
    这种叫「体积小」才不撒谎。徽章文字要跟着这个标签走（见 `poor_badge`）。
    """
    if not wh or wh[0] <= 0 or wh[1] <= 0:
        return "unknown", "解析不了"
    long_side = max(int(wh[0]), int(wh[1]))
    mp = (int(wh[0]) * int(wh[1])) / 1e6
    if long_side < 480 or mp < 0.20:
        return "poor", "低清"
    if size is not None and size < 20 * 1024 and long_side < 1024:
        return "poor", "体积小"
    if long_side < 1024 or mp < 0.80:
        return "fair", "一般"
    return "good", "清晰"


def poor_badge(q):
    """质量不行的图要挂的徽章文字；没问题就返回 None。

    直接用 `quality_of` 给的标签，别在外面再写一遍「低清」——
    否则同一张图会「副标题写体积小、徽章写低清」，自相矛盾（这个坑踩过）。
    """
    return q[1] if q and q[0] == "poor" else None


QUALITY_RANK = {"poor": 0, "fair": 1, "unknown": 2, "good": 3}


# ---------------------------------------------------------------------------
# NIQE（无参考图像质量评价）
# ---------------------------------------------------------------------------
#
# 为什么开销要卡死在一处：NIQE 的开销随像素数线性涨，而它是纯 Python 实现
# （没有第三方依赖）。实测 640px 的图约 0.7 秒、960px 约 1.7 秒。
# 所以**统一先缩到长边 640 再算**：
#   * 好处：一批图横着比是公平的，耗时可控，能放进后台线程慢慢算；
#   * 代价：轻微模糊会被缩小这一步抹掉一部分（只看得见「明显劣化」）。
#     实测标定与代价说明见 README「NIQE」那一节，不要当成精确刻度用。

NIQE_MAX_SIDE = 640


def niqe_score(path, max_side=NIQE_MAX_SIDE):
    """算一张图的 NIQE，返回 `(分数, 说明, 有效块数)`；算不了时分数是 None。

    **必须走 `winimg.load_pixels_exact`（GDI+）**，不能用 Shell 缩略图那条路：
    NIQE 对像素级改动极敏感，实测同一张图 Shell 回的像素有 70% 的字节不同，
    分数会从 5.73 漂到 6.21 —— 那样算出来的分拿去排序就是错的。
    GDI+ 解不开的格式（WEBP / HEIC）就**如实说算不了**，绝不退回那条路凑一个数。

    第三个返回值是**块数**，给界面判断这个分数值不值得给档位用：
    少于 `niqe.MIN_BLOCKS` 块连分布都估不出（不是「不准」，是没意义），
    少于 `niqe.SOLID_BLOCKS` 块则分数能给、档位只能当参考。
    标定数据见 niqe.py 顶部 MIN_BLOCKS 那一段注释。
    """
    if not path or not os.path.isfile(path):
        return None, "文件不在了", 0
    got = winimg.load_pixels_exact(path, max_side)
    if not got:
        fmt = "未知格式"
        try:
            fmt = imgsize.format_of(path)
        except Exception:
            pass
        return None, "%s 解不开（GDI+ 不支持这种格式）" % fmt, 0
    w, h, bgra = got
    nb = niqe.blocks_of(w, h)
    if nb < niqe.MIN_BLOCKS:
        # 先按尺寸拒掉，省得白算一遍；niqe_rows 里还有同样一道闸门兜底。
        return None, ("图太小（%d × %d 只能切出 %d 个 %d×%d 的块，至少要 %d 个）"
                      % (w, h, nb, niqe.BLOCK, niqe.BLOCK, niqe.MIN_BLOCKS)), nb
    try:
        score, note = niqe.niqe_bgra(w, h, bgra)
    except Exception as e:
        return None, "算的时候出错（%s）" % type(e).__name__, nb
    return score, note, nb


def niqe_field(path, res):
    """信息卡上「清晰度」那一段文字。`res=None` 表示还在算。"""
    if res is None:
        return "清晰度 计算中…"
    score, note, nb = res
    if score is None:
        # 用 `·` 而不是括号包起来：note 里自己就可能带括号，
        # 套两层会变成「算不了（图太小（…））」这种读不顺的东西。
        return "清晰度 算不了 · %s" % note
    if nb < niqe.SOLID_BLOCKS:
        # 实测（见 niqe.py 顶部）：9 个块估出来的分数能有 ±20% 的抖动，
        # 1~2 档的差距就这么被抹平了。所以分数照给，但**不声称档位**。
        return "清晰度 NIQE %.2f（只 %d 块，档位只作参考）" % (score, nb)
    return "清晰度 NIQE %s" % niqe.brief(score)


class NiqeCache:
    """按需算 NIQE：一个后台线程 + 一个按 (路径, mtime) 记的结果缓存。

    两个必须这么做的理由：
      * 0.7 秒/张，放主线程界面会卡一下 —— 用户明确讨厌卡顿；
      * 来回点同一张图不该重复算（切开看、切回来、换对比对象都很频繁）。
    mtime 一起进 key，是因为隔离/移动之后同名文件可能是另一张图。
    """

    def __init__(self, to_ui, limit=400):
        self.to_ui = to_ui                 # 把回调送回主线程（App._ui）
        self.limit = limit
        self._c = {}                       # path -> (mtime, (score, note))
        self._cb = {}                      # path -> [回调]
        self._busy = set()
        self._q = queue.Queue()
        self._lock = threading.Lock()
        threading.Thread(target=self._loop, daemon=True).start()

    def cached(self, path):
        with self._lock:
            got = self._c.get(path)
        if not got:
            return None
        try:
            mt = os.stat(path).st_mtime
        except OSError:
            return None
        return got[1] if got[0] == mt else None

    def request(self, path, on_done):
        """排队算；`on_done(path, (score, note))` 会在**主线程**被调用。"""
        if not path:
            return None
        got = self.cached(path)
        if got is not None:
            self.to_ui(on_done, path, got)          # 已有结果，直接回调
            return got
        with self._lock:
            self._cb.setdefault(path, []).append(on_done)
            if path not in self._busy:
                self._busy.add(path)
                self._q.put(path)
        return None

    def _fire(self, path, res):
        with self._lock:
            cbs = self._cb.pop(path, [])
        for fn in cbs:
            self.to_ui(fn, path, res)

    def _loop(self):
        while True:
            path = self._q.get()
            try:
                res = niqe_score(path)
            except Exception as e:                  # 线程里绝不能抛出去
                res = (None, "内部错误（%s）" % type(e).__name__)
            try:
                mt = os.stat(path).st_mtime
            except OSError:
                mt = None
            with self._lock:
                self._busy.discard(path)
                self._c[path] = (mt, res)
                if len(self._c) > self.limit:       # 别无限涨
                    for k in list(self._c)[:len(self._c) // 4]:
                        del self._c[k]
            self._fire(path, res)

    def drop(self, path=None):
        with self._lock:
            if path is None:
                self._c.clear()
            else:
                self._c.pop(path, None)


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------

class Meta:
    """图片的「真实信息」缓存 —— 读文件头很便宜，但别在滚动时反复读。"""

    def __init__(self):
        self._c = {}

    def of(self, path: str) -> dict:
        got = self._c.get(path)
        if got is not None:
            return got
        d = {"path": path, "name": os.path.basename(path),
             "dir": os.path.dirname(path),
             "size": None, "format": "?", "wh": None, "mtime": None}
        try:
            st = os.stat(path)
            d["size"] = st.st_size
            d["mtime"] = st.st_mtime
        except OSError:
            pass
        try:
            d["format"] = imgsize.format_of(path)
        except Exception:
            pass
        try:
            d["wh"] = imgsize.size_of_file(path)
        except Exception:
            pass
        self._c[path] = d
        return d

    def quality(self, path: str):
        m = self.of(path)
        return quality_of(m["wh"], m["size"])

    def drop(self, path: str):
        self._c.pop(path, None)


META = Meta()


class ThumbCache:
    """小缩略图 -> 带圆角的 tk.PhotoImage。

    PhotoImage 必须有人持有引用，否则被 GC 掉就显示成空白 —— 这里统一存住。
    """

    def __init__(self, root: tk.Misc):
        self.root = root
        self._c = {}

    def get(self, path: str, size: int, radius: int = 0, page=None):
        key = (path, size, radius, page)
        if key in self._c:
            return self._c[key]
        img = None
        got = winimg.load_pixels(path, size, winimg.SIIGBF_RESIZETOFIT, raw=True)
        if got:
            w, h, bgra = got
            try:
                png = (uikit.rounded_png(w, h, bgra, radius, page)
                       if radius else thumbs.bgra_to_png(w, h, bgra))
                img = tk.PhotoImage(data=png, master=self.root)
            except Exception:
                img = None
        self._c[key] = img
        if len(self._c) > 500:               # 别无限涨
            for k in list(self._c)[:150]:
                del self._c[k]
        return img

    def drop(self, path: str):
        for k in [k for k in self._c if k[0] == path]:
            del self._c[k]


class BigCache:
    """大预览的解码缓存。

    ⚠️ 别再按「2 的幂 / 256 的倍数」给尺寸分档了：分档之后很容易解出一张比
    需要的**大一倍**的图（横图和方图在同一个长边档下，方图那一维会超出容器），
    于是又得让 Tk 的 `subsample` 兜底缩一次 —— 白解一遍大图，画质还多损一道。
    这里改成「需求落在已缓存的 1.15 倍以内就复用，否则重解」，
    解码尺寸始终贴着真实需求走。

    ⚠️ 除了 BGRA 还缓存 **`PhotoImage`**（`_p`），这是 2026-10-05 修「选一组卡
    672ms」的关键：实测 3600×2400 的图，**编码成给 Tk 的字节要 145ms、Tk 再
    解成图要 38ms** —— 也就是说，光「把已经解码好的像素变成 Tk 图」就
    比解码本身还贵。以前每渲染一次都重做一遍，切回同一组、滚轮回到同一档
    就白白再付 180ms。现在按 (路径, 需求尺寸, 容器尺寸) 存成品图，
    命中就直接 `create_image`，只剩 ~40ms 的贴图开销。
    """

    # ⚠️ 别按「个数」淘汰：一张 3600×2400 的 BGRA 是 34MB，一张 400×300 才
    #    0.5MB。按个数只留 5 个时，在 6 个组之间来回点就**全部被挤掉** ——
    #    实测选一组还是 82~152ms（看着像没缓存，其实是缓存太小）。
    #    改成按**字节预算**淘汰：填满预算为止，热的那几张才真的留得住。
    BYTE_BUDGET = 192 * 1024 * 1024      # 原始像素
    PHOTO_BUDGET = 256 * 1024 * 1024     # 成品 Tk 图（Tk 那边大约也是 4 字节/px）
    # 基准像素（给缩放取块用）：分 256 一档，多家共用。
    BASE_MAX = 4096
    BASE_STEP = 256
    BASE_BUDGET = 320 * 1024 * 1024

    def __init__(self, limit: int = 5):
        self._c = {}
        self._p = {}
        self._b = {}                      # 基准像素：path -> (w, h, bgra, max_side)
        self._c_bytes = 0
        self._p_bytes = 0
        self._b_bytes = 0

    @staticmethod
    def _psize(img):
        try:
            return img.width() * img.height() * 4
        except Exception:
            return 0

    def _evict_c(self):
        while self._c_bytes > self.BYTE_BUDGET and len(self._c) > 1:
            k = next(iter(self._c))
            self._c_bytes -= len(self._c.pop(k)[3])

    def _evict_p(self):
        while self._p_bytes > self.PHOTO_BUDGET and len(self._p) > 1:
            k = next(iter(self._p))
            self._p_bytes -= self._psize(self._p.pop(k))

    def _level(self, near: int, native: int = 0) -> int:
        """把「要多大」量化到 256 一档 —— 同一档能被不同 zoom 共用。

        ⚠️ `native` 是原图长边，**必须封顶**：原图 3000×2000 的照片，
        要 4096 那一档的话解码器会拿 SCALEUP 硬放大到 4096×2731 ——
        像素数凭空多 37%，什么新信息都没有，白花 83ms + 43MB 内存。
        最高只需要原图那么大。
        """
        near = max(64, int(near))
        lv = min(self.BASE_MAX,
                 int(math.ceil(near / float(self.BASE_STEP))
                     * self.BASE_STEP))
        if native > 0:
            lv = min(lv, int(native))
        return max(64, lv)

    def peek_base(self, path: str, near: int = 0, native: int = 0):
        """已有档里**最小的、够用的**那一档（**绝不解码**）。

        放wheel 的快速档要用它 —— 那时候绝不能触发解码，否则就是卡顿本身。
        """
        lv = self._level(near, native)
        d = self._b.get(path)
        if not d:
            return None
        best = None
        for k in d:
            if k >= lv and (best is None or k < best):
                best = k
        if best is None:
            return None
        got = d[best]
        d[best] = d.pop(best)                    # 挪到末尾，算 LRU
        return got[0], got[1], got[2]

    def peek_best(self, path: str, near: int, native: int = 0):
        """已有档里**不超过要用的、最清晰的**那一档（**绝不解码**）。

        ⚠️ 为什么不能退回 `peek_any`（最粗的那一档）：精确帧是要给用户看清楚
        的，退回最粗档之后**块的尺寸就变了**，成品图的 key 跟着变 —— 于是
        刚才异步算好的那一块全白算，主线程还得再算一遍（实测 28.7ms 就是
        这么来的）。拿最接近的清晰档，key 对得上，缓存直接命中。
        """
        lv = self._level(near, native)
        d = self._b.get(path)
        if not d:
            return None
        best = None
        for k in d:
            if k <= lv and (best is None or k > best):
                best = k
        got = d.pop(best) if best is not None else None
        if got is None:
            # 手上的档都比要用的大：那就取最小的够用档（多出来的清晰度白给）
            return self.peek_base(path, near, native)
        d[best] = got                             # 挪到末尾，算 LRU
        return got[0], got[1], got[2]

    def peek_any(self, path: str):
        """手上**任意**一档（优先最小的），**绝不解码**。

        给滚轮的快速档兜底：合适那一档还没准备好时，宁可先拿粗的顶上
        （会糊一点，但几毫秒就画好了），也绝不能在主线程上解码 ——
        那一解就是 100~300ms，正好是主人说的「卡一下」。
        """
        d = self._b.get(path)
        if not d:
            return None
        lv = min(d)
        got = d[lv]
        d[lv] = d.pop(lv)
        return got[0], got[1], got[2]

    def base(self, path: str, near: int = 0, native: int = 0):
        """取一级基准像素（按 256 分档，多家共用）。

        ⚠️ 为什么必须分档：固定用 2048 那一档时，**块的大小跟 zoom 成反比**
        ——zoom 小的时候块接近整张基准（3.1M 像素），实测缩小时要 200~342ms，
        比放大还贵。分档之后「视口该多大就取哪一档」，块始终 ≈ 视口大小，
        成本跟 zoom 基本无关。
        """
        lv = self._level(near or self.BASE_MAX, native)
        d = self._b.setdefault(path, {})
        got = d.get(lv)
        if got is not None:
            d[lv] = d.pop(lv)
            return got[0], got[1], got[2]
        r = winimg.load_pixels(path, lv,
                               winimg.SIIGBF_RESIZETOFIT | winimg.SIIGBF_SCALEUP,
                               raw=True)
        if not r:
            return None
        d[lv] = (r[0], r[1], r[2])
        self._b_bytes += len(r[2])
        self._evict_b()
        return r

    def _evict_b(self):
        while self._b_bytes > self.BASE_BUDGET:
            oldest = None
            for p, d in self._b.items():
                if oldest is None:
                    oldest = p
                if len(d) > 1 or len(self._b) > 1:
                    break
            d = self._b.get(oldest)
            if not d:
                break
            k = next(iter(d))
            if len(d) == 1 and len(self._b) == 1:
                break                            # 只剩最后一张，别把自己删空
            self._b_bytes -= len(d.pop(k)[2])
            if not d:
                self._b.pop(oldest, None)

    def get(self, path: str, need: int, force: bool = False):
        need = max(64, int(need))
        got = self._c.get(path)
        if not force and got is not None and need <= got[0] <= need * 1.15:
            self._c[path] = self._c.pop(path)        # 触碰一下，算 LRU
            return got[1], got[2], got[3]
        if got is not None:
            self._c_bytes -= len(got[3])
        # SIIGBF_SCALEUP：允许系统把图放大到我们要的尺寸（不加这个标志它只缩不放）。
        # raw=True：预览不参与指纹，走单次读取，尺寸精确（见 winimg.load_pixels）
        r = winimg.load_pixels(path, need,
                               winimg.SIIGBF_RESIZETOFIT | winimg.SIIGBF_SCALEUP,
                               raw=True)
        if not r:
            return None
        self._c[path] = (need, r[0], r[1], r[2])
        self._c_bytes += len(r[2])
        self._evict_c()
        return r

    def peek(self, path: str):
        """只取缓存里已有的（**任何尺寸**），没有就返回 None —— 绝不重解。

        给滚轮缩放的**快速档**用：连滚时每一格都同步重解码大图（几十到
        几百毫秒）就是「缩放好卡」的根源。快速档先用缓存里那张顶上
        （尺寸可能不贴，稍微糊/偏一点），随后防抖一帧按新 zoom 精确重解。
        """
        got = self._c.get(path)
        if got is None:
            return None
        self._c[path] = self._c.pop(path)        # 触碰一下，算 LRU
        return got[1], got[2], got[3]

    # ---- 成品 Tk 图（PhotoImage）的缓存 --------------------------------
    def photo(self, key):
        """按 (路径, 需求尺寸, 容器宽, 容器高) 取已经做好的 Tk 图。"""
        got = self._p.get(key)
        if got is not None:
            self._p[key] = self._p.pop(key)      # LRU
        return got

    def put_photo(self, key, img):
        old = self._p.pop(key, None)
        if old is not None:
            self._p_bytes -= self._psize(old)
        self._p[key] = img
        self._p_bytes += self._psize(img)
        self._evict_p()

    def drop(self, path: str):
        got = self._c.pop(path, None)
        if got is not None:
            self._c_bytes -= len(got[3])
        gotb = self._b.pop(path, None)
        if gotb is not None:
            for _lv, v in gotb.items():
                self._b_bytes -= len(v[2])
        for k in [k for k in self._p if k[0] == path]:
            self._p_bytes -= self._psize(self._p.pop(k))


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------

class App(tk.Tk):

    def __init__(self, argv):
        # DPI 感知必须在建窗口之前开（幂等，main() 里也会先调一次）
        uikit.enable_dpi_awareness()
        super().__init__()
        # 开了感知之后 1 逻辑像素 = 1 物理像素，写死的像素值要按 DPI 放大。
        # 字不用管：用的是点，Tk 按 tk scaling 自己换算。
        uikit.set_scale(self.winfo_fpixels("1i") / 96.0)

        self.title("图片查重 · ImageScout")
        # 窗口/任务栏图标：源码跑用同目录 app.ico，打包后从解包目录拿
        try:
            ico = resource_path("app.ico")
            if os.path.exists(ico):
                self.iconbitmap(ico)
        except tk.TclError:
            pass                      # 图标失败不影响主功能
        # 别写死尺寸：屏幕小的时候会把底部的操作按钮顶出可视区。
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w = min(S(1420), max(S(1120), sw - S(60)))
        h = min(S(920), max(S(680), sh - S(100)))
        self.geometry("%dx%d+%d+%d" % (w, h, max(0, (sw - w) // 2),
                                       max(0, (sh - h) // 3)))
        self.minsize(S(1060), S(660))
        self.configure(bg=P.PAGE)

        self.thumb = ThumbCache(self)
        self.big = BigCache()
        # NIQE 后台算（详见 NiqeCache 的注释）；_niqe_lbl 记着信息卡里那一行，
        # 结果回来时只改那一段文字，不整块重建（重建会闪一下）
        self.niqec = NiqeCache(self._ui)
        self._niqe_lbl = {}
        self.store = scan.FingerStore(scan.default_cache_path())
        self.descs = {}
        self.links = []
        self.groups_raw = []      # 真组
        self.groups = []          # 真组 + 「疑似对」伪组，界面统一处理
        self.weak = []
        self.files = []
        self.roots = []
        self.group = None         # 当前组（dict）
        self.gidx = -1
        self._group_mem = 0       # 上次看的是第几组（切到「全部图片」再切回来要用）
        self.cur_member = None
        # ⚠️ 组内 **3 张及以上**时，用户在成员列表里自己点选的那两张
        #    （v1.10，主人 2026-10-07：「本组成员里面如果 3 个及以上
        #     最好是自定义组员图片来对比」）。
        #    最多两项：`[A, B]`；空列表 = 还没自定义，沿用 `_auto_pair`。
        self.pick_pair = []
        # 撤销栈：每次「移入隔离夹」压一条**完整现场快照**（见 `_push_undo`）。
        self.undo_stack = []
        self.path_a = None
        self.path_b = None
        self.pair = None
        self.rect_a = None
        self.rect_b = None
        self.stop_flag = False
        self.busy = False
        self.t_start = 0.0
        self.mode = "pair"
        self.view = "groups"      # groups / all
        # 排列方向（v1.9）。False = 默认（分组按张数降序、质量差的排前面），
        # True = 整体倒序。见 `toggle_sort`。
        self.sort_rev = False
        # 缩放是**每一侧各一份**：滚轮只作用于鼠标底下那一张。
        # 早先是一个全局值，一滚两边一起变大，想「A 放大看细节、B 保持全貌」就做不到。
        self.zoom = [1.0, 1.0]
        self.pan = [[0, 0], [0, 0]]
        self._drag = None
        # ⚠️⚠️ **不能用 `0.0` 当「还没重取过」的初值**（v1.7 修）。
        # 判据是 `now - self._drag_last >= PAN_LAG`，而 `now` 是
        # `time.time()`（秒级时间戳，1.7e9 量级）—— 于是
        # `1.7e9 - 0.0 >= 0.02` **第一次拖动就恒真**，
        # `PAN_LAG` 这个节流**从头到尾一次都没生效过**。
        # 后果：拖动时每一步都重画（实测 59/60 步，每步 65~104ms
        # = 丢掉 4~6 帧），这就是主人说的「拖拽有抖动」。
        #
        # 用 `None` 表达「还没重取过」，第一次无条件放行（对），
        # 之后才真的按 PAN_LAG 限流。
        self._drag_last = None        # 拖动时「上一次重取一块」的时刻（节流用）
        # ⚠️ 「当前这一帧是不是拖动补块」—— `_render_tile` 据它决定缓冲用
        #   厚还是薄（滚轮和拖动传的都是 `precise=0.75`，需求相反，
        #   详见 `_render_tile` 里 `_pad_scale` 那段注释）。
        self._pan_redraw_active = False
        # ⚠️ **联动对齐的「待办」**：侧 -> ((nx, ny), zoom)。
        # 另一侧画布还没布局好时 `_link_apply` 只存不套，
        # 等它真出图（`_render_side` 建好 `_view` 之后）再取用。
        # 不存的话，「只在第一格错」的偏差会永久留在画面上
        # （见 `_link_apply` 里 v1.8 那段实测记录）。
        self._link_pend = {}
        self._prewarm_q = []
        self._prewarm_busy = False
        self._prewarm_pause = 0.0
        # ⚠️ 拖动中攒下的预热字节（`_prewarm_done` 挂起的），松手再建 Tk 图
        self._prewarm_hold = None
        self._base_busy = set()       # 正在后台解码的 (路径, 档位)
        self._base_prep_done = set()  # 解过（成功/失败都记，免得反复重试）
        self._base_want = {}          # 想要但还没动手的 (路径, 档位) -> 参数
        self._q = queue.Queue()
        self._later_ids = {}
        # 进度回调的节流状态（详见 `_progress` 的注释）
        self._prog_pend = None    # 还没落地的 (frac, text)
        self._prog_t = 0.0        # 上次落地的时间
        self._keep = []           # 主预览图的引用
        self._view = {}           # 侧 -> {画布, 图元, 出图时的位置/平移量, 显示尺寸}
        self._fit_pend = {}       # 侧 -> 正在后台编码的那一帧（防重算 / 防张冠李戴）
        self._fit_pend_state = {}  # 侧 -> 发起时的(zoom, 平移)，回来时比对用
        self._info_min_h = 0      # 信息卡被「只增不减」钉住的高度（见 _fit_card）

        self.preset_key = scan.DEFAULT_PRESET
        self.recursive_var = tk.BooleanVar(value=True)
        self.crop_only = tk.BooleanVar(value=False)
        # ⚠️⚠️ **双图联动**：默认**关**。
        #
        # 为什么默认关：并排对比时两张图**内容不同、构图不同**，锁定缩放后
        # 一张图看到的脸在另一张上根本不在同一处，联动会让人找不着北。
        # 所以这是**给「要看同一处细节」准备的**（比如查同源图有没有被裁改），
        # 由用户主动打开。
        self.link_view = tk.BooleanVar(value=False)

        self._fonts()
        self._style()
        self._build()
        for a in argv:
            if os.path.exists(a):
                self.roots.append(os.path.abspath(a))
        self._refresh_roots()

    # -- 字体 / ttk 样式 --------------------------------------------------
    def _fonts(self):
        f = lambda s, b=False: uikit.ui_font(self, s, b)      # noqa: E731
        self.f_h1 = f(15, True)
        self.f_h2 = f(11, True)
        self.f_txt = f(10)
        self.f_small = f(9)
        self.f_tiny = f(8)
        self.f_key = f(9, True)      # 说明弹窗里的「键名」列

    def _style(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=P.PAGE, foreground=P.TEXT,
                     font=(uikit.pick_family(self), 9))
        st.configure("TProgressbar", background=P.PRIMARY,
                     troughcolor=P.CARD_SOFT, bordercolor=P.PRIMARY_L,
                     lightcolor=P.PRIMARY_L, darkcolor=P.PRIMARY,
                     thickness=S(8))
        st.configure("TCombobox", padding=(6, 3))

    # -- 骨架 ------------------------------------------------------------
    def _build(self):
        root = tk.Frame(self, bg=P.PAGE)
        root.pack(fill="both", expand=True, padx=S(12), pady=S(10))
        # ⚠️ 存一份（原来只有局部变量）：测试要拿它来验「快捷键绑在哪、
        #    焦点让路」——`self` 是 Tkinter 的 Misc 子类，`focus_get`
        #    在它上面能问，但造一个真拿到焦点的 Entry 需要 `Toplevel(root)`。
        self._frame = root

        self._build_top(root)

        body = tk.Frame(root, bg=P.PAGE)
        body.pack(fill="both", expand=True, pady=(S(8), 0))
        self._build_left(body)
        self._build_right(body)
        self._fill_info()

        self.after(60, self._pump_queue)      # 队列泵只能由主线程启动
        self._bind_keys(root)

    # ---- 快捷键 --------------------------------------------------------
    def _bind_keys(self, root):
        """键盘快捷键（2026-10-06 主人加的）。

        主人要的是「方向键或者 wasd 可以快捷换组、删除」。
        绑定在**根窗口**而不是各个列表上，这样不管焦点在哪都能用 ——
        用户点完图预览之后焦点通常不在列表上，绑在列表上等于没绑。

        ⚠️⚠️ **必须在输入框获得焦点时让路**。没有这一步的话，
        用户在「添加文件夹」那类 Entry 里按 Delete 想删字，结果把图删了 ——
        这种误删要弹确认框都救不回来（用户会一路按 Yes）。
        """
        def on_key(ev):
            # ⚠️⚠️ **说明弹窗开着时一律让路**（v1.11）。弹窗只是「挡在前面」，
            #    并不会阻止 `bind_all` 的键盘事件 —— 用户在说明里按
            #    A / D / Delete，**后台的图会照样被移除**，而他完全看不见。
            if getattr(self, "_help_win", None) is not None:
                return
            # 焦点在输入类控件里 -> 只放行编辑键，其余一律不抢
            w = self.focus_get()
            if isinstance(w, (tk.Entry, tk.Text)) or \
                    (w is not None and str(w).startswith("tkinter.Entry")):
                return
            k = (ev.keysym or "").lower()
            ch = (ev.char or "").lower()
            # Ctrl+Z = 撤销（v1.11）。⚠️ state 的 0x4 位就是 Ctrl。
            if k == "z" and ((ev.state or 0) & 0x4):
                self.undo()
                return "break"
            step = None
            if k in ("up", "w"):
                step = -1
            elif k in ("down", "s"):
                step = 1
            elif k in ("left", "a"):
                self._key_delete()
                return
            elif k in ("right", "d"):
                self._key_delete(side=1)
                return
            elif k in ("delete", "backspace"):
                self._key_delete()
                return
            elif k == "0":
                self.reset_zoom()
                self._toast("两侧都回到适应窗口")
                return
            elif ch in ("j",):
                step = 1
            elif ch in ("k",):
                step = -1
            if step is not None:
                self._key_move(step)
            return "break" if (step is not None or k in
                               ("a", "d", "left", "right", "delete",
                                "backspace", "0")) else None

        try:
            root.bind_all("<Key>", on_key)
        except tk.TclError:
            pass
        # ⚠️ 挂成属性，测试才能**直接调它**验「焦点在输入框时让路」。
        #    用 `event_generate("<Key>")` 派发是派发不到的（那是模拟真实
        #    键盘输入，测试环境没有焦点链），第一版就靠「派发不到就直调
        #    兜底」蒙过去 —— 那是假测：断言过了，但派发那条路根本没验证。
        self._on_key = on_key

    def show_help(self):
        """「说明 / 快捷键」弹窗（v1.11，主人：「右上可以添加一个说明按钮」）。

        ⚠️⚠️ 打开时必须让 `_bind_keys` 的 `on_key` **让路**（见那里的
        `_help_win` 判断）：不然弹窗挡在前面，用户在说明里按 A / D /
        Delete，**后台的图照样被删掉** —— 而且他完全看不见发生了什么。
        """
        if getattr(self, "_help_win", None) is not None:
            try:
                self._help_win.lift()
                self._help_win.focus_force()
                return
            except tk.TclError:
                self._help_win = None

        keys = [
            ("↑ / W", "上一张（上一组 / 上一张图）"),
            ("↓ / S", "下一张"),
            ("J / K", "同上（vim 风格）"),
            ("← / A", "移除左侧这一张"),
            ("→ / D", "移除右侧这一张"),
            ("Delete", "移除当前这一侧"),
            ("Ctrl + Z", "撤销上一次移除"),
            ("0", "缩放回到「适应窗口」"),
            ("滚轮", "在图上滚 = 缩放"),
            ("按住拖动", "平移画面"),
            ("双击图片", "交给系统看图器打开"),
            ("双击左栏", "换主图 / 打开这一张"),
        ]
        funcs = [
            ("添加文件夹", "选一个目录来查重（可以加多个）"),
            ("添加图片", "直接挑几张图丢进来"),
            ("含子文件夹", "连子目录一起扫"),
            ("灵敏度", "严格 / 标准 / 宽松，越严越少误报"),
            ("相似分组", "张数多的在前；同数量比质量、再比组号"),
            ("全部图片", "不看分组，把所有图按质量平铺"),
            ("排列 ↓ / ↑", "一键倒序"),
            ("本组成员", "3 张以上可点两张自己比（挂 A / B）"),
            ("只看匹配区域", "并排时只显示两张相同的部分"),
            ("双图联动", "缩放和平移同时作用到两侧"),
            ("定位 A / B", "在资源管理器里选中这个文件"),
            ("复制 A / B", "把图放进剪贴板，可以直接粘贴"),
            ("移除 A / B", "移进「_隔离」夹，不是删除；能撤销"),
            ("撤销", "把上一次移除的图搬回原位"),
            ("清理完全重复", "只清哈希完全一样的，留最好的一张"),
        ]

        w = tk.Toplevel(self)
        self._help_win = w
        w.title("说明 / 快捷键")
        w.configure(bg=P.PAGE)
        w.resizable(False, False)
        try:
            w.transient(self)
        except tk.TclError:
            pass

        tk.Label(w, text="图片查重 · 使用说明", bg=P.PAGE, fg=P.TEXT,
                 font=self.f_h1, anchor="w").pack(
                     fill="x", padx=S(16), pady=(S(14), S(3)))
        tk.Label(w, text="这里的「移除」都是移进「_隔离」文件夹，不是真删除 ——"
                         "随时可以撤销，或者把文件拖回原目录。",
                 bg=P.PAGE, fg=P.TEXT_2, font=self.f_small,
                 anchor="w").pack(fill="x", padx=S(16), pady=(0, S(10)))

        body = tk.Frame(w, bg=P.PAGE)
        body.pack(fill="both", expand=True, padx=S(16))
        self._help_col(body, "快捷键", keys, (0, S(10)))
        self._help_col(body, "功能", funcs, (0, 0))

        foot = tk.Frame(w, bg=P.PAGE)
        foot.pack(fill="x", padx=S(16), pady=(S(12), S(14)))
        uikit.RoundButton(foot, "知道了", command=self._close_help,
                          kind="primary", page=P.PAGE, size=10,
                          padx=S(26), pady=S(8)).pack(side="right")

        w.bind("<Escape>", lambda e: self._close_help())
        w.protocol("WM_DELETE_WINDOW", self._close_help)
        w.update_idletasks()
        try:
            x = self.winfo_rootx() + (self.winfo_width() - w.winfo_width()) // 2
            y = self.winfo_rooty() + (self.winfo_height() - w.winfo_height()) // 3
            w.geometry("+%d+%d" % (max(0, x), max(0, y)))
        except tk.TclError:
            pass
        w.focus_force()

    def _help_col(self, parent, title, pairs, pad):
        """说明弹窗里的一栏（标题 + 若干「键名 -> 说明」行）。

        ⚠️ 用 `grid` 而不是 pack 排「键名 / 说明」两列：中文键名
        （「按住拖动」）和拉丁键名（「Ctrl + Z」）的**显示宽度差一倍**，
        用 `Label(width=N)` 对齐会歪。grid 的两列各自按最宽的那条对齐。
        """
        card = tk.Frame(parent, bg=P.CARD)
        card.pack(side="left", fill="both", expand=True, padx=pad)
        card.columnconfigure(1, weight=1)
        tk.Label(card, text=title, bg=P.CARD, fg=P.TEXT, font=self.f_h2,
                 anchor="w").grid(row=0, column=0, columnspan=2, sticky="w",
                                  padx=S(14), pady=(S(12), S(8)))
        for i, (k, v) in enumerate(pairs):
            tk.Label(card, text=k, bg=P.CARD, fg=P.PRIMARY_D,
                     font=self.f_key, anchor="w").grid(
                         row=i + 1, column=0, sticky="w",
                         padx=(S(14), S(10)), pady=S(3))
            tk.Label(card, text=v, bg=P.CARD, fg=P.TEXT_2,
                     font=self.f_small, anchor="w", justify="left").grid(
                         row=i + 1, column=1, sticky="w",
                         padx=(0, S(14)), pady=S(3))
        tk.Frame(card, bg=P.CARD, height=S(10)).grid(row=len(pairs) + 1, column=0)
        return card

    def _close_help(self):
        # ⚠️ 用 getattr 兜底：这个方法会被 `WM_DELETE_WINDOW` / Escape 绑定调用，
        #    也可能在「从没开过弹窗」时被直接调（探针就会这么干）——
        #    直接 `self._help_win` 会 AttributeError 把回调炸掉。
        w, self._help_win = getattr(self, "_help_win", None), None
        if w is not None:
            try:
                w.destroy()
            except tk.TclError:
                pass

    def _key_move(self, step):
        """上下 / W S：切到上下一组（分组视图）或上下一张（全部图片视图）。

        ⚠️ 两种视图语义不同：分组视图换的是「组」，全部图片视图没有组，
        换的就是「张」。别混 —— 混了会出现「按 Down 什么都没发生」。
        """
        if self.view == "groups":
            if not self.groups:
                return
            n = len(self.groups)
            gi = (self.gidx + step) % n if self.gidx >= 0 else \
                (0 if step > 0 else n - 1)
            # ⚠️ `glist.select` 在「目标就是当前选中」时直接 return
            #（它只重画旧行+新行）。首次按键时 `self.gidx` 可能是 0 而
            #    列表 `sel` 还没同步，按 Down 就**没反应**。
            #    `_sync_list_sel` 统一处理（两个视图都要，别只改一处）。
            self._sync_list_sel(gi)
            self.select_group(gi)
            self._toast("第 %d / %d 组" % (gi + 1, n))
        else:
            # ⚠️⚠️⚠️ **必须按「列表顺序」走，不能按 `self.files` 的顺序**
            #（v1.7 修，主人会真按的）。
            #
            # `_fill_all_list` 把列表**按质量重排**了
            #（`QUALITY_RANK` + 文件名），所以 `glist.items` 的顺序
            # 跟 `self.files` **不一样**。实测 59 张图里：
            #     列表第 0 行 = ...\00_wave\crop_16_9.png -> files 下标 1
            #     files[0]    = ...\低清小图.png
            # 我原来用 `files.index(path_a)` 算下标，于是「按 ↓ 换到下一张」
            # 换的是**列表里看不见的那一张** —— 用户连按几下，屏幕上的行
            # 和右边显示的图对不上，看起来就是「按键坏了」。
            #
            # 现在直接读列表：用户看到什么顺序，按键就走什么顺序。
            order = [it["tag"] for it in (getattr(self.glist, "items", None)
                                          or [])]
            if not order:
                order = list(self.files or [])
            if not order:
                return
            cur = self.path_a
            try:
                i = order.index(cur)
            except ValueError:
                # 当前图不在列表里（刚被删 / 列表重排过）-> 从头/从尾开始
                i = -1
            j = (i + step) % len(order) if i >= 0 else \
                (0 if step > 0 else len(order) - 1)
            # ⚠️ `glist.select` 在「目标就是当前选中」时直接 return（只重画
            # 旧行+新行），首次按键时列表 `sel` 还没跟 `path_a` 同步，
            # 按 Down 会**没反应**。`_sync_list_sel` 统一处理。
            self._sync_list_sel(j)
            self._show_all_item(order[j])

    def _sync_list_sel(self, idx):
        """把分组/图片列表的选中态对齐到第 `idx` 行（越界就不动）。

        ⚠️ `glist.select()` 在「目标就是当前选中」时直接 return（只重画
        旧行+新行），所以首次按键时 `gidx`/`path_a` 跟列表 `sel` 可能不一致，
        按 Down 就**没反应**。这里先调 `select` 让它自己走正常的通知流程，
        不一致时再兜底直接设 `sel` 并重画那两行。
        """
        items = getattr(self.glist, "items", None) or []
        if not (0 <= idx < len(items)):
            return
        try:
            self.glist.select(idx, notify=True)
        except TypeError:
            # 老签名没有 notify 参数
            try:
                self.glist.select(idx)
            except Exception:
                pass
        except Exception:
            pass
        if getattr(self.glist, "sel", None) != idx:
            try:
                self.glist.sel = idx
                self.glist._redraw_rows([idx])
            except Exception:
                pass

    def _key_delete(self, side=0):
        """Delete / A / D / Backspace：删掉当前这一侧的图。

        ⚠️ 复用 `delete_side`，不要另写一套 —— 删完之后的
        列表/统计/结论的整套重算都在它里面。

        ⚠️⚠️ **单图模式下按 `D`（删右侧）不能静默改成删左侧**。
        原来 `col = side if (mode == "pair" and side == 1) else 0` 一把
        把 side 吞掉 —— 单图模式下根本没有右侧，按 `D` 却在删**当前
        正在看的那张**。用户以为「删右边」，实际删的是眼前这张。

        ⚠️ v1.10 起 `delete_side` **不再弹确认框**（移入隔离夹可逆，
        主人明确要求去掉），所以这里**更要**把「删的是哪一张」说清楚、
        并在删除前把左右两侧区分开 —— 以前那道帘子没了。
        """
        pair = (self.mode == "pair" and bool(self.path_b))
        if side == 1 and not pair:
            self._toast("现在只有一张图 —— 删的是正在看的这张")
            self._toast("（想换另一张：按 ↑↓ 或 W S）")
            side = 0
        if not self.path_a:
            self._toast("还没有图可删")
            return
        self.delete_side(0 if side == 0 else 1)

    # ---- 顶栏 ----------------------------------------------------------
    def _build_top(self, root):
        card = uikit.Card(root, radius=16, pad=S(8))
        card.pack(fill="x")
        bar = card.body

        # 所有按钮挤在**同一行**、从左往右连着排（之前「开始扫描」被顶到最右边，
        # 离别的按钮老远，用起来要来回找）。常用按钮放大一号。
        row = tk.Frame(bar, bg=P.CARD)
        row.pack(fill="x", anchor="w")
        tk.Label(row, text="图片查重", bg=P.CARD, fg=P.TEXT,
                 font=self.f_h1).pack(side="left", padx=(S(8), S(14)))

        def B(txt, cmd, kind="ghost", size=10, padx=S(15), pady=S(7),
              bold=False, side="left", gap=(0, S(7))):
            b = uikit.RoundButton(row, txt, command=cmd, kind=kind,
                                  page=P.CARD, size=size, padx=padx,
                                  pady=pady, bold=bold)
            b.pack(side=side, padx=gap)
            return b

        B("添加文件夹", self.add_folder)
        B("添加图片", self.add_files)
        B("刷新", self._refresh_roots)
        B("清空", self.clear_all, gap=(0, S(14)))

        self._sep(row)

        self.btn_rec = B("含子文件夹", self.toggle_recursive,
                         kind="on" if self.recursive_var.get() else "ghost",
                         gap=(0, S(14)))

        tk.Label(row, text="灵敏度", bg=P.CARD, fg=P.TEXT_2,
                 font=self.f_small).pack(side="left", padx=(0, S(6)))
        self.preset_btns = {}
        for k in PRESET_ORDER:
            b = uikit.RoundButton(row, preset_short(k),
                                  command=lambda kk=k: self.set_preset(kk),
                                  kind="ghost", page=P.CARD, size=10,
                                  padx=S(13), pady=S(7))
            b.pack(side="left", padx=(0, S(5)))
            self.preset_btns[k] = b
        self._sync_preset_btns()

        self._sep(row)

        # 扫描按钮跟着一起走，中间只隔一点
        self.btn_stop = B("停止", self.stop_scan, "soft", gap=(0, S(7)))
        self.btn_stop.configure_state("disabled")
        self.btn_scan = B("开始扫描", self.start_scan, "primary", size=11,
                          bold=True, padx=S(24), pady=S(9), gap=(0, 0))

        # 第二行：进度 + 状态 + 一键清理重复 + 剪贴板提示
        line2 = tk.Frame(bar, bg=P.CARD)
        line2.pack(fill="x", pady=(S(6), 0))
        self.pb = ttk.Progressbar(line2, mode="determinate", length=S(260))
        self.pb.pack(side="left", padx=(S(8), S(10)))
        self.stat = tk.Label(line2, text="就绪", bg=P.CARD, fg=P.TEXT_2,
                             font=self.f_small, anchor="w")
        self.stat.pack(side="left")
        # ⚠️ `side="right"` 是**先 pack 的在最右**，所以下面的 pack 顺序
        #    就是「从右到左」的排布：说明 -> 清理 -> 撤销 -> 剪贴板提示。
        #    主人 2026-10-07：「右上可以添加一个说明按钮」-> 说明放最右上。
        self.btn_help = uikit.RoundButton(
            line2, "说明 / 快捷键", command=self.show_help,
            kind="ghost", page=P.CARD, size=10, padx=S(13), pady=S(6))
        self.btn_help.pack(side="right", padx=(0, S(6)))

        self.btn_dedup = uikit.RoundButton(
            line2, "清理完全重复", command=self.clean_exact_dups,
            kind="soft", page=P.CARD, size=10, padx=S(15), pady=S(6))
        self.btn_dedup.pack(side="right", padx=(0, S(6)))

        # ⚠️ 「撤销」紧挨着删除类操作放（v1.11）。它不是装饰 ——
        #    `delete_side` 已经不弹确认框了，撤销就是那道兜底。
        self.btn_undo = uikit.RoundButton(
            line2, "撤销", command=self.undo,
            kind="soft", page=P.CARD, size=10, padx=S(15), pady=S(6))
        self.btn_undo.pack(side="right", padx=(0, S(6)))
        self.btn_undo.configure_state("disabled")   # 栈空时灰着

        self.clip_hint = tk.Label(line2, text="", bg=P.CARD, fg=P.TEXT_3,
                                  font=self.f_tiny, anchor="e")
        self.clip_hint.pack(side="right", padx=(S(8), S(8)))
        self._clipboard_hint()

    def _sep(self, row):
        """按钮组之间的竖线分隔。"""
        f = tk.Frame(row, bg=P.LINE, width=max(1, S(1)))
        f.pack(side="left", fill="y", padx=S(7), pady=S(4))
        return f

    # ---- 左栏 ----------------------------------------------------------
    def _build_left(self, body):
        card = uikit.Card(body, radius=16, pad=S(9), fit=None)
        card.pack(side="left", fill="y")
        card.configure(width=S(340))
        L = card.body

        # 视图切换：相似分组 / 全部图片
        head = tk.Frame(L, bg=P.CARD)
        head.pack(fill="x")
        self.btn_view_groups = uikit.RoundButton(
            head, "相似分组", command=lambda: self.set_view("groups"),
            kind="on", page=P.CARD, size=9, padx=S(13), pady=S(6))
        self.btn_view_groups.pack(side="left", padx=(S(4), S(6)))
        self.btn_view_all = uikit.RoundButton(
            head, "全部图片", command=lambda: self.set_view("all"),
            kind="ghost", page=P.CARD, size=9, padx=S(13), pady=S(6))
        self.btn_view_all.pack(side="left")
        # 排列方向（v1.9）。主人 2026-10-07：
        # 「图片分组可以根据每组的图片数量来排序，数量多的放上面，
        #   每组和全部图片按照图片质量来排序，同时支持倒序。」
        # 一个按钮管全部：分组顺序 + 「全部图片」的质量顺序 + 组内成员顺序。
        self.btn_sort = uikit.RoundButton(
            head, "排列 ↓", command=self.toggle_sort,
            kind="ghost", page=P.CARD, size=9, padx=S(12), pady=S(6))
        self.btn_sort.pack(side="right", padx=(0, S(4)))
        self._sync_sort_btn()

        # 上面这块（分组）给大：让它吃掉左栏的剩余高度
        self.glist = uikit.NiceList(
            L, on_pick=self._on_pick_group, row_h=54, thumb=40,
            page=P.CARD, size=9, radius=11,
            empty_text="还没有分组。\n选好文件夹后点「开始扫描」。")
        self.glist.pack(fill="both", expand=True, pady=(S(8), 0))

        tk.Frame(L, bg=P.LINE, height=max(1, S(1))).pack(fill="x", pady=S(8))

        head2 = tk.Frame(L, bg=P.CARD)
        head2.pack(fill="x")
        self.lbl_members = tk.Label(head2, text="本组成员", bg=P.CARD,
                                    fg=P.TEXT_2, font=self.f_h2, anchor="w")
        self.lbl_members.pack(side="left", padx=(S(6), 0))
        tk.Label(head2, text="双击打开", bg=P.CARD,
                 fg=P.TEXT_3, font=self.f_tiny).pack(side="right", padx=(0, S(6)))

        # 下面这块（成员）刻意小一些
        self.mlist = uikit.NiceList(
            L, on_pick=self._on_pick_member,
            # ⚠️ 「再点一次」也要通知（v1.10）：组内 >=3 张时靠它取消自己
            #    点选的那张。只有成员表需要这个通道 —— 分组表点同一行
            #    重复触发没有意义。
            on_repick=self._on_pick_member,
            on_activate=self._on_activate_member,
            row_h=50, thumb=36, page=P.CARD, size=9, radius=11,
            height=S(186),
            empty_text="选中上面的一组，这里会列出它的成员。")
        self.mlist.pack(fill="x", pady=(S(6), 0))

    # ---- 右栏（对比区）-------------------------------------------------
    def _build_right(self, body):
        right = tk.Frame(body, bg=P.PAGE)
        right.pack(side="left", fill="both", expand=True, padx=(S(12), 0))

        # 结论 + 信息（**合并成一个卡**，2026-10-05 主人：「这两个窗口可以合并」）。
        # 位置在**预览区下面**（主人订正：信息窗在预览窗下面）——
        # 第一行是结论（pill + 主文字 + 副说明 + tag 靠右），下面是两列信息。
        # 行数也压掉了：原来的「工作网格 / 匹配区域 / NIQE 说明」整行去掉，
        # 质量与清晰度（NIQE）并进「分辨率 · 格式 · 大小 · 时间」那一行 ——
        # 省出来的每一像素高度都给上面的对比图。
        # 图片对比区（**吃满剩余空间**，这是主角）
        pics = uikit.Card(right, radius=16, pad=S(6), fit=None, height=S(200))
        pics.pack(fill="both", expand=True)
        self.pics = pics.body
        self.pics.columnconfigure(0, weight=1, uniform="p")
        self.pics.columnconfigure(1, weight=1, uniform="p")
        self.pics.rowconfigure(0, weight=1)

        self.cell_a, self.cv_a, self.cap_a = self._make_pane(self.pics, 0)
        self.cell_b, self.cv_b, self.cap_b = self._make_pane(self.pics, 1)

        icard = uikit.Card(right, radius=16, pad=S(6))
        icard.pack(fill="x", pady=(S(6), 0))
        self.info_card = icard
        self.verdict_card = icard          # 结论行住在信息卡里（同一张卡）
        self.info = icard.body
        v = self.info
        vrow = tk.Frame(v, bg=P.CARD)
        vrow.pack(fill="x")
        self.verdict_pill = uikit.Pill(vrow, "—", color=P.TEXT_3,
                                       bgcolor=P.CARD_SOFT, page=P.CARD)
        self.verdict_pill.pack(side="left", padx=(S(6), S(10)))
        self.verdict_text = tk.Label(vrow, text="等待扫描",
                                     bg=P.CARD, fg=P.TEXT, font=self.f_h2,
                                     anchor="w")
        self.verdict_text.pack(side="left")
        self.verdict_sub = tk.Label(vrow, text="左边选一组，这里立刻给出对比",
                                    bg=P.CARD, fg=P.TEXT_3,
                                    font=self.f_tiny, anchor="w")
        self.verdict_sub.pack(side="left", padx=(S(10), 0), pady=(0, S(2)))
        self.verdict_tag = tk.Label(vrow, text="", bg=P.CARD, fg=P.TEXT_3,
                                    font=self.f_small)
        self.verdict_tag.pack(side="right", padx=(0, S(8)))

        # 两列信息放**独立容器**：Tk 不允许 pack 和 grid 管同一个父容器，
        # 结论行是 pack 的，信息列要用 grid（两列等宽），只能隔一层。
        self._info_grid = tk.Frame(self.info, bg=P.CARD)
        self._info_grid.pack(fill="x")

        # 操作（一行：2026-10-05 主人「按钮可以缩小让其只有一行」）
        acard = uikit.Card(right, radius=16, pad=S(6))
        acard.pack(fill="x", pady=(S(6), 0))
        self.acts = acard.body
        self._build_actions()

    def _make_pane(self, master, col):
        cell = tk.Frame(master, bg=P.CARD)
        cell.grid(row=0, column=col, sticky="nsew", padx=S(6))
        cv = tk.Canvas(cell, bg=P.CARD, highlightthickness=0, bd=0)
        cv.pack(fill="both", expand=True)
        cap = tk.Frame(cell, bg=P.CARD)
        cap.pack(fill="x", pady=(S(4), 0))
        name = tk.Label(cap, text="", bg=P.CARD, fg=P.TEXT_2,
                        font=self.f_tiny, anchor="w")
        name.pack(side="left")
        hint = tk.Label(cap, text="" if col else "滚轮缩放 · 双击打开 · 按住拖动",
                        bg=P.CARD, fg=P.TEXT_3, font=self.f_tiny, anchor="center")
        hint.pack(side="left", expand=True)
        dim = tk.Label(cap, text="", bg=P.CARD, fg=P.TEXT_3,
                       font=self.f_tiny, anchor="e")
        dim.pack(side="right")
        cap.name_lbl, cap.dim_lbl, cap.hint_lbl = name, dim, hint

        cv.bind("<Configure>", lambda e, k=col: self._later("resize%d" % k,
                                                           130,
                                                           self._on_canvas_resize))
        # 双击 = 用系统看图器打开（原来那两个「打开 A/B」按钮删掉了）
        cv.bind("<Double-Button-1>", lambda e, k=col: self._open_side(k))
        cv.bind("<MouseWheel>", lambda e, k=col: self._on_wheel(k, e))
        cv.bind("<ButtonPress-1>", lambda e, k=col: self._pan_start(k, e))
        cv.bind("<B1-Motion>", lambda e, k=col: self._pan_move(k, e))
        cv.bind("<ButtonRelease-1>", lambda e: self._pan_end())
        return cell, cv, cap

    def _build_actions(self):
        """操作按钮：**一行放下**（2026-10-05 主人：「按钮可以缩小让其只有一行」）。

        两行改一行的代价是文字必须变短 —— 「复制 A 到剪贴板」→「复制 A」、
        「删除 A（移到隔离夹）」→「删除 A」。「移到隔离夹」这句话不用丢：
        删除的确认框里本来就写着移动目标，删完的 toast 也会再说一遍。
        """
        a = self.acts
        top = tk.Frame(a, bg=P.CARD)
        top.pack(fill="x")

        def B(master, txt, cmd, kind="soft", gap=(0, S(5)), **kw):
            b = uikit.RoundButton(master, txt, command=cmd, kind=kind,
                                  page=P.CARD, size=9, pady=S(4),
                                  padx=S(10), **kw)
            b.pack(side="left", padx=gap)
            return b

        self.btn_copy_a = B(top, "复制 A",
                            lambda: self.do_copy_image(self.path_a), "primary",
                            gap=(S(4), S(5)))
        self.btn_copy_b = B(top, "复制 B",
                            lambda: self.do_copy_image(self.path_b), "primary")
        B(top, "复制文件名", self.copy_names)
        B(top, "复制路径", self.copy_paths)
        B(top, "定位 A", lambda: self._reveal(self.path_a), "ghost")
        B(top, "定位 B", lambda: self._reveal(self.path_b), "ghost")

        self.btn_del_a = B(top, "删除 A",
                           lambda: self.delete_side(0), "danger",
                           gap=(S(10), S(5)))
        self.btn_del_b = B(top, "删除 B",
                           lambda: self.delete_side(1), "danger")

        self.btn_zoom = B(top, "重置缩放", self.reset_zoom, "ghost",
                          gap=(S(10), S(5)))
        self.btn_crop = uikit.RoundButton(top, "只看匹配区域",
                                          command=self.toggle_crop,
                                          kind="ghost", page=P.CARD, size=9,
                                          pady=S(4), padx=S(10))
        self.btn_crop.pack(side="left", padx=(0, S(4)))
        # ⚠️⚠️ **双图联动开关**（v1.8，主人要的功能）。
        #
        # 放「只看匹配区域」旁边：两个都是「看图时怎么显示」类的开关，
        # 挤在一起比塞到别处好找。
        #
        # ⚠️ **只在并排对比时才有意义**，单图模式下按了没反应 ——
        # 所以 `toggle_link` 里会提示一句，而不是默默不动。
        self.btn_link = uikit.RoundButton(top, "双图联动",
                                          command=self.toggle_link,
                                          kind="ghost", page=P.CARD, size=9,
                                          pady=S(4), padx=S(10))
        self.btn_link.pack(side="left", padx=(0, S(4)))

    # ------------------------------------------------------------------
    # 剪贴板
    # ------------------------------------------------------------------
    def _clipboard_hint(self):
        try:
            ok = net.clipboard_available()
        except Exception:
            ok = False
        if ok:
            self.clip_hint.configure(text="剪贴板可用", fg=P.TEXT_3)
        else:
            self.clip_hint.configure(
                text="⚠ 系统不允许访问剪贴板，复制会自动降级成「导出临时副本」",
                fg=P.WARN_D)

    def _toast(self, text):
        self.clip_hint.configure(text="● " + text, fg=P.PRIMARY_D)

    # ------------------------------------------------------------------
    # 来源 / 扫描
    # ------------------------------------------------------------------
    def add_folder(self):
        d = filedialog.askdirectory(title="选择要扫描的文件夹")
        if d:
            self.roots.append(os.path.abspath(d))
            self._refresh_roots()

    def add_files(self):
        fs = filedialog.askopenfilenames(
            title="选择图片",
            filetypes=[("图片", "*.jpg *.jpeg *.png *.gif *.bmp *.webp "
                        "*.tif *.tiff *.heic *.avif *.ico *.jxl"),
                       ("所有文件", "*.*")])
        if fs:
            for f in fs:
                self.roots.append(os.path.abspath(f))
            self._refresh_roots()

    def clear_all(self):
        self.roots = []
        self.descs = {}
        self.links = []
        self.groups_raw = []
        self.groups = []
        self.weak = []
        self.files = []
        self.group = None
        self.gidx = -1
        self._group_mem = 0
        self.cur_member = None
        self.path_a = self.path_b = None
        self._drag = None
        self._drag_last = None      # 同 __init__：None = 还没重取过（见那段）
        self._prewarm_q = []
        self._prewarm_busy = False
        self._base_busy = set()
        self._base_prep_done = set()
        self._base_want = {}
        self._prewarm_pause = 0.0
        self.pair = None
        self.zoom = [1.0, 1.0]        # 每侧一份（和 __init__ 里一致）
        self.pan = [[0, 0], [0, 0]]
        self.thumb = ThumbCache(self)
        self.big = BigCache()
        self._fit_pend = {}           # 侧 -> 正在后台算的那一帧（防重算/防张冠李戴）
        self._fit_pend_state = {}
        self._niqe_lbl = {}           # 信息卡里「清晰度」那一段的引用
        self.glist.set_items([])
        self.mlist.set_items([])
        self._sync_view_btns()
        self._update_verdict(None, fit=False)
        self._fill_info(fit=False)
        self._fit_card()
        self._apply_grid()
        self.render_all()
        self.stat.configure(text="就绪")
        self.pb["value"] = 0

    def _refresh_roots(self):
        roots = list(self.roots)
        if not roots:
            return
        self.stat.configure(text="正在列出图片…")
        self.update_idletasks()
        seen, files = set(), []
        for p in roots:
            for f in scan.iter_images([p], recursive=self.recursive_var.get()):
                if f not in seen:
                    seen.add(f)
                    files.append(f)
        self.files = files
        if self.busy:
            return
        if self.view == "all":
            self._fill_all_list()
        if self.groups:
            self.stat.configure(text="文件列表已更新（%d 张）" % len(files))
        else:
            self.stat.configure(text="已选 %d 张图片，点「开始扫描」"
                                     % len(files))

    def set_preset(self, key):
        self.preset_key = key
        self._sync_preset_btns()
        if self.groups:
            self.stat.configure(
                text="灵敏度已切到「%s」—— 点「开始扫描」用新档位重算配对"
                     % preset_short(key))

    def toggle_recursive(self):
        v = not self.recursive_var.get()
        self.recursive_var.set(v)
        self.btn_rec.set_kind("on" if v else "ghost")
        self._refresh_roots()

    def _sync_preset_btns(self):
        for k, b in self.preset_btns.items():
            b.set_kind("on" if k == self.preset_key else "ghost")

    def start_scan(self):
        if self.busy:
            return
        if not self.files:
            self._refresh_roots()
        if not self.files:
            messagebox.showinfo("没有图片", "先添加文件夹或图片。")
            return
        self.busy = True
        self.stop_flag = False
        self.btn_scan.configure_state("disabled")
        self.btn_stop.configure_state("normal")
        self.pb["value"] = 0
        self.t_start = time.time()
        key = self.preset_key
        files = list(self.files)
        store = self.store

        def worker():
            try:
                self._ui(self._phase, "计算指纹…")
                descs, stats = scan.describe_paths(
                    files, workers=4,
                    on_progress=lambda d, t: self._ui(
                        self._progress, 0.35 * d / max(1, t),
                        "指纹 %d/%d" % (d, t)),
                    should_stop=lambda: self.stop_flag, store=store)
                if self.stop_flag:
                    self._ui(self._done, None, None, None, stats, key)
                    return
                store.save()
                self._ui(self._phase, "比较配对…")
                links = scan.find_links(
                    descs,
                    on_progress=lambda d, t: self._ui(
                        self._progress, 0.35 + 0.65 * d / max(1, t),
                        "配对 %d/%d" % (d, t)),
                    should_stop=lambda: self.stop_flag, preset=key)
                groups, weak = scan.build_groups(descs, links)
                self._ui(self._done, descs, links, (groups, weak), stats, key)
            except Exception as e:      # 后台异常必须回到界面，否则静默失败
                self._ui(self._fail, "%s: %s" % (type(e).__name__, e))

        threading.Thread(target=worker, daemon=True).start()

    def stop_scan(self):
        self.stop_flag = True
        self._prog_pend = None
        self.stat.configure(text="正在停止…")

    def _ui(self, fn, *a):
        """把要在界面上做的事交给主线程。

        为什么不用 `self.after(0, ...)`：Tk 的 `after` 会去 `createcommand`，
        而 Tcl 解释器只认主线程 —— 后台线程直接调它会抛
        「RuntimeError: main thread is not in main loop」。
        所以后台线程只往队列里塞，主线程定期取出来执行（见 `_pump_queue`）。
        """
        self._q.put((fn, a))

    def _pump_queue(self):
        # ⚠️⚠️ **正在拖动就整个让路**（v1.7 修拖动卡顿的最后一块）。
        #
        # 这里跑的是**主线程**任务：后台解码好的 PPM 在这儿变成 PhotoImage，
        # 一次 50~150ms（实测拖动时 p95 稳定在 95~109ms、max 200ms，
        # 连跑三次复现）。而它每 60ms 就来一次 —— 正好撞在拖动帧上，
        # 用户看到的就是「拖一下卡一下」。
        #
        # ⚠️ 判据要先自证这不是「补块慢」：把 `_pan_move` 的补块判据强制
        #   打开（每步都补）后，p95 仍是 95~109ms，而补块次数从 12涨到 33
        #   —— **慢与补块次数无关**，只能是这条队列。
        #
        # 让路是安全的：队列不丢，`after` 照常重新排，拖动一松手就补上。
        # 而且这时画面本来就是「拖动中」，用户看的不是高清帧。
        if self._drag:
            self.after(60, self._pump_queue)
            return
        try:
            while True:
                fn, a = self._q.get_nowait()
                try:
                    fn(*a)
                except Exception as e:          # 单个回调出错不要拖垮轮询
                    print("UI 回调出错：%s: %s" % (type(e).__name__, e),
                          file=sys.stderr)
        except queue.Empty:
            pass
        self.after(60, self._pump_queue)

    def _later(self, key, ms, fn):
        """去抖：拖窗口时会连发几十个 Configure，没必要每个都重画。"""
        tid = self._later_ids.pop(key, None)
        if tid:
            try:
                self.after_cancel(tid)
            except Exception:
                pass
        self._later_ids[key] = self.after(ms, fn)

    def _phase(self, text):
        """切换阶段文字（「计算指纹…」「比较配对…」）。

        ⚠️ 要把还挂着的进度更新**丢掉**：阶段文字比进度新，
        留着的话下一次 `_progress` 落地会把阶段文字顶掉。
        """
        self._prog_pend = None
        self.stat.configure(text=text)

    # 进度条 / 状态文字的**最短刷新间隔**（秒）。
    # 实测 400 张图只有 400 多个进度事件，400Hz 的刷新率用户根本看不见，
    # 但每一次都要主线程去抢一次 GIL —— 见 `_progress` 的长注释。
    PROG_MIN_DT = 0.2

    # 组内**几张以上**才让用户自己点选对比的两张（v1.10）。
    #
    # ⚠️ 为什么是 3：只有两张时「比这两张」是**唯一解**，`_auto_pair`
    #    直接摆好就行，让用户再点两下纯属多事；三张以上才有
    #    「到底比哪两张」的选择空间。主人原话：「本组成员里面如果
    #    3 个及以上最好是自定义组员图片来对比」。
    PICK_MIN = 3

    def _progress(self, frac, text):
        """带节流的进度更新。

        ⚠️⚠️⚠️ **这里慢的不是控件，是「等 GIL」**（v1.9 实测，主人
        2026-10-07 报「加载 400 图片就会很慢很卡」）。

        证据（`probe_prog.py` + `_bench_gil` 对照）：
          · 同样的 300 次 `pb["value"]=` + `stat.configure()`：
            **没有后台线程**时合计 **33.8ms**（每次 0.1ms）；
            **4 个扫描线程在跑**时合计 **133 秒**。
          · 单看 `_progress` 的分位数：p50 = 0.12ms（拿得到 GIL 时几乎免费），
            p90 = 30ms、max = 79ms —— **中位极小、尾部极大**，
            典型的「不是这段代码慢，是它跑之前先排队等锁」。
          · 慢调用**散布全程**（序号 7,8,11,13,14,18...），不是冷启动；
            状态文字长度 448 次里只变了 5 次，所以也不是「宽度变触发 relayout」。

        根因：扫描的 4 个工作线程跑的是**纯 Python** 的 `crops.describe`
        （见那边的注释），全程握着 GIL，主线程每次想动一下都要等它让出来。
        400 张图 438 次回调 -> 主线程被占 **5090ms**（占全程 28%），
        最长一次冻了 **1425ms** —— 用户看到的就是「很卡」。

        节流为什么有效：主线程抢 GIL 的次数从 438 次降到十余次，
        等待总时长按比例下降；而「进度条走得顺不顺」肉眼根本无法分辨
        0.2 秒的粒度（原来平均 41ms 刷一次，比这还密）。
        """
        now = time.monotonic()
        self._prog_pend = (frac, text)
        if now - self._prog_t >= self.PROG_MIN_DT:
            self._prog_flush()

    def _prog_flush(self):
        """把挂着的进度落地（节流窗口到了 / 收尾时强制调用）。"""
        p = self._prog_pend
        if p is None:
            return
        self._prog_pend = None
        self._prog_t = time.monotonic()
        self.pb["value"] = max(0.0, min(1.0, p[0])) * 100
        self.stat.configure(text=p[1])

    def _fail(self, msg):
        self.busy = False
        self.btn_scan.configure_state("normal")
        self.btn_stop.configure_state("disabled")
        messagebox.showerror("扫描出错", msg)

    def _done(self, descs, links, built, stats, key):
        self.busy = False
        # 收尾时把挂着的进度丢掉：下面会自己把进度条钉到 100 / 状态改成汇总
        self._prog_pend = None
        self.btn_scan.configure_state("normal")
        self.btn_stop.configure_state("disabled")
        if descs is None:
            self.pb["value"] = 0
            self.stat.configure(text="已停止")
            return
        self.descs, self.links = descs, links
        self.groups_raw, self.weak = built
        self._make_view_groups()
        dt = time.time() - self.t_start
        self.pb["value"] = 100
        self.stat.configure(
            text="%d 张 → %d 组（含 %d 对仅疑似），耗时 %.1fs"
                 "（缓存命中 %d / 新解码 %d / 失败 %d）"
            % (len(descs), len(self.groups_raw), len(self.weak),
               dt, stats.get("cached", 0), stats.get("decoded", 0),
               stats.get("failed", 0)))
        # ⚠️⚠️ **重活推到下一轮事件循环**（v1.9）。
        #
        # 下面 `_finish_view` 里「填列表 + 选中第一组」会现场把第一对预览
        # 解出来 + 编码 + 建 Tk 图，**冷缓存实测 1221ms**（其中
        # `compare_pair` 335ms + `render_all` 357ms + 其余在 `set_pair`）。
        # 原来这段是**跟 `pb["value"]=100` 同一个回调**跑的，于是
        # 「扫描完成」这件事从头到尾没被画出来过 —— 用户看到的就是
        # 「进度条卡在 99% 一秒多，然后突然 100%」，像程序卡死。
        # 拆开之后：进度条 / 汇总文字立刻可见，重活等下一次事件循环。
        # （总耗时不变，但「有反馈」和「没反馈」的手感完全是两回事。）
        self.after(1, self._finish_view)

    def _finish_view(self):
        """扫描收尾：把左栏列表填上、选中第一组、开始预热。"""
        if self.view == "all":
            self._fill_all_list()
        else:
            self._fill_group_list()
        # 主人：「每组第一次打开会卡顿一下」—— 扫完就趁空闲把各组要用的图
        # 在后台准备好，点开哪一组都是现成的。
        self._prewarm_start()

    def _make_view_groups(self):
        """真组 + 「疑似对」伪组拼成一个列表。

        伪组是为了让界面只有一种列表：点一样的东西走一样的逻辑，
        不然得给「疑似对」单独写一套选中/渲染。
        """
        pseudo = []
        for l in self.weak:
            a, b, kind, score, d, i, j = l
            pseudo.append({"members": [a, b], "strong_links": [],
                           "weak_links": [l], "kind": scan.UI_LOOSE,
                           "best": score, "best_strong": 0.0, "pseudo": True})
        self.groups = list(self.groups_raw) + pseudo

    # ------------------------------------------------------------------
    # 视图切换
    # ------------------------------------------------------------------
    def set_view(self, v):
        if v == self.view:
            return
        self.view = v
        self._sync_view_btns()
        if v == "groups":
            self._fill_group_list(select=self._group_mem)   # 回到上次看的那一组
        else:
            # ⚠️「全部图片」视图里 `self.group` **必须是 None**：
            # 左下那块列表、`_on_pick_member` / `_on_activate_member` 全都是按
            # 「有没有当前组」来分支的。不清掉就会出现「上面写着全部图片、
            # 左下角还挂着上一组的成员」这种半截状态（踩过）。
            self.group = None
            self.gidx = -1
            self.cur_member = None
            # `_fill_all_list` 内部会把「当前该看哪一张」定下来并 notify，
            # 所以这里不用再补 `_fill_member_list()`。
            self._fill_all_list()

    def _sync_view_btns(self):
        n_g = len(self.groups_raw)
        n_a = len(self.files)
        self.btn_view_groups.set_text("相似分组 · %d" % n_g)
        self.btn_view_all.set_text("全部图片 · %d" % n_a)
        self.btn_view_groups.set_kind("on" if self.view == "groups"
                                      else "ghost")
        self.btn_view_all.set_kind("on" if self.view == "all"
                                   else "ghost")

    # ------------------------------------------------------------------
    # 列表
    # ------------------------------------------------------------------
    def _group_sort_key(self, gi):
        """分组排序键（**升序**用它）：张数多的在前，同张数按**组号**。

        主人 2026-10-07：「图片分组可以根据每组的图片数量来排序，
        数量多的放上面」、「由于是按照数量排序的，但是分组的却不是
        按照顺序」-> 张数取负（升序即降序），**并列时按组号 `gi` 升序**。

        ⚠️⚠️ 这里**只留两档，不要再插「代表图质量」那一档**（v1.11 修正）。
        原来它是 `(-张数, 质量档, 文件名)`，第三档是文件名 —— 那固然乱。
        但把第三档换成组号**并没有解决问题**：质量档还夹在中间，
        并列的组仍然不按编号排。真实数据实测（18 张样本、三组各 6 张）：

            视图[0] 6 张 质量=poor rank=0 -> 第 1 组
            视图[1] 6 张 质量=good rank=3 -> 第 3 组
            视图[2] 6 张 质量=poor rank=0 -> 第 2 组
            屏幕上是「第 1 / 3 / 2 组」—— 主人一眼就说这是乱序，
            而且**他没有任何办法从界面上看出这个顺序是按什么排的**。

        ⚠️ 为什么不改成「按显示顺序重新编号」：那个编号是要**给人报的**
        （`clean_exact_dups` 的提示里就是「报第几组就能定位」），
        必须**稳定** —— 切一次排列顺序就全变号的编号没法交流。
        所以正确做法是反过来：让**顺序服从编号**。
        """
        g = self.groups[gi]
        return (-len(g["members"]), gi)

    def _sorted_group_idx(self):
        """分组在列表里的显示顺序（返回的仍是 `self.groups` 的下标）。"""
        idx = sorted(range(len(self.groups)), key=self._group_sort_key)
        if self.sort_rev:
            idx.reverse()
        return idx

    def _quality_key(self, p):
        """「全部图片」/ 组内成员的排序键：质量档位 + 文件名。

        ⚠️ 默认是**质量差的排前面**（`QUALITY_RANK` 越小越差）——
        这是原来就定的口径（用户要「质量差的强调出来」）。
        「排列」按钮把这个顺序整体翻过来。
        """
        return (QUALITY_RANK.get(META.quality(p)[0], 2),
                os.path.basename(p).lower())

    def toggle_sort(self):
        """切换默认排列 / 倒序。

        ⚠️ 重填之后要**保住当前选中的东西**：分组视图保住组号，
        「全部图片」保住当前看的那张。不然一按排列，右边预览就跳回第一张。
        """
        self.sort_rev = not self.sort_rev
        self._sync_sort_btn()
        if self.view == "groups":
            keep = self.gidx if self.gidx >= 0 else self._group_mem
            self._fill_group_list(select=keep)
        else:
            self._fill_all_list(keep=self.path_a)

    def _sync_sort_btn(self):
        self.btn_sort.set_text("排列 ↓" if not self.sort_rev else "排列 ↑")

    def _fill_group_list(self, select=0):
        """填分组列表。

        ⚠️ 行序由 `_sorted_group_idx()` 决定（张数降序，可倒序），
        但 `item["tag"]` 仍然是**组号 gi**（`self.groups` 的下标）——
        选中/切组全靠它，不能换成行号，否则一按排列就切到别的组去了。
        所以要选的那一组得**从组号反查行号**再 `select`。
        """
        items = []
        order = self._sorted_group_idx()
        gi2row = {}
        for row, gi in enumerate(order):
            gi2row[gi] = row
            g = self.groups[gi]
            g["index"] = gi
            rep = self._representative(g)
            if g.get("pseudo"):
                title = "疑似对 · 2 张"
                sub = "疑似同场景 %.3f" % g["best"]
                tone = "loose"
            else:
                title = "第 %d 组 · %d 张" % (gi + 1, len(g["members"]))
                if g["kind"] == scan.UI_STRONG:
                    sub = "同源 / 裁剪 %.3f" % g["best_strong"]
                    tone = "strong"
                else:
                    sub = "疑似同场景 %.3f" % g["best"]
                    tone = "loose"
                if g["weak_links"]:
                    sub += "  (+%d 疑似)" % len(g["weak_links"])
                poor = sum(1 for p in g["members"]
                           if META.quality(p)[0] == "poor")
                if poor:
                    # 「低质」是涵盖「低清 / 体积小」两种标签的说法，别写死一种
                    sub += "  · %d 张低质" % poor
            items.append({
                "title": title, "sub": sub, "tone": tone,
                "photo": self.thumb.get(rep, S(40), S(9), P.CARD),
                "tag": gi})
        self.glist.set_items(items)
        if items:
            # 要选的那一组可能因为删除而消失了 → 退回第一组，
            # 免得「列表里有东西、右边却没人被选中」卡在空状态
            if select not in gi2row:
                select = order[0]
            self.glist.select(gi2row[select])
        else:
            self.group = None
            self.gidx = -1
            self.cur_member = None
            self.path_a = self.path_b = None
            self.mlist.set_items([])
            self.lbl_members.configure(text="本组成员")
            self._update_verdict(None, fit=False)
            self._fill_info(fit=False)
            self._fit_card()
            self.render_all()
        self._sync_view_btns()

    def _fill_all_list(self, keep=None):
        """「全部图片」视图：连没配上对的也列出来。

        排序刻意把**质量差的排前面**（用户要「质量差的强调出来」），
        同级按文件名。质量差的还会带一个 `quality_of` 给的低质徽章 + 琥珀色标题。

        ⚠️⚠️ **小预览是懒加载的**（v1.9，主人 2026-10-07 报「加载 400 图片
        很慢很卡」）。原来这里对 400 项**每项都同步 `thumb.get(...)`** ——
        实测 **1483ms**，全糊在扫描结束那一刻的主线程上（`_done` 因此整块
        占了 1019~1484ms，tick 间隔 max 1134ms，肉眼就是「卡死一下」）。
        改成给 `photo_fn`（见 `uikit.NiceList._draw_row`）：列表**画到哪一行
        才取哪一行的缩略图**，而虚拟化让一屏只有 ~9 行 -> 400 项也只做 9 张。
        取回来的图会写回 item，重画不重复取。

        ⚠️ 本函数**必须顺手把「看哪一张」定下来**（挑一行并 `notify=True`）：
        右边预览区和信息卡是跟着「列表选中的那一项」走的。只填列表不选行，
        切过来右边就是**空的**、点哪一行都像没反应 —— 用户报过这个。
        优先保住原来那张（`keep` / `self.path_a`），它不在列表里（比如刚被删）
        就退回第一行，不会停在空状态。
        """
        files = list(self.files)
        if not files:
            files = list(self.descs)

        # 质量排序：默认「差的在前」，可倒序 —— 见 `_quality_key`
        files.sort(key=self._quality_key, reverse=self.sort_rev)
        items = []
        for p in files:
            m = META.of(p)
            q = META.quality(p)
            wh = m["wh"]
            dim = ("%d × %d" % wh) if wh else m["format"]
            sub = "%s · %s · %s" % (dim, human_size(m["size"]), q[1])
            items.append({
                "title": short_name(m["name"], 30), "sub": sub,
                "tone": "poor" if q[0] == "poor" else "normal",
                "badge": poor_badge(q),
                "badge_tone": "poor",
                # 懒加载：`photo` 先留空，画到这一行时 `photo_fn` 才去解码
                "photo": None,
                "photo_fn": (lambda pp=p: self.thumb.get(
                    pp, S(40), S(9), P.CARD)),
                "tag": p})
        self.glist.set_items(items)
        if not items:
            # 一张都没有：把右边也清干净，别留着上一次的图
            self.group = None
            self.gidx = -1
            self.cur_member = None
            self.path_a = self.path_b = None
            self.mlist.set_items([])
            self.lbl_members.configure(text="与它相似")
            self._update_verdict(None, fit=False)
            self._fill_info(fit=False)
            self._fit_card()
            self.render_all()
            self._sync_view_btns()
            return
        want = keep or self.path_a
        idx = 0
        for i, it in enumerate(items):
            if it["tag"] == want:
                idx = i
                break
        self.glist.select(idx, notify=True)      # notify → 右边立刻跟着换
        self._sync_view_btns()

    def _fill_member_list(self):
        g = self.group
        if g is None:
            if self.view == "all" and self.path_a:
                self._fill_member_list_for(self.path_a)
            else:
                self.mlist.set_items([])
                self.lbl_members.configure(text="本组成员")
            return
        rep = self._representative(g)
        # ⚠️ 切组 / 重新扫描 / 删图之后，`pick_pair` 里可能混着**不属于
        #    当前组**的路径。在这里统一剔除，比在每一处 reset
        #    （`cur_member = None` 那 5 个地方）都记着清一遍靠谱 ——
        #    漏一处就是「换了组，对比区还在比上一组的图」。
        order = self._member_order()
        self.pick_pair = [p for p in self.pick_pair if p in order]
        items = []
        for p in order:
            score, kind = self._score_to_rep(g, rep, p)
            m = META.of(p)
            wh = m["wh"]
            q = META.quality(p)
            dim = ("%d × %d" % wh) if wh else m["format"]
            sub = "%s · 代表图" % dim if p == rep else "%s · %.3f" % (dim, score)
            if q[0] == "poor":
                tone = "poor"
            elif p == rep or kind == scan.UI_STRONG:
                tone = "strong"
            else:
                tone = "loose"
            # ⚠️ 自己点选的那两张挂 `A` / `B` 徽章（v1.10）。行里只有一个
            #    徽章位置，所以这里**让质量徽章先让位** —— 正在挑对比图
            #    的时候，用户最需要知道的是「我选了哪两张」。
            mark = ""
            if p in self.pick_pair:
                mark = "A" if self.pick_pair[0] == p else "B"
            items.append({"title": short_name(m["name"], 28), "sub": sub,
                          "tone": "match" if mark else tone, "tag": p,
                          "badge": mark or poor_badge(q),
                          "badge_tone": "poor",
                          "photo": self.thumb.get(p, S(36), S(9), P.CARD)})
        self.mlist.set_items(items)
        hint = "（点两张自己比）" if len(items) >= self.PICK_MIN else ""
        self.lbl_members.configure(
            text="本组成员 · %d%s" % (len(items), hint))
        idx = 0
        if self.cur_member:
            for i, it in enumerate(items):
                if it["tag"] == self.cur_member:
                    idx = i
                    break
        self.mlist.select(idx, notify=False)

    def _fill_member_list_for(self, p):
        """「全部图片」视图用的：列出这张 + 与它相似的那些。"""
        items = []
        m = META.of(p)
        q = META.quality(p)
        wh = m["wh"]
        items.append({
            "title": short_name(m["name"], 28),
            "sub": "%s · %s" % (("%d × %d" % wh) if wh else m["format"], q[1]),
            "tone": "poor" if q[0] == "poor" else "strong",
            "badge": poor_badge(q), "badge_tone": "poor",
            "photo": self.thumb.get(p, S(36), S(9), P.CARD), "tag": p})
        n = 0
        for other, kind, score in scan.neighbors_of(self.links, p):
            mo = META.of(other)
            qo = META.quality(other)
            who = mo["wh"]
            items.append({
                "title": short_name(mo["name"], 28),
                "sub": "%s · %.3f" % (("%d × %d" % who) if who
                                      else mo["format"], score),
                "tone": "poor" if qo[0] == "poor" else (
                    "strong" if kind == scan.UI_STRONG else "loose"),
                "badge": poor_badge(qo),
                "badge_tone": "poor",
                "photo": self.thumb.get(other, S(36), S(9), P.CARD),
                "tag": other})
            n += 1
        self.mlist.set_items(items)
        self.lbl_members.configure(text="与它相似 · %d" % n)
        self.mlist.select(0, notify=False)

    # ---- 列表交互 ------------------------------------------------------
    def _on_pick_group(self, item, idx):
        if self.view == "all":
            # 「全部图片」列表里 `tag` 存的是**路径**（只有分组视图里才是组号）。
            # 以前这里直接 return，等于「点了没反应」—— 右边永远是空的。
            self._show_all_item(item["tag"])
            return
        gi = item["tag"]
        if gi >= len(self.groups):
            return
        self.select_group(gi)

    def _show_all_item(self, path):
        """「全部图片」里看某一张：右边显示**它 + 最像它的一张**，左下切成「与它相似」。

        没有相似对象时就进单图模式 —— 照样能看预览和它自己的信息，
        只是没有右边那一栏可比。
        """
        if not path:
            return
        best, bs = None, -1.0
        for other, _kind, score in scan.neighbors_of(self.links, path):
            if score > bs:
                best, bs = other, score
        self.set_pair(path, best)
        self._fill_member_list_for(path)

    def select_group(self, gi):
        self.group = self.groups[gi]
        self.gidx = gi
        self._group_mem = gi          # 切走再切回来要认出上次看的是哪一组
        # ⚠️ 原来这里填了**两次**成员表（先 cur_member=None 填一次，
        #    再设成 order[0] 填一次）—— 两次结果一模一样，等于整表重画两遍。
        #    先定好 cur_member，只填一次。
        order = self._member_order()
        self.cur_member = order[0] if order else None
        self._fill_member_list()
        self._auto_pair()

    def _on_pick_member(self, item, idx):
        path = item["tag"]
        if self.group is None:
            # 「全部图片」视图（没有当前组）：点哪张就看哪张
            self._show_all_item(path)
            return
        order = self._member_order()
        self.cur_member = path
        # ⚠️⚠️ 组内 **3 张及以上**：单击改成「自己点选两张来比」（v1.10，
        #    主人 2026-10-07：「本组成员里面如果 3 个及以上最好是自定义
        #    组员图片来对比」）。两张时保持原来的自动语义 —— 那种情况
        #    没有可选的余地，让用户点两下纯属多事。
        if len(order) >= self.PICK_MIN:
            self._pick_pair_click(path, order)
            return
        if order and path == order[0] and len(order) > 1:
            # 点的是代表图本身 —— 那就跟第二像的比，别自己跟自己比
            a, b = order[0], order[1]
            self.mlist.select(1, notify=False)
        elif order:
            a, b = order[0], path
        else:
            a = b = path
        self.set_pair(a, b)

    def _pick_pair_click(self, path, order):
        """组内 >=3 张时：点一下选进来，选满两张立刻并排；再点一下取消。

        ⚠️ 为什么不做「点一张就换掉 A」：那样用户点第二张时**第一张被
        无声顶掉**，他只看到画面变了、不知道刚才选的那张去哪了。
        改成「累积到两张」+ 行上挂 `A` / `B` 徽章，每一步都看得见。

        ⚠️ 超过两张用**滑动窗口**（挤掉最早那张），不是「清空重来」：
        连着点第三、四张时，用户想的是「我在比最近这两张」，
        清空重来会让画面先闪一下、还得多点一次。

        ⚠️ 全部取消（空列表）就交回 `_auto_pair()` —— 「我不管了，
        你自己挑」这条路必须留着，不然用户想回到默认状态只能切组。
        """
        pp = self.pick_pair
        if path in pp:
            pp.remove(path)                  # 再点一下 = 取消这一张
        else:
            pp.append(path)
            del pp[:-2]                      # 只留最近两张
        if not pp:
            self._auto_pair()
        elif len(pp) == 1:
            # 只选了一张：拉代表图陪它；选的本身就是代表图就换第二像的
            rep = order[0]
            self.set_pair(pp[0], order[1] if (pp[0] == rep and len(order) > 1)
                          else rep)
        else:
            self.set_pair(pp[0], pp[1])
        self._fill_member_list()             # 刷新 A / B 徽章

    def _on_activate_member(self, item, idx):
        """双击成员：分组视图里是「把它设为主图」，全部图片视图里是「打开它」。

        分组视图的双击保持原来的对比语义（那边双击 = 换主图），
        真正「双击图片=打开」是右栏预览区的行为。
        """
        path = item["tag"]
        if self.group is None:
            if os.path.isfile(path):
                net.open_path(path)
            return
        best, bk = None, None
        for other in self._member_order():
            if other == path:
                continue
            s, k = self._score_to_rep(self.group, path, other)
            if bk is None or s > bk:
                best, bk = other, s
        if best:
            self.cur_member = path
            self.set_pair(path, best)

    # ---- 代表图 / 打分 --------------------------------------------------
    def _representative(self, g):
        """代表图 = 连接最多（最像大家）的那张，并列时选像素最多的。"""
        deg = {}
        for a, b, kind, score, d, i, j in g["strong_links"]:
            for x in (a, b):
                deg[x] = deg.get(x, 0) + 1
        best, best_key = None, None
        for p in g["members"]:
            wh = META.of(p)["wh"] or (0, 0)
            key = (deg.get(p, 0), wh[0] * wh[1])
            if best_key is None or key > best_key:
                best, best_key = p, key
        return best

    def _score_to_rep(self, g, rep, p):
        if p == rep:
            return 1.0, scan.UI_STRONG
        best, kind = 0.0, scan.UI_LOOSE
        for a, b, k, score, d, i, j in g["strong_links"] + g["weak_links"]:
            if {a, b} == {rep, p} and score > best:
                best, kind = score, k
        if best == 0.0:
            # 与代表图没有直接边（靠别的图连进来的）：取它最强的一条边作参考
            for a, b, k, score, d, i, j in g["strong_links"] + g["weak_links"]:
                if p in (a, b) and score > best:
                    best, kind = score, k
        return best, kind

    def _member_order(self):
        """[代表图, 其余按**质量**排（可倒序）]。

        ⚠️ 代表图**永远排第一**：它是这一组的参照物，`_auto_pair` /
        `mlist.select(0)` / `_on_pick_member` 全都把 `order[0]` 当基准。
        主人 2026-10-07：「每组……按照图片质量来排序」—— 指的是
        **代表图之后的成员**按质量排，不是把代表图也一起排掉。
        """
        g = self.group
        if not g:
            return []
        rep = self._representative(g)
        rest = [p for p in g["members"] if p != rep]
        rest.sort(key=self._quality_key, reverse=self.sort_rev)
        return [rep] + rest

    # ------------------------------------------------------------------
    # 对比
    # ------------------------------------------------------------------
    def _auto_pair(self):
        """选中一组后自动决定比哪两张 —— 不用用户再点按钮。

        ⚠️ 但**用户已经自己点选了两张**时以他的为准（v1.10）。切排列
        顺序、重建列表、`_fill_group_list` 都会走到这里，不能把他的
        选择悄悄冲掉 —— 否则「点好两张 -> 切一下排列 -> 白点了」。
        """
        if len(self.pick_pair) == 2:
            self.set_pair(self.pick_pair[0], self.pick_pair[1])
            return
        order = self._member_order()
        if not order:
            self.set_pair(None, None)
            return
        if len(order) == 1:
            self.set_pair(order[0], None)
            return
        b = order[1]
        if self.cur_member and self.cur_member in order[1:]:
            b = self.cur_member
        self.set_pair(order[0], b)

    def set_pair(self, a, b):
        self.path_a, self.path_b = a, b
        self.mode = "pair" if (a and b) else "single"
        self._apply_grid()
        self._sync_link_ui()      # 单图下把「双图联动」按钮藏掉(v1.8)
        descs = self.descs
        self.pair = None
        self.rect_a = self.rect_b = None
        self.pan = [[0, 0], [0, 0]]
        # ⚠️⚠️ **换图必须把缩放也归位**（主人 2026-10-06 报「换组不会
        # 自动重置放缩」）。原来这里只清了 `pan`、没清 `zoom` ——
        # 于是上一组放大到 400% 时切到下一组，那张图也还是 400%，
        # 而它的显示图可能根本没那么大（`disp` 比视口还小），
        # 结果就是「一片糊/一片空白，看着像坏了」。
        # 换图 = 换了个内容，缩放状态不该继承 —— 这跟「切窗口要复位」
        # 是一回事。`reset_zoom` 里那两个 `_toast` 提示在这里会吵，
        # 所以直接归位、不提示（用户是主动换图的，不算意外）。
        self.zoom = [1.0, 1.0]
        if a and b and a in descs and b in descs:
            try:
                self.pair = scan.compare_pair(
                    descs[a], descs[b], th=scan.thresholds(self.preset_key))
            except Exception:
                self.pair = None
        if self.pair:
            kind, score, d, i, j = self.pair
            self.rect_a = descs[a]["rects"][i]
            self.rect_b = descs[b]["rects"][j]
        # 同上：结论行与信息卡是同一张，fit 只做一次
        self._update_verdict(self.pair, fit=False)
        self._fill_info(fit=False)
        self._fit_card()
        self.render_all()
        # 主人：「每组第一次打开会卡顿一下」—— 刚显示出来的这两张要**排到预热
        # 队列最前面**去备基准档，别排在那几十张后面慢慢等（防抖 250ms，
        # 连着点组只会重排一次队列，不会每点一次就解一遍）。
        self._later("prewarm", 250, self._prewarm_start)

    def _apply_grid(self):
        if self.mode == "pair":
            self.cell_b.grid(row=0, column=1, sticky="nsew", padx=S(6))
            self.cell_a.grid_configure(column=0, columnspan=1)
        else:
            self.cell_b.grid_remove()
            self.cell_a.grid_configure(column=0, columnspan=2)

    # ---- 缩放 / 平移 ---------------------------------------------------
    def _on_canvas_resize(self):
        """画布尺寸变了：重画，并且**按新尺寸重做一次预热**。

        ⚠️ 预热是按「当时的画布尺寸」准备成品图的，尺寸一变 key 就全对不上，
        白准备一轮（实测：刚扫完时画布 748×849、信息卡定型后是 748×733，
        于是回到 100% 那一档时又得现场解码 + 编码，130ms）。
        """
        self.render_all()
        self._later("prewarm", 400, self._prewarm_start)

    def _on_wheel(self, col, ev):
        """滚轮只缩放**鼠标底下那一侧**。

        `col` 是事件所属画布的序号（0 = 左 / A，1 = 右 / B），由绑定时的
        `lambda e, k=col:` 带过来 —— 不能用鼠标坐标去猜，因为单图模式下
        左画布会横跨整行，猜出来的列号是错的。
        """
        if col == 1 and self.mode != "pair":
            col = 0
        if not self.path_a:
            return
        if self.crop_only.get():
            self._toast("「只看匹配区域」下不缩放，先关掉它")
            return
        if col == 1 and not self.path_b:
            col = 0
        step = ZOOM_STEP if ev.delta > 0 else 1.0 / ZOOM_STEP
        cur = self.zoom[col]
        z = max(ZOOM_MIN, min(ZOOM_MAX, cur * step))
        if abs(z - cur) < 1e-6:
            return
        # ⚠️⚠️ **以鼠标底下那一点为锚点缩放，别把它送回画面中间**。
        #
        # 原来这里是 `self.pan[col] = [0, 0]` —— 一滚就把平移清零，
        # 于是「拖到某个位置再放大」会**跳回中间**（主人 2026-10-06 报的）。
        # 那不是「缩放该有的行为」，是根本没实现锚点：清零等于宣告
        # 「缩放后重新居中」，可用户明明还没挪回去。
        #
        # 锚点缩放的算式。图元画在 `ox + vx0`（`vx0` 是「显示图」坐标），
        # 其中 `ox = (cw - disp_w)//2 + pan`，而 `disp_w ∝ zoom`。于是
        # 「鼠标底下那点」的**基准像素**坐标是
        #     p = (mx - ox) * bw/disp_w = (mx - ox) / (fit*zoom)
        # 缩放比例 z/cur，要让它缩放前后不动：
        #     (mx - ox') / z  ==  (mx - ox) / cur
        # 解得   pan' = mx - (cw - disp_w*z/cur)//2 - z*(mx-ox)/cur
        # ⚠️⚠️ **两处都是除以 zoom**（`(mx-ox)/cur` 和 `disp_w*z/cur`）。
        #    我第一版两处都写成乘法，连滚 6 格把平移推到 129 万像素 ——
        #    详见方法体里那段记录。
        # ⚠️⚠️⚠️ **双图联动：先改本侧，再取归一化位置**（v1.8 修「缩放不同步」）。
        #
        # 顺序是**硬要求**，两步都不能挪：
        #   ① `_zoom_anchor` + 改 `zoom[col]` + 改 `pan[col]`  —— 先做完
        #   ② **然后**才取 norm，并按**新 zoom** 传给 `_link_apply`
        #
        # ⚠️ 我第一版把 `_lnorm = self._link_norm(col)` 写在最前面（在改动之前），
        #   那取到的是**缩放前**的位置 —— 而缩放恰恰会把它挪走
        #   （`_zoom_anchor` 按「鼠标底下那点不动」算，缩放后注视点确实变了）。
        #   实测同尺寸两张图，每一格偏差 x≈0.077 / y≈0.035，**方向和数值都一样**，
        #   而「拖一下」偏差立刻变 0.0000 —— 因为拖动时 zoom 不变。
        _linked = self._link_target(col) is not None
        keep = self._zoom_anchor(col, ev.x, ev.y, cur, z)
        self.zoom[col] = z
        if keep is not None:
            self.pan[col] = [keep[0], keep[1]]
        # 另一侧：倍率是**绝对值**，直接同步；位置按归一化换算
        #（这样两张图构图不同也能对上同一处）。
        if _linked:
            self.zoom[1 - col] = z
            # ⚠️⚠️ `frm=cur`不能省（见 `_link_disp`）：`_view["disp"]` 是
            # **旧 zoom（cur）** 下记下的，而 `self.zoom[1-col]` 此刻
            # 已经是 z了 —— 不显式给基准就会算出 `z/z = 1`，**压根不换算**。
            _lnorm = self._link_norm(col, z, cur)
            self._link_apply(col, _lnorm, z, cur)
        self._prewarm_yield()         # 正在缩放：预热别来抢 GIL
        # ⚠️⚠️ **联动时两侧都要重画**（v1.8）。
        # 只画 `only=col` 的话，另一侧的 zoom/pan 变了却没换图元 ——
        # 画面还停在旧倍率上，看起来就是「联动只动了一半」。
        # 代价实测：另一侧多半是缓存命中（几毫秒），比联动不同步划算。
        # 先用缓存里已有的图立刻响应（绝不同步重解大图 —— 那是「缩放好卡」
        # 的根源），再防抖 170ms 按新 zoom 精确重解一帧。连滚 N 格只解一次。
        # ⚠️ 系数 0.75 不是 0.5：粗档是 2 倍欠采样，主人反馈「放大还有马赛克」。
        #    0.75 欠采样 1.33 倍（看不出糊），而正在看的那两张在预热里已经
        #    备满了档，这里几乎都是缓存命中，代价一样是几毫秒。
        self.render_all(precise=0.75, only=None if _linked else col)
        # ⚠️⚠️ **防抖到期的那一帧，拖动中必须跳过**（v1.7 修帧间隔 185ms）。
        #
        # 埋点抓到：滚轮防抖留的 `zoomhi` 回调是 `precise=True` 精确重画
        # （实测单次 **69~74ms**），而它**不看 `_drag`** —— 用户滚完轮
        # 立刻开始拖，那一帧就会在拖动中途炸出来。AFTERDBG 统计里它是
        # 拖动期间仅次于 `_pan_fill` 的耗时源（每方向 2 次 × 73ms）。
        #
        # 跳过是安全的：拖动中每一步都在按 0.75 补块，松手时 `_pan_end`
        # 会用 `precise=True` 补最终帧 —— 清晰度一秒都不亏。
        def _zoom_hi(c=col):
            if self._drag:
                return
            #⚠️⚠️ 联动时 `only=None`（两侧都补精确帧）。原来固定 `c`，
            #   联动后另一侧就永远停在 0.75 那一档 —— 「联动看着糊」。
            self.render_all(precise=True, only=None if _linked else c)
        self._later("zoomhi%d" % col, 170, _zoom_hi)
        if _linked:
            # 另一侧也要有自己的防抖键，否则它那份精确帧永远不会被安排
            self._later("zoomhi%d" % (1 - col), 170, _zoom_hi)
        self._toast("%s缩放 %.0f%%（滚轮调整，按住可拖动）"
                    % ("" if self.mode != "pair" else ("左" if col == 0 else "右"),
                       z * 100))

    # -- 双图联动 --------------------------------------------------------
    #
    # ⚠️⚠️ **联动不能直接抄 `pan` 数值**，必须按「归一化位置」换算。
    #
    # 原因：两幅图的**画布尺寸可能不同、图的长宽比也可能不同**
    #（左边那张 3000x2000、右边 1328x2048），`pan` 是**画布像素**。
    # 同一个 `pan=(120,80)` 在两幅图上指的是完全不同的位置。
    #
    # ✅ 正确做法：把操作表达成「我正盯着图的哪个位置、放大到几倍」——
    #    即 `(相对中心的偏移 / 显示尺寸)` 这个**无量纲比值**，
    #    另一侧按同样的比值反解出它自己的 `pan`。
    #    这样「看同一处」在构图完全不同的两张图上也成立。
    #
    # 这是本函数唯一正确的同步依据；`zoom` 则直接同步（倍率是绝对的）。

    def _link_target(self, col):
        """联动目标侧（`1 - col`）；不联动 / 非并排 / 另一侧没图时给 `None`。"""
        if not self.link_view.get():
            return None
        if self.mode != "pair":
            return None
        other = 1 - col
        if other == 1 and not self.path_b:
            return None
        if other == 0 and not self.path_a:
            return None
        return other

    def _link_disp(self, col, zoom=None, frm=None):
        """该侧在 `zoom` 倍下的显示尺寸。

        `zoom` = 目标倍率（None = 就是当前 zoom）。
        `frm`  = `disp` 当前对应的倍率（None = `self.zoom[col]`）。

        ⚠️⚠️⚠️ **换算的基准必须是「`disp` 被记下来时那个 zoom」，
        不是 `self.zoom[col]`**（v1.8 修「滚轮缩放两侧不同步」时踩的坑，
        主人 2026-10-07 报）。

        症状是「直接缩放不同步，要拖一下才同步上」。手算复现（两张
        同尺寸图、画布 748x757、`_view["disp"]` = 477x736 即 zoom 1.0、
        滚到 1.25 倍、鼠标偏离中心）：

            本侧缩放后真实注视位置 norm = **(-0.0503, 0.1239)**
            旧代码把「缩放前」的 norm 套到另一侧 -> pan=(0,0)
            -> 另一侧 norm = **(-0.1275, 0.0891)**，偏差 x = **0.0772**
            （探针实测 0.0771，对得上）

        两个独立的错叠在一起，**只修一个还是错**：

        ① **取 norm 的时机**：缩放前取的是旧位置，而 `_zoom_anchor` 按
           「鼠标底下那点不动」算完之后注视点**已经变了**。
           ⇒ 必须先改 zoom/pan，再取 norm（`_on_wheel` 里现在就是这么排的）。

        ② **disp 用哪一档**：`_view["disp"]` 是**上一帧渲染时**的尺寸
           （对应旧 zoom），而 `render_all` 还没跑。所以要按
           `disp * zoom / frm` 换算到新 zoom。

        ⚠️⚠️ **② 里的 `frm` 不能用 `self.zoom[col]`**：
        `_on_wheel` 里 `self.zoom[other] = z` 是**先于** `_link_apply`
        执行��，所以那一刻 `self.zoom[other]` 已经是新 z 了，
        `zoom/frm = 1` ⇒ **根本没换算**（实测 `_view["disp"]` 停在
        旧值 597x920，而正确的新值应该是 746x1150）。

        ⇒ 调用方必须**显式**给出「这批 disp 是哪个 zoom 记下的」。
        滚轮路径传 `cur`（缩放前的 zoom），拖动路径本来就是当前 zoom。
        """
        v = self._view.get(col)
        if not v:
            return None
        disp = v.get("disp") or (0, 0)
        if disp[0] <= 0 or disp[1] <= 0:
            return None
        # ⚠️⚠️⚠️ **`frm` 默认取 `_view` 自己记的「disp 是哪一档算的」**
        # （v1.8 修「滚轮缩放两侧不同步」时栽在这两次）。
        #
        # `_view["disp"]` 是**渲染那一刻的 zoom** 下的尺寸，而这个
        # `zoom` 与 `self.zoom[col]` **可能已经不是同一个数**了：
        #   · 滚轮联动时，`self.zoom` 先改、`render_all` 后跑
        #     -> 中间那段窗口里两者必然不等
        #   · 拖动时 zoom 不变，两者相等
        #   · `_fit_ready` 回来补精确帧时两者也可能不等
        #
        # 我原来拿 `frm` 当**参数**从调用处传进来，两次都传错：
        #   ① 不传 -> 用 `self.zoom[col]`（已是新 zoom）-> 多乘/少乘
        #   ② 传 `cur`（缩放前）-> 若 `disp` 已被就地更新成新档，又多乘
        # 实测两种都量到偏差（0.0012 / 0.0022），而且**方向相反**。
        #
        # ✅ 正确：**让 `_view` 自己记**（写入时带上 `dz`= 当时的 zoom），
        #    读的时候用它当基准 —— 不靠调用方传，调用方就传不错。
        cur = v.get("dz")
        if cur is None or cur <= 0:
            cur = self.zoom[col] if frm is None else frm
        if zoom is None or abs(zoom - cur) < 1e-9:
            return (disp[0], disp[1])
        k = float(zoom) / cur
        return (max(1, int(round(disp[0] * k))),
                max(1, int(round(disp[1] * k))))

    def _link_norm(self, col, zoom=None, frm=None):
        """当前这一侧「正盯着的位置」，返回 `(nx, ny)` = 偏移/显示尺寸。

        > 1 表示已经拖到图的边界外（夹持会拦住，正常不会）。
        `None` 表示拿不到（画布还没布局好）⇒ 调用方要跳过联动。
        """
        v = self._view.get(col)
        if not v:
            return None
        disp = self._link_disp(col, zoom, frm)
        if not disp:
            return None
        cw = max(1, v["cv"].winfo_width())
        ch = max(1, v["cv"].winfo_height())
        m = ((cw - disp[0]) // 2, (ch - disp[1]) // 2)
        return ((self.pan[col][0] - m[0]) / float(disp[0]),
                (self.pan[col][1] - m[1]) / float(disp[1]))

    def _link_apply(self, col, norm, zoom=None, frm=None):
        """把 `col` 侧的归一化位置套到另一侧去（另一侧的 zoom 保持不变）。

        ⚠️ `zoom`/`frm` 语义见 `_link_disp`：滚轮联动时传
          (`新 zoom`, `旧 zoom`)，拖动时都不传。
        """
        other = self._link_target(col)
        if other is None or norm is None:
            return
        v = self._view.get(other)
        if not v:
            # 另一侧还没出图：记下来，等它出图时再套
            # （`_render_side` 开头会取，见那里的 `_link_pend`）
            self._link_pend[other] = (tuple(norm), zoom, frm)
            return
        disp = self._link_disp(other, zoom, frm)
        if not disp:
            return
        # ⚠️ **先把 norm 存起来**（v1.8）：画布还没布局时套不上，
        # 而滚轮第 1 格常常正好撞上（刚 `set_pair`，尺寸还没定）。
        # 不存的话那一格的对齐会被永久丢掉 —— 实测偏差 0.19。
        self._link_pend[other] = (tuple(norm), zoom, frm)
        cw = max(1, v["cv"].winfo_width())
        ch = max(1, v["cv"].winfo_height())
        if cw <= 1 or ch <= 1:
            return                      # 画布还没布局 -> 等 `_render_side` 来取
        m = ((cw - disp[0]) // 2, (ch - disp[1]) // 2)
        self.pan[other] = [int(round(m[0] + norm[0] * disp[0])),
                           int(round(m[1] + norm[1] * disp[1]))]

    def _link_sync_pan(self, col):
        """拖动联动：把本侧**这一步的画布像素位移**换算成归一化，套到另一侧。

        ⚠️ 为什么要除以显示尺寸：拖动的物理含义是「图跟着手走」，
        位移得按**该图自己的比例**折算，否则长边图和方图会差很远
        （同一份 `dx=120`，3000px 宽的图上是一小步，
        800px 宽的图上就是一大步）。

        ⚠️⚠️⚠️ **`dx/dy` 必须是「这一步的增量」，不是「从拖动起点的总位移」**
        （v1.8 修，埋点抓出来的）。

        我原来在 `_pan_move` 里传的是 `px - info["px"]`，
        而 `info["px"]` 要等**本侧补块重画后**才更新 ——
        于是这个「增量」在几步里是 0、0、8、16… 这种阶梯，
        而另一侧每次都在**已有位置之上再减一个累计量**：
            步1: norm(已含+0) - 0/disp -> 不动
            步2: norm(已含+8) - 8/disp -> 回到原位
            步3: norm(已含+16) -16/disp -> 又回到原位
        **两侧的 pan 看起来「跟着走」，实际每步都被拉回原处**，
        只有本侧补块重画那几步才跳一下（实测 14 步里只动 5 次）。

        ✅ 正确：**用归一化位置本身当唯一真值**。
        本侧 `pan` 一变，`_link_norm` 立刻反映新的注视位置，
        另一侧直接对齐过去 —— 不需要任何增量、也不会有累积误差。
        （`dx/dy` 参数因此不再需要，留着只为调用处表达意图。）
        """
        if self._link_target(col) is None:
            return
        self._link_apply(col, self._link_norm(col))

    def _link_move_item(self, col):
        """把该侧图元挪到「当前 pan 该在的位置」（只挪，不出图）。

        ✅ 公式与 `_pan_move` **完全一致**：`bx + (pan - px)`。

        ⚠️⚠️⚠️ **千万不要更新 `view["px"]/["py"]`**（v1.8，埋点抓出来的，
        我第一版正好写反了，害我debug 了三轮）。

        **本侧 `_pan_move` 也不更新 `info["px"]`** —— 那不是漏了，
        而是**故意的**，它就是这套公式成立的前提：
            `px` 一直是「**出图时**的 pan」（渲染的基准），
            于是 `bx + (pan_now - px)` = 「出图落点 + 之后挪了多远」
            = **图元此刻该在的绝对位置**，天然累积、不需要每步推进。

        我第一版以为「不推进基准会重复累加」，于是每步
        `v["px"] = pan` —— 结果**每步都把图元重置回出图落点 `bx`
        再加当步增量**，之前累积的挪动全被丢掉：

            埋点实测（右侧该每步动 5.2px）
            pan    130 -> 135 -> 141 -> 146
            coords -85 -> -78 -> -77 -> -78   ← 在出图落点 -83 附近来回晃
            典型症状：**pan 完全正确、画面却在原地打转**。

        ⇒ 教训：**照抄本侧已有的正确写法**，别自己"优化"出一个
          看似更合理的版本。同一件事在同一个文件里已经做对了。
        """
        v = self._view.get(col)
        if not v or v.get("item") is None:
            return
        cv = v["cv"]
        dx = self.pan[col][0] - (v.get("px") or 0)
        dy = self.pan[col][1] - (v.get("py") or 0)
        if not dx and not dy:
            return                # 与出图时一致，落点已经对了
        # ⚠️ `bx/by` 是「出图时的落点」，`px/py` 是「出图时的 pan」
        #   —— 两个基准一起用，位置才是绝对正确的（同 `_pan_move`）。
        cv.coords(v["item"], v.get("bx", v["ox"]) + dx,
                  v.get("by", v["oy"]) + dy)
        # ⚠️⚠️ **这里绝对不要写** `v["px"], v["py"] = self.pan[col]`！
        #   写了就等于每步重置基准，画面会「在原地打转」（见上面）。

    def _zoom_anchor(self, col, mx, my, cur, z):
        """算「以鼠标底下那点为锚点」缩放后该有的平移量，返回 `(pan_x, pan_y)`。

        ⚠️ 为什么必须有这个（主人 2026-10-06 报「拖到一个位置想放大，
        它会重新回到画面中间」）：图元画在 `ox + vx0`（`vx0` 是**显示图**
        坐标），其中 `ox = (cw - disp_w)//2 + pan`。要让鼠标底下那点
        （画布坐标 `(mx, my)`）缩放前后**停在同一块像素上**，就得满足

            (mx - ox') / z  ==  (mx - ox) / cur

        （两边都是「画布偏移 ÷ 该级的 zoom」= 该点的**基准像素**坐标，
        相等就说明盯着的是同一块像素）。解出

            pan' = mx - (cw - disp_w*z/cur)//2 - z * (mx - ox)/cur

        ⚠️ 注意两处都是**除以 zoom**：`disp_w` 随 zoom 线性变化，所以
        新显示尺寸是 `disp_w*z/cur`，不是 `disp_w*z`。

        ⚠️ 返回 `None` 表示**这次不算锚点**（拿不到画布尺寸/显示图尺寸），
        调用方要沿用旧平移，而不是清零 —— 清零就是「跳回中间」，
        正是要修的那个毛病。
        """
        cv = self.cv_a if col == 0 else self.cv_b
        if self.mode != "pair":
            cv = self.cv_a
        if cv is None:
            return None
        cw = max(1, cv.winfo_width())
        ch = max(1, cv.winfo_height())
        info = self._view.get(col) or {}
        disp = info.get("disp")
        if not disp or disp[0] <= 0 or disp[1] <= 0:
            return None
        dw, dh = disp
        # ⚠️⚠️⚠️ **`dw/dh` 是「当前 zoom 下」的显示尺寸**，不是原图尺寸。
        #    新 zoom 下的显示尺寸要按 `z/cur` 换算 —— 直接 `dw*z` 是错的
        #    （见下面「我第一版写错了」的记录）。
        ndw = max(1, int(round(dw * z / cur)))
        ndh = max(1, int(round(dh * z / cur)))
        # 缩放前：鼠标点在画布上的偏移（相对显示图左缘）
        ox = (cw - dw) // 2 + self.pan[col][0]
        oy = (ch - dh) // 2 + self.pan[col][1]
        # ⚠️⚠️⚠️ **不变量必须除以 zoom，不是乘**（我第一版写反了）。
        #
        # 图元画在 `ox + vx0`（显示坐标），而显示坐标 `vx` 对应的基准像素是
        #     `p = vx * bw/disp_w = vx / (fit*zoom)`
        # 所以缩放不变量是 **`(mx - ox) / zoom`**，不是 `(mx - ox) * zoom`。
        # 写成乘法的话每滚一格误差乘 `cur²`，连滚 6 格放大 2000 多倍 ——
        # 实测平移量被推到 **129 万像素**（视口才 1515 宽），整块图飞到天边，
        # 块尺寸也塌成 `blk=(8,8)`，拖动全程在空白里挪。
        px = (mx - ox) / cur
        py = (my - oy) / cur
        # 缩放后：让同一点仍落在 (mx, my)
        nox = (cw - ndw) // 2
        noy = (ch - ndh) // 2
        return (int(round(mx - nox - px * z)),
                int(round(my - noy - py * z)))

    def reset_zoom(self):
        """两侧一起回到「适应窗口」。

        在图上滚轮是「只管这一侧」，但这个按钮是工具栏上的全局动作，
        只重置一侧会让人以为按钮坏了 —— 所以两侧都归位，并在提示里说清楚。
        """
        both = self.mode == "pair"
        self.zoom = [1.0, 1.0]
        self.pan = [[0, 0], [0, 0]]
        self.render_all()
        self._toast("两侧都回到适应窗口" if both else "已回到适应窗口")

    def _pan_start(self, col, ev):
        self._drag = (col, ev.x, ev.y, self.pan[col][0], self.pan[col][1])

    def _pan_move(self, col, ev):
        """拖动平移。

        ⚠️ 这里**只挪画布上的图元**，绝不重新出图 —— 一移动就重画会卡成幻灯片。

        ⚠️ 但分块渲染之后画布上只有「视口 + 一圈缓冲」，拖到缓冲外面就会露出
        空白。所以超过缓冲就**节流**（80ms 一次）重取一块新的；松手时再补一张
        精确的（`_pan_end`）。平时在缓冲内移动仍然是纯挪图元，零成本。
        """
        d = self._drag
        # ⚠️⚠️⚠️ **不能因为「没放大」就整个冻结拖动**
        #（主人 2026-10-06：「缩放时会强制定位到图片中心」+
        #  「在拖动前必须进行缩放，不然无法拖拽」）。
        #
        # 原来这里直接 `return`：`zoom <= 1.001` 时**整个拖动被冻结**。
        # 而 `zoom=1` 是「适应窗口」—— 此时缩回 1.0，画面立刻回到居中、
        # 再也拖不动。用户看到的就是「一缩放就被甩回中间」。
        #
        # 现在改成：**任何情况下都不在入口冻结**，一律交给下面的夹持
        # （`lo/hi` 取 min/max，区间宽度恒为 `|cw-dw|`）。
        # 夹持会自动做到「拖得动，但不把图拖出视口」。
        if not d or d[0] != col:
            return
        info = self._view.get(col)
        if not info:
            return
        disp = info.get("disp") or (0, 0)
        cv = info["cv"]
        cw = max(1, cv.winfo_width())
        ch = max(1, cv.winfo_height())
        # ⚠️⚠️⚠️ **不许在这里 return**（v1.7 修「图比视口小时完全拖不动」，
        # 主人 2026-10-07：「每次拖拽前必须缩放一下，在这个位置无法拖拽」
        # + 截图显示 36%）。
        #
        # 原来这四行是「图比视口小就直接不动」：
        #     _room_x = disp[0] - cw
        #     _room_y = disp[1] - ch
        #     if _room_x <= 0 and _room_y <= 0:
        #         return
        #
        # ⚠️⚠️ **它的想法是错的**：把「图完全塞得下」当成了「拖了没意义」。
        # 可图比视口小时恰恰**最能拖** —— 整张图都在画布里，
        # 往任意方向挪都不会切掉内容，这正是「拖着看整图」最自然的交互。
        # 实测主人那个位置：图 477x736、视口 1515x757，
        # **横向本该有 1038px 可拖范围**（69% 视口宽），却被冻成 pan=0。
        #
        # 真正「拖不动」的只有一种情况：`dw == cw` 且 `dh == ch`
        # （图正好铺满视口，一个像素都挪不动）。那种情况**下面的夹持
        # 会自动处理**（区间宽度 `|cw-dw|` = 0，夹完还是 0），
        # 不需要在入口处特判 —— 入口特判反而漏掉了「一维刚好铺满、
        # 另一维有余量」这种常见情形。
        px = d[3] + (ev.x - d[1])
        py = d[4] + (ev.y - d[2])
        # ⚠️⚠️ **把平移量夹在「图能移动的范围」内**，两维分别算。
        #
        # 图在画布上的范围 = `[ox, ox+dw]`，`ox = (cw-dw)//2 + pan`。
        # 「图边最多比视口边多出 T 像素」解出：
        #     pan >= -T - (cw-dw)//2
        #     pan <=  cw + T - dw - (cw-dw)//2
        # ⚠️⚠️ **两个界限不对称**（`(cw-dw)//2` 本身可能是负数）。
        #   我第一版图省事写成 `±(dw-cw)//2`，在 dw 略大于 cw 时
        #   上下限差不多对，但**实测 y 方向仍露了 293px** ——
        #   因为图高 1050 / 视口 757，`(757-1050)//2 = -147`，
        #   正确区间是 `[147, -146]`（**空的**，见下），而 `±147` 超出了。
        #
        # ⚠️⚠️⚠️ **图比视口大时，「零露白」在数学上就不可能**：
        #   区间宽度 = `cw - dw` = **负数**。也就是说无论怎么拖，
        #   都至少有一边露 `dw-cw` 像素 —— 那是**图比视口多出来的部分**，
        #   是图的固有属性，不是拖坏的。
        #   所以这里的 T 就是「允许拖出视口多少」：图大时按 T=0 夹
        #   （拖到图边对齐视口边为止，最自然），图小时 T 也=0
        #   （不许拖出）。**两维独立**，一维有余量另一维没余量都常见。
        _T = 0
        # ⚠️⚠️⚠️ **区间公式：lo/hi 谁大谁小必须先推对**（v1.7 踩了整整三轮）。
        #
        # 图在画布上的范围 `[ox, ox+dw]`，`ox = (cw-dw)//2 + pan`。
        # 「零露白」= `ox <= 0`（图左沿不越过视口左沿）
        #           且 `ox + dw >= cw`（图右沿不短于视口右沿）。解出：
        #     pan <= -(cw-dw)//2                       = 上界
        #     pan >=  cw - dw - (cw-dw)//2              = 下界
        # 于是区间宽度 = `dw - cw`：**图比视口大时是正的**（可以拖），
        # 图比视口小时是负的（区间为空= 塞得下，拖了必然露白）。
        #
        # ⚠️ 我头一版把这两条写反了（`lo = -_cx`、`hi = cw-dw-_cx`），
        # 于是**无论图大图小，`lo` 都大于 `hi`**：
        #   实测 3.81 档 `_cx = -1348` -> `lo=1348, hi=-1348`，
        #   被我当「空区间」处理、取并集钉死在 `pan=1348` —— 而日志里
        #   `pan=(1348,1026)` **恰好就是这个数**，一下就暴露了公式反了。
        #   危害是 60 步全被吸回同一点，画面覆盖卡 0% / 58%。
        _cx = (cw - disp[0]) // 2
        _cy = (ch - disp[1]) // 2
        # ⚠️⚠️⚠️ **lo/hi 必须取 min/max，方向随「图比视口大还是小」翻转**
        #（v1.7 修「图比视口小时完全拖不动」，主人 2026-10-07 报：
        #  「每次拖拽前必须缩放一下，在这个位置无法拖拽」+ 截图 36%）。
        #
        # 这和 v1.7 那次「lo/hi 写反」是**同一类错误的第二次** —— 两次都是
        # 没把「两个候选界的大小关系会翻转」这件事想清楚。
        #
        # 推导（记 `m = (cw-dw)//2`，图在画布上 `ox = m + pan`）：
        #   `dw <= cw`（图比视口小）-> 要「图**完整可见**」
        #       ox >= 0      =>  pan >= -m
        #       ox + dw <=cw =>  pan <=  cw - dw - m
        #     => 区间 `[-m, cw-dw-m]`，宽度 `cw-dw`
        #   `dw >  cw`（图比视口大）-> 要「画面**无空白**」（图盖住视口）
        #       ox <= 0     =>  pan <= -m
        #       ox + dw >= cw => pan >=  cw - dw - m
        #     => 区间 `[cw-dw-m, -m]`，宽度 `dw-cw`
        #
        # ⚠️⚠️ **两行的界是同样的两个数 `-m` 和 `cw-dw-m`，只是大小关系相反。**
        # 所以正确写法只有一行 —— 取 min/max，不必分情况：
        #     lo, hi = min(-m, cw-dw-m), max(-m, cw-dw-m)
        #
        # 我原来只实现了第二种（且用 `lo <= hi` 判「能不能拖」），
        # 于是**图比视口小时区间恒空 -> 被判成「图塞得下、拖了只会露白」
        # -> 整个拖动被冻结**。实测主人那个位置：图 477x736、视口 1515x757，
        # 横向本该有 **1038px** 的可拖范围（69% 视口宽），实测 pan 恒为 0。
        #
        # ✅ 附带好处：**「真空间隙」根本不存在**。
        #   `hi - lo = |cw - dw|`，只有 `dw == cw`（正好铺满）时才是 0。
        #   所以下面那个 `if not free_x: px = 中点` 的分支是多余的 ——
        #   它是「用错一套界」的产物，留着只会让人以为空区间是合法状态。
        lo_x, hi_x = min(-_T - _cx, cw + _T - disp[0] - _cx), \
            max(-_T - _cx, cw + _T - disp[0] - _cx)
        lo_y, hi_y = min(-_T - _cy, ch + _T - disp[1] - _cy), \
            max(-_T - _cy, ch + _T - disp[1] - _cy)
        # 逐维夹住：图比视口小的那一维，区间就是「让它贴边但不越界」；
        # 图比视口大的那一维，区间是「不露白」。两维互不干扰。
        px = max(lo_x, min(hi_x, px))
        py = max(lo_y, min(hi_y, py))
        # ⚠️⚠️ **必须用 `bx/by`（图元自己的落点），不能用 `ox/oy`**。
        #
        # `ox/oy` 是**整张显示图的原点**，而分块之后贴上画布的只是
        # 视口 + 缓冲那一小块，它贴在 `ox + px0/sx`（实测 pan=0 时
        # ox=-1348、图元实际在 -151）。拿 `ox` 当落点 = 每一步都把图元
        # 瞬移回显示图左上角，露出 1044px 空白 -> 判定「该重画」->
        # 每步 60~90ms —— **这就是主人说的「拖拽有抖动」**。
        # 整张贴图那条路径 `bx == ox`，所以两边写法统一没有副作用。
        cv.coords(info["item"], info.get("bx", info["ox"]) + (px - info["px"]),
                  info.get("by", info["oy"]) + (py - info["py"]))
        self.pan[col][0], self.pan[col][1] = px, py
        # ⚠️⚠️ **双图联动：把另一侧的图元也挪到对应位置**（v1.8）。
        #
        # 这一步**只挪图元、绝不出图** —— 和本侧一样「一移动就重画会卡成幻灯片」。
        # 所以另一侧的补块交给下面 `_pan_fill` 统一做（`only=None` 两侧都画）。
        if self._link_target(col) is not None:
            self._link_sync_pan(col)         # 对齐另一侧（见其docstring）
            self._link_move_item(1 - col)    # 只挪图元，出图交给 _pan_fill
        # ⚠️⚠️ **真正的判据是「离露白还有多远」**，不是「盖没盖满」。
        #
        # v1.6 的 `_tile_needs_more` 判的是「bbox 盖不满画布就重画」，
        # 而它配的节流又因为 `_drag_last = 0.0` **一次都没生效过**
        #（`time.time() - 0.0 >= 0.02` 恒真）。两层一起失效 =
        # 拖动时**每一步都重画**：实测 60 步里 60 步、每步 65~94ms
        #（16ms 一帧 = 掉 4~6 帧），这就是主人说的「拖拽有抖动」。
        #
        # 只把节流修好也不行：「盖不满」那一刻画面**已经露白了**。
        # 块是「视口 + 一圈约 152px 缓冲」，挪 8px 就吃掉 8px 余量，
        # 等余量归零，前面的帧早就在白跑重画了。
        #
        # 所以判据换成 `left`：缓冲还剩厚厚一层（>= PAN_REDRAW_PX）就
        # **别重画** —— 挪 8px 不可能露白。重画频率从「每步」降到
        # 「每 PAN_REDRAW_PX / 步长」步一次。
        # ⚠️⚠️ **逐维判「这一维还有没有富余」**，而不是整体 `_at_limit`。
        #
        # 踩过的两个坑（都是判据的错）：
        #   ① `or`：任一维到极限就不补块 -> 另一维真露白也不补，
        #      实测「画面盖满画布最差 0%」。
        #   ② `and`：两维都到极限才算贴边 -> 只到一维时另一维真露白，
        #      同样漏补（换个姿势犯同一个错）。
        #
        # 正确口径：**只有「slack 为负的那一维」真的没富余了才不用补**。
        # 有富余的维度不管到没到极限，露白都还能靠补块解决。
        left = self._tile_slack(col, cv)
        # ⚠️⚠️⚠️ **「到头了就不用补块」= 区间宽度耗尽**（v1.7 第三版，
        # v1.7 收尾再修一次口径）。
        #
        # 修好 lo/hi 方向之后（见上面那段推导），`hi - lo` 恒等于
        # `|cw - dw|`，**再也不会是负数**了 —— 之前那些「负数导致
        # `_no_room` 恒真」的 troubles 一并不存在了。现在它就是字面意思：
        # **这一维还能再拖多少像素**。
        #
        #   - 宽度 > 2 -> 还能拖 -> 拖的过程中可能露白 -> **要补块**
        #   - 宽度 <= 2 -> 已经拖到头（`dw==cw` 正好铺满，或贴到边），
        #     再拖也是原地；此时 slack 若不足，那是**图的边**够不到，
        #     补块也补不出来 -> **不用补**
        #
        # 逐维判：任一维还能动，那一维就可能露白，得补。
        _room_x_left = hi_x - lo_x
        _room_y_left = hi_y - lo_y
        _no_room = (_room_x_left <= 2 and _room_y_left <= 2)
        # ⚠️⚠️⚠️ **贴边死循环：pan 已经不动了，就不该再补块**（v1.7）。
        #
        # 探针抓到：拖到图边缘后最后 25 步 **pan 完全不变**（被夹住），
        # 可图元**每一步都换**（`.........1...........1111111111111111111111111`）
        # -> 每步一次 `delete + create_image` + Tk blit，**实测 p95 120ms**。
        # 原因是我上一版把「图比视口大」判成「有无限空间」（那时的
        # `free=True` 口径），于是 `_no_room` 恒假 —— 可「空间无限」
        # 说的是**拖动**，拖不动的时候照样不该重画。
        #
        # ⚠️ 那个 `free_*` 口径本身已经被上面 lo/hi 的修复淘汰了
        #   （`hi - lo = |cw-dw|` 直接就是真实余量，不需要间接推断）。
        #   但这条判据依然必要 —— 「区间还有余量」和「这一步真的动了」
        #   是两件事：区间宽 1038px 但鼠标停在原地不动时，同样不该重画。
        #
        # 正确判据：**这一步 pan 相对上一帧真的动了吗**。
        # 没动 = 已经贴边，画面不会变化，重画纯属白花 100ms。
        _prev = info.get("px"), info.get("py")
        _moved = (px != _prev[0]) or (py != _prev[1])
        # ⚠️⚠️⚠️ **判据必须是「有没有跨出当前块」，不是「离边缘多远」**
        #（v1.7，配合「块长恒定」这个前提）。
        #
        # 块长定长之后，slack 里**横向那一维恒定不变**（块比视口宽固定的
        # 那一截），纵向也在变 —— 但 `min(四边)` 会被恒定的那维顶住，
        # 于是 slack 看着还有 42、纵向其实早露白了。trace 实测补块间隔
        # 正好等于网格步长（56px = 8 步），比该有的多 4~5 倍。
        #
        # 正确判据直接对齐机制：**当前视口还落在已渲染那一块里吗**。
        # 块起点是 `bx/by`、块显示尺寸可由 `_view["want"]` 与基准比例
        # 反推；更省事的等价写法是**比较网格坐标** ——
        # `_render_tile` 用 `floor((-o - pad) / pad)` 定网格，
        # 这里用同一个式子，两边永远一致。
        _pad = info.get("pad") or (max(8, int(min(disp[0], cw) * self.TILE_PAD)),
                                   max(8, int(min(disp[1], ch) * self.TILE_PAD)))
        # ⚠️ **两维各用自己的 pad**：横向 pad 和纵向 pad 不同（视口宽高不同），
        #   网格步长也不同，必须分开算 —— 我第一版拿一个标量两边共用，
        #   结果纵向判错格、补块从 12涨到 41。
        _padx = max(8.0, float(_pad[0]))
        _pady = max(8.0, float(_pad[1]))
        # ⚠️ 用**这一帧记录的显示图原点**，而不是当前算的 ——
        #   `ox/oy` 是渲染那帧算的（含当时的 pan），`_pan_move` 里
        #   没有自己的 ox/oy（我第一版直接写 `ox`，NameError）。
        _vox = info.get("ox", 0)
        _voy = info.get("oy", 0)
        # ⚠️⚠️⚠️ **必须先把 pan 的增量算进去，而且符号是「加」**（v1.8 修）。
        #   网格是**渲染那帧**的 ox 算出来的，而 `info["ox"]` 是**那一帧**的
        #   —— 两者差一个本步的 pan 增量。漏掉这一步判据会整体偏一格
        #   ->「提前一格补块」（实测补块从 12涨到 41 就是这个）。
        #
        # ⚠️⚠️⚠️ **符号必须是 `+=`，我原来写成了 `-=`（v1.7 遗留）**。
        #
        #   `ox = (cw - disp_w)//2 + pan` —— **pan 增大，ox 就增大**。
        #   所以「本步的 ox」= 「渲染那帧的 ox + 本步 pan 增量」。
        #
        #   写成减号的后果（实测，不是推演）：
        #     向左拖（pan 减小）-> `_vox` 反而变大 -> `_g` 偏大
        #     -> 每一格都误判「跨格」-> **每步都补块**。
        #   埋点（`_d5.py`）在「左」方向量到：
        #     k=7  my_g=(2,5)  g0=(3,5)   XX   <- 本侧算的格子比记录的小
        #     k=9  my_g=(2,5)  g0=(3,5)   XX
        #   即「自己算的格」和「渲染记录的格」在反向漂移，越拖越偏。
        #   `probe_pan2.py` 那时量到「左 43/115、上 57/115 补块过频」，
        #   根因就在这一行 —— 不是网格量化、不是贴边退化。
        #
        # ⚠️ 同理纵向也是加号（`oy = (ch - disp_h)//2 + pan_y`）。
        _vox += (px - _prev[0])
        _voy += (py - _prev[1])
        # ⚠️⚠️ **索引也必须夹到 0，与 `_render_tile` 的 `vx0 = max(0, _gridx)`
        # 完全一致**（v1.7 修露白）。拖到图顶/图左时 `_gridx` 是负数、
        # 被 `max(0, ...)` 夹成了 0 —— 可索引还留着那个负数，于是
        # 「当前格」永远算不对 -> 判该补时不补 -> **画面露白 11~12%**。
        # 一处夹一处不夹，两边就永远对不上。
        _gx = int((-_vox - _padx) // _padx)
        _gy = int((-_voy - _pady) // _pady)
        # 夹到 0 之后再乘回去，就是 `_render_tile` 里那个网格起点
        _g = (max(0, _gx), max(0, _gy))
        _g0 = info.get("grid")
        # ⚠️⚠️ **两维都要看网格**（v1.8，配合「两维都定长」）。
        #
        # v1.7 是「横向定长 + 纵向跟视口」，那时只有横向跨格才意味着
        # 缓存 key 变了，纵向靠 slack 判。v1.8 把纵向也改成定长
        # （`vy1 = vy0 + _ny`，为了缓存能命中），于是：
        #   · **纵向跨格 = 缓存 key 变了** → 和横向一样必须补
        #   · **纵向 slack 也变恒定** → 靠 slack 判**再也不会触发**
        #
        # ⚠️⚠️ 若只改成「两维跨格」，那个 `left <1` 的兜底会跟着失效 ——
        #   slack 恒定在缓冲厚度上，永远 `>1`，于是**只有网格跨格才补**。
        #   这本身是对的（网格步长 = 缓冲厚度，跨格前一定还没露白），
        #   但必须**显式验一遍**，不能假设。见 `probe_stall.py` 的场景 S：
        #   判据是「拖 200 步全程不许露白，且 p95 帧耗时 < 20ms」。
        _crossed = (_g0 is None) or (_g[0] != _g0[0]) or (_g[1] != _g0[1])
        # ⚠️⚠️⚠️ **「跨格」和「缓冲还厚」是两种不同的补块理由，不能用一个
        # `and` 卡在一起**（v1.8 修，纵向定长之后才暴露）。
        #
        # `left < _pan_redraw_px(...)` 是 v1.7 为「纵向跟视口」加的闸门：
        # 纵向块滑动时 slack 会一路掉到很小，靠它**提前**补，别等露白。
        #
        # 可 v1.8 两维都定长之后，slack 恒定在缓冲厚度上（实测 pad=66/133），
        # **永远大于阈值** —— 于是那个 `and` 让 `left < 1` 和 `left < 阈值`
        # 两条路全都被堵死，`_crossed` 单独决定补不补。
        # 结果：跨格时补块时机被 `left` 意外否决（缓冲厚的时候反而不补），
        # 或者反过来被 slack 意外放行，两种都错。
        #
        # ✅ 正确：**两个理由各自独立成立就补**。
        #   · `_crossed`：缓存 key 变了（定长块，跨格 = 必须换）
        #   · `left < 阈值`：缓冲快用完了（滑动块的老口径，留着兜底）
        _need = _crossed or (left < self._pan_redraw_px(disp[0], disp[1], cw, ch))
        if not _no_room and _moved and _need:
            self._drag_last = time.time()
            # ⚠️⚠️⚠️ **补块必须挪到「下一轮事件循环」，不能在这一帧里做**
            #（v1.7 修「最慢一帧 164~204ms」）。
            #
            # 埋点抓到：补块那一帧的`render_all` 本身要 108~113ms
            #（真编码一整块），加上 Tk blit 就是 160~200ms —— 一帧丢掉
            # 10~12 帧，用户看到的是「拖一下停一下」。
            #
            # 为什么挪走是安全的：**这一帧该做的已经做完了** ——
            # 图元已按新 pan 挪好（跟手比值恒-1）、判据也判过了。
            # 块只是「下几帧才用得上」的缓存，现在补和 16ms 后补
            # 在画面上**完全一样**（人眼分辨不出 16ms）。
            # 而且拖动越快、块越早被复用 —— 但那时早就补好了。
            #
            # `after(1)` 而不是 `after(0)`：给 Tk 一个真实的下一轮，
            # 保证这一帧的 update() 能先把挪好的图元blit 出去。
            #
            # ⚠️ 用**固定 key**（去抖）：拖动中会连发几十次「该补块」，
            #   不去抖就变成排几十个回调，一次性全跑完反而更卡。
            self._pan_fill_pend = True
            if not self._later_ids.get("panfill"):
                self._later_ids["panfill"] = self.after(1, self._pan_fill)
        return

    def _pan_fill(self):
        """拖动补块（延迟到下一轮事件循环，见 `_pan_move` 那段注释）。

        ⚠️⚠️⚠️ **同步补块依然是 108~113ms，主线程躲不掉**（v1.7 实测）。
        #
        # `after(1)` 只是把它推到下一次 `update()`，可下一次 `update()`
        # 同样会等它跑完 —— 用户看到的还是「拖一下停一下」（实测最慢单帧
        # 186~196ms）。
        #
        # ✅ 所以拖动补块**必须上后台线程**。好在设施是现成的：
        # `_fit_async` 已经在做「后台 GDI+ + 编码 -> `_fit_ready` 换图元」，
        # 拖动补块只要把 `precise` 那一档也丢过去就行。
        # 主线程这一帧就只挪图元（0~2ms），**彻底不卡**。
        #
        # ⚠️ 为什么滚轮/精确帧不这么做：它们要「立刻看到结果」
        #（滚轮要在170ms 内给清晰帧），后台化会让手感变差。
        # 拖动补块是「下一帧才用得上」的缓存，后台化零代价。
        #
        # ⚠️⚠️ **实测：后台化对这一步毫无收益，别做**（v1.7，probe_fillcost.py）。
        #
        # 我原以为「主线程被 108ms 堵住」=> 该上后台线程。量完发现前提是错的：
        #   整链（裁块 6.4 + GDI+ 9.9 + 编码 30.1）同步 **49.0ms**，
        #   搬后台线程 **48.9ms** —— **快 0%**。
        # 原因：`bgra_to_ppm` 是「扩展切片赋值」，C 层 memcpy，**不释放 GIL**；
        # `crop_bgra` 逐行切片同理。只有 GDI+（ctypes）那 9.9ms 释放 GIL，
        # 而它只占整链的 20%。剩下 30.1ms 的编码 + 31.9ms 的
        # `tk.PhotoImage`（Tk 只认主线程）**一个都搬不走**。
        #
        # 真正的 108ms 里另外 ~60ms 是 `tk.PhotoImage` + 贴图，
        # 那部分必然在主线程。所以这一步的 ~95ms 是**物理下限**。
        # 该修的是「别让别的回调在这时候炸出来」—— 见 `_on_wheel` 里
        # `_zoom_hi` 那道拖动守卫，以及 `_prewarm_*` / `_base_prep_go`
        # 的拖动让路（那两条加起来就是 260ms 里多出来的 110ms）。
        """
        self._later_ids.pop("panfill", None)
        d = self._drag
        if not d:
            return                      # 已经松手了 —— 松手路径自己会补精确帧
        col = d[0]
        # ⚠️ 只在**真的会露白**时才去补块。拖动中绝大多数步都在缓冲内，
        #   那些步不该付出「裁块 + 插值 + 编码」的成本。
        info = self._view.get(col)
        if not info:
            return
        self._pan_redraw_active = True
        try:
            # ⚠️⚠️ **联动时两侧都要补**（v1.8）：另一侧的图元虽然被挪动了，
            # 但它**自己的缓冲也在被消耗** —— 只补本侧的话，另一侧拖几步
            # 就会露白（那才是「联动看起来会撕」的真凶）。
            # 代价：另一侧多半是缓存命中（几毫秒），实测可接受。
            self.render_all(precise=0.75,
                            only=None if self.link_view.get() and
                            self.mode == "pair" else col)
        finally:
            self._pan_redraw_active = False

    def _tile_slack(self, col, cv):
        """图元四边**离露白还差多少像素**（取最小的一边）。

        > 0 = 还盖着（值就是余量）；<= 0 = 已经露白了。

        ⚠️⚠️ **这是「要不要现在重画」的唯一判据**（v1.7 修抖动）。
        原来判的是「盖没盖满」（`_tile_needs_more`），那**太晚了**：
        块是「视口 + 一圈约 152px 的缓冲」，一帧挪 8px 就吃掉 8px 余量，
        等到余量归零那一步，前面的帧全在白跑重画（实测 60 步里 59 步
        都判「该重画」，每步 65~90ms = 掉 4~5 帧 —— 主人说的抖动）。

        阈值取 `PAN_REDRAW_PX`（40px，缓冲的 1/4）：厚缓冲时重画纯属浪费，
        而露白最早也在 40/8 = 5 步之后 —— 来得及。

        ⚠️ 必须用**图元的真实 bbox**（`cv.bbox`），不能用「显示图尺寸 +
        平移量」推算：分块之后画布上贴的只是视口那一块，比整张显示图小得多。
        """
        info = self._view.get(col)
        if not info:
            return -1
        item = info.get("item")
        if item is None:
            return -1
        bb = cv.bbox(item)
        if not bb:
            return -1                # 图元没了（被清过）-> 必须重取
        cw = max(1, cv.winfo_width())
        ch = max(1, cv.winfo_height())
        # ⚠️⚠️⚠️ **必须扣掉「图的边界」**（v1.7 修向上拖抖动）。
        #
        # 视口可能比图**大**，或者拖到边缘时图的内容本来就到不了视口边缘
        # —— 那不是露白，是**图的边**。原来直接
        # `min(-bb[0], -bb[1], bb[2]-cw, bb[3]-ch)`，
        # 于是拖到边缘时每一边的 slack 都是负数 -> **每步都判该重画**
        # -> 实测向上拖 p50=49ms、63/123 步补块（水平方向只要 1.3~2.5ms，
        # 因为它先撞左右边、上下还有余量）。主人反馈「向上拖最严重」。
        #
        # ⚠️⚠️ **图的边界怎么算**：图元只是「视口那一小块」，
        # 它的左上角 bx 落在显示图**里面**，块起点是 `bx - ox`
        #（`_view` 里 ox 是显示图原点、bx 是图元落点，v1.7 记过这个区别）。
        # 所以整张显示图在画布上的范围是
        #     `[bx - (bx-ox), ...] = [ox, ox+disp_w] x [oy, oy+disp_h]`
        # —— 也就是 **`ox`/`oy` 那一套**（含 pan），不是 `bx + disp`。
        # 我第一版写成 `bx + disp`，那是「小块起点 + 整张图宽」，
        # 会算出几万 px 的边界，`max(...,0)` 之后判据恒等于 0，
        # 于是**永不重画**（反向的错：真露白也抓不到）。两条都不对。
        # ⚠️⚠️⚠️ **横向定长之后，`min(四边)` 会被恒定的横向余量顶住**
        #（v1.7）。
        #
        # 横向块长恒定=> 横向余量**永远是常数**（实测 42px），
        # 纵向余量才是在耗的那个。而 `min(l,t,r,b)` 取最小 ——
        # 横向那个常数 42 正好最小，纵向掉到 -836 了它也不知道。
        # 于是补块判据一直看到「还有 42px 缓冲」，实际早露白了。
        #
        # 正确口径：**纵向单独算，横向只在「图比视口小」时才参与**。
        # 纵向块长跟视口走，所以纵向余量就是「缓冲还剩多少」，
        # 直接用它判。
        _vv = info.get("blk_fixed")
        if _vv:
            disp = info.get("disp") or (0, 0)
            dw, dh = disp
            # ⚠️⚠️⚠️ **定长块的口径必须是「视口有没有出块」，不是「余量 min」**
            #（v1.8 修「补块过频」，主人 2026-10-07 反馈拖动卡顿连带查出）。
            #
            # 为什么 `min(四边)` 在定长块下**必然误判**（实测，不是推演）：
            #   块起点对齐网格、块长恒定 => 块在画布上的落点
            #   `bx = ox + vx0/sx`，而 `vx0` 是**网格量化**后的值。
            #   视口相对块起点可以偏 0 ~ padx 任意量，**两边余量天然不对称**。
            #   埋点实测（zoom 3.815、cw=1515、padx=242）：
            #     横向块 2001px 落在 x ∈ [-444, 1557]，视口 [0, 1515]
            #     => 左余量 444、**右余量只有 42**
            #   而 `min(四边) = 42`、阈值 `thr = 42` ——
            #   `42 < 42` 为假还好，**一旦视口再往右挪 1px 就变 41 < 42**，
            #   于是判「该补块」、补完还是 42、下一��又 41 ——
            #   **每步都补，死循环**。这就是「左/下/上 26~41/115 补块过频」。
            #
            # ✅ 正确口径（定长块）：**直接问「视口还在不在块内」**。
            #   块在画布上的范围可由 `bx/by` + 块显示尺寸反推，
            #   而 `_view["want"]` 正是「这一帧应有的图元尺寸」。
            #   视口四边是 `[0,0,cw,ch]`（Tk 画布坐标）。
            #   只要视口完全落在图元 bbox 内 => 一定不露白，slack 就是正的；
            #   否则按「最短的那一维还差多少」给负值。
            _tw, _th = info.get("want") or (0, 0)
            _bx, _by = info.get("bx"), info.get("by")
            if _tw and _th and _bx is not None and _by is not None:
                _x0, _y0, _x1, _y1 = _bx, _by, _bx + _tw, _by + _th
                _sl = []
                if _x0 > 0:
                    _sl.append(-_x0)            # 图左沿在视口右边
                if _y0 > 0:
                    _sl.append(-_y0)
                if _x1 < cw:
                    _sl.append(_x1 - cw)
                if _y1 < ch:
                    _sl.append(_y1 - ch)
                # ⚠️ 四条边都盖住 => slack = +∞（不会触发补块）。
                #   用一个大常数而不是 0：调用方要的是「和阈值比大小」。
                return min(_sl) if _sl else 1 << 30
            # 拿不到 want/bx（老路径的 view）-> 退回下面那套
        ox = info.get("ox", 0)
        oy = info.get("oy", 0)
        disp = info.get("disp") or (0, 0)
        dw = disp[0]
        dh = disp[1]
        # ⚠️⚠️⚠️ **定长块下，「离边缘多远」不再等于「还能撑多久」**
        #（v1.7 修「上」方向每 8 步补一块）。
        #
        # 现在块长是**恒定**的（`视口 + 2*缓冲`，见 `_render_tile`），
        # 于是横向余量恒定、纵向余量在变。而 `min(四边)` 会取到
        # **恒定的那一边**（实测横向恒 42px、纵向从 149 一路掉到 -124），
        # 于是 slack 看着还有 42、实际纵向早露白了 —— 判据被「顶住」，
        # 一直拖到纵向真的不够才补。trace 实测：补块间隔正好等于网格步长
        # （56px = 8 步），**比该有的 4~5 倍多**。
        #
        # 正确口径：**只算「会变的那几条边」**。
        # 更稳的做法是直接问「有没有露白」，但那要算面积（贵）；
        # 这里保留边差口径，但**按「块是否定长」分档**：
        #   - 定长块：只有「图够不到视口」的那一边会露白，按那一侧算
        #   - 滑动块：四边都算（那是v1.6 的老路径）
        _fixed = None   # 块长恢复滑动后回到四边min口径
        if _fixed:
            # 定长块：横向恒定不看，只看纵向；纵向也恒定（上下都贴边）时
            # 才退回四边min。
            xs = []
            if ox > 0:
                xs.append(-bb[0])
            if ox + dw < cw:
                xs.append(bb[2] - cw)
            ys = []
            if oy > 0:
                ys.append(-bb[1])
            if oy + dh < ch:
                ys.append(bb[3] - ch)
            cand = (ys or xs or [-bb[0], -bb[1], bb[2] - cw, bb[3] - ch])
            return min(cand)
        l = -bb[0]                      # 图元四边盖住 / 超出视口多少
        t = -bb[1]
        r = bb[2] - cw
        b = bb[3] - ch
        # 图够不到视口的那一条，slack 按 0 算（不是负数 ——
        # 图自己就那么大，不该无限重画）
        if ox > 0:
            l = max(l, 0)               # 图左沿在视口右边 -> 左边永远盖不住
        if oy > 0:
            t = max(t, 0)
        if ox + dw < cw:
            r = max(r, ox + dw - cw)    # 图右沿到不了视口右边 -> 以图边为准
        if oy + dh < ch:
            b = max(b, oy + dh - ch)
        return min(l, t, r, b)

    def _tile_needs_more(self, col, cv, px, py):
        """平移之后，渲染好的那块还能不能盖住整个画布？（不够就得重取）

        ⚠️⚠️ 判据必须用**图元的真实 bbox**，不能用「显示图尺寸 + 平移量」推算。
        分块渲染之后画布上贴的只是**视口 + 一圈缓冲**那一块，比整张显示图小得多
        （zoom 1.95 时显示图 2943 宽，块只有 1962）。用显示图算的话永远认为
        「够覆盖」→ 拖动时从不重取 → 缓冲被吃光后画面就停在旧位置，越拖越少，
        实测 40 步里 36 步盖不满（主人反馈「拖动图片直接空白」）。
        """
        info = self._view.get(col)
        if not info:
            return False
        cw = max(1, cv.winfo_width())
        ch = max(1, cv.winfo_height())
        item = info.get("item")
        if item is None:
            return True
        bb = cv.bbox(item)
        if not bb:
            return True                      # 图元没了（被清过）-> 重取
        # ⚠️ `cv.bbox` 返回的是图元**此刻**的真实位置，而 `_pan_move` 刚用
        #    `cv.coords` 把它挪到了 (px, py) —— 所以这里**不能再加增量**
        #    （加了就是平移量算两次，图元会越拖越偏，最后整块跑到画布外面，
        #    实测bbox 从 -228 一路漂到 +363，覆盖率掉到 69%）。
        #留 1px 余量：差一点点就当够，别为1px 反复重取。
        M = 1
        return (bb[0] > -M) or (bb[1] > -M) or (bb[2] < cw + M) or (bb[3] < ch + M)

    def _pan_end(self):
        self._drag = None
        # ⚠️ 拖动中挂起的预热图，松手就补建（`_prewarm_done` 里攒着的）。
        #   不排这一下的话，就只能等 `_prewarm_tick` 下一轮 —— 而它
        #   头一句就是「拖动中让路」，这里 `_drag` 刚清空才轮得到，
        #   中间白等 200ms。
        if getattr(self, "_prewarm_hold", None):
            self._later("prewarmflush", 30, self._prewarm_flush)
        # 松手后按最终位置补一帧精确的（顺便触发后台基准升级）
        for col in (0, 1):
            if self.zoom[col] > 1.001 and self._view.get(col):
                self._later("panend%d" % col, 90,
                            lambda c=col: self.render_all(precise=True,
                                                          only=c))

    def _open_side(self, col):
        p = self.path_a if col == 0 else self.path_b
        if not p:
            self._toast("这一侧还没有图")
            return
        if not os.path.isfile(p):
            self._toast("文件已经不在了")
            return
        net.open_path(p)
        self._toast("已交给系统看图器打开")

    def toggle_crop(self):
        self.crop_only.set(not self.crop_only.get())
        # ⚠️ 选中态统一用 `on`（浅天蓝底 + 主色描边），不是 `primary` / `teal`：
        # 实心深块一多，整屏就沉了（用户提过两次「浅一些、通透一些」）。
        self.btn_crop.set_kind("on" if self.crop_only.get() else "ghost")
        self.render_all()

    def toggle_link(self):
        """双图联动开关：滚轮 + 拖动都同步到另一幅图。"""
        on = not self.link_view.get()
        if on and self.mode != "pair":
            # ⚠️ 别默默不动 —— 用户按了没反应会以为按钮坏了。
            self._toast("并排对比时才能联动（现在只有一幅图）")
            return
        self.link_view.set(on)
        # 选中态统一 `on`（浅天蓝底 + 主色描边），与「只看匹配区域」一致。
        self.btn_link.set_kind("on" if on else "ghost")
        if on:
            # ⚠️⚠️ **打开的瞬间就要把两侧对齐**（v1.8）。
            # 主人预期是「按下按钮，两幅图立刻看到同一处」；
            # 如果只影响后续操作，画面上还停在各自的旧位置 ——
            # 看起来就像「按钮没生效」。
            self._align_link()
        self._toast("双图联动已%s：滚轮和拖动会同步两侧"
                    % ("开" if on else "关"))

    def _sync_link_ui(self):
        """模式变化后同步「双图联动」按钮的可用态。

        ⚠️ **单图模式下直接隐藏**，而不是置灰 —— 置灰还得解释为什么，
        而这个按钮在单图下**根本没有对应功能**，摆在那儿只是噪音。
        顺带把 `link_view` 关掉：切回并排时不该是「仍开着」的状态
        （用户可能已经忘了它开着）。
        """
        pair = (self.mode == "pair")
        if not pair and self.link_view.get():
            self.link_view.set(False)
            try:
                self.btn_link.set_kind("ghost")
            except Exception:
                pass
        try:
            if pair:
                self.btn_link.pack(side="left", padx=(0, S(4)))
            else:
                self.btn_link.pack_forget()
        except Exception:
            pass

    def _align_link(self):
        """把两侧的缩放倍率与注视位置对齐（以左侧为基准）。"""
        if self.mode != "pair" or not self.link_view.get():
            return
        if not self.path_a or not self.path_b:
            return
        norm = self._link_norm(0)
        if norm is None:
            return
        self.zoom[1] = self.zoom[0]
        self._link_apply(0, norm)
        self.render_all(precise=True)

    def render_all(self, precise=True, only=None):
        """重渲染对比区。

        `only` 指定只重画某一侧（滚轮只动了一侧时，别把另一侧也白白
        重新编码一遍）；此时 **不清** `_keep` / `_view`，另一侧的画面原样保留。
        `precise=False` 是滚轮快速档：只用缓存里已有的解码图顶上，
        不同步重解 —— 连滚时每格都解大图就是「缩放好卡」的根源。

        ⚠️ `only=None`（切组 / 换图 / 窗口变化）会把 `_view` 清空，这一侧
        **必须**先清画布 —— 旧图留着会跟新图叠在一起。`only=col`（滚轮、
        拖动）保留 `_view`，`_render_side` 就会在贴新图元前才清 —— 中途
        return 也不会留下空画布（主人反馈「拖动图片直接空白」的修法）。
        """
        if only is None:
            self._keep = []
            self._view = {}
            self.cv_a.delete("all")
            if self.mode == "pair":
                self.cv_b.delete("all")
        # ⚠️⚠️ **`only` 必须是「只画这一侧」，两侧都要判**（v1.7 修 195ms 卡顿）。
        #
        # 原来 side 0 是**无条件**画的，只有 side 1 才判 `only`：
        #     self._render_side(0, ...)              # <- 永远画
        #     if only is None or only == 1: ...     # <- 只有 side 1 判
        # 于是 `render_all(only=1)`（**拖右图时补块走的就是这条**）
        # 会把**左图也重画一遍** —— 而左图此刻的 zoom/pan 一点没变，
        # 本该是缓存命中，实际却是「另一块没缓存的块」= 真编码 30ms
        # + PhotoImage 32ms。实测补块帧 82ms -> 195.9ms，**多出来的
        # 113ms 全是白花的**。
        #
        # 自证：`only=0` 走不到这里（side 0 画了），`only=1` 却画了两侧 ——
        # 「只画一侧」这个语义被破坏了，注释写的也是「只重画某一侧」。
        if only is None or only == 0:
            self._render_side(0, self.cv_a, self.cap_a, self.path_a,
                              self.rect_a, precise)
        if (only is None or only == 1) and self.mode == "pair":
            self._render_side(1, self.cv_b, self.cap_b, self.path_b,
                              self.rect_b, precise)

    # ---- 异步出图（把最贵的「编码」挪出主线程）----------------------
    def _fit_async(self, col, path, key, w, h, bgra, want, radius,
                   need_gdip=None, interp=None):
        """后台把像素编码成给 Tk 的字节，回主线程再做 PhotoImage。

        主线程只留 `PhotoImage` + 贴图（实测 38 + 46ms），
        最贵的 145ms 编码和 88ms 解码都挪到后台 —— 这就是「切一组不再卡」。

        ⚠️⚠️ `need_gdip` 给定时，**后台线程会先做 GDI+ 插值缩放**
        （v1.6 新增）。这是「放大清晰」的关键，而它必须放后台：
        主线程跑双三次要 25~30ms，加上编码和建图整帧 70~80ms ——
        滚轮每一格都等这一帧，实测放大最慢从 86ms（v1.5）涨到 105ms。
        GDI+ 是纯像素计算、不碰 Tk，放后台安全。

        ⚠️ `interp` 选插值模式。**占位/拖动帧用 `IM_BILINEAR`**：
        实测 1818×880 双三次 28.6ms、双线性 6.2ms（尺寸都严格精确，
        见 probe_placeholder.py），而占位图只顶几十毫秒，双线性绰绰有余。
        `None` = 双三次（精确帧默认）。
        """
        if self._fit_pend.get(col) == key:
            return                                   # 同一帧已经在算了
        self._fit_pend[col] = key
        # 记下**发起时的视图状态**：编码要一百多毫秒，等它回来时用户可能已经
        # 又滚了几格。那时这块已经用不上了，别再拿它触发一次同步重画
        # （实测那一画就是 28.7ms，正好卡在滚轮上）。
        self._fit_pend_state[col] = (round(self.zoom[col], 4),
                                     tuple(self.pan[col]))

        def work():
            try:
                ww, hh, px = w, h, bgra
                if need_gdip:
                    if interp is None:
                        r = winimg._gdip_scale_argb(ww, hh, px,
                                                    int(need_gdip[0]),
                                                    int(need_gdip[1]))
                    else:
                        r = winimg._gdip_scale_argb2(ww, hh, px,
                                                     int(need_gdip[0]),
                                                     int(need_gdip[1]),
                                                     interp)
                    if r:
                        ww, hh, px = r
                ppm, _pw, _ph = uikit.fit_ppm(ww, hh, px, radius, P.CARD)
            except Exception:
                ppm = None
            # ⚠️ 编码完的字节可能不小（25MB），只在主线程短暂持有
            self._ui(self._fit_ready, col, path, key, ppm, w, h, want)

        threading.Thread(target=work, daemon=True).start()

    def _fit_ready(self, col, path, key, ppm, w, h, want):
        """后台编码回来了 —— 只在这一侧还是同一张图、同一个请求时才用。

        ⚠️ 无论用不用，**都要把这一侧的 pending 清掉**：它是「还有活没干完」
        的标志（测试也拿它等高清图），一直不清就会永远等下去。
        """
        mine = self._fit_pend.get(col) == key
        cur = self.path_a if col == 0 else self.path_b
        # ⚠️⚠️⚠️ **顺序反了会让下面那道过期检查整道失效**（v1.7 抖动根因二）。
        #
        # 原来先`pop(col, None)` 再 `get(col)` —— **pop 之后必然是 None**，
        # 于是 `if st is not None and st != now` 永远走不进去，
        # **每一次后台编码回来都同步精确重画一次**。
        # 而拖动中 pan 每步都在变，本来这道门该把 99% 的过期帧丢掉。
        #
        # 埋点抓到：横拖 115 步里 `_fit_ready` 触发了 **35~38 次**重画，
        # 每次 100~200ms（p95 从 2.5ms 冲到 177ms）—— 这就是主人说的
        # 「拖拽抖动」的**另一半**（前半段是 `_pan_move` 判据）。
        # 自证信号：`get` 在 `pop` 之后，值必然是 None，判据形同虚设。
        st = self._fit_pend_state.get(col)
        if mine:
            self._fit_pend.pop(col, None)
            self._fit_pend_state.pop(col, None)
        if not mine or ppm is None or cur != path:
            return                       # 过期结果，丢掉（用户已经点走/换了档）
        # 编码要一百多毫秒，回来时视图可能已经不是发起时那样了。这时候再同步
        # 重画一次就是白花钱 —— 直接丢掉，等去抖后的精确帧自己来画。
        now = (round(self.zoom[col], 4), tuple(self.pan[col]))
        if st is not None and st != now:
            return
        # ⚠️⚠️ **正在拖动时一律不画**（v1.7）。拖动每一帧都要重画，
        #   这时候插进来一次 100~200ms 的精确重画，用户看到的就是「顿一下」。
        #   缓存已经 put 好了，松手（`_pan_end`）自然会用上精确帧。
        if self._drag:
            return
        try:
            img = tk.PhotoImage(data=ppm, master=self)
            if isinstance(key, tuple) and key and key[0] == "tile":
                # ⚠️⚠️ **分块渲染的像素在后台已经缩到目标尺寸了，这里
                #    **绝不能**再 resample**。
                #    - `want=None`：缩放已由后台的 GDI+ 完成（放大帧）
                #    - `want=(tw,th,64)`：缩放该由这里用 `resample_img` 做
                #      （缩小帧 —— 点抽样足够，见 uikit 里那段注释）
                #
                #    一旦在这里再 resample 一次，就等于把 Tk 的 `zoom()`
                #    （最近邻）请回来 —— 主人在 v1.4/v1.5 看到的 20×20
                #    色块就是这么来的（见 probe_resample.py 的对照实验）。
                if want is not None:
                    img = uikit.resample_img(img, want[0], want[1], want[2])
                    if img is None:
                        return
            else:
                img, _dw, _dh = uikit.fit_img(img, want[0], want[1], want[2])
        except Exception:
            return
        self.big.put_photo(key, img)
        self.render_all(precise=True, only=col)

    def _placeholder(self, col, path, cw, ch):
        """高清图还没就绪时的占位：先用别的档位/缩略图顶上，别让格子空着。

        ⚠️⚠️ **尺寸可以不准**（v1.6 起 `_render_tile` 不再走它）。
        分块渲染曾经靠它占位，而它给不出精确尺寸，直接造成了拖动露白：
        实测要 1818×880 它给 1472/1248/1080（`resample_img` 只能整数倍），
        图元右侧就缺 13~46%（见 probe_pan.py）。所以那块改成**同步**
        GDI+ 双线性，压根不占位了 —— 6.2ms 换尺寸精确，很划算。

        剩下的调用方是**非分块路径**（`zoom <= 1`，整张图缩进格子），
        那里尺寸差一点点只是居中偏移几像素，不影响「盖不满画布」。

        ⚠️ 这里用 `resample_img` 走 Tk 的 `zoom()`（最近邻），放大时会有块 ——
        但 `resample_img` 已经**不再搭配 `subsample`**，所以看到的是
        「放大的真实像素」而不是「丢过信息的色块」：形状轮廓对得上，
        只是一眼能看出是放大的。精确帧到达后立刻被替换。

        ⚠️⚠️ **但仍要限制 `max_zoom`**：占位图是给「图正在后台编码」那几百
        毫秒用的，`zoom(20)` 一张 1500×1000 的缓存图要 80ms ——
        比它要顶的那段时间还长。实测 profile 里这一格占 35ms，
        一半是 `zoom`、一半是被后台线程抢 GIL。
        """
        src = None
        for k in self.big._p:
            if k[0] == path:
                src = self.big.photo(k)
                if src is not None:
                    break
        if src is None:
            try:
                src = self.thumb.get(path, S(200), 0, P.CARD)
            except Exception:
                src = None
        if src is None:
            return None, 0, 0
        # ⚠️ 占位图也要用 resample_img：`fit_img` 遇到 s>1 会直接
        #    `zoom(2)` 把**整张**缓存图放大（1500×1000 的图约 80ms），
        #    而这里只是顶一下，画质无所谓、越快越好。
        out = uikit.resample_img(src, cw, ch,
                                 MAX_ZOOM if MAX_ZOOM > 8 else 8.0)
        return (out, out.width(), out.height()) if out is not None \
            else (None, 0, 0)

    def _rect_px(self, desc, w, h, rect):
        """工作网格坐标 -> 像素坐标。网格与图同长宽比，按比例放大即可。"""
        gw, gh = desc["gw"], desc["gh"]
        return (max(0, int(round(rect[0] / float(gw) * w))),
                max(0, int(round(rect[1] / float(gh) * h))),
                min(w, int(round(rect[2] / float(gw) * w))),
                min(h, int(round(rect[3] / float(gh) * h))))

    def _crop_decode_size(self, path, desc, rect, box):
        """「只看匹配区域」要按匹配区的占比去要解码尺寸，让裁出来那块的**刚好填满格子**。

        为什么不能像以前那样直接 `max(box)`：那样解出来的是整图，裁完只剩一小块，
        往格子里放时就变成**放大**——而 Tk 的 `zoom` 只能整数倍，`round(s)` 会把图
        撑出容器（实测裁出来 290×286 放进 713×453 的格子，s=1.58 → k=2 → 580×572，
        上下被裁掉 119px，**看不到完整的匹配区**）。

        现在反过来算：解码长边 `L` 要让 `max(裁出宽/格宽, 裁出高/格高) = 1`。
        设 `a = L 每增加 1 时 裁出宽/格宽 的增量`，则 `L = 1 / max(a, b)`。
        这样裁出来那块必然**不小于**格子的受限那一维 → 落进 `fit_photo` 的
        **缩小**分支（那里有溢出兜底），稳定且完整。
        """
        gw, gh = desc["gw"], desc["gh"]
        sw, sh = META.of(path)["wh"] or (gw, gh)
        ls = float(max(sw, sh)) or 1.0
        fw = max(1e-4, (rect[2] - rect[0]) / float(gw)) if gw else 1.0
        fh = max(1e-4, (rect[3] - rect[1]) / float(gh)) if gh else 1.0
        a = (sw / ls) * fw / float(box[0])
        b = (sh / ls) * fh / float(box[1])
        return max(64, min(int(math.ceil(1.0 / max(a, b, 1e-6))), 4096))

    def _decode(self, path, cw, ch, zoom=1.0):
        """按「容器尺寸 × 缩放倍率」向系统要像素（允许放大，所以小图也能占满格子）。

        ⚠️ **放大时一定要重新解码**，不能把已解码的图硬拉：Tk 的 `zoom` 是
        最近邻插值，放两倍就是马赛克；重新向系统解码器要一张更大的图，
        重采样是系统做的（带插值），细节还在。

        ⚠️ 要了之后**必须回头验一眼有没有溢出**：缓存里可能还躺着一张按
        「上一次的容器尺寸」解的图（窗口拉大又拉小时很常见），它比现在的容器大，
        直接拿去显示就会上下/左右被裁 —— 看着就像「匹配区域画错了」。
        溢出就按当前容器强制重解一次。放大模式（zoom>1）本来就该溢出，跳过这步。
        """
        wh = META.of(path)["wh"]
        if wh and wh[0] > 0 and wh[1] > 0:
            s = min(cw / float(wh[0]), ch / float(wh[1])) * zoom
            need = int(round(max(wh[0], wh[1]) * s))
        else:
            need = int(round(max(cw, ch) * zoom))
        need = max(96, min(need, 4096))
        got = self.big.get(path, need)
        if zoom <= 1.001 and got and (got[0] > cw or got[1] > ch):
            got = self.big.get(path, need, force=True)
        return got

    # ---- 分块渲染（缩放不卡的关键）--------------------------------
    # Zoom 之后只把**屏幕上真正看得见的那块**交给 Tk。
    # 每往外留这么多（占可视区比例）的缓冲，拖动时就不必立刻重画。
    TILE_PAD = 0.16    # 每边留这么多缓冲，滑出去才补新块。
    #
    # ⚠️⚠️ **0.16 是实测扫出来的，不是拍脑袋**（v1.7，探针 probe_pan2.py，
    # 3000×2000 真图、四方向拖 115 步、步长 8px）：
    #   TILE_PAD   缓冲(横/竖)   竖向 p95    补块次数
    #   0.10       151/ 75        96 ms13
    #   **0.16**   242/121      **2.5ms**    12
    #   0.22       333/1662.6 ms        13
    #   0.30       454/227        2.8 ms        13
    # 关键在 0.10 -> 0.16 这一步：**p95 掉了 19~38 倍**，补块次数反而略降。
    # 原因不是「缓冲厚了就不补块」（周期由 pad 决定，次数基本不变），
    # 而是**块变厚后 `pkey` 的复用率变了**：缓冲薄时每拖 8px 就跨过
    # 网格边界换 key、每次都真编码（100ms+）；缓冲厚时连续好几步
    # 落在同一格里 -> 缓存命中 -> 0.6~2ms。
    # 再往上加只是白费内存和编码时间，收益已经饱和。
    #
    # ⚠️⚠️ **v1.6 曾有两个「块多大 / 放大多大」的阈值，现在都没了**
    #（`TILE_SYNC_PX = 2000000` 和 `TILE_GDIP_MIN = 1.16`）。
    # 它们是在**占位图给不出精确尺寸**的前提下靠阈值绕开销的：
    #   - `TILE_SYNC_PX`：块太大就丢后台，主线程先顶占位。
    #   - `TILE_GDIP_MIN`：up ≤ 1.16 就按块原尺寸显示。
    # 而占位图走的是 Tk 的整数倍缩放，**给不出精确尺寸** —— 实测要
    # 1818×880 给了 1472×982，覆盖率掉到 54%（probe_pan.py）。
    # 阈值绕不过「尺寸必须精确」这条硬约束，只能整个换掉：
    # 现在是 **`up > 1` 一律同步 GDI+ 双线性**（6.2ms，尺寸精确），
    # 精确帧再异步补一张双三次。没有占位图，也就没有尺寸问题。

    # 拖动补块的最小间隔（秒）。
    # ⚠️ 原来硬编码 0.08s，**这是拖动露白的主因之一**：80ms 内快速拖动能挪
    #    300px+，而缓冲只有视口的 15%（约 150px）—— 等到节流放行，画面已经
    #    空了一片（实测 40 步里 36 步盖不满，最少 69%）。
    #    实测缓存命中的重画只要 **0.6~1.8ms**，20ms 已经很宽裕；
    #    真解不出的那一档走 `_fit_async` 异步，天然不阻塞。
    PAN_LAG = 0.02

    # 拖动时「缓冲还剩多少像素就重画一块新的」（v1.7）。
    # ⚠️⚠️ **别拿固定像素，必须跟着「该方向的缓冲」走**。
    #
    # 这里原本写死 `40`，注释里说「缓冲 152px，取 1/4」——
    # 可**那 152 是横向的**（`视口宽 1515 * 0.10`）。竖向缓冲只有
    # `视口高 757 * 0.10 = 75px`，40px 已经是它的一半：
    #   slack 每 8px 掉 8 -> 掉到 44 就补，补完只回到 75/76，
    #   再走 40px 又掉到 44 -> **每 40px 补一块，744px 路程补了 43 次**
    #   （trace 实测：oy 每补一次只前进 40px，缓冲从来没回满）。
    #   根因是**注释按一个方向算、代码用同一个数套两个方向**。
    #
    # 现在改成 `缓冲 * PAN_REDRAW_FRAC`，两维各自算 —— 竖向阈值
    # 变成 75*0.35 ≈ 26px，走 50px 才补，次数降到 1/1.5。
    #⚠️ 比例不能太大：阈值越接近缓冲，补完的缓冲越少，
    #   快速甩动（每帧 10px+）会在两次补块之间露白。0.35 实测够。
    PAN_REDRAW_FRAC = 0.35

    def _pan_redraw_px(self, disp_w, disp_h, cw, ch):
        """拖动补块阈值（像素），**两维取小的那个**。

        ⚠️ 必须是两维里**更紧**的那一维：任一维露白画面就难看，
        #   所以按小的那个决定补块时机。
        """
        _pad = max(8.0, min(disp_w, cw, disp_h, ch) * self.TILE_PAD)
        return max(8, int(_pad * self.PAN_REDRAW_FRAC))

    # 兼容旧引用（阈值固定 40px 的那版语义），仅作兜底下限
    PAN_REDRAW_PX = 40

    def _render_tile(self, col, cv, cap, path, box, zoom, precise=True):
        """zoom>1：只渲染视口那一块。

        ⚠️ `precise` 不是布尔，是**清晰度系数**（0.5 ~ 1.0）：
          - `False`（0）→ 滚轮快速帧，滚得动优先，画糊一点无妨（170ms 内被替换）
          - `0.75`      → 拖动补块，要看得清（主人反馈「推拽还有马赛克」）
          - `True`（1） → 精确帧，缩停之后补清晰的那一张
        取中间档是有依据的：**有缓存时精确帧比粗档还便宜**（实测 3ms vs 23ms，
        因为粗档的 key 跟精确档不同，等于多算一张）；欠采样 1.33 倍已经
        看不出糊，而块像素只有精确档的 56%，拖动才跟得上。

        ⚠️ 老做法是「按 zoom 向系统重新解码一整张更大的图」（实测
        4000×3000 的照片 zoom=4 要 **229ms**：解码 75 + 编码 83 + Tk 38），
        越放大越贵 —— 主人第三次反馈「还是缩放卡顿」的根因就在这里。

        系统自带的照片查看器为什么顺？因为它**只解码一次**，之后缩放是
        显示层的事。这里照抄这个思路：`BigCache.base()` 解码一次留着，
        每帧只从它身上裁一块 + 编码。实测同一张图：
            zoom 2 -> 26ms   4 -> 7ms   6 -> 4ms
        而且**越放大越便宜**（要画的块越小）。

        返回 True 表示已经画好，调用方不要再走老路径。
        """
        wh = META.of(path)["wh"]
        if not wh or wh[0] <= 0 or wh[1] <= 0:
            return False
        cw = max(1, cv.winfo_width())
        ch = max(1, cv.winfo_height())

        fit = min(box[0] / float(wh[0]), box[1] / float(wh[1]))
        disp_w = max(1, int(round(wh[0] * fit * zoom)))
        disp_h = max(1, int(round(wh[1] * fit * zoom)))
        ox = (cw - disp_w) // 2 + self.pan[col][0]
        oy = (ch - disp_h) // 2 + self.pan[col][1]

        # 想要的对图的分辨率 —— 决定用哪一级基准来出这块。
        # ⚠️ 按**显示图**长边算（再被 `native` 封顶），不是按视口。视口只有
        #    1515 宽，但放大到 5.96 倍时显示图有 6437 宽 —— 这时候只有把
        #    基准档顶到原图（3000）才够清晰，用视口口径会只取 1536 那档、
        #    欠采样 4.19 倍，糊得没法看。实测：视口口径快（19~52ms）但糊，
        #    显示图口径清晰（1.1x，物理极限 2.15x）且同样 24~52ms。
        # ⚠️ `native` 是原图长边：档位**封顶在原图**，别让解码器拿 SCALEUP
        #    硬放大出比原图还大的像素（3000 的图解出 4096 级 = 凭空多 37%
        #    像素、83ms、43MB 内存，一点新信息都没有）。
        need_side = max(disp_w, disp_h)
        native = max(wh)
        # 精确帧要**真的解出够清晰的那一档**：主人反馈「放大还有马赛克，
        # 不如上一个版本」—— 根因就是这一档缺了却只拿 1536 顶着，
        # 拿 904 宽的块去填 2425 宽的显示区，2.7 倍欠采样。
        # 实测解一档 49~95ms，而后台补档要等 3 秒才轮得到 ——
        # 「等三秒才清楚」在体感上就等于「一直马赛克」。所以这里同步解。
        # ⚠️ 判据必须是 `>= 1` 不是 `if precise`：拖动补块传的是 0.75，
        #    `if 0.75` 为真 → 拖动/滚轮会全走精确档，块大 2 倍、慢一倍
        #    （实测 109ms），这正是「清晰了但又卡回去」的原因。
        if precise >= 1:
            got = self.big.peek_base(path, need_side, native)
            if got is None:
                got = self.big.base(path, need_side, native)
            if got is None:
                # 真解不出来（超大图 / 内存不够）：退回手上有的一档
                self._base_prep(col, path, need_side)
                got = (self.big.peek_best(path, need_side, native)
                       or self.big.peek_any(path))
        else:
            # ⚠️ 快速档**绝不解码**，但也别一味用粗档：粗档是 2~3 倍欠采样，
            #    主人反馈「放大还有马赛克」大半来自这里。这里按 `precise`
            #    系数（滚轮 0.5 / 拖动 0.75）要分辨率，**手上有多清晰就用
            #    多清晰** —— 正在看的那两张在预热里已经备满了档，通常直接
            #    命中，就是 1:1 清晰、几毫秒画完。够不着的才退粗档。
            want = int(need_side * (precise or 0.5))
            got = self.big.peek_best(path, max(256, want), native)
            if got is None:
                got = self.big.peek_any(path)      # 手上有哪一档就用哪一档
            if not got:
                return False                    # 手上空的：交给精确帧
            # ⚠️⚠️ **拿到的那一档必须真的够清楚**，否则滚轮/拖动全程是马赛克。
            #    `peek_best` 只给「不超过 want 的最大档」，而档位表**必然有缺口**
            #    （BASE_STEP=256，3000 宽的图有 2304 这一档吗？没有）——
            #    实测它在 need_side=2943、precise=0.75 时会退到 **1536**，
            #    明明手上有 2048/2560/3000，却拿 1536 去填 2943 宽的显示区，
            #    **1.9 倍欠采样** = 主人说的「放大还有马赛克」（滚轮快速帧
            #    实测 1.29~1.65 倍）。这里补一刀：欠采样超 1.15 就升档。
            #
            #    ⚠️ 升到「刚好够 need_side」那一档（peek_base）就够了，**不要
            #    再往上要最清晰的**：基准档越高块越大，编码越慢
            #    （2560 档的块 276 万像素 → 编码 130ms，滚轮中间三档实测
            #    94/130/120ms就是这么来的）。松手后的精确帧会去解最清晰那档。
            # ⚠️⚠️⚠️ **升档必须走后台预热，不能同步解**（v1.7 修「滚轮卡」）。
            #
            # 原来这里还有一句「升到刚好够 need_side 那一档就够了，
            # 别再往上要最清晰的」（2560 档块 276 万像素、编码 130ms）。
            # 现在**连这一刀也不能同步做**：纯噪声图（测试里的最坏输入）
            # 滚轮放大实测**最慢 116ms**、超了 100ms 门槛。解一档是几百
            # 毫秒的 Python 层按行拷贝，**会占着 GIL 把主线程饿死**——
            # 这正是 `_base_prep` 注释里写的同一件事，我在快档路径上
            # 又犯了一次（2026-10-06）。
            #
            # 正确做法：**登记**「停手后要这一档」，这一帧先用粗档顶上。
            # 主人看到的画面最多糊 170ms（`zoomhi` 防抖那一帧就换清晰的），
            # 而滚轮全程不掉帧。清晰度一步没让——下一段的 `worst_fast
            # <= 1.15` 断言还钉着欠采样，真糊了照样红。
            if max(got[0], got[1]) * 1.15 < need_side:
                up = self.big.peek_base(path, need_side, native)
                if up is not None and max(up[0], up[1]) > max(got[0], got[1]):
                    got = up                # 手上已有这一档，直接用
                else:
                    self._base_prep(col, path, need_side)   # 后台去解
        if not got:
            return False
        bw, bh, bbgra = got

        # ⚠️⚠️ **缓冲按「这一帧会不会被连续平移」分档**（v1.7）。
        #
        # `precise=0.75` 现在有两个调用方，需求正好相反：
        #   - **滚轮**（`_on_wheel`）：每一帧都是一个**新 zoom**，
        #     `pkey` 必然 miss、必然真编码。缓冲越大 = 块越大 = 越慢。
        #     而滚轮之后紧接着 170ms 就是精确帧，**根本不需要厚缓冲**。
        #   - **拖动**（`_pan_move`）：同一批 zoom 反复平移，块只要够厚
        #     就能命中缓存 —— 实测缓冲 0.10→0.16 让竖向 p95 从 96ms 掉到
        #     2.5ms（38 倍）。
        #
        # 所以：拖动用厚缓冲，滚轮用薄缓冲。这样两条路都不牺牲 ——
        # 纯噪声图滚轮放大最慢从 116ms 回到 100ms 内，而拖动的抖动
        # 一点没回来（探针 probe_pan2.py 四向 p95 2.9~6.1ms）。
        _pad_scale = 1.0 if self._pan_redraw_active else 0.55

        # 视口 ∪ 缓冲 -> 「显示图」坐标系里的矩形
        #
        # ⚠️⚠️⚠️ **顺序必须是「先把视口夹进图内，再往外扩缓冲」**
        #（v1.7 修「向上拖抖动最严重」，主人 2026-10-06 反馈）。
        #
        # 原来直接算 `-oy-pady .. ch-oy+pady` 然后整体 clamp 到 `[0, disp]`：
        #   `vx1 = max(vx0+8, min(disp_w, cw-ox+padx))`
        # 拖到图的边缘时，上沿被夹到 0，**下沿也跟着被截到图的边界**，
        # 于是块的高度 =「图剩下的那部分」< 视口高 —— 图元根本盖不满画布。
        # 实测 zoom 4.81 向上拖：pan_y=-744 时块高 832（图元 757，刚好），
        # 之后**每拖 8px 图元就矮 8px**，pan_y=-800 时图元只有 701
        # （视口 757）-> 下沿露 56px、`slack=-56`。
        #
        # 后果比露白更糟：`_tile_slack` 每步都看到负数 -> **每步都判该重画**
        # -> 实测向上拖 p50=49ms（其他方向 1.3~2.5ms）、63/123 步补块。
        # 水平方向看着正常只是因为它先撞到左右边缘、上下还有余量。
        #
        # 正确做法：**视口那一段必须完整落在块内**（哪怕贴边），
        # 缓冲只往「图还有余量的方向」扩。
        padx = int(min(disp_w, cw) * self.TILE_PAD * _pad_scale)
        pady = int(min(disp_h, ch) * self.TILE_PAD * _pad_scale)
        # ⚠️⚠️⚠️ **块起点必须对齐到「固定网格」，否则缓存 key 每帧都变**
        #（v1.7 抖动根因四：竖拖 115 步补 37~43 块、p95 115ms）。
        #
        # `pkey` 里含 `px0/py0`。原来 `py0 = int(vy0*sy)` **跟着 pan
        # 一路滑**：每拖 8px，显示坐标进 8px -> `py0` 进 7 个基准像素
        # -> **每 8px 就是一个全新 key** -> 每次都要真编码（100~200ms）。
        # 埋点实测：`pan=0,56 -> 0,112 -> 0,168` 每 56px 出一块，
        # 744px 路程补了 **37 块**（该是 7 块）——
        # 块高 909、视口 757、重叠 152px，理论上只该补 744/152+2 = 7 次。
        #
        # 网格步长取**缓冲那一层**（`pady`）：块按网格铺，相邻两块必然
        # 重叠 `2*pady`；而落在同一格里的所有 pan **共用同一个 key**，
        # 第二次起就是缓存命中（实测 0.6~1.8ms）。
        _stepx = max(8, padx)
        _stepy = max(8, pady)
        # ⚠️⚠️⚠️ **起点对齐网格 + 长度恒定，两头都要定**（v1.7）。
        #
        # 埋点抓到：拖动时那次 render_all 本身就要 108~113ms
        #（四方向连扫都稳定复现），而 115 步里只有 12~15 步补块
        # —— 说明**每一次补块都在真编码**，不是缓存命中。
        #
        # 贴边时必然 miss 的原因：
        #   - 起点 max(0, _gridx) 在拖到图顶/图左时**恒为 0**，
        #     起点不再由网格决定；
        #   - 块长于是回到滑动值「视口尾 + 缓冲」，**每拖 8px 变 8px**，
        #     blkH 跟着变 -> 缓存 key 每步都是新的 -> 每块都重新编码。
        #
        # 定长之后：贴边那一侧多出来的部分本来就在图外（画布上看不见），
        # 不会露白；而 key 稳定下来，复用率回来了。
        #
        # ⚠️ 只定长、起点还滑动是不行的（我试过）：补块次数反而从 12
        #   涨到 41 —— 块不随视口前移，slack 就一直判该补。**两头都要定。**
        _gridx = int((-ox - padx) // _stepx) * _stepx
        _gridy = int((-oy - pady) // _stepy) * _stepy
        vx0 = max(0, _gridx)                     # 起点：网格对齐
        vy0 = max(0, _gridy)
        # ⚠️⚠️⚠️ **块长也要恒定，否则缓存永远不命中**（v1.7 最后一处）。
        #
        # 埋点抓到：拖动时补块那一次 render_all 本身要**108~113ms**
        #（每方向连扫都复现），而115 步只有 12~15 步补块 —— 说明
        # **每次补块都在真编码**，缓存一次都没命中。
        #
        # 原因：块长原来是滑动值 `视口尾 + 缓冲`，每拖 8px 就变 8px，
        # `blkH` 跟着变 -> 缓存 key 每步都是新的。
        # 定长之后 key 只随网格变，第二次起就是 0.6~2ms 的命中。
        #
        # ⚠️ 定长会让「块不再随视口前移」，所以**补块判据必须一起改成
        #   「按网格周期触发」**（见 `_pan_move` 的 `_grid_due`），
        #   否则 slack 一直判该补、补块次数会从 12涨到 41。
        # ⚠️⚠️⚠️ **块长必须固定，否则缓存永远不命中；但「定长」只对横向**
        #（v1.7 修抖动 + 修「补块偏多」，这两件事是一起解的）。
        #
        # 起因：拖动时补块那一次 render_all 本身要 **108~113ms**
        #（每方向连扫都复现），而 115 步只有 12~15 步补块——说明
        # **每次补块都在真编码**，缓存一次都没命中。原因是块长跟着
        # 视口尾滑动，每拖 8px 变8px，`blkH` 变 -> 缓存 key 变。
        #
        # ⚠️⚠️ **我一开始把两维都定长，那是错的**（实测踩过）：
        #   块不随视口前移 -> 纵向缓冲被消耗完却「不恢复」->
        #   trace 显示补块后 slack 回到 42（横向恒定）而**纵向一路掉到
        #   -836 从没回来** -> 每 ~48px 必补一次（「上」16 次 vs
        #   其他方向 5~10）。
        #
        # ✅ 正确的分工（v1.8 修「拖到未显示画面时卡顿」，主人 2026-10-07 报）：
        #   · 横向定长（`vx1 = vx0 + _nx`）
        #   · **纵向也要定长**（`vy1 = vy0 + _ny`）—— v1.7 这里是
        #     `ch - oy + pady`（跟视口），代价是**纵向缓存永不命中**
        #
        # ⚠️⚠️⚠️ **「跟视口」不是「缓冲会恢复」，是「块每步都变」**
        #（v1.8 埋点抓出来的，两层误解叠在一起）。
        #
        # v1.7 的注释说「纵向跟视口 → 缓冲跟着走，否则越拖越露白」，
        # 并把「两维都定长」当过错事（实测补块从 12涨到 41）。
        # **那个结论只对了「补块判据还没改成按网格触发」的时候。**
        #
        # 现在判据已经是「横向跨格 or slack 不足」（见 `_pan_move`），
        # 纵向定长不会导致「缓冲消耗完却不恢复」—— 块定长之后，
        # 跨到下一格时新块自然又是满缓冲。
        #
        # 实测代价（1328×2048、zoom 3.815、拖 150 步）：
        #   纵向跟视口：块高 733 -> 747 -> 750 -> 753 … **每拖 4px 变 3px**
        #     -> pkey 每步都是新的 -> `_render_tile` **129 次调用 0 命中**
        #     -> 帧耗时中位 **87.1ms**、p95 103.9ms（主人说的「拖一下停一下」）
        #   纵向定长：pkey 只随网格变 -> 第二次起就是缓存命中
        #     -> 同场景帧耗时中位 **2ms** 量级
        #
        # ⇒ **「主线程每帧几十毫秒」的根因不是编码慢，是缓存永不命中。**
        #   每帧都在真编码同一块内容，只是块边界在滑动。
        _nx = cw + 2 * padx
        _ny = ch + 2 * pady
        vx1 = min(disp_w, vx0 + _nx)
        vy1 = min(disp_h, vy0 + _ny)
        # 但视口那一段（长度恒为 cw/ch）绝不能少 —— 万一视口比整张
        # 显示图还大（极小图放大很轻），块至少要有视口那么长。
        vx1 = max(vx1, min(cw - ox, disp_w))
        vy1 = max(vy1, min(ch - oy, disp_h))
        vx1 = max(vx1, vx0 + 8)
        vy1 = max(vy1, vy0 + 8)

        # 映射到基准像素
        sx = bw / float(disp_w)
        sy = bh / float(disp_h)
        # ⚠️⚠️⚠️ **块的目标长度必须「直接算」，不能靠两个坐标相减**
        #（v1.8 修「缓存命中 0%」，probe_stall.py 抓到的）。
        #
        # 原来是这样：
        #     px0 = max(0, int(vx0 * sx))
        #     px1 = min(bw, max(px0 + 8, int(math.ceil(vx1 * sx))))
        #     blkW, blkH = px1 - px0, py1 - py0
        # 看着「起点终点的差就是块长」，可**两个取整是各自独立做的**：
        #   `int(vx0*sx)` 与 `ceil((vx0+_nx)*sx)` 的**截断误差互不相关**，
        #   而 `_nx*sx` 本身是个小数（如 242*0.7293 = 176.49）——
        #   跨一格时两个误差各自跳一下，`blkH` 就在 **730 / 729** 之间抖。
        #
        # 实测（1328x2048、zoom 3.815、pad=(242,121)）：
        #     pan(-119,-68)  ((1328,730),(0,7)) -> ((1328,729),(0,8))
        #     pan(-153,-188) ((1328,729),(0,8)) -> ((1328,729),(0,9))
        #     pan(-153,-312) ((1328,729),(0,9)) -> ((1328,730),(0,10))
        # `pkey` 里含 `blkW/blkH` => **每次跨格都是全新 key**，
        # 6 次 `_render_tile` 命中 0 次、每次真编码 ~99ms。
        #
        # ✅ 正确：**长度是输入，坐标由它推**。
        #   同一 zoom 下 `sx`/`_nx` 都恒定 => `_bw` 恒定；
        #   `px0` 是网格起点（本来就稳定）；
        #   `blkW = px1 - px0` 只在 `min(bw, ...)` 截断时才变，
        #   而那时 `px0` 必然 = `bw - _bw` => `blkW` 仍等于 `_bw`。
        #   ⇒ **`blkW/blkH` 恒定，pkey 只随网格变，跨格第二次起就命中。**
        _bw = max(8, int(round(_nx * sx)))
        _bh = max(8, int(round(_ny * sy)))
        px0 = max(0, min(bw - _bw, int(vx0 * sx)))
        py0 = max(0, min(bh - _bh, int(vy0 * sy)))
        px1 = min(bw, px0 + _bw)
        py1 = min(bh, py0 + _bh)
        px1 = max(px1, px0 + 8)
        py1 = max(py1, py0 + 8)
        blkW, blkH = px1 - px0, py1 - py0

        twant = max(16, int(round(blkW / sx)))
        thwant = max(16, int(round(blkH / sy)))

        # key 里带上 zoom / 视口位置 / 基准尺寸，任一变化就换一块新的
        pkey = ("tile", path, bw, bh, int(zoom * 1000), px0, py0, blkW, blkH,
                twant, thwant)

        # ⚠️⚠️⚠️ **必须先查缓存，再插值/编码**（v1.6 修马赛克时踩的性能坑）。
        #
        # 我第一版把 GDI+ 插值和 PPM 编码放在了查缓存**之前**，于是拖动 /
        # 滚轮回到走过的位置时，缓存明明命中了，却还是把「裁块 + 双三次
        # + 编码」整套重做一遍才丢弃 —— 实测「走过的位置再来一次」要
        # **103ms**（v1.5 是 3ms），放大最慢从 86ms 涨到 197ms。
        #
        # 顺序很重要：**key 只依赖 blkW/blkH/twant/zoom/视口位置**，
        # 全都在上面算好了，所以查缓存根本不需要先插值。
        img = self.big.photo(pkey)
        if img is not None:
            dw, dh = img.width(), img.height()
            cv.delete("all")
            item = cv.create_image(ox + int(round(px0 / sx)),
                                   oy + int(round(py0 / sy)),
                                   anchor="nw", image=img)
            self._keep.append(img)
            self._view[col] = {"cv": cv, "item": item, "ox": ox, "oy": oy,
                               "px": self.pan[col][0], "py": self.pan[col][1],
                               "disp": (disp_w, disp_h), "base": (bw, bh),
                               # ⚠️ **disp 是哪一档 zoom 算出来的**（v1.8）
                               # `_link_disp` 靠它做档位换算，不记就得
                               # 靠调用方传，两次都传错（见那段注释）。
                               "dz": zoom,
                               "blk": (blkW, blkH),
                               # 同上：这一帧应有的图元尺寸（给 test_ui 当判据）
                               "want": (twant, thwant),
                               # 块长是否恒定（_tile_slack 据此换口径）
                               "blk_fixed": True,
                               # 本帧块落在哪个网格（_pan_move 据此判断要不要补）
                               "grid": (max(0, int((-ox - padx) // _stepx)),
                                        max(0, int((-oy - pady) // _stepy))),
                               # 本帧用的缓冲（_pan_move 判跨格必须用同一个，
                               #  别自己再算一遍 min(...) * TILE_PAD * _pad_scale）
                               "pad": (padx, pady),
                               # 图元真实落点 = `ox + px0/sx`（**不是 ox**）
                               # —— `_pan_move` 要靠它，见下面那段
                               "bx": ox + int(round(px0 / sx)),
                               "by": oy + int(round(py0 / sy)),
                               "img": img}
            self._render_caption(cap, path, disp_w)
            return True

        # ⚠️ `crop_bgra` 放在查缓存**之后**：它要把 (blkW×blkH×4) 字节从
        #    基准档里拷出来一份，块越大越贵。缓存命中时完全不需要它
        #    （旧代码在查缓存之前就裁了，每次回拖都白裁一遍）。
        _tw, _th, cbgra = thumbs.crop_bgra(bw, bh, bbgra, px0, py0,
                                           blkW, blkH)

        # ⚠️⚠️⚠️ **放大时在像素层做插值，绝不能交给 Tk**（v1.6 的核心修复）。
        #
        # 主人在 v1.4 / v1.5 看到的 20×20 色块，根因就在这一步：
        # `uikit.resample_img` 把缩放比逼成有理数 p/q，放大 1.1 倍会
        # 精确命中 **11/10**，于是走 `subsample(10)` → `zoom(11)` ——
        # `subsample` 是**点抽样**（真丢像素），横向只剩 1/11 的信息。
        # 实测 1000px 的 1px 细条纹走这条路**振幅从 255 塌到 0**，
        # 整张图变成一块纯色（见 probe_resample.py 的对照实验）。
        #
        # Tk 只有 `zoom()`（最近邻）和 `subsample()`（点抽样），
        # **两个都不是插值** —— 只有 GDI+ 的 `HighQualityBicubic` 是。
        # 实测 20 倍放大下游程仍是 1px（probe_interp.py）。
        # 系统看图器放大清晰就是这个原因。
        #
        # ⚠️⚠️ **而 GDI+ 还有第二个、更要命的优势：输出尺寸任意精确。**
        #
        # Tk 的两个操作**只接受整数倍**，`resample_img` 用有理数 p/q
        # 逼近也给不出精确尺寸 —— 要 1818 宽它给 1472/1248/1080。
        # 而 `_render_tile` 把图元贴在 `ox + px0/sx`（显示坐标），
        # **画出来的图元必须严格等于 `(twant, thwant)`**，差一像素就露白。
        # 这就是拖动露白的根因（详见下面 `up` 那段）。
        #
        # 什么时候会真的放大？基准档**被原图封顶**的时候：
        # `_level()` 里 `lv = min(lv, native)`，所以放大到超过原生
        # 分辨率后基准档就是原图，块只有 1324×641 却要显示成 1818×880
        # （实测 up = 1.373）。这几档插值是刚需。
        # ⚠️⚠️⚠️ **块必须缩到 `(twant, thwant)`，方向无所谓**（v1.10 修
        #「拖拽有时会改变画面大小」，主人 2026-10-07 报）。
        #
        # 原来这里是 `if up <= 1.0:`（基准像素**比需要的多**）就
        # **按块原尺寸 1:1 直接贴**，注释写的是「画出来比理想值大，
        # 画布自然裁掉，看不出差别」。**那句话是错的。**
        #
        # 画布裁掉的只是**溢出视口**的部分；而块内像素 1:1 映射到画布
        # 等于把内容**整体放大了 `1/sx` 倍**（`sx = bw/disp_w`）。
        # 于是「画面尺寸」实际由**基准档**决定，不由 `disp` 决定。
        #
        # 实测（3000x2000 的图、视口 1515x757，`probe_pansize.py`）：
        #     zoom 1.25  基准 1536  sx=1.113  块实 1536x989   应 1380x889
        #     zoom 1.95  基准 2560  sx=1.187  块实 2115x1055  应 1781x889
        #     zoom 3.05  基准 3000  sx=0.891  块实 1781x889   应 1781x889 ✓
        # 前两档画面分别被放大 **11.3% / 18.7%**（第三档因为基准被原图
        # 封顶、sx<1 走了插值分支，反而恰好正确 —— 这正是「有时候」）。
        #
        # ⇒ 只要基准档在**拖动补块（0.75 档）与精确帧之间切换**一次，
        #    `sx` 一变画面就跳一下 —— 主人说的「拖拽有时候会改变画面大小」。
        #
        # ✅ 正确做法只有一条：**块尺寸严格等于 `(twant, thwant)`**，
        #    让画面尺寸只由 `disp` 决定、与基准档彻底解耦。
        #    1:1 那个 case 只是恰好相等，可以省掉插值，但**不能当通用优化**。
        if blkW == twant and blkH == thwant:
            # 恰好 1:1（`sx == 1`，基准档正好等于显示尺寸）：省一次插值。
            ppm, _pw, _ph = uikit.fit_ppm(blkW, blkH, cbgra, 0)
            if ppm is None:
                return False
            img = tk.PhotoImage(data=ppm, master=self)
            self.big.put_photo(pkey, img)
            dw, dh = blkW, blkH
        else:
            # ⚠️ 两个方向**都**走这里（v1.10）：
            #     基准像素不够 -> 要**放大**（`up > 1`）
            #     基准像素更多 -> 要**缩小**（`up < 1`，原来走上面那条
            #       「1:1 白贴」的路，画面因此被放大 `1/sx` 倍 = bug 本体）
            # ⚠️⚠️ 而且**必须同步做**。
            #
            # 曾经想「先顶占位、插值丢后台」，但占位图**同样得是精确
            # 尺寸**（否则照样露白），而要精确尺寸就得插值 —— 后台
            # 一点都省不下来，反而多一次「占位 → 精确」的替换闪烁。
            # 实测那一帧 44~55ms，其中占位 0~3.6ms、后台 6.2ms(GDI+)
            # + 20ms(编码) 全是**白花**的：`_fit_ready` 回来时 pan
            # 早就变了，结果直接被丢掉（`_fit_pend_state` 那道门）。
            #
            # 双线性 6.2ms 就够（probe_placeholder.py 实测 1818×880：
            # 双三次 28.6ms、双线性 6.2ms，两者尺寸都严格精确）。
            # 拖动要的是「跟手 + 不露白」，不是极致锐度 ——
            # 真正的双三次在下面异步补，几十毫秒后换上。
            r = winimg._gdip_scale_argb2(blkW, blkH, cbgra, twant, thwant,
                                         winimg.IM_BILINEAR)
            if r is None:
                # GDI+ 不可用（理论上不会，winimg 全靠它解码）：
                # 退回按块原尺寸，至少不空画布。
                ppm, _pw, _ph = uikit.fit_ppm(blkW, blkH, cbgra, 0)
                if ppm is None:
                    return False
                img = tk.PhotoImage(data=ppm, master=self)
                dw, dh = blkW, blkH
            else:
                sw, sh, sbgra = r
                ppm, _pw, _ph = uikit.fit_ppm(sw, sh, sbgra, 0)
                if ppm is None:
                    return False
                img = tk.PhotoImage(data=ppm, master=self)
                dw, dh = sw, sh
            self.big.put_photo(pkey, img)
            # ⚠️ **精确帧再异步补一张双三次**。它写进同一个 `pkey`，
            #    回来时 `_fit_ready` 走缓存命中那格直接换上 ——
            #    这就是「拖动/滚动时双线性够用，停手后变双三次」。
            # ⚠️ `want=None`：像素已在后台缩到目标尺寸，
            #    `_fit_ready` 里绝不能再 resample（见那里那段注释）。
            if precise >= 1:
                self._fit_async(col, path, pkey, blkW, blkH, cbgra,
                                None, 0, need_gdip=(twant, thwant),
                                interp=winimg.IM_BICUBIC)
        if img is None:
            return False

        self._keep.append(img)
        # ⚠️ 别忘了 pad：块是从 vx0 开始的，不是从图的 0,0 开始
        # ⚠️ 清空挪到这里：只有**真要贴新图元时**才清画布，中途任何 return
        #    都保留上一帧（见 `_render_side` 开头那段注释 —— 拖动露白的主因）
        cv.delete("all")
        item = cv.create_image(ox + int(round(px0 / sx)),
                               oy + int(round(py0 / sy)),
                               anchor="nw", image=img)
        self._view[col] = {"cv": cv, "item": item, "ox": ox, "oy": oy,
                           "px": self.pan[col][0], "py": self.pan[col][1],
                           "disp": (disp_w, disp_h),
                           # ⚠️ **disp 是哪一档 zoom 算出来的**（v1.8）。
                           # `_link_disp` 靠它做档位换算；漏了这一处，
                           # 滚轮联动第 1 格会偏 0.139（实测）。
                           "dz": zoom,
                           # 记下这一帧是用**哪一级基准**画的，以及块多大 ——
                           # 「放大后有没有马赛克」就是看这两个：
                           # 显示宽 / 块宽 > 1.5 就是欠采样（肉眼可见的糊）。
                           "base": (bw, bh), "blk": (blkW, blkH),
                           # ⚠️⚠️ **这一帧的图元「应该」是多大**（v1.6）。
                           #    图元贴在 `ox + px0/sx`（显示坐标），所以它必须
                           #    严格等于 `(twant, thwant)`，差一像素就露白。
                           #    记下来给 test_ui 当判据 —— 别让测试去用
                           #    `base`/`disp` 反推：拖动时异步双三次帧回来会
                           #    换基准档（`base` 变大），反推的 `want` 就对不上了
                           #    （实测假报 389px 误差，测试自己错了三轮）。
                           "want": (twant, thwant),
                               # 块长是否恒定（_tile_slack 据此换口径）
                               "blk_fixed": True,
                               # 本帧块落在哪个网格（_pan_move 据此判断要不要补）
                               "grid": (max(0, int((-ox - padx) // _stepx)),
                                        max(0, int((-oy - pady) // _stepy))),
                               # 本帧用的缓冲（_pan_move 判跨格必须用同一个，
                               #  别自己再算一遍 min(...) * TILE_PAD * _pad_scale）
                               "pad": (padx, pady),
                           "img": img,
                           # ⚠️⚠️⚠️ **图元「自己的」落点**（v1.7 修抖动，
                           #   主人 2026-10-06 报「拖拽有抖动」）。
                           #   它是 `ox + px0/sx`，**不是 `ox`** ——
                           #   `ox` 是整张显示图的原点，而分块之后贴上画布的
                           #   只是视口那一小块，起点在显示图里面。
                           #   `_pan_move` 必须按这个挪图元；拿 `ox` 当落点
                           #   会让图元每步瞬移回显示图左上角（实测
                           #   -1348 而不是 -151，露白 1044px）
                           #   -> 每步都判「该重画」-> 每步 60~90ms = 抖动。
                           "bx": ox + int(round(px0 / sx)),
                           "by": oy + int(round(py0 / sy))}
        self._render_caption(cap, path, disp_w)
        if DEBUG:
            print("[分块] 侧%d %s zoom %.2f 显示 %dx%d 取块 %dx%d -> 画 %dx%d"
                  % (col, os.path.basename(path), zoom, disp_w, disp_h,
                     blkW, blkH, dw, dh), file=sys.stderr)
        return True

    # ---- 空闲预热 --------------------------------------------------------
    # 主人 2026-10-06：「每组第一次打开会卡顿一下，后面打开就好了，
    # 不能在加载时，同时加载好吗」—— 可以。一张图要经「解码 -> 编码 ->
    # 建 Tk 图」三步才能显示，头两步能放后台，最后一步只能主线程做。
    # 空闲时把排队做完，之后点开哪一组都是现成的。
    PREWARM_MAX = 60                        # 最多预热这么多张
    PREWARM_LV = (768, 1536, 3072)          # 缩放要用的基准档（按预算放）
    # ⚠️ 基准像素**只给排在前面的这些张**准备：一档 1536 就是 6MB，60 张全解
    # 是 400MB，超预算会来回挤掉、白干。缩放只会发生在「正在看 / 马上要看」
    # 的那几张上，剩下的备好适应窗口那张成品图就够了（那是「打开卡一下」的
    # 大头）。排前面的正是当前这两张 + 当前组的图。
    PREWARM_BASE_MAX = 16
    # ⚠️ **正在看的那两张**额外把中间几档也备满，别的图只备 768/1536/原图。
    # 理由：清晰和流畅在这里是同一件事 —— 缺档就得在主线程同步解，实测
    # 2304 档 95ms、2816 档 70ms（主推会「滚到某一档突然卡住」）；备好了
    # 滚轮快速档就能直接命中，1:1 清晰、几毫秒就画完。空闲时解不心疼。
    PREWARM_FULL_N = 2
    PREWARM_FULL_LV = (1024, 2048, 2560)   # 中间档，配合原图档盖住 768~3000

    def _prewarm_start(self):
        """扫描完 / 布局稳定后，后台把各组要用的图准备好。

        ⚠️ 顺序是**紧着当前会看到的先做**：当前这一组的两张排最前面，
        否则力气全花在后面的组上，点第一组还是得等。
        """
        paths = []

        def add(p):
            if p and p not in paths:
                paths.append(p)

        add(self.path_a)
        add(self.path_b)
        if self.view == "groups":
            for g in self.groups:
                for p in g["members"][:2]:
                    add(p)
        for it in self.glist.items:
            # ⚠️⚠️ **只收字符串**：分组视图里 `tag` 是**组号（int）**，
            #    不是路径。原来这里无脑 `add(it["tag"])`，于是
            #    `_prewarm_q` 里混进一堆 int，后台线程拿它去
            #    `META.of(path)` -> `os.path.basename(int)` 直接抛
            #    `TypeError: expected str, bytes or os.PathLike object, not int`。
            #    这个异常在**工作线程**里被 `traceback.print_exc()` 打到 stderr，
            #    界面上什么都看不到 —— 一批「总能复现但从没人报」的红色堆栈
            #    就是这么来的（`test_ui.py` 里 18 次，`git show HEAD` 一样有）。
            #    后果不只是刷屏：`PREWARM_MAX` 的名额被这些 int 白占掉，
            #    真正该预热的图反而被挤出队列。
            tag = it.get("tag")
            if isinstance(tag, str):
                add(tag)
        # ⚠️ 兜底：一个组都没有时（比如一批互不相干的图），上面三处**全是空的**，
        #    预热就空转 —— 实测 0 组时队列长度 0、`_b` 一片空白，第一次滚轮
        #    还得现解 1536 那一档，128ms 就这么来的。
        for p in (self.files or []):
            add(p)
        self._prewarm_q = paths[:self.PREWARM_MAX]
        if not self._prewarm_q:
            return
        # ⚠️ 别在这里清 `_prewarm_busy`：还有线程在跑时把它置 False，
        #    `_prewarm_tick` 会再起一条 —— 两条后台解码抢 GIL，反而更卡。
        #    队列重建后由 `_prewarm_done` 接着往下走。
        # 按**当前画布尺寸**准备，尺寸对了成品图的 key 才对得上
        cw = max(1, self.cv_a.winfo_width())
        ch = max(1, self.cv_a.winfo_height())
        margin = S(8)
        box = (max(16, cw - margin), max(16, ch - margin))
        self._prewarm_box = (max(16, box[0] // 16 * 16),
                             max(16, box[1] // 16 * 16))
        self._prewarm_n = 0
        if not getattr(self, "_prewarm_busy", False):
            self._prewarm_tick()

    def _base_prep(self, col, path, need):
        """登记「想要哪一档基准像素」，等用户停手了再后台解。

        页面上是先拿粗的那档顶着（几毫秒），解好之后自动换成清晰的。

        ⚠️ **绝不能在用户正滚滚轮的时候起线程**：解一档是几百毫秒的 Python
        层按行拷贝，会占着 GIL 把主线程饿死 —— 实测那一帧主线程自己的
        每个环节都没超过 3ms，整帧却被拖到 97ms，全花在等 GIL 上。
        所以这里只登记，防抖 300ms（连滚 N 格只解最后一次要的那一档）。
        """
        wh = META.of(path)["wh"]
        lv = self.big._level(need, max(wh) if wh else 0)
        key = (path, lv)
        if key in self._base_prep_done or key in getattr(self, "_base_busy", ()):
            return
        self._base_want[key] = (col, path, need)
        self._later("baseprep", 300, self._base_prep_go)

    def _base_prep_go(self):
        """防抖到期，真的开始解 —— 用户还在动就再往后推。

        ⚠️⚠️ **拖动中也要让路**（v1.7）。`big.base()` 里的解码是
        Python 层按行拷贝，**占着 GIL**；它一跑，主线程的 `update()`
        就会被拖长（实测拖动中一次 update 到 243ms，而同一次补块
        自己只花 105ms —— 差额就来自这些后台解码）。
        判据：慢与补块次数**无关**（把补块判据强制打开后 p95 不变），
        只能是这两条 GIL 竞争的路。
        """
        if getattr(self, "_prewarm_pause", 0) > time.time() or self._drag:
            self._later("baseprep", 250, self._base_prep_go)
            return
        want = getattr(self, "_base_want", None)
        if not want:
            return
        # 连滚过好几档的话，只有**最后**那一档还用得上，中间的别浪费力气
        key = list(want)[-1]
        col, path, need = want.pop(key)
        want.clear()
        self._base_busy.add(key)

        def work():
            try:
                wh = META.of(path)["wh"]
                ok = self.big.base(path, need, max(wh) if wh else 0) is not None
            except Exception:
                ok = False
            self._ui(self._base_prepped, key, col, ok)

        threading.Thread(target=work, daemon=True).start()

    def _base_prepped(self, key, col, ok):
        self._base_busy.discard(key)
        if ok:
            self._base_prep_done.add(key)
        cur = self.path_a if col == 0 else self.path_b
        if cur != key[0] or self.zoom[col] <= 1.001:
            return                       # 用户已经点走 / 退回适应窗口了
        self.render_all(precise=True, only=col)

    def _prewarm_yield(self, sec=0.8):
        """用户在操作（滚轮/点组）—— 预热先让路。

        ⚠️ 后台解码会占着 GIL（读像素那段是 Python 层的按行拷贝），
        跟它抢的话主线程就被拖住：实测滚轮前两档被拖到 129ms / 84ms。
        用户一动就暂停一会儿，等他停下来再继续。
        """
        self._prewarm_pause = time.time() + sec

    def _prewarm_tick(self):
        """一次处理一张：**后台**解码 + 编码，回调里只建 Tk 图。

        ⚠️⚠️ **拖动中整个让路**（v1.7 修「上」方向帧间隔 259ms）。

        这一步看着「全在后台」，其实**后台解码会占着 GIL**：
        `winimg.load_pixels` / `big.base` 里的按行拷贝是 Python 层代码，
        GIL 握在它手里时主线程的 `update()` 只能干等。实测拖动中
        一次 `update()` 被拖到 243ms，而同一次 `_pan_fill` 自己只花 105ms。

        ⚠️ 为什么 `_pump_queue` 那道让路挡不住：预热是 `after` 直接排的
        `_prewarm_tick`，跟队列泵是两条独立的路。
        """
        if getattr(self, "_prewarm_pause", 0) > time.time() or self._drag:
            self.after(200, self._prewarm_tick)
            return
        # ⚠️ 上一步刚攒下的字节（拖动中挂起的），先在后台之外处理掉：
        #   `_prewarm_flush` 一次只建一张太慢，而这里已经是空闲期了。
        if getattr(self, "_prewarm_hold", None):
            self._prewarm_flush()
            self.after(16, self._prewarm_tick)
            return
        if self._prewarm_busy or not getattr(self, "_prewarm_q", None):
            return
        self._prewarm_busy = True
        path = self._prewarm_q.pop(0)
        box = getattr(self, "_prewarm_box", (640, 480))
        lvs = list(self.PREWARM_LV)
        n = getattr(self, "_prewarm_n", 0)
        self._prewarm_n = n + 1
        want_base = n < self.PREWARM_BASE_MAX
        # 正在看的那两张多备中间档（见 PREWARM_FULL_N）
        full = n < self.PREWARM_FULL_N
        if full:
            lvs = sorted(set(lvs) | set(self.PREWARM_FULL_LV))

        def work():
            out = []
            try:
                got = self._decode(path, box[0], box[1], 1.0)
                if got:
                    w, h, bgra = got
                    ppm, _pw, _ph = uikit.fit_ppm(w, h, bgra,
                                                  S(PIC_RADIUS), P.CARD)
                    Q = 16
                    pkey = (path, w, h, max(Q, box[0] // Q * Q),
                            max(Q, box[1] // Q * Q), False)
                    out.append((pkey, ppm))
                # 缩放要用的基准档：只给排在前面的几张准备（见 PREWARM_BASE_MAX）
                if want_base:
                    wh = META.of(path)["wh"]
                    top = max(wh) if wh else max(box)
                    for lv in lvs:
                        if lv < top:
                            self.big.base(path, lv, top)
                    # ⚠️ 预热时顺手把**原图那一档**也备上：放大到 2 倍以上时
                    #    需要的正是它，不备的话第一帧要同步解 49~95ms。
                    self.big.base(path, top, top)
            except Exception:
                import traceback
                traceback.print_exc()
            self._ui(self._prewarm_done, out)

        threading.Thread(target=work, daemon=True).start()

    def _prewarm_done(self, out):
        """后台那张回来了 —— 主线程只做「字节 -> Tk 图」这最后一步。

        ⚠️⚠️ **拖动中必须整个让路**（v1.7 修「上」方向 update 243ms）。

        埋点抓到的：`_pan_fill` 内部只花 105~114ms，可探针量到的帧间隔
        是 **259.7ms** —— 差额 130ms 不在补块里，而在同一次 `update()`
        的**别的回调**上。两个来源，都在这一条链上：

          ① `_prewarm_done` 在**主线程** `tk.PhotoImage(data=ppm)`，
             而预热的是**整张图**（`box` 口径 1500 级，不是分块），
             单张 30~150ms（`_fit_async` 的注释里就记着「38 + 46ms」）。
          ② 它前面那条 `self.big.base(...)` 在**后台线程**跑 GDI 解码 ——
             解码那段是 Python 层按行拷贝，**占着 GIL**，主线程被抢。

        判据自证：这不是「补块慢」。上表里 `_pan_fill` 105ms 对帧间隔
        259ms，比例稳定在 2.2~2.5；而补块快的「右」方向帧间隔只有
        116.6ms（`_pan_fill` 100ms）—— **差的就是预热那一张**。

        ⚠️ 为什么 `_pump_queue` 的让路不够：它只挡住了「后台解码好的
        回调队列」，而预热是 `after` 直接排的 `_prewarm_tick` /
        `_prewarm_done`，走的是另一条路。
        """
        self._prewarm_busy = False
        # ⚠️ 拖动中：字节先攒着，松手再一起建图。
        #   `put_photo` 是纯字典写、`_keep` 是 list append，都不碰 Tk，
        #   放后台线程也安全 —— 但为了不引入新的线程假设，这里只把
        #   **建 Tk 图**这一步推迟，缓存写入照常（后台线程本来就在跑，
        #   字节已经是现成的）。
        if self._drag:
            self._prewarm_hold = list(out)
            self._prewarm_busy = False
            self.after(80, self._prewarm_flush)
            return
        for pkey, ppm in out:
            try:
                img = tk.PhotoImage(data=ppm, master=self)
            except Exception:
                continue
            self.big.put_photo(pkey, img)
            self._keep.append(img)
        if getattr(self, "_prewarm_q", None):
            # 每 60ms 才做一张，中间留出空闲，界面不会被拖慢
            self.after(60, self._prewarm_tick)

    def _prewarm_flush(self):
        """拖动结束了 —— 把攒着的预热图建出来。

        ⚠️ **一次只建一张**，剩下的下轮再建：一次全建会 100~300ms
        糊在松手后的第一帧上，用户刚松手就看到「顿一下」以为没跟上。
        """
        held = getattr(self, "_prewarm_hold", None)
        if not held or self._drag:
            return
        self._prewarm_hold = None
        pkey, ppm = held[0]
        try:
            self.big.put_photo(pkey, tk.PhotoImage(data=ppm, master=self))
            self._keep.append(self.big.photo(pkey))
        except Exception:
            pass
        if len(held) > 1:
            self._prewarm_hold = held[1:]
            self.after(16, self._prewarm_flush)
        elif getattr(self, "_prewarm_q", None):
            self.after(60, self._prewarm_tick)

    def _render_caption(self, cap, path, disp_w, crop_info=None):
        """画布上方那行小字（文件名 + 分辨率 / 显示比例）。"""
        m = META.of(path)
        wh0 = m["wh"]
        cap.name_lbl.configure(text=short_name(m["name"], 34))
        if crop_info is not None:
            cap.dim_lbl.configure(text=crop_info)
        elif wh0:
            pct = 100.0 * disp_w / float(wh0[0])
            cap.dim_lbl.configure(text="%d × %d · 显示 %.0f%%"
                                       % (wh0[0], wh0[1], pct))
        else:
            cap.dim_lbl.configure(text="解析不了尺寸")

    def _render_side(self, col, cv, cap, path, rect, precise=True):
        cw = max(1, cv.winfo_width())
        ch = max(1, cv.winfo_height())
        # ⚠️⚠️ **取用联动待办**（v1.8，见 `_link_apply` 里那段）。
        #
        # 滚轮联动的第 1 格常常撞上「另一侧画布还没布局好」
        #（`winfo_width()` 返回 1），那一格的对齐当时存了下来、没套上去。
        # 必须在这里取 —— **这一侧刚拿到能用的画布尺寸**，
        # 而本函数后面无论走哪条分支（分块 / 整张 / 占位）都会重画，
        # 所以在这里套用pan 最稳：不会漏，也不会套到一半。
        _lp = self._link_pend.pop(col, None)
        if _lp is not None and cw > 1 and ch > 1 and \
                self._link_target(1 - col) == col:
            _norm0, _z0, _f0 = _lp
            disp0 = self._link_disp(col, _z0, _f0)
            if disp0:
                _m0 = ((cw - disp0[0]) // 2, (ch - disp0[1]) // 2)
                self.pan[col] = [int(round(_m0[0] + _norm0[0] * disp0[0])),
                                 int(round(_m0[1] + _norm0[1] * disp0[1]))]
        # ⚠️⚠️ **先不清空画布**。这条渲染路径有多个 `return`（取不到档、
        #    `fit_ppm` 失败、占位图也拿不到）—— 任何一条都会留下一个**空画布**。
        #    主人反馈「拖动的时候图片直接空白了」就是这个：拖动补块传的是
        #    快速档，手上没档就 return 了，而画布已经被 `delete("all")` 清空。
        #    现在改成「真正要贴新图元的那一刻才清」，中间一律保留上一帧 ——
        #    宁可停一帧不动，也绝不让画面空掉。
        cap.name_lbl.configure(text="")
        cap.dim_lbl.configure(text="")
        if not path:
            cv.delete("all")
            cv.create_text(cw // 2, ch // 2,
                           text="左边选一组 / 选一张，这里立刻并排对比",
                           fill=P.TEXT_3, font=self.f_small)
            return
        if cw < 40 or ch < 40:
            return
        desc = self.descs.get(path)
        crop = bool(rect) and self.crop_only.get() and bool(desc)
        zoom = 1.0 if crop else self.zoom[col]

        # 按「容器去掉留边」的尺寸去要像素 —— 和下面 fit_photo 用的格子一致。
        # 不统一的话，解码图会比格子高出那么几像素，然后被 fit_photo 的溢出兜底
        # 直接砍掉一半（只超 8px 却缩成 50%，特别冤）。
        margin = S(8)
        box = (max(16, cw - margin), max(16, ch - margin))
        # ⚠️ box 也要量化（向下取整到 16px），而且**必须先于**解码尺寸的计算。
        #    只量化下面的 `want` 没用：画布抖 8px -> 解码尺寸 `need` 变 ->
        #    重新解码 -> 图的 (w,h) 变 -> 成品图 key 里那两个分量还是不一样，
        #    照样每次选一组都未命中、都要重编码一遍。
        #    向下取整：解码出来的图只会略小于格子，绝不会溢出被裁。
        box = (max(16, box[0] // 16 * 16), max(16, box[1] // 16 * 16))
        # ---- 放大后的渲染：走「只取视口那一块」的新路径 ----
        # 老做法是按 zoom 重新向系统解码一整张更大的图，越放大越贵；
        # 这里改成分块渲染（见 _render_tile）。返回 True 表示已经画好了。
        if zoom > 1.001 and not crop:
            if self._render_tile(col, cv, cap, path, box, zoom, precise):
                return
        if crop:
            got = self.big.get(path, self._crop_decode_size(path, desc, rect, box))
        elif precise >= 1:
            got = self._decode(path, box[0], box[1], zoom)
        else:
            # 快速档：**只用缓存里已有的**，绝不同步重解。
            # 缓存全空（第一次显示）就保持现有画面，等防抖的精确帧。
            got = self.big.peek(path)
            if not got:
                return
        if not got:
            cv.delete("all")          # 要显示错误文字了，旧图得让位
            cv.create_text(cw // 2, ch // 2, text="读不出这张图\n（%s）"
                           % META.of(path)["format"], fill=P.WARN_D,
                           font=self.f_small, justify="center")
            return
        w, h, bgra = got
        if crop:
            px = self._rect_px(desc, w, h, rect)
            cw0 = max(1, px[2] - px[0])
            ch0 = max(1, px[3] - px[1])
            w, h, bgra = thumbs.crop_bgra(w, h, bgra, px[0], px[1], cw0, ch0)

        radius = S(PIC_RADIUS) if not crop else S(6)
        # ⚠️ 先把「这一帧要什么」算出来，再去查成品图缓存：
        #    把已经解码好的像素变成 Tk 图要 ~180ms（编码 145 + Tk 解码 38），
        #    比解码本身还贵。命中就直接贴图，切回同一组/滚回同一档不再重付这笔钱。
        if zoom > 1.001 and not crop:
            if precise >= 1:
                # 放大：解码时已经按 zoom 要过更大的图，这里 1:1 贴上去，
                # 超出的部分由画布自然裁掉，正好可以拖着看局部。
                want = (w, h, 1e9)
            else:
                # 快速档：缓存图还是上一档的小图，整数放大贴到 zoom 该在的
                # 位置 —— 像素糊一点，但位置/大小立刻对，防抖后马上换精确帧。
                want = (box[0] * zoom, box[1] * zoom, 8.0)
        else:
            want = (box[0], box[1], float(MAX_ZOOM))
        # ⚠️ 成品图的 key 一定要**量化**（向下取整到 16px）。
        #    不量化的话，信息卡高度随文件名/目录长短变几个像素 -> 画布高度变
        #    几个像素 -> `want` 变几个像素 -> 上一次的成品图**整张作废**。
        #    实测就是：12 张图在缓存里躺着 24 份，每次选一组都未命中、都要
        #    后台重编码一遍。量化之后画布抖几个像素照样命中。
        #    向下取整是为了让缓存里的图**不大于**格子（宁可小几个像素，
        #    也绝不能溢出被裁）。
        Q = 16
        wq = max(Q, int(want[0]) // Q * Q)
        hq = max(Q, int(want[1]) // Q * Q)
        pkey = (path, w, h, wq, hq, crop)
        img = self.big.photo(pkey)
        if img is not None:
            dw, dh = img.width(), img.height()
        else:
            # 没现成的图：**不要在主线程等**（编码 145ms + Tk 解码 38ms，
            # 两格同时就是半秒，切一组就卡一下）。先顶一张占位上去，
            # 编码交给后台线程，算完再换（见 `_fit_async`）。
            self._fit_async(col, path, pkey, w, h, bgra,
                            (wq, hq, want[2]), radius)
            img, dw, dh = self._placeholder(col, path, want[0], want[1])
        if img is None:
            cv.delete("all")          # 要显示错误文字了，旧图得让位
            cv.create_text(cw // 2, ch // 2, text="这张图显示不了",
                           fill=P.WARN_D, font=self.f_small)
            return
        self._keep.append(img)
        ox = (cw - dw) // 2 + self.pan[col][0]
        oy = (ch - dh) // 2 + self.pan[col][1]
        # ⚠️ 清空挪到这里（见 `_render_side` 开头）：只有真要贴新图元才清
        cv.delete("all")
        item = cv.create_image(ox, oy, anchor="nw", image=img)
        # ox/oy 是**画那一刻**的位置，px/py 是**那一刻**的平移量。
        # 拖动时只需要在这个基础上加平移的增量，不用重算居中。
        # ⚠️ `bx/by` = 图元真实落点。整张贴图时它**恰好等于** ox/oy，
        #    但分块那条路径不是（那是 `ox + px0/sx`）—— `_pan_move`
        #    只认 `bx/by`，见 `_view` 赋值处那段注释。
        self._view[col] = {"cv": cv, "item": item, "ox": ox, "oy": oy,
                           "bx": ox, "by": oy,
                           "px": self.pan[col][0], "py": self.pan[col][1],
                           "disp": (dw, dh), "img": img, "dz": zoom}

        m = META.of(path)
        wh0 = m["wh"]
        if crop:
            # 裁剪模式画的是匹配区那一块，此时拿整图分辨率说「显示 107%」是误导
            # （看着像整图缩了一半，其实根本不是整图）。改成说清楚「这是哪一块、多大」。
            gw, gh = desc["gw"], desc["gh"]
            rw = max(1, int(round((rect[2] - rect[0]) / float(gw) * wh0[0]))) if wh0 else 0
            rh = max(1, int(round((rect[3] - rect[1]) / float(gh) * wh0[1]))) if wh0 else 0
            self._render_caption(cap, path, dw,
                                 crop_info=("仅匹配区 %d × %d · 占本图 %.0f%%"
                                            % (rw, rh, 100.0 * rw * rh
                                               / float(wh0[0] * wh0[1])))
                                 if wh0 else "只看匹配区域")
        else:
            self._render_caption(cap, path, dw)
        if DEBUG:
            print("[渲染] 侧%d %s 解码 %dx%d 显示 %dx%d 缩放 %.2f 画布 %dx%d"
                  % (col, os.path.basename(path), w, h, dw, dh, self.zoom[col],
                     cw, ch), file=sys.stderr)

    # ------------------------------------------------------------------
    # 结论 / 信息
    # ------------------------------------------------------------------
    def _update_verdict(self, pair, fit=True):
        """`fit=False` 表示「 caller 待会儿会统一 fit 一次」，别自己刷。

        结论行并进信息卡之后这两者是同一张卡，各刷一次就是两遍
        全局 `update_idletasks()`（19ms/次）—— 切一组白付 38ms。
        """
        try:
            self._verdict_impl(pair)
        finally:
            if fit:
                # 内容换过就得让卡片重新量一次高度，否则新文字被裁
                # （见 Card.fit_to_content / App._fit_card）
                self._fit_card()

    def _verdict_impl(self, pair):
        a, b = self.path_a, self.path_b
        if not a:
            self.verdict_pill.set("—", P.TEXT_3, P.CARD_SOFT)
            self.verdict_text.configure(text="等待扫描")
            self.verdict_sub.configure(text="左边选一组，这里立刻给出对比")
            self.verdict_tag.configure(text="")
            return
        if not b:
            self.verdict_pill.set("单图", P.TEXT_3, P.CARD_SOFT)
            self.verdict_text.configure(text=short_name(
                os.path.basename(a), 40))
            self.verdict_sub.configure(text="没有可比的另一张（这一组只有它）")
            self.verdict_tag.configure(text="")
            return
        if a not in self.descs or b not in self.descs:
            self.verdict_pill.set("无指纹", P.TEXT_3, P.CARD_SOFT)
            self.verdict_text.configure(text="这两张没参与扫描，算不出相似度")
            self.verdict_sub.configure(text="重新点「开始扫描」试试")
            self.verdict_tag.configure(text="")
            return
        if not pair:
            self.verdict_pill.set("未达判据", P.WARN_D,
                                  uikit.mix(P.WARN, "#ffffff", 0.86))
            self.verdict_text.configure(
                text="按当前灵敏度，这两张不算相似", fg=P.TEXT)
            self.verdict_sub.configure(
                text="把灵敏度切到「宽松」再扫一次，可能就出来了")
            self.verdict_tag.configure(text="")
            return
        kind, score, d, i, j = pair
        strong = kind == scan.UI_STRONG
        self.verdict_pill.set("%.3f" % score,
                              P.DANGER_D if strong else P.WARN_D,
                              uikit.mix(P.DANGER if strong else P.WARN,
                                        "#ffffff", 0.86))
        self.verdict_text.configure(
            text="同一张图的不同裁剪" if strong else "疑似同场景、不同构图",
            fg=P.TEXT)
        self.verdict_sub.configure(
            text="%s  ↔  %s" % (short_name(os.path.basename(a), 22),
                                short_name(os.path.basename(b), 22)))
        self.verdict_tag.configure(text="强边 · 参与分组" if strong
                                   else "宽松边 · 仅疑似")

    def _fit_card(self):
        """量一次信息卡高度，并把它**只增不减地钉住**。

        为什么必须钉住：信息卡高度取决于文件名/目录那一行的长短
        （长一点就多折一行）。高度一变 -> 下面预览画布的高度跟着变
        -> 解码尺寸变 -> 成品图缓存的 key 变 -> **整批作废**。
        实测：卡片在 162 / 186 之间来回跳，画布 757 / 733 来回跳，
        于是同一张图在缓存里躺着两份（720 和 736），每次选一组都未命中、
        都要后台重编码一遍。钉住之后画布尺寸恒定，缓存才真的命中。

        ⚠️ v1.7：**曾经这里还为 NIQE 多留了一行**，那才是卡片在
        162/186 之间跳的另一半原因（NIQE 算完才拼上去、长度变了就多
        折一行）。现在「清晰度」拆成独立一行了，算完前后行数不变，
        预留行已删 —— 钉住机制本身仍然留着（文件名/目录那两行还是会折）。
        """
        self.info_card.fit_to_content()
        # ⚠️ 用**内容本身**的高度（req_height），不是 `cget("height")`：
        #    后者已经跟 min_h 取过 max，在它上面加预留会逐次累加
        #    —— 实测一路加到 306px，信息卡吃掉半个窗口。
        h = self.info_card.req_height()
        if DEBUG:
            print("[卡片] req=%d min_h(旧)=%d -> 量到 %d；钉住前 %d"
                  % (self.info_card.body.winfo_reqheight(),
                     self.info_card.min_h, h, self._info_min_h),
                  file=sys.stderr)
        # ⚠️ v1.7：**曾经**在这里为「NIQE 那半句」盲留一行高度
        #（`if self.niqec.cached(...) is None: h += linespace`）。
        # 那是主人截图里「下面还有一块空位」的来源 —— 留的那行在 NIQE
        # 算完之前**一直是空的**（实测 min_h=186 而内容只要 130px，
        # 白占 56px，等于把预览画布压小 56px）。
        #
        # 现在不需要预留了：清晰度已经**拆成独立一行**（见 `_fill_info_impl`），
        # 算完前后那一行的**行数不变**，所以高度从第一帧起就是最终值 ——
        # 而 `_fit_card` 存在的全部理由（别让卡片高度跳、进而让画布尺寸
        # 变、进而让解码缓存 key 变）也就自动满足了。
        if h > self._info_min_h:
            self._info_min_h = h
        self.info_card.min_h = self._info_min_h

    def _fill_info(self, fit=True):
        try:
            self._fill_info_impl()
        finally:
            if fit:
                self._fit_card()

    def _fill_info_impl(self):
        """两图信息**并排对照**，压成紧凑几行（把高度让给图片）。

        每一侧排下去是：标题（文件名）+ 「分辨率 · 格式 · 大小 · 时间 ·
        质量 · 清晰度(NIQE)」一行 + 目录，共 3 行。
        左右两列的字段顺序完全一致，方便竖着扫一眼比差异。
        2026-10-05 主人：「信息窗口占比太大，第3行的工作网格什么的可以去掉，
        一行可以显示两行的信息」—— 网格/匹配区域那行整个去掉，
        质量与 NIQE 并进分辨率那行。
        """
        for ch in self._info_grid.winfo_children():
            ch.destroy()
        # 卡片重建了，旧的「清晰度」标签引用全部作废（算完的结果靠路径比对丢弃）
        self._niqe_lbl = {}
        self._info_grid.columnconfigure(0, weight=1, uniform="i")
        self._info_grid.columnconfigure(1, weight=1, uniform="i")
        sides = [(self.path_a, self.rect_a, 0)]
        if self.mode == "pair":
            sides.append((self.path_b, self.rect_b, 1))
        else:
            self._info_grid.columnconfigure(1, weight=0, uniform="")
        for path, rect, col in sides:
            box = tk.Frame(self._info_grid, bg=P.CARD)
            box.grid(row=0, column=col, sticky="nsew", padx=S(6))
            if not path:
                continue
            m = META.of(path)
            wh = m["wh"]
            q = quality_of(wh, m["size"])

            head = tk.Frame(box, bg=P.CARD)
            head.pack(fill="x")
            tk.Label(head, text=short_name(m["name"], 42), bg=P.CARD,
                     fg=P.TEXT, font=self.f_h2, anchor="w").pack(
                         side="left", fill="x", expand=True)
            if q[0] in ("poor", "fair"):
                tk.Label(head, text="  " + q[1], bg=P.CARD,
                         fg=P.POOR if q[0] == "poor" else P.TEXT_3,
                         font=self.f_tiny).pack(side="left")

            # ⚠️⚠️ **清晰度（NIQE）放标题行右侧，不另起一行**（v1.7）。
            #
            # 这里换过两次位置，每次都是被截图逼的（主人 2026-10-06
            # 「NIQE 由于长度显示不出数据，看得到下面还有点空位」）：
            #   ① 原来拼在信息行末尾 + `_fit_card` 盲留一行高度
            #      → NIQE 算完前那半句和那行**都是空的**（截图就是这样）。
            #   ② 改成独立一行、删掉预留 → 空位没了，但**卡片反而高了 6px**
            #      （实测 186 -> 192，因为多了一整行 Label 的 30px，
            #      比省下的 24px 预留还多 6）—— 跟「适当调整」反着来。
            #   ③ 现在放进标题行右侧：那一行右边本来就是空的
            #      （只有文件名 + 可选的质量徽章），**一行都不多占**，
            #      预留也就不需要了（算完前后行数不变 -> 卡片高度恒定
            #      -> 画布尺寸恒定 -> 解码缓存 key 不变，见 `_fit_card`）。
            #
            # `wraplength=0` = **不折行**：宁可右边被裁掉一点，
            # 也不能让高度跳 —— 高度一跳，下面画布就变，缓存整批作废。
            nq = self.niqec.cached(path)
            lbl2 = tk.Label(head, text=niqe_field(path, nq),
                            bg=P.CARD, fg=P.TEXT_3, font=self.f_small,
                            anchor="e", justify="right", wraplength=0)
            lbl2.pack(side="right")
            self._niqe_lbl[col] = (path, lbl2)
            if nq is None:
                self.niqec.request(path,
                                   lambda p, res, c=col: self._niqe_done(c, p, res))

            f1 = ["分辨率 %s" % (("%d × %d（%.1f MP）"
                                  % (wh[0], wh[1], imgsize.megapixels(wh)))
                                 if wh else "解析不了（%s）" % m["format"]),
                  "%s · %s" % (m["format"], human_size(m["size"])),
                  human_time(m["mtime"]),
                  "质量 %s" % q[1]]
            tk.Label(box, text=" · ".join(f1), bg=P.CARD, fg=P.TEXT_2,
                     font=self.f_small, anchor="w", justify="left",
                     wraplength=S(780)).pack(fill="x")
            tk.Label(box, text=m["dir"], bg=P.CARD, fg=P.TEXT_3,
                     font=self.f_tiny, anchor="w", justify="left",
                     wraplength=S(440)).pack(fill="x")

    def _niqe_done(self, col, path, res):
        """NIQE 算好了，把信息卡里那一段换掉。

        只在「这一侧还是当时那张图」时才动它 —— 用户可能已经点到别的图上了，
        那时候旧结果必须丢掉，否则会张冠李戴。

        ⚠️ **只改「清晰度」那一行**（v1.7：它已经拆成独立 Label 了）。
        原来它是拼在信息行末尾的，回填得拿整行模板重写一遍 —— 现在不用了。
        """
        got = self._niqe_lbl.get(col)
        if not got or got[0] != path:
            return
        _path, lbl = got
        try:
            if not lbl.winfo_exists():
                return
            lbl.configure(text=niqe_field(path, res))
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # 撤销（v1.11）
    # ------------------------------------------------------------------
    # 主人 2026-10-07：「添加撤销功能，当误删时可以撤销」。
    #
    # ⚠️ 为什么撤销**必须有**：`delete_side` 从 v1.11 起不再弹确认框
    #    （移入隔离夹可逆，主人明确要求去掉那道帘子）。帘子拿掉之后，
    #    「按错键把图删了」的兜底就从「弹窗」变成了「撤销」——
    #    这两件事是**一起来的**，只去掉弹窗不给撤销就是留了个坑。
    UNDO_MAX = 20          # 最多记多少步（再多也没人会去翻）

    def _push_undo(self, label, moves, keep):
        """把「这次移除的完整现场」压栈。

        `keep` 是**移除之前**拍的快照：`descs` / `store.items` / `links`
        / `files`。为什么整份存而不是只存那几张：移除会把 `links` 里
        牵涉到它们的边**一次过滤掉**，事后想反向推算出「原来有哪些边」
        极容易漏；账本本来就不大（几百张图的 links 也就几 MB）。
        """
        self.undo_stack.append({
            "label": label, "moves": list(moves),
            "descs": keep["descs"], "items": keep["items"],
            "links": keep["links"], "files": keep["files"]})
        del self.undo_stack[:-self.UNDO_MAX]
        self._sync_undo_btn()

    def _sync_undo_btn(self):
        """按钮可用态跟着撤销栈走 —— 栈空就灰掉，别让人点了没反应。"""
        try:
            self.btn_undo.configure_state(
                "normal" if self.undo_stack else "disabled")
        except Exception:
            pass

    @property
    def can_undo(self):
        return bool(self.undo_stack)

    def undo(self):
        """撤销上一次「移入隔离夹」：文件搬回原位 + 还原内存结构。"""
        if self.busy:
            return
        if not self.undo_stack:
            self._toast("没有可撤销的操作")
            return
        rec = self.undo_stack.pop()
        back, fail = [], []
        for src, dest in rec["moves"]:
            # ⚠️ 三道前置检查一条都不能省，否则要么**覆盖用户自己的文件**、
            #    要么在「隔离夹被手动清空」时报一个看不懂的异常。
            if os.path.exists(src):
                fail.append((src, "原位已经有同名文件，没敢覆盖"))
                continue
            if not os.path.isfile(dest):
                fail.append((src, "隔离夹里那份已经不在了"))
                continue
            try:
                d = os.path.dirname(src)
                if d and not os.path.isdir(d):
                    os.makedirs(d, exist_ok=True)   # 原目录被删了也能还原
                shutil.move(dest, src)
                back.append(src)
            except Exception as e:
                fail.append((src, str(e)))
        if not back:
            # ⚠️ **一条都没搬回来时，记录必须压回去**：多半是「原位已有
            #    同名文件 / 隔离夹被手动清空」这类**可以补救**的冲突，
            #    用户处理完还想再撤一次。这里 pop 完不还，记录就永远没了。
            self.undo_stack.append(rec)
            self._sync_undo_btn()
            messagebox.showwarning("撤销不了", "\n".join(
                "%s\n  %s" % (os.path.basename(p), why)
                for p, why in fail[:6]))
            return
        # 内存结构：快照是「移除前」拍的，**直接覆盖回去**即可
        self.descs.update(rec["descs"])
        self.store.items.update(rec["items"])
        self.links = list(rec["links"])
        self.files = list(rec["files"])
        self.groups_raw, self.weak = scan.build_groups(self.descs, self.links)
        self._make_view_groups()
        self.store.save()
        self._after_edit()
        self._sync_undo_btn()
        self.stat.configure(text="已撤销「%s」—— 搬回 %d 张，还原到移除前的样子"
                                 % (rec["label"], len(back)))
        self._toast("已撤销：%s" % rec["label"])
        if fail:
            messagebox.showwarning("有 %d 张没搬回来" % len(fail), "\n".join(
                "%s\n  %s" % (os.path.basename(p), why)
                for p, why in fail[:6]))

    # ------------------------------------------------------------------
    # 删除 / 清理完全重复
    # ------------------------------------------------------------------
    def _need(self, path):
        if path and os.path.isfile(path):
            return True
        messagebox.showinfo("没有这张图", "先在左边选一组。")
        return False

    def delete_side(self, col):
        """把这一侧的图移进隔离夹。**故意不弹确认框。**

        ⚠️⚠️ 主人 2026-10-07 明确要求：「移除的时候由于不是直接删除，
        所以除了清理完全重复外都不需要提醒弹窗」。

        理由是**操作本身可逆** —— 移进隔离夹是 `move` 不是 `delete`，
        文件原样躺在磁盘上，拖回去就还原了。为一次可逆操作弹一个
        「你确定吗」，代价是每次都多点一下，而收益接近于零。

        ⚠️ 全程序**只有 `clean_exact_dups` 保留确认框**：它一次动
        **一批**（可能几十上百张）、横跨多个目录，误触代价完全不是一个
        量级。单张删除随时能拖回来，不需要拦。
        """
        p = self.path_a if col == 0 else self.path_b
        if not p:
            self._toast("这一侧没有图")
            return
        if not os.path.isfile(p):
            self._toast("文件已经不在了")
            return
        n = self.delete_paths([p])
        if n:
            self._toast("已移入隔离夹 · 想还原就把它拖回原目录")

    def delete_paths(self, paths, label=None):
        """把一批图移进隔离夹，并把它们从所有内存结构里摘干净。

        `label` 只用来给**撤销**那条记录起个名字（「撤销：清理完全重复 20 张」
        比「撤销：移走 20 张」有用得多）。
        """
        paths = [p for p in dict.fromkeys(paths) if p and os.path.isfile(p)]
        if not paths:
            return 0
        done, failed = quarantine.isolate_many(
            paths, self.roots,
            on_progress=lambda d, t: self._progress(0.6 * d / max(1, t),
                                                    "移入隔离夹 %d/%d" % (d, t)))
        gone = set()
        # ⚠️⚠️ **撤销要的快照必须在「摘干净」之前拍**：下面的循环会把
        #     `descs` / `store.items` 里对应项 pop 掉、`links`/`files`
        #     也要被过滤，事后没法反向推算。账本不大（几百张图的 links
        #     也就几 MB），直接整份存最稳 —— 同 `SnapshotStack` 的思路。
        keep = {"descs": {s: self.descs[s] for s, _ in done if s in self.descs},
                "items": {s: self.store.items[s]
                          for s, _ in done if s in self.store.items},
                "links": list(self.links), "files": list(self.files)}
        for src, dest in done:
            gone.add(src)
            META.drop(src)
            self.thumb.drop(src)
            self.big.drop(src)
            self.descs.pop(src, None)
            self.store.items.pop(src, None)
        if not gone:
            if failed:
                messagebox.showerror("移动失败", "\n".join(
                    "%s\n  %s" % (os.path.basename(p), why) for p, why in failed[:6]))
            return 0
        self._push_undo(label or ("移走 %d 张" % len(gone)), list(done), keep)

        self.files = [f for f in self.files if f not in gone]
        self.links = [l for l in self.links
                      if l[0] not in gone and l[1] not in gone]
        self.groups_raw, self.weak = scan.build_groups(self.descs, self.links)
        self._make_view_groups()
        self.store.save()

        if self.path_a in gone:
            self.path_a = None
            self.rect_a = None
        if self.path_b in gone:
            self.path_b = None
            self.rect_b = None
        self.pair = None
        self.mode = "pair" if (self.path_a and self.path_b) else "single"
        self._apply_grid()
        self._sync_link_ui()      # 隔离后可能变成单图(v1.8)

        self._after_edit()
        self.stat.configure(text="已把 %d 张移入隔离夹（从列表里摘掉了）"
                                 % len(gone))
        if failed:
            messagebox.showwarning(
                "有 %d 张没移成功" % len(failed),
                "\n".join("%s\n  %s" % (os.path.basename(p), why)
                          for p, why in failed[:6]))
        return len(gone)

    def clean_exact_dups(self):
        """一键清理**内容 100% 相同**的图（文件哈希一样），每组留最好的一张。"""
        if self.busy:
            return
        files = list(self.files) or list(self.descs)
        files = [p for p in files if os.path.isfile(p)]
        if len(files) < 2:
            messagebox.showinfo("没有可比的文件", "先添加并扫描一些图片。")
            return
        self.busy = True
        self.btn_dedup.configure_state("disabled")
        self.btn_scan.configure_state("disabled")

        def worker():
            try:
                groups = quarantine.find_exact_dups(
                    files,
                    on_progress=lambda d, t: self._ui(
                        self._progress, d / max(1, t),
                        "查重 %d/%d（只比对大小相同的文件）" % (d, t)),
                    should_stop=lambda: self.stop_flag)
                self._ui(self._ask_clean, groups)
            except Exception as e:
                self._ui(self._fail, "%s: %s" % (type(e).__name__, e))

        threading.Thread(target=worker, daemon=True).start()

    def _ask_clean(self, groups):
        self.busy = False
        self.btn_dedup.configure_state("normal")
        self.btn_scan.configure_state("normal")
        self.pb["value"] = 100
        if not groups:
            self.stat.configure(text="没有内容完全相同的图片")
            self._toast("没有 100% 相同的图")
            return
        plan = quarantine.plan_exact_dups(groups)
        total_drop = sum(len(d) for _, d, _ in plan)
        freed = sum(f for _, _, f in plan)
        # ⚠️⚠️ **每组必须编号**（主人 2026-10-06：「显示哪一组，名字不好找」）。
        #
        # 原来 6 组直接罗列、文件名截断到 24~30 字（`v2-cc86d05542d9…4aef5846_r`
        # 这种哈希名截完几乎全一样），用户根本没法跟屏幕上的内容对上，
        # 想反悔只能全盘接受或全盘拒绝。
        # 编号之后用户能直接说「第 3 组那两个别动」——虽然现在还不能
        # 单独勾选，但至少能定位、能跟别人说清楚。
        #
        # 顺带把「保留/移走」的对齐做掉：用固定宽度前缀，文件名长短不一时
        # 也能一眼看出是同一组的。
        _KEEP_W = 46
        lines = []
        multi_dir = False
        for gi, (keep, drop, _f) in enumerate(plan, 1):
            m = META.of(keep)
            wh = m["wh"]
            # ⚠️ 同一组可能来自**不同文件夹**（扫描了多个目录时）。
            #    只给文件名的话，用户看到两个同名文件根本分不清是哪个 ——
            #    那正是「名字不好找」的另一半。
            dirs = set(os.path.dirname(p) for p in [keep] + list(drop))
            if len(dirs) > 1:
                multi_dir = True
            lines.append("【第 %d 组】共 %d 张 · 省 %s" % (
                gi, len(drop) + 1, human_size(_f)))
            lines.append("  保留  %s%s" % (
                short_name(m["name"], _KEEP_W),
                ("（%d × %d）" % wh) if wh else ""))
            for p in drop:
                _bn = short_name(os.path.basename(p), _KEEP_W)
                _dn = ""
                if len(dirs) > 1:
                    # 只标所在文件夹的**最后一级**，够区分又不至于太长
                    _dn = "  ← %s" % short_name(
                        os.path.basename(os.path.dirname(p)) or
                        os.path.dirname(p), 18)
                lines.append("  移走  %s%s" % (_bn, _dn))
            if gi < len(plan):
                lines.append("")
        if len(plan) > 10:
            lines.append("（只列了前 10 组，共 %d 组）" % len(plan))
        _hint = ("\n\n同名文件分属不同文件夹，已在右边标出所在目录。"
                 if multi_dir else "")
        ok = messagebox.askyesno(
            "清理完全重复的图片",
            "找到 %d 组**内容完全相同**的图（文件哈希一致，100%% 相同），"
            "共可移走 %d 张、省出 %s。\n\n"
            "每组保留分辨率最高 / 文件最大的那张，**其余移到隔离夹**"
            "（每组编号，报「第几组」就能定位）：\n\n%s%s\n\n"
            "被移走的会进各自的「%s」隔离夹（移动，不是删除）。继续吗？"
            % (len(plan), total_drop, human_size(freed), "\n".join(lines),
               _hint, quarantine.QUARANTINE_NAME),
            icon="warning", default="no")
        if not ok:
            self.stat.configure(text="已取消清理")
            return
        drops = [p for _k, d, _f in plan for p in d]
        n = self.delete_paths(drops, label="清理完全重复 %d 张" % len(drops))
        self.stat.configure(text="清理完成：移走 %d 张，省出 %s"
                                 % (n, human_size(freed)))

    # ------------------------------------------------------------------
    # 编辑之后重建界面
    # ------------------------------------------------------------------
    def _after_edit(self):
        """删除 / 清理之后，重建列表与对比区。

        尽量保住用户当前的位置：还在分组视图就回到原来那个组，
        还在「全部图片」视图就保持同一张图。
        """
        if self.view == "groups":
            n = len(self.groups)
            if n:
                idx = min(max(self.gidx, 0), n - 1)
                self.gidx = idx
                self._fill_group_list(select=idx)
            else:
                self.gidx = -1
                self._fill_group_list()
        else:
            # `_fill_all_list` 自己会把「看哪一张」定下来（原来那张没了就退回第一行），
            # 并且顺带填好左下「与它相似」和右侧 —— 这里不用再补一遍。
            self._fill_all_list()
        self._sync_view_btns()
        self._update_verdict(self.pair, fit=False)
        self._fill_info(fit=False)
        self._fit_card()
        self.render_all()

    # ------------------------------------------------------------------
    # 操作
    # ------------------------------------------------------------------
    def do_copy_image(self, path):
        if not self._need(path):
            return
        ok, info = net.copy_image(path)
        if ok:
            self._toast("已复制 %s 的图片到剪贴板（%s）"
                        % (short_name(os.path.basename(path), 20), info))
            return
        out, msg = net.export_copy(path)
        if out:
            net.reveal_in_explorer(out)
            self._toast("剪贴板不可用，已导出副本并定位")
            messagebox.showwarning(
                "剪贴板不可用",
                "系统拒绝了剪贴板访问，已改成导出一份临时副本：\n%s\n\n"
                "已在资源管理器里定位到它，拖进网页/聊天窗口即可。" % out)
        else:
            messagebox.showerror("复制失败", msg)

    def do_copy_text(self, text):
        if net.copy_text(text):
            self._toast("已复制到剪贴板")
        else:
            messagebox.showwarning(
                "剪贴板不可用", "系统拒绝了剪贴板访问，没法复制文本。\n\n"
                                "要复制的内容：\n\n%s" % text)

    def copy_names(self):
        ps = [p for p in (self.path_a, self.path_b) if p]
        if not ps:
            return
        self.do_copy_text("\n".join(os.path.basename(p) for p in ps))

    def copy_paths(self):
        ps = [p for p in (self.path_a, self.path_b) if p]
        if not ps:
            return
        self.do_copy_text("\n".join(ps))

    def _reveal(self, path):
        if self._need(path):
            net.reveal_in_explorer(path)


# ---------------------------------------------------------------------------



def main(argv):
    uikit.enable_dpi_awareness()      # 必须在建窗口之前
    app = App(argv)
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
