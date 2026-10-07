# -*- coding: utf-8 -*-
"""裁剪不变 + 同场景相似的描述子。

问题：同一张图被裁成不同比例（4:3 / 1:1 / 16:9），普通感知哈希认不出来 ——
pHash 描述的是「整张图长什么样」，裁掉一圈后构图变了，哈希跟着变。

做法（全部建立在「64 长边的工作网格 + 积分图」上）：
  1. build_grid：BGRA -> (gw, gh, 灰度扁平数组 + 积分图)。
     工作网格保持长宽比；积分图让任意矩形的面积平均变成 O(1)。
  2. 撒区域：在一张图的网格上生成一批候选「区域」——
     长宽比（自身 + 1:1 / 4:3 / 3:4 / 16:9 / 9:16）
     x 缩放（1.0 / 0.72 / 0.52）
     x 位置（横向纵向各取 0 / 半 / 满 三个偏移 -> 九宫格）。
     每个区域算一个 64 位 DCT 指纹。
  3. 匹配：A 的「整图」指纹 与 B 的任意区域指纹 距离小 => A 是 B 的裁剪（或两者同源）。
     反过来同理，两个方向都要查。
  4. 验证：命中的区域对做**像素级 Pearson 相关**，杀掉哈希层面的偶然碰撞。
     这一步是关键 —— 光看 64 位哈希，不同图案也会偶尔撞到很近的距离。
  5. 同场景不同构图：颜色直方图 + 粗桶键，单独一条弱通道。

坐标一律是「工作网格坐标」，不是原图像素坐标。
"""

from __future__ import annotations

import math
from operator import mul

__all__ = [
    "describe", "hamming64", "window_list", "region_gray", "corr",
    "hist_distance", "hist_sim", "color_sample", "color_hist",
    "cset_jaccard", "cset_cover", "snap_aspect", "pair_region_stats",
    "LADDER", "WORK", "HASH_N", "HASH_KEEP", "VER_N", "CSET_BINS",
    "HIST_BINS", "CENTER_SCALES", "POS_SCALES", "POS_STEPS",
]

WORK = 64          # 工作网格长边
HASH_N = 16        # 算指纹时把区域归一到的灰度边长
HASH_KEEP = 8      # 保留的低频系数边长 -> 8x8 = 64 位指纹
VER_N = 24         # 验证级把区域归一到的灰度边长
CSET_BINS = 12     # 颜色箱集合：每通道档数（12^3 = 1728 箱），闸门用
HIST_BINS = 6      # 颜色直方图：每通道档数（6^3 = 216 箱），场景通道用

# ---------------------------------------------------------------------------
# 长宽比阶梯 + 区域集
# ---------------------------------------------------------------------------
# 裁剪**不会拉伸画面** —— 两张同源图的内容之间永远是「等比缩放 + 平移」。
# 所以两块裁剪的**重叠矩形**的长宽比可以是任意值（既不等于 A 的、也不等于 B 的），
# 想让区域集覆盖到它，长宽比就必须是**密梯**而不是几个标准值。
LADDER = tuple(2.0 ** (k / 8.0) for k in range(-8, 9))     # 0.5 .. 2.0，17 档
_LADDER_LOG = tuple(math.log(a) for a in LADDER)

CENTER_SCALES = (1.0, 0.8, 0.62, 0.48)    # 居中区域：密梯，覆盖「居中裁剪」
POS_SCALES = (1.0, 0.62)                  # 带位置的区域：只对常用比例撒位置
POS_ASPECTS = (1.0, 4.0 / 3, 3.0 / 2, 16.0 / 9,
               3.0 / 4, 2.0 / 3, 9.0 / 16)
POS_STEPS = 3

MIN_SIDE = 8       # 区域短边下限（工作网格单位）

# 平坦区域的相关系数毫无意义 —— 两块接近纯色（或近线性渐变）的区域，
# 像素值几乎共线，Pearson 相关能轻松上 0.99，但它证明不了「内容相同」。
# 实测过：不加这道闸，不同图案的图之间会冒出 corr 0.997 的假匹配。
FLAT_STD = 12.0        # 区域灰度标准差下限（灰度 0..255）
MIN_AREA = 0.05        # 区域面积占工作网格的比例下限


