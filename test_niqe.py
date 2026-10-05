# -*- coding: utf-8 -*-
"""测试 NIQE 的实现、门槛与「用它算分的那条链路」。

这个模块值得单独一套测试，因为 NIQE 有两个特点：
  1. **算错了也照样返回一个数** —— 方向顺序排错、亮度口径用错、块拼错，
     都不会报错，只会安静地给出一个没意义的分数。
  2. **对像素级差异极敏感** —— 同一张图换一条解码路径（缩略图 vs 原图），
     分数能从 5.73 变成 6.21。

所以这里分五层来钉：
  A. **对官方数值**：testdata/baboon480.png 必须算到 5.7296（官方 MATLAB
     R2021a 是 5.72957338，BasicSR 是 5.7295763），偏差 < 1e-4。
     这一条同时验收了亮度换算、MSCN、分块、双尺度拼接、模型装配、线性代数。
  B. **逐环节对照一份「另写一遍」的参考**：可分离高斯 vs 直接二维卷积、
     二分查 alpha vs 暴力扫 9801 项、环绕相乘 vs 显式取模索引。
  C. **冻结值回归**：小尺寸 resize 的 36 个输出、4 张合成图的分数。
  D. **退化与边界**：常数图 / 太小的图 → 必须返回 None 并说明原因，不许编数。
  E. **门槛与数据一致**：把原图逐级磨糊/加噪，分数必须单调上升，
     且落在标定时定下的档位里。

不依赖 numpy：需要参照实现的地方都在本文件里另写一遍纯 Python 版本。
跑法：python test_niqe.py
"""
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import niqe               # noqa: E402
import niqe_model as M    # noqa: E402
import winimg             # noqa: E402

FIXTURE = os.path.join(HERE, "testdata", "baboon480.png")
BABOON_OFFICIAL_MATLAB = 5.72957338
BABOON_OFFICIAL_BASICSR = 5.7295763
BLOCK = 96

FAIL = []


def check(cond, msg):
    print("   %s %s" % ("OK  " if cond else "FAIL", msg))
    if not cond:
        FAIL.append(msg)
    return cond


def close(a, b, tol):
    return a is not None and b is not None and abs(a - b) <= tol


# ---------------------------------------------------------------------------
# 另写一遍的参考实现（要点：和 niqe.py 走**不同**的代码路径，不是复制）
# ---------------------------------------------------------------------------

_ALPHAS = [0.2 + i * 0.001 for i in range(9801)]          # 官方 gam = 0.2:0.001:10
_RGAM = [(math.gamma(2.0 / a) ** 2) / (math.gamma(1.0 / a) * math.gamma(3.0 / a))
         for a in _ALPHAS]


def aggd_bruteforce(vals):
    """官方 estimateaggdparam.m 的直译：**暴力扫**全部 9801 个 alpha，不二分。"""
    neg = [v * v for v in vals if v < 0]
    pos = [v * v for v in vals if v > 0]
    if not neg or not pos:
        return None
    left = math.sqrt(sum(neg) / len(neg))
    right = math.sqrt(sum(pos) / len(pos))
    if left <= 0 or right <= 0:
        return None
    gh = left / right
    rhat = (sum(abs(v) for v in vals) / len(vals)) ** 2 / (sum(v * v for v in vals) / len(vals))
    rn = (rhat * (gh ** 3 + 1) * (gh + 1)) / ((gh ** 2 + 1) ** 2)
    best_i, best_d = 0, None
    for i, r in enumerate(_RGAM):
        d = (r - rn) ** 2
        if best_d is None or d < best_d:
            best_d, best_i = d, i
    a = _ALPHAS[best_i]
    k = math.sqrt(math.gamma(1.0 / a) / math.gamma(3.0 / a))
    return a, left * k, right * k


def blur2d_ref(rows, bw, bh):
    """直接用 7x7 二维窗卷积（不做可分离分解），`replicate` 边界。"""
    win = M.GAUSSIAN_WINDOW
    out = []
    for y in range(bh):
        row = []
        for x in range(bw):
            acc = 0.0
            for dy in range(-3, 4):
                yy = y + dy
                if yy < 0:
                    yy = 0
                elif yy >= bh:
                    yy = bh - 1
                src = rows[yy]
                wrow = win[dy + 3]
                for dx in range(-3, 4):
                    xx = x + dx
                    if xx < 0:
                        xx = 0
                    elif xx >= bw:
                        xx = bw - 1
                    acc += wrow[dx + 3] * src[xx]
            row.append(acc)
        out.append(row)
    return out


