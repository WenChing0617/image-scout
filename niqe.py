# -*- coding: utf-8 -*-
"""NIQE（Natural Image Quality Evaluator）—— 无参考图像质量评价，**纯标准库**实现。

出处：Mittal, Soundararajan, Bovik, "Making a 'Completely Blind' Image Quality
Analyzer", IEEE Signal Processing Letters 20(3), 2013.
官方发布：http://live.ece.utexas.edu/research/quality/niqe_release.zip

## 它在做什么

自然图像的 MSCN 系数（局部均值/方差归一化后的系数）有很稳定的统计规律。
把一张图切成 96×96 的块，每块提取 18 个统计量（2 个广义高斯参数 + 4 个方向上
相邻系数乘积的非对称广义高斯参数），两个尺度拼成 36 维；再看这 36 维向量的
均值/协方差离「pristine 图像库标定出来的那套模型」有多远（马氏距离）。
**分数越低越接近自然图像、通常也越清晰**；模糊、噪声、JPEG 块效应都会把它推高。

## 三条必须守住的事

1. **模型参数必须用官方的**（见 `niqe_model.py`）。这套 36 维均值 + 36×36 协方差
   是作者在 pristine 库上标定出来的，自己造一套出来的分数没有任何意义。
2. **块尺寸 96×96、不重叠、双尺度** —— 官方推荐值，改了就不能和公开数值比较。
3. **只有同尺寸 / 同预处理的结果才好横向比**。NIQE 的绝对值会随图像尺寸和分块
   数量漂移（块越多，协方差估得越稳），所以本工具里它用于**同一次扫描内部排序**，
   不要拿它跟论文表格里的数字较劲。

## 与参考实现的一致性

以 BasicSR 的 Python 版为基准（它自称对得上官方 MATLAB 到小数点后 6 位：
baboon.png → MATLAB 5.72957338 / 复现 5.7295763）。

实测（`testdata/baboon480.png`，走本工程真实的「GDI+ 精确解码 → NIQE」管线）：

| 来源 | 数值 |
| --- | --- |
| 官方 MATLAB R2021a | 5.72957338 |
| BasicSR | 5.7295763 |
| **本实现** | **5.7295739** |

`test_niqe.py` 就锚在这张图上，偏差要求 < 1e-4。

为了在纯 Python 里跑得动，做了三处**数学等价**的加速（都验证过，不是近似）：
* 7×7 高斯窗是**可分离**的（实测外积误差 1.4e-17，一维窗和恰好 1.0）
  → 每像素 49 次乘加降到 14 次
* `r_gam(alpha)` 在 [0.2, 10] 上**单调递增**（实测 0.0630 → 0.7405）
  → 用二分查最近的 alpha，替掉逐项扫 9801 个表项
* 马氏距离里那步「伪逆」改成分解求解：`(COV_PRIS + COV_DIST)/2` 因为加了
  正定的 COV_PRIS 一定正定（COV_DIST 自己是奇异的 —— 块数比 36 维还少），
  所以 Cholesky 解 `M z = d` 再算 `d·z` 即可，比求逆稳定

## ⚠️ 怎么用才不算骗人（很重要）

1. **它对「轻微劣化」不单调，别当清晰度刻度尺。** 实测对同一张 baboon：
   原图 5.73，轻度模糊（σ=0.6）**反而降到 3.16**，高压缩（JPEG q=30）**也降到 3.16**，
   σ=1.2 才回到 5.60，σ=2.5 涨到 8.73，σ=5.0 涨到 11.76。
   原因：NIQE 量的是「离自然图像统计有多远」，把一张纹理很密的图轻轻磨平一点，
   反而更接近 pristine 库的统计。**所以不能读成「分低＝清晰」**。
2. **只在「同一张图的不同版本」之间比才有意义**，也就是本工具的主场景
   （一组相似图里挑最能留的那张）。跨不同照片比绝对值没有意义 ——
   内容不同，MSCN 统计本来就不同。
3. **必须用原图像素算。** NIQE 对像素级改动极敏感：同一张图，
   Windows Shell 缩略图路径（`winimg.load_pixels`）回过来的像素有 70% 的字节不同，
   分数就从 5.73 变成 6.21。所以这里配的是 `winimg.load_pixels_exact`（GDI+）。

## 已知的诚实边界

* **块退化就丢弃**：整块是常数（比如截图里的大白底）时 MSCN 全为 0，
  非对称高斯估不出来。参考实现会得到 nan 然后把该行排除，这里直接丢弃该块，
  行为一致。丢得太多（有效块 < 2）就没法估协方差，返回 `None` 并说明原因 ——
  **不编一个数出来**。
* 绝对值和 MATLAB 不会逐位相同（浮点累加顺序不同），量级和排序一致。
* 只认 PNG / JPEG / BMP / GIF / TIFF（GDI+ 的编解码范围），
  WEBP / HEIC 解不了就说解不了。
"""
import bisect
import math