def snap_aspect(a: float) -> int:
    """把一个长宽比吸附到 LADDER 上，返回档位下标。

    所有区域都按「阶梯档位」生成（不是按图像自身比例），
    这样同一个档位里的区域长宽比严格一致，比较起来才公平。
    """
    la = math.log(max(1e-6, a))
    best, bi = 1e9, 0
    for i, lv in enumerate(_LADDER_LOG):
        d = abs(la - lv)
        if d < best:
            best, bi = d, i
    return bi


# ---------------------------------------------------------------------------
# 汉明距离：查表比 bin(x).count('1') 快好几倍
# ---------------------------------------------------------------------------

_POP = bytes(bin(i).count("1") for i in range(65536))
_MASK = (1 << 64) - 1


def hamming64(a: int, b: int) -> int:
    x = (a ^ b) & _MASK
    return (_POP[x & 0xFFFF] + _POP[(x >> 16) & 0xFFFF]
            + _POP[(x >> 32) & 0xFFFF] + _POP[(x >> 48) & 0xFFFF])


# ---------------------------------------------------------------------------
# 16 点 DCT（只算前 HASH_KEEP 个频率）
# ---------------------------------------------------------------------------

def _make_cos(n=HASH_N, keep=HASH_KEEP):
    cos = [[math.cos((2 * y + 1) * v * math.pi / (2 * n)) for y in range(n)]
           for v in range(keep)]
    norm = [math.sqrt(1.0 / n) if u == 0 else math.sqrt(2.0 / n)
            for u in range(keep)]
    return cos, norm


_COS, _NORM = _make_cos()