def roll_ref(rows, bw, bh, dy, dx):
    """`np.roll(a, (dy, dx), axis=(0,1))` 的显式写法：元素 [i][j] 取 [i-dy][j-dx]。"""
    out = []
    for i in range(bh):
        for j in range(bw):
            out.append(rows[i][j] * rows[(i - dy) % bh][(j - dx) % bw])
    return out


def weights_indices_ref(in_len, out_len, scale):
    """BasicSR `calculate_weights_indices` 的直译（用于和 niqe._weights_indices 对照）。"""
    kw = 4.0 / scale
    p = int(math.ceil(kw)) + 2
    ind_rows, w_rows = [], []
    for i in range(out_len):
        u = (i + 1) / scale + 0.5 * (1.0 - 1.0 / scale)
        left = math.floor(u - kw / 2.0)
        ind = [left + t for t in range(p)]
        ws = [scale * niqe._cubic((u - j) * scale) for j in ind]
        s = sum(ws)
        ind_rows.append(ind)
        w_rows.append([w / s for w in ws])
    if any(w[0] == 0 for w in w_rows):
        ind_rows = [r[1:-1] for r in ind_rows]
        w_rows = [w[1:-1] for w in w_rows]
    if any(w[-1] == 0 for w in w_rows):
        ind_rows = [r[:-1] for r in ind_rows]
        w_rows = [w[:-1] for w in w_rows]
    lo = min(min(r) for r in ind_rows)
    hi = max(max(r) for r in ind_rows)
    sym_s = int(-lo + 1)
    sym_e = int(hi - in_len)
    ind_rows = [[int(t) + sym_s - 1 for t in r] for r in ind_rows]
    return w_rows, ind_rows, sym_s, sym_e


# ---------------------------------------------------------------------------
# A. 对官方数值
# ---------------------------------------------------------------------------

def test_anchor_official():
    print("\n=== A. 对官方数值（最关键的一条）===")
    if not os.path.isfile(FIXTURE):
        check(False, "缺夹具 %s —— 这条不跑就等于没验收" % FIXTURE)
        return
    got = winimg.load_pixels_exact(FIXTURE)
    if not check(got is not None, "GDI+ 能解出夹具（精确解码可用）"):
        return
    w, h, bgra = got
    check((w, h) == (480, 480), "夹具是 480x480（实际 %dx%d）" % (w, h))
    score, note = niqe.niqe_bgra(w, h, bgra)
    print("       本实现      = %.7f  (%s)" % (score, note))
    print("       官方 MATLAB = %.8f" % BABOON_OFFICIAL_MATLAB)
    print("       BasicSR     = %.7f" % BABOON_OFFICIAL_BASICSR)
    check(close(score, BABOON_OFFICIAL_MATLAB, 1e-4),
          "与官方 MATLAB 偏差 %.2e < 1e-4" % abs(score - BABOON_OFFICIAL_MATLAB))
    check(close(score, BABOON_OFFICIAL_BASICSR, 1e-4),
          "与 BasicSR 偏差 %.2e < 1e-4" % abs(score - BABOON_OFFICIAL_BASICSR))
    check("25 个块" in note, "块数是 5x5=25（%s）" % note)

    # 再算一遍必须逐位相同 —— 精确解码不许有随机性
    again = winimg.load_pixels_exact(FIXTURE)
    check(again[2] == bgra, "同一文件两次精确解码逐字节相同")
    s2, _ = niqe.niqe_bgra(again[0], again[1], again[2])
    check(s2 == score, "同一文件两次算分完全相同")


# ---------------------------------------------------------------------------
# B. 逐环节对照
# ---------------------------------------------------------------------------