import niqe_model as M

BLOCK = 96
SCALES = (1, 2)

# 一张图能切出多少个 96×96 的块，直接决定这个分数值不值得信 —— 这是**量出来的**：
# 拿 baboon480（正好 5×5 = 25 个块）做自举，随机抽 k 个块重算分数（抽 40 次）：
#     k=2  -> 中位 10.19，范围 6.80~22.69（极差 156%）
#     k=4  -> 中位  8.69，范围 6.33~13.29（极差  80%）
#     k=9  -> 中位  6.84，范围 5.79~ 8.59（极差  41%）
#     k=16 -> 中位  6.17，范围 5.45~ 7.27（极差  30%）
#     k=25 -> 全用，5.7296（即官方值，无抖动）
# 同一张图的同一批块，只是「用几个」不同，2 个块时就能在 6.8~22.7 之间乱跳 ——
# 档位会从「自然」一路跳到「明显劣化」。所以：
#   * 少于 MIN_BLOCKS：连分布都估不出，如实说「图太小」；
#   * 少于 SOLID_BLOCKS：分数照给（还是有信息量的），但档位只当参考。
# ⚠️ 16 这条线**是个判断，不是定论**：上表的极差是拿 baboon（纹理很花、块间差异大）
#    自举出来的**最坏情况**，平滑照片的块之间更一致、抖动会小些。换成 12 或 20 也
#    说得通 —— 改这一个数就行，别的地方都从它派生。
MIN_BLOCKS = 9
SOLID_BLOCKS = 16


