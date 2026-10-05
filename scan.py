# -*- coding: utf-8 -*-
"""扫描 + 分组引擎：把一堆图片按「相似」分门别类。

相似的定义（一条主通道 + 一道闸门，全部由数据定标）：

  闸门（颜色箱集合）
    crop 出来的图，用到的颜色必然是父图的子集；两块不同裁剪如果同源，
    颜色集合的交集也很大。用 12^3=1728 个颜色箱的**位图**做集合运算，
    几个位运算就能把绝大多数无关配对判掉。实测：同组召回 168/168，
    异组只留 151/1485（10%）。

  主通道（区域像素相关）
    把图归一到一个 64 长边的工作网格（保持长宽比）建积分图；
    在网格上撒一批候选「区域」（17 档长宽比 x 几档缩放 x 九宫格偏移），
    每个区域算一个 64 位 DCT 指纹。比较时只在**同一长宽比档位内**两两比，
    取指纹最近的若干对，再用**像素级 Pearson 相关**判定。

为什么要两级：64 位哈希会偶然撞车（实测不同图案也能撞到距离 8），
所以哈希只负责「提出候选」，判定必须落到像素上。
反过来，只有像素相关也不够 —— 平坦区域之间天然的相关系数就很高，
所以必须排除面积过小 / 对比度过低的区域。

定标数据（见 tune_all.py，82 张合成图 / 3321 对，含 168 同源裁剪 + 168 跨图案裁剪 + 24 同场景 + 2961 反例）：

  闸门 G>=0.60 且 相关>=0.93 且 哈希<=18
    -> 逐对命中 175 / 360 正例，**跨图案误报 0**
    -> 分组级：完整组 2/8、被拆 6、跨图案污染 0

  这里有个反直觉的点：**哈希上限不是越紧越好**。只取 G>=0.60 / C>=0.86 / H<=10
  只能命中 162 条，而放到 H<=18 能命中 175 条 —— 因为裁剪不改变画面结构，
  相关系数常常是 1.000，真正拦住它们的是「指纹距离」（缩放会让高频系数漂移）。
  哈希的作用是「提出候选」，不是「否决」；否决交给像素相关和闸门。
  H 再往上放到 20 才开始出现跨图案污染，所以停在 18。
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor

import crops
import imgsize
import quarantine
import winimg

CACHE_VERSION = 4          # 打包格式变了就 +1，旧缓存自动失效

# ---- 判定参数（由 tune_all.py 标定，改动请重跑那个脚本）----
DECODE = 128               # 解码尺寸：长边不超过它
TOPK = 3                   # 每个图像对取指纹最近的几个候选区域对
SLACK = 2                  # 允许比最近距离大多少（等效于并列）

# 区域面积占比下限：太小的区域相关系数不可信（假匹配实测都是小区域）
MIN_AREA = 0.15

UI_STRONG = "strong"       # 同源 / 裁剪
UI_LOOSE = "loose"         # 疑似同场景、不同构图

# 界面上的「灵敏度」档。**标准档是标定出来的**（见 tune_all.py / 模块开头那段），
# 另外两档是围绕标准档收紧 / 放松，标定脚本里也一并评估，不是随手填的数。
PRESETS = {
    "strict": {
        "label": "严格（宁可漏、不要误报）",
        "gate": 0.70, "corr_strong": 0.93, "hash_max": 18,
        "corr_loose": 0.90, "hash_loose": 22,
    },
    "standard": {
        "label": "标准（推荐，已用 82 张样本标定）",
        "gate": 0.60, "corr_strong": 0.93, "hash_max": 18,
        "corr_loose": 0.86, "hash_loose": 22,
    },
    "loose": {
        "label": "宽松（多找同场景，可能夹带误报）",
        "gate": 0.60, "corr_strong": 0.90, "hash_max": 20,
        "corr_loose": 0.80, "hash_loose": 24,
    },
}
DEFAULT_PRESET = "standard"

_std = PRESETS[DEFAULT_PRESET]
# 模块级常量保留成「标准档」的值，方便零散引用（测试里也是这么用的）
GATE = _std["gate"]
CORR_STRONG = _std["corr_strong"]
HASH_MAX = _std["hash_max"]
CORR_LOOSE = _std["corr_loose"]
HASH_LOOSE = _std["hash_loose"]


def thresholds(preset: str = DEFAULT_PRESET) -> dict:
    """按名字取档位参数，名字不对就退回标准档（不抛异常）。"""
    return PRESETS.get(preset) or PRESETS[DEFAULT_PRESET]


# ---------------------------------------------------------------------------
# 找图
# ---------------------------------------------------------------------------

def iter_images(roots, recursive: bool = True, on_skip=None):
    """把目录（或文件）展开成图片路径列表。

    先按扩展名粗筛，再按**文件头**确认真实格式 —— 有些「图片」其实是
    改名的其它文件，有些图片没有扩展名。
    """
    out = []
    seen = set()
    for root in roots:
        if os.path.isfile(root):
            cands = [root]
        else:
            cands = []
            if recursive:
                for dirpath, dirnames, filenames in os.walk(root):
                    # `_隔离` 是「删除」的去处，扫的时候必须跳过 ——
                    # 否则删完再扫一遍，那些图又回来了。
                    dirnames[:] = [d for d in dirnames
                                   if not d.startswith(".")
                                   and d not in ("__pycache__", "$RECYCLE.BIN",
                                                 "System Volume Information",
                                                 quarantine.QUARANTINE_NAME)]
                    for fn in filenames:
                        cands.append(os.path.join(dirpath, fn))
            else:
                try:
                    for fn in os.listdir(root):
                        p = os.path.join(root, fn)
                        if os.path.isfile(p):
                            cands.append(p)
                except OSError:
                    continue
        for p in cands:
            ap = os.path.abspath(p)
            if ap in seen:
                continue
            seen.add(ap)
            ext = os.path.splitext(ap)[1].lower()
            if ext in imgsize.NOT_IMAGE:
                if on_skip:
                    on_skip(ap, "扩展名排除")
                continue
            if ext not in imgsize.IMG_EXT:
                # 没见过的扩展名：看看文件头像不像图片
                if not imgsize.is_image(ap):
                    if on_skip:
                        on_skip(ap, "不是图片")
                    continue
            out.append(ap)
    return out


# ---------------------------------------------------------------------------
# 指纹缓存（落盘成 JSON，可以自己打开看）
# ---------------------------------------------------------------------------

def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _unb64(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


def pack_desc(desc) -> dict:
    """把描述子压成可 JSON 存的形式。

    rects 每个 5 字节（4 个坐标都是 0..64、档位 0..16，都塞得进一个字节），
    灰度图先 zlib 再 base64。指纹拼成一条十六进制串。
    """
    rb = bytearray()
    for r in desc["rects"]:
        rb += bytes((r[0] & 255, r[1] & 255, r[2] & 255, r[3] & 255,
                     r[4] & 255))
    return {
        "gw": desc["gw"], "gh": desc["gh"],
        "g": _b64(zlib.compress(desc["gray"], 6)),
        "r": _b64(bytes(rb)),
        "hh": "".join("%016x" % h for h in desc["hashes"]),
        "h": desc["hist"],
        "cs": "%x" % desc["cset"],
    }


def unpack_desc(d: dict):
    """从缓存条目还原描述子。"""
    gw, gh = int(d["gw"]), int(d["gh"])
    rb = _unb64(d["r"])
    rects = []
    for i in range(0, len(rb), 5):
        rects.append((rb[i], rb[i + 1], rb[i + 2], rb[i + 3], rb[i + 4]))
    hh = d["hh"]
    hashes = [int(hh[i:i + 16], 16) for i in range(0, len(hh), 16)]
    buckets = {}
    for k, r in enumerate(rects):
        buckets.setdefault(r[4], []).append(k)
    return {
        "gw": gw, "gh": gh,
        "gray": zlib.decompress(_unb64(d["g"])),
        "rects": rects, "hashes": hashes, "buckets": buckets,
        "phash": hashes[0] if hashes else 0,
        "hist": d["h"], "cset": int(d["cs"], 16),
    }


class FingerStore:
    """路径 -> 描述子 的磁盘缓存。

    key 里带上 mtime / 文件大小 / 解码尺寸 / 缓存版本 —— 任何一个变了就重算，
    免得把改过的图片当成老的指纹用。
    """

    def __init__(self, path: str):
        self.path = path
        self.items = {}
        self.dirty = False
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        if data.get("v") != CACHE_VERSION or data.get("decode") != DECODE:
            return
        self.items = data.get("items", {})

    def key_ok(self, path: str):
        it = self.items.get(path)
        if not it:
            return None
        try:
            st = os.stat(path)
        except OSError:
            return None
        if (it.get("m") != int(st.st_mtime) or it.get("s") != st.st_size):
            return None
        try:
            return unpack_desc(it["d"])
        except (KeyError, ValueError, zlib.error):
            return None

    def put(self, path: str, desc):
        try:
            st = os.stat(path)
            m, s = int(st.st_mtime), st.st_size
        except OSError:
            m, s = 0, 0
        self.items[path] = {"m": m, "s": s, "d": pack_desc(desc)}
        self.dirty = True

    def prune(self, alive: set):
        dead = [k for k in self.items if k not in alive]
        for k in dead:
            self.items.pop(k, None)
        if dead:
            self.dirty = True
        return len(dead)

    def save(self, force: bool = False):
        if not self.dirty and not force:
            return False
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"v": CACHE_VERSION, "decode": DECODE,
                           "items": self.items}, f)
            os.replace(tmp, self.path)
            self.dirty = False
            return True
        except OSError:
            return False


def default_cache_path() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "ImageScout", "finger-cache.json")


# ---------------------------------------------------------------------------
# 扫描
# ---------------------------------------------------------------------------

def describe_paths(paths, workers: int = 4, on_progress=None, should_stop=None,
                   store: FingerStore | None = None):
    """批量算描述子。返回 ({path: desc}, 统计)。

    先用缓存，缺的才真解码。解码走线程池（COM 每个线程都会自己初始化），
    但结果按路径回填，所以**多线程不影响结果**。
    """
    stats = {"total": len(paths), "cached": 0, "decoded": 0, "failed": 0}
    descs = {}
    todo = []
    if store is not None:
        for p in paths:
            got = store.key_ok(p)
            if got is not None:
                descs[p] = got
                stats["cached"] += 1
            else:
                todo.append(p)
    else:
        todo = list(paths)

    done = len(descs)
    if on_progress:
        on_progress(done, stats["total"])

    lock = threading.Lock()

    def work(p):
        got = winimg.load_pixels(p, DECODE)
        if not got:
            return p, None
        w, h, bgra = got
        try:
            return p, crops.describe(w, h, bgra)
        except (ValueError, IndexError):
            return p, None

    if todo:
        nw = max(1, min(int(workers or 1), len(todo)))
        with ThreadPoolExecutor(max_workers=nw) as ex:
            for p, d in ex.map(work, todo):
                if should_stop and should_stop():
                    break
                with lock:
                    if d is None:
                        stats["failed"] += 1
                    else:
                        descs[p] = d
                        stats["decoded"] += 1
                        if store is not None:
                            store.put(p, d)
                    done += 1
                if on_progress:
                    on_progress(done, stats["total"])
    return descs, stats


# ---------------------------------------------------------------------------
# 配对
# ---------------------------------------------------------------------------

def gate_score(da, db) -> float:
    """候选闸门：颜色箱集合的 max(Jaccard, 互相覆盖率)。"""
    a, b = da["cset"], db["cset"]
    if not a or not b:
        return 0.0
    inter = (a & b).bit_count()
    return max(inter / float((a | b).bit_count() or 1),
               inter / float(a.bit_count()),
               inter / float(b.bit_count()))


def compare_pair(da, db, topk: int = TOPK, th: dict | None = None):
    """判定两个描述子。返回 (kind, score, matched_hash, rectA, rectB) 或 None。

    先在指纹最近的前 topk 个候选区域对里挑**相关最高**的那一对 ——
    注意不是挑「指纹最近」的那一对：实测指纹最近的那对经常是假的。
    th 是档位参数（scan.thresholds()），不传就用标准档。
    """
    th = th or thresholds()
    _, cand = crops.pair_region_stats(da, db, topk, SLACK, min_area=MIN_AREA)
    best = (0.0, 99, 0, 0)
    for d, i, j in cand:
        va, _ = crops.region_ver(da, da["rects"][i])
        vb, _ = crops.region_ver(db, db["rects"][j])
        c = crops.corr(va, vb)
        if c > best[0]:
            best = (c, d, i, j)
    c, d, i, j = best
    if c >= th["corr_strong"] and d <= th["hash_max"]:
        return (UI_STRONG, c, d, i, j)
    if c >= th["corr_loose"] and d <= th["hash_loose"]:
        return (UI_LOOSE, c, d, i, j)
    return None


def find_links(descs, on_progress=None, should_stop=None, preset: str = DEFAULT_PRESET):
    """两两比较，返回 [(pathA, pathB, kind, score, hash, i, j)]。

    闸门不通过的直接跳过 —— 这是整个扫描能跑得动的原因。
    """
    th = thresholds(preset)
    gate = th["gate"]
    paths = list(descs)
    n = len(paths)
    total = n * (n - 1) // 2
    links = []
    done = 0
    last = 0.0
    for ai in range(n):
        a = paths[ai]
        da = descs[a]
        for bi in range(ai + 1, n):
            b = paths[bi]
            done += 1
            if should_stop and should_stop():
                return links
            if gate and gate_score(da, descs[b]) < gate:
                continue
            got = compare_pair(da, descs[b], th=th)
            if got:
                kind, score, d, i, j = got
                links.append((a, b, kind, score, d, i, j))
        now = time.time()
        if on_progress and (now - last > 0.05 or ai == n - 1):
            last = now
            on_progress(done, total)
    return links


# ---------------------------------------------------------------------------
# 分组
# ---------------------------------------------------------------------------

class UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        p = self.parent
        if x not in p:
            p[x] = x
            return x
        root = x
        while p[root] != root:
            root = p[root]
        while p[x] != root:          # 路径压缩
            p[x], x = root, p[x]
        return root

    def union(self, x, y):
        rx, ry = self.find(x), self.find(y)
        if rx != ry:
            self.parent[rx] = ry
            return True
        return False


def build_groups(descs, links):
    """把相似关系连成组。

    返回 (groups, weak_pairs)：
      groups      [{'members', 'strong_links', 'weak_links', 'best', ...}]
                  只含 >=2 张的组，且**只用强边**连。
      weak_pairs  未并进任何组的宽松边 [(a, b, kind, score, hash, i, j)]

    为什么分组只用强边：并查集有传递性，宽松边会把「A像B、B像C」串成
    「A像C」—— 实测用宽松边连会把 wave / blobs / dots / stripes 串成一个
    36 张的跨类大组。强边定标过跨类误报为 0，只用它就干净。
    宽松边不丢，单独交给界面显示成「疑似」，让用户自己判断。
    """
    uf = UnionFind()
    for a, b, kind, score, d, i, j in links:
        if kind == UI_STRONG:
            uf.union(a, b)

    inside = {}
    for l in links:
        r = uf.find(l[0])
        if r == uf.find(l[1]):
            inside.setdefault(r, []).append(l)

    groups = []
    for root, ls in inside.items():
        members = sorted({x for a, b, *_ in ls for x in (a, b)})
        if len(members) < 2:
            continue
        strong = [x for x in ls if x[2] == UI_STRONG]
        weak = [x for x in ls if x[2] == UI_LOOSE]
        groups.append({
            "members": members,
            "strong_links": strong,
            "weak_links": weak,
            "kind": UI_STRONG if strong else UI_LOOSE,
            "best": max(x[3] for x in ls),
            "best_strong": max((x[3] for x in strong), default=0.0),
        })
    groups.sort(key=lambda g: (-g["best_strong"], -len(g["members"])))

    weak_pairs = [l for l in links
                  if l[2] == UI_LOOSE and uf.find(l[0]) != uf.find(l[1])]
    weak_pairs.sort(key=lambda l: -l[3])
    return groups, weak_pairs


def neighbors_of(links, path):
    """某张图关联的所有相似图及分数，按分数降序。"""
    out = []
    for a, b, kind, score, d, i, j in links:
        if a == path:
            out.append((b, kind, score))
        elif b == path:
            out.append((a, kind, score))
    out.sort(key=lambda t: -t[2])
    return out