def test_luma():
    print("\n=== B1. 亮度换算（BT.601 studio range）===")
    # 纯红 vs 纯蓝：BT.601 里红的系数(65.481)远大于蓝(24.966)，红必须更亮。
    # 这个断言就是用来钉住「别把 BGRA 当 RGBA 读」的 —— 对调之后结论会反过来。
    red = bytes((0, 0, 255, 255)) * 4          # B=0 G=0 R=255
    blue = bytes((255, 0, 0, 255)) * 4         # B=255 G=0 R=0
    white = bytes((255, 255, 255, 255)) * 4
    black = bytes((0, 0, 0, 255)) * 4
    lr = niqe.luma_rows(4, 1, red)[0]
    lb = niqe.luma_rows(4, 1, blue)[0]
    lw = niqe.luma_rows(4, 1, white)[0]
    lk = niqe.luma_rows(4, 1, black)[0]
    print("       红=%.1f 蓝=%.1f 白=%.1f 黑=%.1f" % (lr[0], lb[0], lw[0], lk[0]))
    check(lr[0] > lb[0], "红比蓝亮（说明 B/R 没读反）")
    check(lb[0] > lk[0], "蓝比黑亮")
    check(close(lr[0], round(16.0 + 65.481), 1e-9), "纯红 = round(16+65.481) = 81")
    check(close(lb[0], round(16.0 + 24.966), 1e-9), "纯蓝 = round(16+24.966) = 41")
    check(close(lk[0], 16.0, 1e-9), "纯黑 = 16（studio range 的下限，不是 0）")
    check(close(lw[0], round(16.0 + 219.0), 1e-9), "纯白 = 235（上限，不是 255）")


def test_blur_vs_2d():
    print("\n=== B2. 可分离高斯 vs 直接二维卷积 ===")
    bw = bh = 32
    rows = [[float(((x * 13 + y * 7) % 17) * 9 + 40) for x in range(bw)] for y in range(bh)]
    mine = niqe._blur_rows(rows, bw, bh)
    ref = blur2d_ref(rows, bw, bh)
    d = max(abs(mine[y][x] - ref[y][x]) for y in range(bh) for x in range(bw))
    print("       最大偏差 = %.3e" % d)
    check(d < 1e-9, "可分离分解与直接二维卷积一致（差 %.1e）" % d)

    # 常数图必须保持常数（否则 MCSN 分母会出问题）
    flat = [[123.0] * bw for _ in range(bh)]
    f = niqe._blur_rows(flat, bw, bh)
    d2 = max(abs(v - 123.0) for r in f for v in r)
    check(d2 < 1e-9, "常数图模糊后仍是同一常数（差 %.1e）" % d2)


def test_aggd_vs_bruteforce():
    print("\n=== B3. AGGD 参数：二分查表 vs 暴力扫 9801 项 ===")
    seed = 987654321
    vals = []
    for _ in range(500):
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
        vals.append((((seed >> 16) % 2001) - 1000) / 100.0)        # -10 .. 10
    mine = niqe._aggd(vals)
    ref = aggd_bruteforce(vals)
    check(mine is not None and ref is not None, "两边都估出了参数")
    if mine and ref:
        print("       alpha 我=%.4f 参考=%.4f" % (mine[0], ref[0]))
        print("       beta  我=(%.6f, %.6f) 参考=(%.6f, %.6f)" % (mine[1], mine[2], ref[1], ref[2]))
        check(mine[0] == ref[0], "alpha 完全相同（表项一致）")
        check(close(mine[1], ref[1], 1e-12) and close(mine[2], ref[2], 1e-12),
              "beta_l / beta_r 一致")


def test_shift_vs_roll():
    print("\n=== B4. 环绕相乘 vs 显式取模索引 ===")
    bw = bh = 5
    rows = [[float(y * bw + x) for x in range(bw)] for y in range(bh)]
    ok = True
    for dy, dx in ((0, 1), (1, 0), (1, 1), (1, -1)):
        a = niqe._shift_prod(rows, bw, bh, dy, dx)
        b = roll_ref(rows, bw, bh, dy, dx)
        if a != b:
            ok = False
            print("       方向 (%d,%d) 不一致" % (dy, dx))
    check(ok, "4 个方向的环绕语义都和 np.roll 一致")