def blocks_of(w, h):
    """这个尺寸能切出几个有效块（口径与 niqe_rows 一致，只数 scale 1 的那批）。"""
    if w <= 0 or h <= 0:
        return 0
    return int(w // BLOCK) * int(h // BLOCK)

# 一维高斯窗（由 7x7 可分离窗开平方得到，和恰好为 1）
_W1D = tuple(math.sqrt(M.GAUSSIAN_WINDOW[i][i]) for i in range(7))

_GAM_ALPHAS = None       # alpha 表：0.2, 0.201, ..., 10.0
_GAM_RS = None           # 对应的 r_gam 值（单调递增，供二分）


def _gam_table():
    """构造 alpha -> r_gam 表（只做一次）。

    `r_gam(a) = gamma(2/a)^2 / (gamma(1/a) * gamma(3/a))`。
    用 `0.2 + i*0.001` 而不是累加，跟 `np.arange(0.2, 10.001, 0.001)` 的取值方式一致。
    """
    global _GAM_ALPHAS, _GAM_RS
    if _GAM_ALPHAS is not None:
        return
    alphas = []
    rs = []
    for i in range(9801):                    # 0.2 .. 10.0，共 9801 项
        a = 0.2 + i * 0.001
        inv = 1.0 / a
        r = (math.gamma(inv * 2.0) ** 2) / (math.gamma(inv) * math.gamma(inv * 3.0))
        alphas.append(a)
        rs.append(r)
    _GAM_ALPHAS = alphas
    _GAM_RS = rs


def alpha_for(rhatnorm):
    """给 rhatnorm 找最接近的 alpha（表里 0.2~10，步长 0.001）。

    参考实现是 `argmin((r_gam - rhatnorm)**2)`，也就是「找最近的表项」。
    表是单调的，所以二分到相邻两项比一下即可，等价且快得多。
    """
    _gam_table()
    rs = _GAM_RS
    i = bisect.bisect_left(rs, rhatnorm)
    if i <= 0:
        return _GAM_ALPHAS[0]
    if i >= len(rs):
        return _GAM_ALPHAS[-1]
    return _GAM_ALPHAS[i - 1] if (rhatnorm - rs[i - 1]) <= (rs[i] - rhatnorm) \
        else _GAM_ALPHAS[i]


# ---------------------------------------------------------------------------
# 图像准备
# ---------------------------------------------------------------------------

def luma_rows(w, h, bgra):
    """BGRA 字节 -> BT.601 亮度行（和参考实现同一套换算）。

    `Y = 16 + (65.481*R + 128.553*G + 24.966*B) / 255`，然后取整。
    这是 BT.601 的 studio range（16~235），参考实现就是这么算的
    （`to_y_channel` 里 `/255` → `bgr2ycbcr` → `*255`，净效果一致），
    不是 0.299/0.587/0.114 那种 full range 写法 —— 用错会让分数偏掉。
    """
    rows = []
    for y in range(h):
        off = y * w * 4
        row = []
        ap = row.append
        for x in range(off, off + w * 4, 4):
            b = bgra[x]
            g = bgra[x + 1]
            r = bgra[x + 2]
            ap(float(round(16.0 + (65.481 * r + 128.553 * g + 24.966 * b) / 255.0)))
        rows.append(row)
    return rows


def _blur_rows(rows, bw, bh):
    """7 抽头可分离高斯，边缘按 `nearest` 复制（跟 scipy 的 mode='nearest' 一致）。"""
    k0, k1, k2, k3, k4, k5, k6 = _W1D
    r = 3
    # 横向：每行先把两端各复制 3 个，再一次列表推导算完 7 个抽头
    hor = []
    for row in rows:
        e = [row[0]] * r + row + [row[-1]] * r
        hor.append([k0 * e[i] + k1 * e[i + 1] + k2 * e[i + 2] + k3 * e[i + 3]
                    + k4 * e[i + 4] + k5 * e[i + 5] + k6 * e[i + 6]
                    for i in range(bw)])
    # 纵向：行索引夹到 [0, bh-1]
    out = []
    for i in range(bh):
        a = hor[i - 3 if i >= 3 else 0]
        b = hor[i - 2 if i >= 2 else 0]
        c = hor[i - 1 if i >= 1 else 0]
        d = hor[i]
        e = hor[i + 1 if i + 1 < bh else bh - 1]
        f = hor[i + 2 if i + 2 < bh else bh - 1]
        g = hor[i + 3 if i + 3 < bh else bh - 1]
        out.append([k0 * a[j] + k1 * b[j] + k2 * c[j] + k3 * d[j]
                    + k4 * e[j] + k5 * f[j] + k6 * g[j] for j in range(bw)])
    return out


def mscn_rows(rows, bw, bh):
    """MSCN 归一化（论文 Eq.1）：`(I - mu) / (sigma + 1)`。"""
    mu = _blur_rows(rows, bw, bh)
    sq = [[v * v for v in row] for row in rows]
    mu_sq = _blur_rows(sq, bw, bh)
    out = []
    for i in range(bh):
        m = mu[i]
        m2 = mu_sq[i]
        ri = rows[i]
        out.append([(ri[j] - m[j]) / (math.sqrt(abs(m2[j] - m[j] * m[j])) + 1.0)
                    for j in range(bw)])
    return out


def _cubic(x):
    """Keys 三次核（a = -0.5），和 MATLAB `imresize` 用的那个一致。"""
    ax = -x if x < 0 else x
    if ax <= 1.0:
        return 1.5 * ax ** 3 - 2.5 * ax ** 2 + 1.0
    if ax <= 2.0:
        return -0.5 * ax ** 3 + 2.5 * ax ** 2 - 4.0 * ax + 2.0
    return 0.0


def _weights_indices(in_len, out_len, scale):
    """算 imresize 的权重和索引（照抄 BasicSR 的 `calculate_weights_indices`）。"""
    kernel_width = 4.0 / scale               # scale<1 且开抗锯齿
    u = [((i + 1) / scale) + 0.5 * (1.0 - 1.0 / scale) for i in range(out_len)]
    p = int(math.ceil(kernel_width)) + 2
    idx_rows = []
    w_rows = []
    for v in u:
        left = math.floor(v - kernel_width / 2.0)
        ind = [left + t for t in range(p)]
        w = [scale * _cubic((v - k) * scale) for k in ind]
        s = sum(w)
        w = [t / s for t in w]
        idx_rows.append(ind)
        w_rows.append(w)
    # 首/尾整列为 0 就去掉（参考实现只处理这两列）
    for col in (0, -1):
        if all(abs(wr[col] if col == 0 else wr[-1]) > 0 for wr in w_rows):
            continue
        if col == 0:
            idx_rows = [ir[1:] for ir in idx_rows]
            w_rows = [wr[1:] for wr in w_rows]
        else:
            idx_rows = [ir[:-1] for ir in idx_rows]
            w_rows = [wr[:-1] for wr in w_rows]
    lo = min(min(ir) for ir in idx_rows)
    hi = max(max(ir) for ir in idx_rows)
    sym_s = int(-lo + 1)
    sym_e = int(hi - in_len)
    idx_rows = [[int(t) + sym_s - 1 for t in ir] for ir in idx_rows]
    return w_rows, idx_rows, sym_s, sym_e


def _pad_ends(line, sym_s, sym_e):
    """给一行（或一列）做对称补齐。

    等价 `numpy.pad(line, (sym_s, sym_e), mode='symmetric')`：
    **反向镜像且边界样本只出现一次**（不是 `mode='reflect'`）。
    参考实现是 `img[:, :sym_len, :]` 倒序后贴到前面，就是 `[v[n-1], ..., v[0]]`。
    """
    return ([line[k] for k in range(sym_s - 1, -1, -1)] + list(line)
            + [line[-k] for k in range(1, sym_e + 1)])


def resize_rows(rows, bw, bh, scale):
    """MATLAB `imresize` 等价的双三次缩放（含抗锯齿），只用于 0.5 缩小那一处。"""
    out_h = int(math.ceil(bh * scale))
    out_w = int(math.ceil(bw * scale))
    wh, ih, ss_h, se_h = _weights_indices(bh, out_h, scale)
    ww, iw, ss_w, se_w = _weights_indices(bw, out_w, scale)
    # 纵向
    aug = ([rows[k] for k in range(ss_h - 1, -1, -1)] + list(rows)
           + [rows[-k] for k in range(1, se_h + 1)])
    mid = []
    for i in range(out_h):
        w = wh[i]
        idx = ih[i]
        mid.append([sum(w[t] * aug[idx[t]][j] for t in range(len(w)))
                    for j in range(bw)])
    # 横向
    aug2 = [_pad_ends(row, ss_w, se_w) for row in mid]
    out = []
    for row in aug2:
        out.append([sum(ww[j][t] * row[iw[j][t]] for t in range(len(ww[j])))
                    for j in range(out_w)])
    return out, out_w


# ---------------------------------------------------------------------------
# 特征
# ---------------------------------------------------------------------------

def _aggd(vals):
    """非对称广义高斯参数估计 -> (alpha, beta_l, beta_r)，退化块返回 None。"""
    n = len(vals)
    if n == 0:
        return None
    n_l = n_r = 0
    ss_l = ss_r = 0.0
    s_abs = 0.0
    s_sq = 0.0
    for v in vals:
        if v < 0.0:
            n_l += 1
            ss_l += v * v
        elif v > 0.0:
            n_r += 1
            ss_r += v * v
        s_abs += -v if v < 0.0 else v
        s_sq += v * v
    if n_l == 0 or n_r == 0 or s_sq <= 0.0:
        return None                # 退化成常数块：参考实现这里会变 nan 并被排除
    left_std = math.sqrt(ss_l / n_l)
    right_std = math.sqrt(ss_r / n_r)
    if right_std <= 0.0 or left_std <= 0.0:
        return None
    gammahat = left_std / right_std
    rhat = (s_abs / n) ** 2 / (s_sq / n)
    denom = (gammahat ** 2 + 1.0) ** 2
    rhatnorm = rhat * (gammahat ** 3 + 1.0) * (gammahat + 1.0) / denom
    a = alpha_for(rhatnorm)
    k = math.sqrt(math.gamma(1.0 / a) / math.gamma(3.0 / a))
    return a, left_std * k, right_std * k


def _shift_prod(rows, bw, bh, dy, dx):
    """块内与相邻系数相乘（`np.roll` 的环绕语义），返回摊平的列表。"""
    out = []
    for i in range(bh):
        r = rows[i]
        r2 = rows[(i - dy) % bh]
        if dx == 1:
            out.extend([a * b for a, b in zip(r, r2[-1:] + r2[:-1])])
        elif dx == -1:
            out.extend([a * b for a, b in zip(r, r2[1:] + r2[:1])])
        else:
            out.extend([a * b for a, b in zip(r, r2)])
    return out


def block_features(brows, bw, bh):
    """一个块的 18 个特征（与参考实现同序）。

    顺序：`[alpha, (beta_l+beta_r)/2]` + 4 个方向各 `[alpha, eta, beta_l, beta_r]`，
    方向顺序 `(0,1) (1,0) (1,1) (1,-1)` —— **顺序必须和模型一致**，
    排错了不会报错，只会安静地给出一个没意义的分数。
    """
    flat = []
    for row in brows:
        flat.extend(row)
    got = _aggd(flat)
    if got is None:
        return None
    a, bl, br = got
    feat = [a, (bl + br) / 2.0]
    for dy, dx in ((0, 1), (1, 0), (1, 1), (1, -1)):
        got = _aggd(_shift_prod(brows, bw, bh, dy, dx))
        if got is None:
            return None
        a, bl, br = got
        eta = (br - bl) * (math.gamma(2.0 / a) / math.gamma(1.0 / a))
        feat.extend([a, eta, bl, br])
    return feat


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def _solve_sym(cov, d):
    """解 `cov z = d`（Cholesky）。不是正定就返回 None。"""
    n = len(d)
    L = [[0.0] * n for _ in range(n)]
    for i in range(n):
        Li = L[i]
        for j in range(i + 1):
            s = cov[i][j] - sum(Li[k] * L[j][k] for k in range(j))
            if i == j:
                if s <= 0.0:
                    return None
                Li[j] = math.sqrt(s)
            else:
                Lj = L[j][j]
                if Lj == 0.0:
                    return None
                Li[j] = s / Lj
    y = [0.0] * n
    for i in range(n):
        y[i] = (d[i] - sum(L[i][k] * y[k] for k in range(i))) / L[i][i]
    z = [0.0] * n
    for i in range(n - 1, -1, -1):
        z[i] = (y[i] - sum(L[k][i] * z[k] for k in range(i + 1, n))) / L[i][i]
    return z


def niqe_rows(rows, bw, bh):
    """核心：给定亮度行，算 NIQE。返回 (分数, 说明)。

    算不了的时候返回 `(None, 原因文字)` —— 宁可如实说算不了，也不编数字。
    """
    nbh = int(bh // BLOCK)
    nbw = int(bw // BLOCK)
    if nbw * nbh < MIN_BLOCKS:
        return None, ("图太小（只能切出 %d 个 %d×%d 的块，至少要 %d 个）"
                      % (nbw * nbh, BLOCK, BLOCK, MIN_BLOCKS))
    h_use = nbh * BLOCK
    w_use = nbw * BLOCK
    rows = [row[:w_use] for row in rows[:h_use]]
    bw, bh = w_use, h_use

    feats = []
    cur = rows
    cbw, cbh = bw, bh
    for scale in SCALES:
        if scale != 1:
            # 参考实现是 imresize(img/255, 0.5) * 255 —— 除以 255 再乘回来
            # 在浮点上不是完全无损，照做以免和它有两三个 ulp 的差异。
            scaled = [[v / 255.0 for v in row] for row in cur]
            scaled, cbw = resize_rows(scaled, cbw, cbh, 0.5)
            cbh = len(scaled)
            cur = [[v * 255.0 for v in row] for row in scaled]
        norm = mscn_rows(cur, cbw, cbh)
        bs = BLOCK // scale
        for bi in range(nbh):
            for bj in range(nbw):
                blk = [norm[bi * bs + r][bj * bs:(bj + 1) * bs]
                       for r in range(bs)]
                feats.append(block_features(blk, bs, bs))
        if scale == 1:
            base = len(feats)
    # 现在 feats 是 [scale1 的 n 个 18 维, scale2 的 n 个 18 维]，
    # 要按行拼成 36 维（和参考实现的 hstack 一致）
    n = base
    if len(feats) != 2 * n:
        return None, "两个尺度的块数不一致（%d / %d）" % (n, len(feats) - n)
    rows36 = []
    for i in range(n):
        f1 = feats[i]
        f2 = feats[n + i]
        if f1 is None or f2 is None:
            continue               # 退化块：整行丢掉（等价于参考实现的 nan 行排除）
        rows36.append(f1 + f2)
    if len(rows36) < MIN_BLOCKS:
        # 走到这儿说明块数本来是够的，是**块本身退化**（大面积纯色 → MSCN 全 0 →
        # AGGD 估不出来）被丢掉了。措辞要对用户可读：说「协方差估不出」等于没说。
        return None, ("能用的块只剩 %d 个（图太单调，至少要 %d 个）"
                      % (len(rows36), MIN_BLOCKS))

    k = len(rows36)
    mu = [0.0] * 36
    for r in rows36:
        for j in range(36):
            mu[j] += r[j]
    mu = [v / k for v in mu]

    cov = [[0.0] * 36 for _ in range(36)]
    for r in rows36:
        dev = [r[j] - mu[j] for j in range(36)]
        for i in range(36):
            di = dev[i]
            ci = cov[i]
            for j in range(i + 1):
                ci[j] += di * dev[j]
    for i in range(36):
        for j in range(i + 1):
            v = cov[i][j] / (k - 1)
            cov[i][j] = v
            cov[j][i] = v

    # (COV_PRIS + COV_DIST)/2 —— COV_DIST 自己必然奇异（块数 < 36），
    # 加上正定的 COV_PRIS 之后一定正定，所以能直接 Cholesky 解。
    m = [[(M.COV_PRIS[i][j] + cov[i][j]) / 2.0 for j in range(36)]
         for i in range(36)]
    d = [M.MU_PRIS[i] - mu[i] for i in range(36)]
    z = _solve_sym(m, d)
    if z is None:                  # 理论上不会走到；真走到了就加一点抖动再试
        m = [[m[i][j] + (1e-12 if i == j else 0.0) for j in range(36)]
             for i in range(36)]
        z = _solve_sym(m, d)
    if z is None:
        return None, "协方差没法分解（数值问题）"
    q = sum(d[i] * z[i] for i in range(36))
    if q < 0.0:
        return None, "马氏距离算出负数（数值问题）"
    return math.sqrt(q), "%d 个块（%d×%d，两尺度）" % (k, nbw, nbh)


def niqe_bgra(w, h, bgra):
    """给 BGRA 字节算 NIQE。返回 (分数, 说明)。"""
    if not bgra or w <= 0 or h <= 0:
        return None, "没有像素数据"
    return niqe_rows(luma_rows(w, h, bgra), w, h)


# ---------------------------------------------------------------------------
# 分级
# ---------------------------------------------------------------------------

# 门槛是**实测标定**出来的，不是拍脑袋。标定办法见 test_niqe.py 的 test_thresholds()
# 与 README「NIQE 门槛是怎么定的」：
#   2 张基准图（真实照片 baboon480 + 1/f 合成 1600×1200）×
#   {原始, 高斯模糊 σ=0.6/1.2/2.5/5, JPEG q=30/15/5, 噪声 σ=15, 压扁 4×/8×}
#   = 22 个样本，全部走真实管线（GDI+ 精确解码 → 640 上限 → NIQE）。
# 落点：两张「干净原图」是 5.73 / 6.39；轻度劣化落在 3.2~7.1；
#       明显劣化（σ≥2.5 模糊、q≤5 压缩、噪声、压扁 4× 以上）落在 8.0~23.9。
# 6.5 / 8.0 就是把「和原图一个量级」「比原图明显更远」分开的位置。
# ⚠️ 只有这样粗的档位是站得住的 —— 样本里只有 2 张基准图，
#    而且 NIQE 对轻微劣化本来就不单调（见模块头注释），别当精确刻度用。
NIQE_GOOD = 6.5        # <= 6.5 与原图一个量级
NIQE_FAIR = 8.0        # <= 8.0 有劣化迹象；> 8.0 明显劣化


def grade(score):
    """NIQE 分数 -> (级别, 中文标签)。分数越低越接近自然图像。

    标签刻意**不叫「清晰 / 模糊」** —— NIQE 量的是「离自然图像统计有多远」，
    轻度模糊反而可能让分数变低。所以只说「自然 / 轻微劣化 / 明显劣化」，
    不承诺它没承诺过的事。
    """
    if score is None:
        return "unknown", "算不了"
    if score <= NIQE_GOOD:
        return "good", "自然"
    if score <= NIQE_FAIR:
        return "fair", "轻微劣化"
    return "poor", "明显劣化"


def brief(score):
    """给界面用的短说明：分数 + 档位。算不了就返回 None。"""
    if score is None:
        return None
    return "%.2f（%s）" % (score, grade(score)[1])
