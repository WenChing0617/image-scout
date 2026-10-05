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
    """

    def __init__(self, limit: int = 5):
        self._c = {}
        self.limit = limit

    def get(self, path: str, need: int, force: bool = False):
        need = max(64, int(need))
        got = self._c.get(path)
        if not force and got is not None and need <= got[0] <= need * 1.15:
            self._c[path] = self._c.pop(path)        # 触碰一下，算 LRU
            return got[1], got[2], got[3]
        # SIIGBF_SCALEUP：允许系统把图放大到我们要的尺寸（不加这个标志它只缩不放）。
        # raw=True：预览不参与指纹，走单次读取，尺寸精确（见 winimg.load_pixels）
        r = winimg.load_pixels(path, need,
                               winimg.SIIGBF_RESIZETOFIT | winimg.SIIGBF_SCALEUP,
                               raw=True)
        if not r:
            return None
        self._c[path] = (need, r[0], r[1], r[2])
        while len(self._c) > self.limit:
            self._c.pop(next(iter(self._c)))
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

    def drop(self, path: str):
        self._c.pop(path, None)


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
        # 缩放是**每一侧各一份**：滚轮只作用于鼠标底下那一张。
        # 早先是一个全局值，一滚两边一起变大，想「A 放大看细节、B 保持全貌」就做不到。
        self.zoom = [1.0, 1.0]
        self.pan = [[0, 0], [0, 0]]
        self._drag = None
        self._q = queue.Queue()
        self._later_ids = {}
        self._keep = []           # 主预览图的引用
        self._view = {}           # 侧 -> (画布, 图元, 显示宽, 显示高, 画布宽, 画布高)

        self.preset_key = scan.DEFAULT_PRESET
        self.recursive_var = tk.BooleanVar(value=True)
        self.crop_only = tk.BooleanVar(value=False)

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

        self._build_top(root)

        body = tk.Frame(root, bg=P.PAGE)
        body.pack(fill="both", expand=True, pady=(S(8), 0))
        self._build_left(body)
        self._build_right(body)
        self._fill_info()

        self.after(60, self._pump_queue)      # 队列泵只能由主线程启动

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
        self.clip_hint = tk.Label(line2, text="", bg=P.CARD, fg=P.TEXT_3,
                                  font=self.f_tiny, anchor="e")
        self.clip_hint.pack(side="right", padx=(S(8), S(8)))
        self.btn_dedup = uikit.RoundButton(
            line2, "清理完全重复", command=self.clean_exact_dups,
            kind="soft", page=P.CARD, size=10, padx=S(15), pady=S(6))
        self.btn_dedup.pack(side="right", padx=(0, S(4)))
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
            L, on_pick=self._on_pick_member, on_activate=self._on_activate_member,
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
                                                           130, self.render_all))
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
        self.pair = None
        self.zoom = [1.0, 1.0]        # 每侧一份（和 __init__ 里一致）
        self.pan = [[0, 0], [0, 0]]
        self.thumb = ThumbCache(self)
        self.big = BigCache()
        self._niqe_lbl = {}           # 信息卡里「清晰度」那一段的引用
        self.glist.set_items([])
        self.mlist.set_items([])
        self._sync_view_btns()
        self._update_verdict(None)
        self._fill_info()
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
        self.stat.configure(text=text)

    def _progress(self, frac, text):
        self.pb["value"] = max(0.0, min(1.0, frac)) * 100
        self.stat.configure(text=text)

    def _fail(self, msg):
        self.busy = False
        self.btn_scan.configure_state("normal")
        self.btn_stop.configure_state("disabled")
        messagebox.showerror("扫描出错", msg)

    def _done(self, descs, links, built, stats, key):
        self.busy = False
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
        if self.view == "all":
            self._fill_all_list()
        else:
            self._fill_group_list()

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
    def _fill_group_list(self, select=0):
        items = []
        for gi, g in enumerate(self.groups):
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
            if not (0 <= select < len(items)):
                select = 0
            self.glist.select(select)
        else:
            self.group = None
            self.gidx = -1
            self.cur_member = None
            self.path_a = self.path_b = None
            self.mlist.set_items([])
            self.lbl_members.configure(text="本组成员")
            self._update_verdict(None)
            self._fill_info()
            self.render_all()
        self._sync_view_btns()

    def _fill_all_list(self, keep=None):
        """「全部图片」视图：连没配上对的也列出来。

        排序刻意把**质量差的排前面**（用户要「质量差的强调出来」），
        同级按文件名。质量差的还会带一个 `quality_of` 给的低质徽章 + 琥珀色标题。

        ⚠️ 本函数**必须顺手把「看哪一张」定下来**（挑一行并 `notify=True`）：
        右边预览区和信息卡是跟着「列表选中的那一项」走的。只填列表不选行，
        切过来右边就是**空的**、点哪一行都像没反应 —— 用户报过这个。
        优先保住原来那张（`keep` / `self.path_a`），它不在列表里（比如刚被删）
        就退回第一行，不会停在空状态。
        """
        files = list(self.files)
        if not files:
            files = list(self.descs)

        def key(p):
            q = META.quality(p)[0]
            return (QUALITY_RANK.get(q, 2), os.path.basename(p).lower())

        files.sort(key=key)
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
                "photo": self.thumb.get(p, S(40), S(9), P.CARD),
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
            self._update_verdict(None)
            self._fill_info()
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
        items = []
        for p in self._member_order():
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
            items.append({"title": short_name(m["name"], 28), "sub": sub,
                          "tone": tone, "tag": p,
                          "badge": poor_badge(q),
                          "badge_tone": "poor",
                          "photo": self.thumb.get(p, S(36), S(9), P.CARD)})
        self.mlist.set_items(items)
        self.lbl_members.configure(text="本组成员 · %d" % len(items))
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
        self.cur_member = None
        self._fill_member_list()
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
        if order and path == order[0] and len(order) > 1:
            # 点的是代表图本身 —— 那就跟第二像的比，别自己跟自己比
            a, b = order[0], order[1]
            self.mlist.select(1, notify=False)
        elif order:
            a, b = order[0], path
        else:
            a = b = path
        self.set_pair(a, b)

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
        """[代表图, 其余按与代表图的相似度降序]"""
        g = self.group
        if not g:
            return []
        rep = self._representative(g)
        rest = [p for p in g["members"] if p != rep]
        rest.sort(key=lambda p: -self._score_to_rep(g, rep, p)[0])
        return [rep] + rest

    # ------------------------------------------------------------------
    # 对比
    # ------------------------------------------------------------------
    def _auto_pair(self):
        """选中一组后自动决定比哪两张 —— 不用用户再点按钮。"""
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
        descs = self.descs
        self.pair = None
        self.rect_a = self.rect_b = None
        self.pan = [[0, 0], [0, 0]]
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
        self._update_verdict(self.pair)
        self._fill_info()
        self.render_all()

    def _apply_grid(self):
        if self.mode == "pair":
            self.cell_b.grid(row=0, column=1, sticky="nsew", padx=S(6))
            self.cell_a.grid_configure(column=0, columnspan=1)
        else:
            self.cell_b.grid_remove()
            self.cell_a.grid_configure(column=0, columnspan=2)

    # ---- 缩放 / 平移 ---------------------------------------------------
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
        self.zoom[col] = z
        self.pan[col] = [0, 0]        # 只清这一侧的平移
        # 先用缓存里已有的图立刻响应（绝不同步重解大图 —— 那是「缩放好卡」
        # 的根源），再防抖 170ms 按新 zoom 精确重解一帧。连滚 N 格只解一次。
        self.render_all(precise=False, only=col)
        self._later("zoomhi%d" % col, 170,
                    lambda c=col: self.render_all(precise=True, only=c))
        self._toast("%s缩放 %.0f%%（滚轮调整，按住可拖动）"
                    % ("" if self.mode != "pair" else ("左" if col == 0 else "右"),
                       z * 100))

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

        ⚠️ 这里**只挪画布上的图元**，绝不能重新渲染 —— 重渲染会把整张图
        重新编码成 PNG 再交给 Tk，鼠标一动就卡成幻灯片。
        """
        d = self._drag
        if not d or d[0] != col or self.zoom[col] <= 1.001:
            return
        info = self._view.get(col)
        if not info:
            return
        cv, item, dw, dh, cw, ch = info
        px = d[3] + (ev.x - d[1])
        py = d[4] + (ev.y - d[2])
        self.pan[col][0], self.pan[col][1] = px, py
        cv.coords(item, (cw - dw) // 2 + px, (ch - dh) // 2 + py)

    def _pan_end(self):
        self._drag = None

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

    def render_all(self, precise=True, only=None):
        """重渲染对比区。

        `only` 指定只重画某一侧（滚轮只动了一侧时，别把另一侧也白白
        重新编码一遍）；此时 **不清** `_keep` / `_view`，另一侧的画面原样保留。
        `precise=False` 是滚轮快速档：只用缓存里已有的解码图顶上，
        不同步重解 —— 连滚时每格都解大图就是「缩放好卡」的根源。
        """
        if only is None:
            self._keep = []
            self._view = {}
        self._render_side(0, self.cv_a, self.cap_a, self.path_a, self.rect_a,
                          precise)
        if only is None or only == 1:
            if self.mode == "pair":
                self._render_side(1, self.cv_b, self.cap_b, self.path_b,
                                  self.rect_b, precise)

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

    def _render_side(self, col, cv, cap, path, rect, precise=True):
        cw = max(1, cv.winfo_width())
        ch = max(1, cv.winfo_height())
        cv.delete("all")
        cap.name_lbl.configure(text="")
        cap.dim_lbl.configure(text="")
        if not path:
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
        if crop:
            got = self.big.get(path, self._crop_decode_size(path, desc, rect, box))
        elif precise:
            got = self._decode(path, box[0], box[1], zoom)
        else:
            # 快速档：**只用缓存里已有的**，绝不同步重解。
            # 缓存全空（第一次显示）就保持现有画面，等防抖的精确帧。
            got = self.big.peek(path)
            if not got:
                return
        if not got:
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
        if zoom > 1.001 and not crop:
            if precise:
                # 放大：解码时已经按 zoom 要过更大的图，这里 1:1 贴上去，
                # 超出的部分由画布自然裁掉，正好可以拖着看局部。
                img, dw, dh, _ = uikit.fit_photo(
                    self, w, h, bgra, w, h, max_zoom=1e9, radius=radius,
                    page=P.CARD)
            else:
                # 快速档：缓存图还是上一档的小图，整数放大贴到 zoom 该在的
                # 位置 —— 像素糊一点，但位置/大小立刻对，防抖后马上换精确帧。
                img, dw, dh, _ = uikit.fit_photo(
                    self, w, h, bgra, box[0] * zoom, box[1] * zoom,
                    max_zoom=8.0, radius=radius, page=P.CARD)
        else:
            img, dw, dh, _ = uikit.fit_photo(
                self, w, h, bgra, box[0], box[1], max_zoom=MAX_ZOOM,
                radius=radius, page=P.CARD)
        if img is None:
            cv.create_text(cw // 2, ch // 2, text="这张图显示不了",
                           fill=P.WARN_D, font=self.f_small)
            return
        self._keep.append(img)
        ox = (cw - dw) // 2 + self.pan[col][0]
        oy = (ch - dh) // 2 + self.pan[col][1]
        item = cv.create_image(ox, oy, anchor="nw", image=img)
        self._view[col] = (cv, item, dw, dh, cw, ch)

        m = META.of(path)
        wh0 = m["wh"]
        cap.name_lbl.configure(text=short_name(m["name"], 34))
        if crop:
            # 裁剪模式画的是匹配区那一块，此时拿整图分辨率说「显示 107%」是误导
            # （看着像整图缩了一半，其实根本不是整图）。改成说清楚「这是哪一块、多大」。
            gw, gh = desc["gw"], desc["gh"]
            rw = max(1, int(round((rect[2] - rect[0]) / float(gw) * wh0[0]))) if wh0 else 0
            rh = max(1, int(round((rect[3] - rect[1]) / float(gh) * wh0[1]))) if wh0 else 0
            cap.dim_lbl.configure(
                text=("仅匹配区 %d × %d · 占本图 %.0f%%"
                      % (rw, rh, 100.0 * rw * rh / float(wh0[0] * wh0[1])))
                if wh0 else "只看匹配区域")
        else:
            pct = (dw / float(wh0[0]) * 100.0) if wh0 and wh0[0] else 100.0
            cap.dim_lbl.configure(
                text=("%d × %d · 显示 %.0f%%" % (wh0[0], wh0[1], pct))
                if wh0 else "解析不了尺寸 · 显示 %.0f%%" % pct)
        if DEBUG:
            print("[渲染] 侧%d %s 解码 %dx%d 显示 %dx%d 缩放 %.2f 画布 %dx%d"
                  % (col, os.path.basename(path), w, h, dw, dh, self.zoom[col],
                     cw, ch), file=sys.stderr)

    # ------------------------------------------------------------------
    # 结论 / 信息
    # ------------------------------------------------------------------
    def _update_verdict(self, pair):
        try:
            self._verdict_impl(pair)
        finally:
            # 内容换过就得让卡片重新量一次高度，否则新文字被裁（见 Card.fit_to_content）
            self.verdict_card.fit_to_content()

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

    def _fill_info(self):
        try:
            self._fill_info_impl()
        finally:
            self.info_card.fit_to_content()

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
                     fg=P.TEXT, font=self.f_h2, anchor="w").pack(side="left")
            if q[0] in ("poor", "fair"):
                tk.Label(head, text="  " + q[1], bg=P.CARD,
                         fg=P.POOR if q[0] == "poor" else P.TEXT_3,
                         font=self.f_tiny).pack(side="left")

            # 「分辨率 · 格式 · 大小 · 时间 · 质量 · 清晰度」合并成一行。
            # NIQE 是后台异步算的：先渲染不带它的整行文本（`base`），
            # 算完由 _niqe_done 把「NIQE 那段」拼回去 —— 所以要把 base 留着。
            f1 = ["分辨率 %s" % (("%d × %d（%.1f MP）"
                                  % (wh[0], wh[1], imgsize.megapixels(wh)))
                                 if wh else "解析不了（%s）" % m["format"]),
                  "%s · %s" % (m["format"], human_size(m["size"])),
                  human_time(m["mtime"]),
                  "质量 %s" % q[1]]
            base = " · ".join(f1)
            nq = self.niqec.cached(path)
            lbl2 = tk.Label(box,
                            text=base if nq is None else
                            "%s · %s" % (base, niqe_field(path, nq)),
                            bg=P.CARD, fg=P.TEXT_2,
                            font=self.f_small, anchor="w", justify="left",
                            wraplength=S(780))
            lbl2.pack(fill="x")
            # 回填时要用到「质量」那半句和整行模板，一起记下来
            self._niqe_lbl[col] = (path, lbl2, q[1], base)
            if nq is None:
                self.niqec.request(path,
                                   lambda p, res, c=col: self._niqe_done(c, p, res))
            tk.Label(box, text=m["dir"], bg=P.CARD, fg=P.TEXT_3,
                     font=self.f_tiny, anchor="w", justify="left",
                     wraplength=S(440)).pack(fill="x")

    def _niqe_done(self, col, path, res):
        """NIQE 算好了，把信息卡里那一段换掉。

        只在「这一侧还是当时那张图」时才动它 —— 用户可能已经点到别的图上了，
        那时候旧结果必须丢掉，否则会张冠李戴。
        行合并后这一行还背着「分辨率 · 格式 · 大小 · 时间 · 质量」，
        所以要拿存下来的整行模板把文本**整体**重写。
        """
        got = self._niqe_lbl.get(col)
        if not got or got[0] != path:
            return
        _path, lbl, _quality, base = got
        try:
            if not lbl.winfo_exists():
                return
            lbl.configure(text="%s · %s" % (base, niqe_field(path, res)))
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # 删除 / 清理完全重复
    # ------------------------------------------------------------------
    def _need(self, path):
        if path and os.path.isfile(path):
            return True
        messagebox.showinfo("没有这张图", "先在左边选一组。")
        return False

    def delete_side(self, col):
        p = self.path_a if col == 0 else self.path_b
        if not p:
            self._toast("这一侧没有图")
            return
        if not os.path.isfile(p):
            self._toast("文件已经不在了")
            return
        dest = quarantine.quarantine_dir_for(p, self.roots)
        ok = messagebox.askyesno(
            "移到隔离文件夹",
            "把这张图移走？\n\n%s\n\n隔离位置：\n%s\n\n"
            "（是**移动**不是删除 —— 想还原就把文件从隔离夹拖回去；"
            "以后扫描会自动跳过「%s」）"
            % (p, dest, quarantine.QUARANTINE_NAME),
            icon="warning", default="no")
        if ok:
            n = self.delete_paths([p])
            if n:
                self._toast("已移入隔离夹 · %s" % os.path.basename(dest))

    def delete_paths(self, paths):
        """把一批图移进隔离夹，并把它们从所有内存结构里摘干净。"""
        paths = [p for p in dict.fromkeys(paths) if p and os.path.isfile(p)]
        if not paths:
            return 0
        done, failed = quarantine.isolate_many(
            paths, self.roots,
            on_progress=lambda d, t: self._progress(0.6 * d / max(1, t),
                                                    "移入隔离夹 %d/%d" % (d, t)))
        gone = set()
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
        lines = []
        for keep, drop, _f in plan[:10]:
            m = META.of(keep)
            wh = m["wh"]
            lines.append("保留 %s%s\n   移走 %s" % (
                short_name(m["name"], 30),
                ("（%d × %d）" % wh) if wh else "",
                "、".join(short_name(os.path.basename(p), 24) for p in drop)))
        if len(plan) > 10:
            lines.append("……还有 %d 组" % (len(plan) - 10))
        ok = messagebox.askyesno(
            "清理完全重复的图片",
            "找到 %d 组**内容完全相同**的图（文件哈希一致，100%% 相同），"
            "共可移走 %d 张、省出 %s。\n\n每组保留分辨率最高 / 文件最大的那张：\n\n%s\n\n"
            "被移走的会进各自的「%s」隔离夹（移动，不是删除）。继续吗？"
            % (len(plan), total_drop, human_size(freed), "\n".join(lines),
               quarantine.QUARANTINE_NAME),
            icon="warning", default="no")
        if not ok:
            self.stat.configure(text="已取消清理")
            return
        drops = [p for _k, d, _f in plan for p in d]
        n = self.delete_paths(drops)
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
        self._update_verdict(self.pair)
        self._fill_info()
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