def test_weights_indices():
    print("\n=== B5. 缩放权重与索引 vs 另写一遍 ===")
    for n, out in ((480, 240), (12, 6), (97, 49)):
        w1, i1, s1, e1 = niqe._weights_indices(n, out, 0.5)
        w2, i2, s2, e2 = weights_indices_ref(n, out, 0.5)
        check((s1, e1) == (s2, e2), "n=%d 对称补齐长度一致 (%d,%d)" % (n, s1, e1))
        check(len(w1[0]) == len(w2[0]), "n=%d 权重列数一致 (%d)" % (n, len(w1[0])))
        dw = max(abs(a - b) for ra, rb in zip(w1, w2) for a, b in zip(ra, rb))
        di = max(abs(a - b) for ra, rb in zip(i1, i2) for a, b in zip(ra, rb))
        check(dw < 1e-15 and di == 0, "n=%d 权重差 %.1e / 索引差 %d" % (n, dw, di))


# ---------------------------------------------------------------------------
# C. 冻结值回归
# ---------------------------------------------------------------------------

RESIZE_12X12 = [
    [71.7086791992, 120.51361084, 85.2429199219, 122.429199219, 93.2211303711, 84.5016479492],
    [105.542602539, 100.703735352, 100.046386719, 106.063537598, 82.3910522461, 129.822998047],
    [118.266296387, 92.9669189453, 99.7787475586, 98.5885620117, 100.08972168, 100.309753418],
    [110.757446289, 83.0410766602, 118.067932129, 81.9320678711, 116.95892334, 89.2425537109],
    [99.9557495117, 99.4104003906, 100.911560059, 100.486755371, 107.177734375, 82.643737793],
    [70.4879760742, 122.700195312, 98.4133911133, 98.0389404297, 95.9817504883, 86.604309082],
]

SYNTH = {
    "sharp": 462.994117,
    "noise": 64.525643,
    "smooth": 77.754105,
}
# sharp / smooth 这两张合成图会踩到「某些块接近退化」的情形，换一份等价实现
# （浮点累加顺序不同）就能差出 0.6，所以给它宽一点的相对容差；这里要抓的是
# 「整段算错」（换位、方向排错会一口气差好几个数），不是末位。
SYNTH_TOL = {"sharp": 0.01, "noise": 1e-3, "smooth": 0.01}      # 前两个是相对，noise 是绝对
SYNTH_REL = {"sharp": True, "noise": False, "smooth": True}


def clamp(v):
    return 0 if v < 0 else (255 if v > 255 else v)