def dct_hash16(gray) -> int:
    """HASH_N x HASH_N 灰度（二维列表）-> 64 位指纹。

    DCT 取左上 HASH_KEEP x HASH_KEEP 低频；丢掉 DC 分量（整体亮度不该影响指纹）；
    与中位数比较出比特。

    ⚠️⚠️ **这是个逐位敏感的纯 Python 热点**（v1.9 优化，主人 2026-10-07
    报「加载 400 图片就会很慢很卡」）。cProfile 显示它独占 `describe`
    的 **65%**：每张图 139 个区域，每个区域 128 次长度 16 的点积，
    原来写成 `sum(a * b for a, b in zip(...))` —— 光生成器就有 **1800 万次**
    `__next__` 调用，全在 Python 层。

    改法（**结果逐位不变**，由 `probe_desc_opt.py` 的 E2/E3 逐位比对兜底）：
      · `sum(map(mul, x, y))` 代替 `sum(a*b for a,b in zip(x,y))` ——
        循环回到 C 层，不用建生成器、不用解包元组。
        两者**都走 `sum()` 的浮点补偿求和**，所以第一趟比特级相同。
      · `tmp` 改成 **v 为主序**（`tmpv[v][y]`），第二趟直接从列上取，
        省掉 `tmp[y][v]` 的双层下标。
      · ⚠️⚠️ **第二趟必须保留朴素的 `s += `**，不能图快也换成 `sum()`：
        CPython 3.12 起 `sum()` 对全 float 序列会走 **Neumaier 补偿求和**，
        累加舍入跟朴素循环**不一样**。第一版就踩了这个坑 ——
        均匀灰图（各项互相抵消到 1e-16 量级）上 55/400 个指纹变了，
        而真实照片上 5560 个区域恰好全都一致，光看真实图根本发现不了。
    """
    cos, norm = _COS, _NORM
    n, keep = HASH_N, HASH_KEEP

    # 可分离 DCT：先按行。tmpv[v][y] = Σ_x gray[y][x] * cos[v][x]
    tmpv = [[0.0] * n for _ in range(keep)]
    for y in range(n):
        gy = gray[y]
        for v in range(keep):
            tmpv[v][y] = sum(map(mul, gy, cos[v]))

    vals = []
    for u in range(keep):
        nu = norm[u]
        cu = cos[u]
        for v in range(keep):
            col = tmpv[v]
            s = 0.0
            # ⚠️ 朴素累加，不许换成 sum()（见上面 docstring 的 Neumaier 陷阱）
            for y in range(n):
                s += col[y] * cu[y]
            vals.append(s * nu * norm[v])

    vals[0] = 0.0                       # 丢掉 DC
    rest = vals[1:]
    median = sorted(rest)[len(rest) // 2]
    bits = 0
    for i, v in enumerate(vals):
        if v > median:
            bits |= (1 << i)
    return bits


# ---------------------------------------------------------------------------
# 工作网格 + 积分图
# ---------------------------------------------------------------------------

def build_grid(w: int, h: int, bgra: bytes):
    """BGRA -> (gw, gh, 灰度 bytes, 积分图 list)。

    长边归一到 WORK，长宽比保持。灰度用「面积平均」得到，比最近邻稳。
    积分图长度 (gw+1)*(gh+1)，行多 1 列多 1，让任意矩形求和变 O(1)。
    """
    if w >= h:
        gw = WORK
        gh = max(1, round(h * WORK / w))
    else:
        gh = WORK
        gw = max(1, round(w * WORK / h))

    gray = bytearray(gw * gh)
    for ty in range(gh):
        y0 = ty * h // gh
        y1 = max(y0 + 1, (ty + 1) * h // gh)
        base_out = ty * gw
        for tx in range(gw):
            x0 = tx * w // gw
            x1 = max(x0 + 1, (tx + 1) * w // gw)
            total = 0
            n = 0
            for y in range(y0, y1):
                base = y * w * 4
                for x in range(x0, x1):
                    i = base + x * 4
                    total += (bgra[i + 2] * 299 + bgra[i + 1] * 587
                              + bgra[i] * 114) // 1000
                    n += 1
            gray[base_out + tx] = (total // n) if n else 0

    I = _build_I(gray, gw, gh)
    return gw, gh, bytes(gray), I


def _build_I(gray, gw, gh):
    """由扁平灰度建积分图（长度 (gw+1)*(gh+1)）。"""
    stride = gw + 1
    I = [0] * ((gh + 1) * stride)
    for y in range(gh):
        base = (y + 1) * stride
        prev = y * stride
        row = y * gw
        acc = 0
        for x in range(gw):
            acc += gray[row + x]
            I[base + x + 1] = I[prev + x + 1] + acc
    return I


def _rect_sum(I, stride, x0, y0, x1, y1) -> int:
    return (I[y1 * stride + x1] - I[y0 * stride + x1]
            - I[y1 * stride + x0] + I[y0 * stride + x0])


def region_gray(I, stride, gw, gh, rect, n=None):
    """从积分图取一个矩形，面积平均成 n x n 灰度（返回扁平 list + n）。

    坐标夹到网格范围内；空矩形返回 (None, n)。

    ⚠️ v1.9 优化（占 `describe` 的 24%，cProfile 里 213 万次 `_rect_sum`）：
      · `_rect_sum` **内联**进循环 —— 那是个 4 项加法，函数调用开销比算术本身还大；
      · `xa/xb` 只跟 `(n, x0, x1)` 有关，**提到 ty 循环外面**算一次
        （原来每个 ty 都重算一遍 n 个 tx 的边界）；
      · `yb = max(ya+1, ...)` 的夹取也提前算。
    数值逐位不变，由 `probe_desc_opt.py` 的 E1/E3 逐元素比对兜底。
    """
    n = HASH_N if n is None else n
    x0 = max(0, min(int(rect[0]), gw - 1))
    y0 = max(0, min(int(rect[1]), gh - 1))
    x1 = max(x0 + 1, min(int(rect[2]), gw))
    y1 = max(y0 + 1, min(int(rect[3]), gh))

    dx = x1 - x0
    dy = y1 - y0
    xa_l = [x0 + dx * t // n for t in range(n)]
    xb_l = [max(xa_l[t] + 1, x0 + dx * (t + 1) // n) for t in range(n)]
    cnt_l = [xb_l[t] - xa_l[t] for t in range(n)]

    out = [0] * (n * n)
    for ty in range(n):
        ya = y0 + dy * ty // n
        yb = max(ya + 1, y0 + dy * (ty + 1) // n)
        bb = yb * stride
        ba = ya * stride
        band_h = yb - ya
        o = ty * n
        for tx in range(n):
            xa = xa_l[tx]
            xb = xb_l[tx]
            # == _rect_sum(I, stride, xa, ya, xb, yb)，内联
            s = I[bb + xb] - I[ba + xb] - I[bb + xa] + I[ba + xa]
            cnt = cnt_l[tx] * band_h
            out[o + tx] = s // cnt if cnt else 0
    return out, n


# ---------------------------------------------------------------------------
# 区域生成
# ---------------------------------------------------------------------------

def _offsets(slack: float, steps: int = POS_STEPS):
    """在一个有 slack 的自由度上取 steps 个等距偏移（贴左 / 居中 / 贴右 ...）。"""
    if slack <= 0.5:
        return (0.0,)
    if steps <= 1:
        return (0.0,)
    return tuple(sorted({round(slack * i / (steps - 1), 3)
                         for i in range(steps)}))


def _max_box(gw: int, gh: int, a: float):
    """长宽比为 a 的矩形在工作网格里能放下的最大尺寸。"""
    own = gw / float(gh)
    if a >= own:
        return float(gw), gw / a
    return gh * a, float(gh)


def window_list(gw: int, gh: int, min_side: int = MIN_SIDE):
    """生成候选区域，返回 [(x0, y0, x1, y1, aspect_bucket)]。

    两层：
      居中密梯 —— 17 档长宽比 x 4 档缩放，全部居中。
        对应「同一个构图裁成不同比例」，这也是最常见的裁剪方式。
      位置网   —— 常用长宽比 x 2 档缩放 x 九宫格偏移。
        对应「裁的时候偏了 / 裁掉某一角」，居中区域盖不到。

    每个区域都带一个 aspect_bucket（LADDER 下标）——
    比较时只在**同一个档位内**两两比，省掉一大半无意义的配对。
    """
    rects = []
    seen = set()

    def emit(bucket: int, bw: float, bh: float, x0: float, y0: float):
        key = (int(round(x0)), int(round(y0)),
               int(round(x0 + bw)), int(round(y0 + bh)))
        if key[2] - key[0] < min_side or key[3] - key[1] < min_side:
            return
        key = key + (bucket,)
        if key in seen:
            return
        seen.add(key)
        rects.append(key)

    # 居中密梯
    for bi, a in enumerate(LADDER):
        bw0, bh0 = _max_box(gw, gh, a)
        for s in CENTER_SCALES:
            bw, bh = bw0 * s, bh0 * s
            emit(bi, bw, bh, (gw - bw) / 2.0, (gh - bh) / 2.0)

    # 位置网
    buckets = sorted({snap_aspect(a) for a in POS_ASPECTS}
                     | {snap_aspect(gw / float(gh))})
    for bi in buckets:
        a = LADDER[bi]
        bw0, bh0 = _max_box(gw, gh, a)
        for s in POS_SCALES:
            bw, bh = bw0 * s, bh0 * s
            for x0 in _offsets(gw - bw, POS_STEPS):
                for y0 in _offsets(gh - bh, POS_STEPS):
                    emit(bi, bw, bh, x0, y0)
    return rects


# ---------------------------------------------------------------------------
# 同场景签名：颜色直方图
# ---------------------------------------------------------------------------

def color_sample(w: int, h: int, bgra: bytes, bins: int = 4):
    """RGB 直方图抽样。返回 (counts, mask)。

    mask 是「这个颜色箱里有没有像素」的位图（Python 大整数）——
    crop 出来的图，它用到的颜色必然是父图的子集，所以拿 mask 的集合关系
    当**候选闸门**极便宜：任意两张图只要几个位运算就能判掉。
    """
    total = w * h
    step = max(1, int((total / 16384.0) ** 0.5))
    cnt = [0] * (bins ** 3)
    for y in range(0, h, step):
        base = y * w * 4
        for x in range(0, w, step):
            i = base + x * 4
            # 用 (v * bins) >> 8 而不是右移常数 —— bins 不是 2 的幂时才不会塌成 8 档
            r = (bgra[i + 2] * bins) >> 8
            g = (bgra[i + 1] * bins) >> 8
            b = (bgra[i] * bins) >> 8
            cnt[(r * bins + g) * bins + b] += 1
    mask = 0
    for i, v in enumerate(cnt):
        if v:
            mask |= (1 << i)
    return cnt, mask


def color_hist(w: int, h: int, bgra: bytes, bins: int = 4):
    """4x4x4 = 64 维 RGB 直方图（总量归一化到 1000）。

    对构图变化不敏感、对「是不是同一场景」敏感 —— 专门抓连拍 / 换角度。
    """
    cnt, _ = color_sample(w, h, bgra, bins)
    n = sum(cnt)
    if n:
        inv = 1000.0 / n
        cnt = [int(v * inv) for v in cnt]
    return cnt


def cset_jaccard(a: int, b: int) -> float:
    """两个颜色箱集合的 Jaccard 相似度（0..1）。

    「两块不同的裁剪、但都来自同一张原图」时，两边的颜色集合都接近原图的子集，
    交集大、并集也不离谱 —— 这是抓「同源不同裁剪」最便宜的一步。
    """
    if not a or not b:
        return 0.0
    inter = (a & b).bit_count()
    union = (a | b).bit_count()
    return inter / union if union else 0.0


def cset_cover(a: int, b: int) -> float:
    """b 的颜色箱有多大比例被 a 覆盖（用于「b 是不是 a 的裁剪」）。

    crop 出来的图颜色是父图的子集，所以 cover 会接近 1。
    """
    if not b:
        return 0.0
    return (a & b).bit_count() / float(b.bit_count())


def hist_distance(a, b) -> int:
    """直方图 L1 距离（两边和都是 1000，范围 0..2000）。"""
    return sum(abs(x - y) for x, y in zip(a, b))


def hist_sim(a, b) -> float:
    """直方图交集（0..1，越大越像）。

    比 L1 距离稳：L1 会被「某个箱差得多」主导，交集看的是「有多少比例的颜色
    是共有的」。同场景不同构图时，共有颜色的比例仍然很高。
    """
    if not a or not b:
        return 0.0
    s = 0
    for x, y in zip(a, b):
        s += x if x < y else y
    return s / 1000.0


# ---------------------------------------------------------------------------
# 一次性描述
# ---------------------------------------------------------------------------

def describe(w: int, h: int, bgra: bytes, wins=None):
    """返回一张图的全部描述子。解码失败的图不要传进来。

    返回字典：
      gw / gh        工作网格尺寸
      gray           工作网格灰度（bytes，长度 gw*gh）—— 验证时从这里取区域
      rects          区域列表，每项 (x0,y0,x1,y1,bucket)；rects[0] 恒为整图
      hashes         与 rects 平行的 64 位指纹（hashes[0] = 整图指纹）
      buckets        {aspect_bucket: [rects 下标...]}，比较时按桶对桶
      hist           颜色直方图（HIST_BINS^3，归一化到 1000）
      cset           颜色箱集合位图（闸门用）
    """
    gw, gh, gray, I = build_grid(w, h, bgra)
    stride = gw + 1

    own_bucket = snap_aspect(gw / float(gh))
    rects = [(0, 0, gw, gh, own_bucket)]
    for r in (window_list(gw, gh) if wins is None else wins):
        if (r[0], r[1], r[2], r[3]) != (0, 0, gw, gh):
            rects.append(r)

    hashes = []
    buckets = {}
    for k, r in enumerate(rects):
        g, _ = region_gray(I, stride, gw, gh, r, HASH_N)
        hashes.append(dct_hash16([g[i * HASH_N:(i + 1) * HASH_N]
                                  for i in range(HASH_N)]))
        buckets.setdefault(r[4], []).append(k)

    # 两种颜色描述各司其职：
    #   cset（HIST_BINS 档，箱数多）-> 候选闸门，靠「集合包含关系」判
    #   hist（HIST_BINS2 档，箱数少）-> 场景通道，靠直方图交集判
    cnt_big, cset = color_sample(w, h, bgra, CSET_BINS)
    cnt, _ = color_sample(w, h, bgra, HIST_BINS)
    n = sum(cnt)
    hist = [int(v * 1000.0 / n) for v in cnt] if n else cnt
    return {
        "gw": gw, "gh": gh, "gray": gray,
        "rects": rects, "hashes": hashes, "buckets": buckets,
        "phash": hashes[0], "hist": hist, "cset": cset,
    }


def pair_region_stats(desc_a, desc_b, topk: int = 3, slack: int = 2,
                      min_area: float = MIN_AREA):
    """两张图之间「区域指纹」的最近配对。

    只在**同一个 aspect 档位内**两两比（不同档位的区域本来就对不上，白算）。
    面积太小的区域直接跳过 —— 小块的相关系数噪声太大。
    先扫一遍拿最小距离，再把距离 <= 最小+slack 的前 topk 对取出来。

    返回 (最小距离, [(距离, a下标, b下标), ...])
    """
    ha, hb = desc_a["hashes"], desc_b["hashes"]
    ra, rb = desc_a["rects"], desc_b["rects"]
    area_a = desc_a["gw"] * desc_a["gh"]
    area_b = desc_b["gw"] * desc_b["gh"]

    keep_a = {i for i, r in enumerate(ra)
              if (r[2] - r[0]) * (r[3] - r[1]) >= min_area * area_a} if min_area else None
    keep_b = {j for j, r in enumerate(rb)
              if (r[2] - r[0]) * (r[3] - r[1]) >= min_area * area_b} if min_area else None

    dmin = 99
    for bucket, idxs_a in desc_a["buckets"].items():
        idxs_b = desc_b["buckets"].get(bucket)
        if not idxs_b:
            continue
        for i in idxs_a:
            if keep_a is not None and i not in keep_a:
                continue
            x = ha[i]
            for j in idxs_b:
                if keep_b is not None and j not in keep_b:
                    continue
                d = (x ^ hb[j]).bit_count()
                if d < dmin:
                    dmin = d
    if dmin == 99:
        return 99, []

    lim = dmin + slack
    cand = []
    for bucket, idxs_a in desc_a["buckets"].items():
        idxs_b = desc_b["buckets"].get(bucket)
        if not idxs_b:
            continue
        for i in idxs_a:
            if keep_a is not None and i not in keep_a:
                continue
            x = ha[i]
            for j in idxs_b:
                if keep_b is not None and j not in keep_b:
                    continue
                d = (x ^ hb[j]).bit_count()
                if d <= lim:
                    cand.append((d, i, j))
    cand.sort()
    return dmin, cand[:topk]


# ---------------------------------------------------------------------------
# 验证级：从已存的工作网格灰度里取区域 + 相关系数
# ---------------------------------------------------------------------------

def grid_integral(desc):
    """懒建并缓存 desc 的积分图（验证阶段会反复取区域）。"""
    I = desc.get("_I")
    if I is None:
        I = _build_I(desc["gray"], desc["gw"], desc["gh"])
        desc["_I"] = I
        desc["_stride"] = desc["gw"] + 1
    return I, desc["_stride"]


def region_ver(desc, rect):
    """验证用的区域灰度：从 desc 的工作网格取 rect，面积平均成 VER_N x VER_N。"""
    I, stride = grid_integral(desc)
    vals, n = region_gray(I, stride, desc["gw"], desc["gh"], rect, VER_N)
    return vals, n


def corr(vals_a, vals_b) -> float:
    """Pearson 相关系数。任一边方差过小（纯色/纯渐变到几乎没结构）时返回 0。

    这是「哈希提出候选之后」的判据：哈希在 64 位上会偶然撞车，
    但像素级的相关系数不会 —— 结构不一样就是对不上。
    返回 -1..1，越接近 1 越像。
    """
    n = len(vals_a)
    if n == 0 or n != len(vals_b):
        return 0.0
    ma = sum(vals_a) / n
    mb = sum(vals_b) / n
    va = vb = cov = 0.0
    for a, b in zip(vals_a, vals_b):
        da = a - ma
        db = b - mb
        va += da * da
        vb += db * db
        cov += da * db
    if va <= (FLAT_STD ** 2) * n or vb <= (FLAT_STD ** 2) * n:
        return 0.0
    return cov / math.sqrt(va * vb)


def std_of(vals) -> float:
    n = len(vals)
    if not n:
        return 0.0
    m = sum(vals) / n
    return math.sqrt(sum((v - m) ** 2 for v in vals) / n)