def gen_sharp(w=288, h=288):
    px = bytearray(w * h * 4)
    for y in range(h):
        for x in range(w):
            v = clamp(90 + (x * 40) // w + (y * 40) // h
                      + (45 if (((x // 8) + (y // 8)) % 2) else 0))
            i = (y * w + x) * 4
            px[i] = px[i + 1] = px[i + 2] = v
            px[i + 3] = 255
    return w, h, bytes(px)


def gen_noise(w=288, h=288):
    px = bytearray(w * h * 4)
    seed = 12345
    for i in range(0, len(px), 4):
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
        v = 80 + ((seed >> 16) % 96)
        px[i] = px[i + 1] = px[i + 2] = v
        px[i + 3] = 255
    return w, h, bytes(px)


def gen_smooth(w=288, h=288):
    px = bytearray(w * h * 4)
    for y in range(h):
        for x in range(w):
            v = clamp(60 + (x * 130) // w + (y * 60) // h)
            i = (y * w + x) * 4
            px[i] = px[i + 1] = px[i + 2] = v
            px[i + 3] = 255
    return w, h, bytes(px)


def box_blur(rows, w, h, k):
    """k x k 盒式模糊（整数盒，确定性）。用来模拟「磨糊」。"""
    r = k // 2
    tmp = [[0.0] * w for _ in range(h)]
    for y in range(h):
        row = rows[y]
        acc = float(sum(row[max(t, 0) if t < 0 else min(t, w - 1)] for t in range(-r, r + 1)))
        for x in range(w):
            tmp[y][x] = acc / k
            acc += row[min(x + r + 1, w - 1)] - row[max(x - r, 0)]
    out = [[0.0] * w for _ in range(h)]
    for x in range(w):
        col = [tmp[y][x] for y in range(h)]
        acc = float(sum(col[max(t, 0) if t < 0 else min(t, h - 1)] for t in range(-r, r + 1)))
        for y in range(h):
            out[y][x] = acc / k
            acc += col[min(y + r + 1, h - 1)] - col[max(y - r, 0)]
    return out


def test_resize_frozen():
    print("\n=== C1. 缩放冻结值（12x12 -> 6x6）===")
    rows = [[float(((x * 7 + y * 5) % 11) * 20) for x in range(12)] for y in range(12)]
    out, ow = niqe.resize_rows(rows, 12, 12, 0.5)
    check(ow == 6 and len(out) == 6, "输出尺寸是 6x6（实际 %dx%d）" % (ow, len(out)))
    d = max(abs(out[y][x] - RESIZE_12X12[y][x]) for y in range(6) for x in range(6))
    print("       与冻结值最大偏差 = %.3e" % d)
    check(d < 1e-9, "缩放输出和冻结值一致（差 %.1e）" % d)

    # 常数图缩小后仍是同一常数
    flat = [[200.0] * 12 for _ in range(12)]
    f, _ = niqe.resize_rows(flat, 12, 12, 0.5)
    d2 = max(abs(v - 200.0) for r in f for v in r)
    check(d2 < 1e-9, "常数图缩放后仍是同一常数（差 %.1e）" % d2)


def feats36(rows, w, h):
    """按 niqe_rows 的流程把 36 维块特征摊出来。

    故意在这里把分块循环**重写一遍**：下面的「锚点判别力」测试要能替换其中的
    拼接顺序，而 niqe_rows 不对外暴露中间量。
    """
    nbh, nbw = h // BLOCK, w // BLOCK
    cur = [r[:nbw * BLOCK] for r in rows[:nbh * BLOCK]]
    cbw, cbh = nbw * BLOCK, nbh * BLOCK
    feats, base = [], 0
    for scale in niqe.SCALES:
        if scale != 1:
            sc = [[v / 255.0 for v in r] for r in cur]
            sc, cbw = niqe.resize_rows(sc, cbw, cbh, 0.5)
            cbh = len(sc)
            cur = [[v * 255.0 for v in r] for r in sc]
        norm = niqe.mscn_rows(cur, cbw, cbh)
        bs = BLOCK // scale
        for bi in range(nbh):
            for bj in range(nbw):
                blk = [norm[bi * bs + r][bj * bs:(bj + 1) * bs] for r in range(bs)]
                feats.append(niqe.block_features(blk, bs, bs))
        if scale == 1:
            base = len(feats)
    n = base
    return [feats[i] + feats[n + i] for i in range(n)]


def score_from(rs):
    """给定 36 维块特征行，照 niqe 的线性代数算分（不重排就应等于 niqe_rows 的结果）。"""
    k = len(rs)
    mu = [sum(r[j] for r in rs) / k for j in range(36)]
    cov = [[sum((r[i] - mu[i]) * (r[j] - mu[j]) for r in rs) / (k - 1) for j in range(36)]
           for i in range(36)]
    m = [[(M.COV_PRIS[i][j] + cov[i][j]) / 2.0 for j in range(36)] for i in range(36)]
    d = [M.MU_PRIS[i] - mu[i] for i in range(36)]
    z = niqe._solve_sym(m, d)
    return math.sqrt(sum(d[i] * z[i] for i in range(36)))


def swap_dir(rs, base, a, b):
    """把某个尺度里第 a、b 个方向组（各 4 维）互换，用来试「顺序排错会怎样」。

    注意是把**每一行内部**的维度互换（rs 是「块的个数」行 x 36 维），
    不是换行 —— 换行的话均值/协方差不变，分数不会动，等于什么都没测。
    """
    pa = [base + 2 + 4 * a + t for t in range(4)]
    pb = [base + 2 + 4 * b + t for t in range(4)]
    out = []
    for r in rs:
        nr = list(r)
        for t in range(4):
            nr[pa[t]], nr[pb[t]] = r[pb[t]], r[pa[t]]
        out.append(nr)
    return out


def test_anchor_discriminates():
    """证明上面那个锚点不是「怎么排都能过」—— 布局一动，分数就离开官方值。"""
    print("\n=== A2. 锚点的判别力（布局换了就必须偏离官方值）===")
    if not os.path.isfile(FIXTURE):
        check(False, "缺夹具")
        return
    g = winimg.load_pixels_exact(FIXTURE)
    rows = niqe.luma_rows(g[0], g[1], g[2])
    rs = feats36(rows, g[0], g[1])
    mine = score_from(rs)
    print("       正确布局            = %.7f" % mine)
    check(close(mine, niqe.niqe_rows(rows, g[0], g[1])[0], 1e-9),
          "重写的分块流程与 niqe_rows 得同一个数（差 %.1e）"
          % abs(mine - niqe.niqe_rows(rows, g[0], g[1])[0]))
    check(close(mine, BABOON_OFFICIAL_MATLAB, 1e-4), "重写版也命中官方值")

    swapped = score_from([r[18:] + r[:18] for r in rs])
    print("       两个尺度互换        = %.7f  (偏离 %.4f)" % (swapped, abs(swapped - mine)))
    check(abs(swapped - mine) > 1e-3, "尺度互换会明显偏离 → 锚点确实锁住了拼接顺序")

    for base, tag in ((0, "scale1"), (18, "scale2")):
        for a, b in ((0, 1), (2, 3), (0, 3)):
            alt = score_from(swap_dir(rs, base, a, b))
            check(abs(alt - mine) > 1e-3,
                  "%s 方向组 %d<->%d 互换偏离 %.4f" % (tag, a, b, abs(alt - mine)))


def test_synth_scores():
    print("\n=== C2. 合成图分数（冻结值回归）===")
    vals = {}
    for name, fn in (("sharp", gen_sharp), ("noise", gen_noise), ("smooth", gen_smooth)):
        w, h, bgra = fn()
        s, note = niqe.niqe_bgra(w, h, bgra)
        exp = SYNTH[name]
        tol = abs(exp) * SYNTH_TOL[name] if SYNTH_REL[name] else SYNTH_TOL[name]
        print("       %-7s = %.6f  期望 %.6f  (容差 %.1e, %s)" % (name, s, exp, tol, note))
        check(close(s, exp, tol), "%s 分数在冻结值容差内（差 %.2e）" % (name, abs(s - exp)))
        vals[name] = s

    # 四张图的内容不同，分数必须分开（重合就说明判据已经退化成常数了）
    check(len(set(round(v, 3) for v in vals.values())) == len(vals),
          "三张不同内容的图分数互不相同: %s" % vals)
    check(all(v > 0 for v in vals.values()), "分数都是正的")

    # 结构性关系：shap 那张棋盘图的块统计离自然图像统计极远，分数应当最高
    check(vals["sharp"] > vals["noise"] and vals["sharp"] > vals["smooth"],
          "强人工结构（棋盘）分数最高")


# ---------------------------------------------------------------------------
# D. 退化与边界
# ---------------------------------------------------------------------------

def test_degenerate():
    print("\n=== D. 退化输入不许编数 ===")

    # 纯黑（MSCN 全 0）→ 估不出 AGGD → 必须如实说
    const = bytes(bytearray([0, 0, 0, 255] * (288 * 288)))
    s, note = niqe.niqe_bgra(288, 288, const)
    check(s is None, "纯黑常数图返回 None（不是编一个数）")
    print("       说明: %s" % note)

    # 纯白也一样
    white = bytes(bytearray([255, 255, 255, 255] * (288 * 288)))
    s2, note2 = niqe.niqe_bgra(288, 288, white)
    check(s2 is None, "纯白常数图返回 None")

    # 小于一个 96x96 的块
    s3, note3 = niqe.niqe_bgra(80, 80, bytes(bytearray([90, 90, 90, 255] * (80 * 80))))
    check(s3 is None and "太小" in note3, "80x80 太小 → 说太小（%s）" % note3)

    # 刚好一个块（160x120 在 scale1 只有 1 个 96x96 块）→ 也凑不出分布，
    # 这条曾经报成「估不出协方差」这种黑话，用户看不懂，钉住现在的说法。
    small = bytearray()
    for y in range(120):
        for x in range(160):
            small += bytearray(((x * 7) % 256, (y * 5) % 256, (x ^ y) % 256, 255))
    s3b, note3b = niqe.niqe_bgra(160, 120, bytes(small))
    check(s3b is None and "太小" in note3b and "协方差" not in note3b,
          "160x120 只有 1 个块 → 说「图太小」而不是「估不出协方差」（%s）" % note3b)

    # 没有像素数据
    s4, note4 = niqe.niqe_bgra(0, 0, b"")
    check(s4 is None, "没有像素数据返回 None")

    # grade 对 None 有话说
    check(niqe.grade(None) == ("unknown", "算不了"), "grade(None) → 算不了")
    check(niqe.brief(None) is None, "brief(None) → None")


def test_grade_bands():
    print("\n=== D2. 分档边界 ===")
    cases = [
        (0.0, "good", "自然"),
        (niqe.NIQE_GOOD, "good", "自然"),
        (niqe.NIQE_GOOD + 0.01, "fair", "轻微劣化"),
        (niqe.NIQE_FAIR, "fair", "轻微劣化"),
        (niqe.NIQE_FAIR + 0.01, "poor", "明显劣化"),
        (99.0, "poor", "明显劣化"),
    ]
    ok = True
    for v, lvl, lab in cases:
        got = niqe.grade(v)
        if got != (lvl, lab):
            ok = False
            print("       %.2f -> %r 期望 (%r, %r)" % (v, got, lvl, lab))
    check(ok, "6 个边界值的档位都对")
    check("清晰" not in niqe.grade(9.9)[1] and "模糊" not in niqe.grade(9.9)[1],
          "档位文字不叫「清晰/模糊」（NIQE 量的是自然度，不是锐度）")


# ---------------------------------------------------------------------------
# E. 门槛与数据一致
# ---------------------------------------------------------------------------

def test_thresholds_match_data():
    print("\n=== E. 门槛与实测数据一致 ===")
    if not os.path.isfile(FIXTURE):
        check(False, "缺夹具，跳过门槛校验")
        return
    g = winimg.load_pixels_exact(FIXTURE)
    lr = niqe.luma_rows(g[0], g[1], g[2])
    base, _ = niqe.niqe_rows(lr, g[0], g[1])
    print("       原图            = %.4f -> %s" % (base, niqe.grade(base)[1]))
    check(base <= niqe.NIQE_GOOD,
          "干净原图落在「自然」档（%.4f <= %.1f）" % (base, niqe.NIQE_GOOD))

    prev = base
    ladder = []
    for k in (5, 9, 15):
        s, _ = niqe.niqe_rows(box_blur(lr, g[0], g[1], k), g[0], g[1])
        ladder.append((k, s))
        print("       盒式模糊 k=%-2d   = %.4f -> %s" % (k, s, niqe.grade(s)[1]))
        check(s > prev, "模糊 k=%d 比上一档分数更高（%.4f > %.4f）" % (k, s, prev))
        prev = s
    check(ladder[-1][1] > niqe.NIQE_FAIR,
          "强模糊落到「明显劣化」（%.4f > %.1f）" % (ladder[-1][1], niqe.NIQE_FAIR))

    # 加噪：确定性 LCG，同样必须把分数推上去
    seed = 20241005
    noisy = []
    for y in range(g[1]):
        row = []
        for x in range(g[0]):
            seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
            row.append(clamp(lr[y][x] + ((seed >> 16) % 61) - 30))
        noisy.append(row)
    sn, _ = niqe.niqe_rows(noisy, g[0], g[1])
    print("       均匀噪声 ±30    = %.4f -> %s" % (sn, niqe.grade(sn)[1]))
    check(sn > niqe.NIQE_FAIR, "加噪落到「明显劣化」（%.4f > %.1f）" % (sn, niqe.NIQE_FAIR))

    check(niqe.NIQE_GOOD < niqe.NIQE_FAIR, "两档门槛大小关系正确")


def main():
    print("NIQE 测试 —— 锚点 %s" % FIXTURE)
    test_anchor_official()
    test_anchor_discriminates()
    test_luma()
    test_blur_vs_2d()
    test_aggd_vs_bruteforce()
    test_shift_vs_roll()
    test_weights_indices()
    test_resize_frozen()
    test_synth_scores()
    test_degenerate()
    test_grade_bands()
    test_thresholds_match_data()

    print()
    if FAIL:
        print("FAILED: %d 项" % len(FAIL))
        for f in FAIL:
            print("  - %s" % f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
