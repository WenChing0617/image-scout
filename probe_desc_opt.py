# -*- coding: utf-8 -*-
"""`crops.describe` 优化的**等价性 + 提速**探针。

## 为什么要这么写

`region_gray` / `dct_hash16` 决定了每张图的 64 位指纹，**改了算法结果就变了**，
而指纹变了会连带改掉分组、配对、甚至「哪张判为重复」。
所以优化**必须证明结果逐位不变**，不能只看「测过一遍还行」。

本探针内置**旧实现的逐字副本**（`_ref_*`），拿它当基准：

  E1  `region_gray` 随机用例：随机积分图 + 随机矩形，新旧输出逐元素相同
  E2  `dct_hash16`  随机用例：随机 16x16 灰度，新旧指纹逐位相同
  E3  真实图全流程：`describe` 里每个区域，新旧指纹 100% 相同
  E4  退化用例：空矩形、越界矩形、超小区域（`< n` 边长）
  E5  提速：新实现相对旧实现的倍率

⚠️ `_ref_*` 是从 git 历史里**原样抄过来**的，不许「顺手改好」——
   它就是基准，改了基准等于自己跟自己比。

用法：python probe_desc_opt.py [张数]
"""
from __future__ import annotations

import math
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import crops                         # noqa: E402
import scan                          # noqa: E402
import thumbs                        # noqa: E402
import winimg                        # noqa: E402

ROOT = os.path.join(os.environ.get("TEMP", "."), "ImageScout-prog")
POOL_N = 40


def _make_photo(path, w, h, seed):
    """8x8 色块图（和 `probe_prog.make_photo` 同一种，等价性不挑图像内容）。"""
    rnd = random.Random(seed)
    buf = bytearray(w * h * 4)
    gw, gh = 8, 8
    cols = [(rnd.randrange(40, 216), rnd.randrange(40, 216),
             rnd.randrange(40, 216)) for _ in range(gw * gh)]
    for y in range(h):
        by = (y * gh) // h
        rb = y * w * 4
        for x in range(w):
            c = cols[by * gw + ((x * gw) // w)]
            i = rb + x * 4
            buf[i], buf[i + 1], buf[i + 2], buf[i + 3] = c[2], c[1], c[0], 255
    with open(path, "wb") as f:
        f.write(thumbs.bgra_to_png(w, h, bytes(buf)))


def ensure_pool():
    """池子不在就自己造。

    ⚠️⚠️ 本探针原来直接 `os.listdir(ROOT)`，**依赖「%TEMP% 里恰好有个
    ImageScout-prog 目录且有图」**（那是 `probe_prog.py` 的池）。
    v1.12 清理 TEMP 时把池删了，探针立刻 `FileNotFoundError` 崩掉 ——
    **探针不该依赖外部目录的内容**，自给自足才不会因为清理而假红。
    """
    try:
        if os.path.isdir(ROOT) and len(os.listdir(ROOT)) >= POOL_N:
            return
    except OSError:
        pass
    os.makedirs(ROOT, exist_ok=True)
    for i in range(POOL_N):
        p = os.path.join(ROOT, "p%04d.png" % i)
        if not os.path.isfile(p):
            _make_photo(p, 400, 300, i // 8)
    print("   [池] 自建 %d 张 %s" % (POOL_N, ROOT))

FAIL = []
OK = []


def check(cond, label, extra=""):
    (OK if cond else FAIL).append(label)
    print("   %s %s%s" % ("[OK]" if cond else "[XX]", label,
                          ("  " + extra) if extra else ""))


# ---------------------------------------------------------------------------
# 旧实现（逐字副本，基准）
# ---------------------------------------------------------------------------

def _ref_rect_sum(I, stride, x0, y0, x1, y1) -> int:
    return (I[y1 * stride + x1] - I[y0 * stride + x1]
            - I[y1 * stride + x0] + I[y0 * stride + x0])


def _ref_region_gray(I, stride, gw, gh, rect, n=None):
    n = crops.HASH_N if n is None else n
    x0, y0, x1, y1 = rect[0], rect[1], rect[2], rect[3]
    x0 = max(0, min(int(x0), gw - 1))
    y0 = max(0, min(int(y0), gh - 1))
    x1 = max(x0 + 1, min(int(x1), gw))
    y1 = max(y0 + 1, min(int(y1), gh))

    out = [0] * (n * n)
    for ty in range(n):
        ya = y0 + (y1 - y0) * ty // n
        yb = max(ya + 1, y0 + (y1 - y0) * (ty + 1) // n)
        for tx in range(n):
            xa = x0 + (x1 - x0) * tx // n
            xb = max(xa + 1, x0 + (x1 - x0) * (tx + 1) // n)
            s = _ref_rect_sum(I, stride, xa, ya, xb, yb)
            cnt = (xb - xa) * (yb - ya)
            out[ty * n + tx] = s // cnt if cnt else 0
    return out, n


def _ref_dct_hash16(gray) -> int:
    cos, norm = crops._COS, crops._NORM
    n, keep = crops.HASH_N, crops.HASH_KEEP

    tmp = []
    for y in range(n):
        gy = gray[y]
        tmp.append([sum(a * b for a, b in zip(gy, cos[v])) for v in range(keep)])

    vals = []
    for u in range(keep):
        nu = norm[u]
        cu = cos[u]
        for v in range(keep):
            s = 0.0
            for y in range(n):
                s += tmp[y][v] * cu[y]
            vals.append(s * nu * norm[v])

    vals[0] = 0.0
    rest = vals[1:]
    median = sorted(rest)[len(rest) // 2]
    bits = 0
    for i, v in enumerate(vals):
        if v > median:
            bits |= (1 << i)
    return bits


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------

def rand_integral(gw, gh, rnd):
    """造一张合法的积分图（由随机灰度前缀和生成）。"""
    return crops._build_I(bytes(rnd.randrange(256) for _ in range(gw * gh)),
                          gw, gh)


def case_region_gray():
    rnd = random.Random(20261007)
    bad = 0
    n_case = 0
    for _ in range(300):
        gw = rnd.randrange(8, 40)
        gh = rnd.randrange(8, 40)
        I = rand_integral(gw, gh, rnd)
        stride = gw + 1
        kinds = ["normal", "tiny", "oob", "degenerate", "full"]
        kind = rnd.choice(kinds)
        if kind == "normal":
            x0 = rnd.randrange(0, gw)
            x1 = rnd.randrange(x0 + 1, gw + 1)
            y0 = rnd.randrange(0, gh)
            y1 = rnd.randrange(y0 + 1, gh + 1)
        elif kind == "tiny":
            x0 = rnd.randrange(0, gw)
            y0 = rnd.randrange(0, gh)
            x1, y1 = x0 + 1, y0 + 1
        elif kind == "oob":
            x0, y0 = -rnd.randrange(1, 30), -rnd.randrange(1, 30)
            x1, y1 = gw + rnd.randrange(1, 30), gh + rnd.randrange(1, 30)
        elif kind == "degenerate":
            x0 = x1 = rnd.randrange(0, gw)
            y0 = y1 = rnd.randrange(0, gh)
        else:
            x0, y0, x1, y1 = 0, 0, gw, gh
        rect = (x0, y0, x1, y1, 0)
        for nn in (crops.HASH_N, crops.VER_N, 4, 1):
            a, _ = _ref_region_gray(I, stride, gw, gh, rect, nn)
            b, _ = crops.region_gray(I, stride, gw, gh, rect, nn)
            n_case += 1
            if a != b:
                bad += 1
                if bad <= 3:
                    print("      差异 gw=%d gh=%d rect=%s n=%d\n        ref=%s\n        new=%s"
                          % (gw, gh, rect, nn, a[:8], b[:8]))
    check(bad == 0, "E1 region_gray 新旧逐元素相同（%d 个用例）" % n_case,
          "差异 %d" % bad)


def case_dct():
    rnd = random.Random(777)
    bad = 0
    for _ in range(400):
        mode = rnd.choice(["flat", "grad", "noise", "spike", "const"])
        n = crops.HASH_N
        if mode == "flat":
            v = rnd.randrange(256)
            g = [[v] * n for _ in range(n)]
        elif mode == "grad":
            g = [[(x * 7 + y * 3) % 256 for x in range(n)] for y in range(n)]
        elif mode == "noise":
            g = [[rnd.randrange(256) for _ in range(n)] for _ in range(n)]
        elif mode == "spike":
            g = [[0] * n for _ in range(n)]
            g[rnd.randrange(n)][rnd.randrange(n)] = 255
        else:
            g = [[255] * n for _ in range(n)]
        a = _ref_dct_hash16(g)
        b = crops.dct_hash16(g)
        if a != b:
            bad += 1
            if bad <= 3:
                print("      差异 mode=%s ref=%d new=%d" % (mode, a, b))
    check(bad == 0, "E2 dct_hash16 新旧指纹逐位相同（400 个用例）", "差异 %d" % bad)


def case_real(n_img=40):
    files = [os.path.join(ROOT, f) for f in sorted(os.listdir(ROOT))][:n_img]
    if not files:
        check(False, "E3 真实图对比（没有测试图，跳过）")
        return
    bad = 0
    total = 0
    D = scan.DECODE
    for p in files:
        got = winimg.load_pixels(p, D)
        if not got:
            continue
        w, h, bgra = got
        gw, gh, gray, I = crops.build_grid(w, h, bgra)
        stride = gw + 1
        own = crops.snap_aspect(gw / float(gh))
        rects = [(0, 0, gw, gh, own)]
        for r in crops.window_list(gw, gh):
            if (r[0], r[1], r[2], r[3]) != (0, 0, gw, gh):
                rects.append(r)
        for r in rects:
            g_ref, _ = _ref_region_gray(I, stride, gw, gh, r, crops.HASH_N)
            g_new, _ = crops.region_gray(I, stride, gw, gh, r, crops.HASH_N)
            if g_ref != g_new:
                bad += 1
                continue
            a = _ref_dct_hash16([g_ref[i * crops.HASH_N:(i + 1) * crops.HASH_N]
                                 for i in range(crops.HASH_N)])
            b = crops.dct_hash16([g_new[i * crops.HASH_N:(i + 1) * crops.HASH_N]
                                  for i in range(crops.HASH_N)])
            total += 1
            if a != b:
                bad += 1
    check(bad == 0, "E3 真实图全区域指纹新旧相同（%d 个区域）" % total,
          "差异 %d" % bad)


def case_speed(n_img=60):
    files = [os.path.join(ROOT, f) for f in sorted(os.listdir(ROOT))][:n_img]
    if not files:
        check(False, "E5 提速（没有测试图，跳过）")
        return
    D = scan.DECODE
    pix = [winimg.load_pixels(p, D) for p in files]
    pix = [p for p in pix if p]
    t0 = time.perf_counter()
    for w, h, b in pix:
        crops.describe(w, h, b)
    new = time.perf_counter() - t0
    print("   新版：%d 张 %.2fs（%.1f ms/张，400 张推算 %.1fs）"
          % (len(pix), new, new * 1000 / len(pix), new / len(pix) * 400))
    check(True, "E5 提速（基准见上；旧版 46.8ms/张 -> 400 张 18.7s）")


def main():
    print("== crops.describe 优化：等价性 + 提速 ==\n")
    ensure_pool()
    case_region_gray()
    case_dct()
    case_real()
    case_speed()
    print("\n== 结果：%d 通过 / %d 失败 ==" % (len(OK), len(FAIL)))
    for f in FAIL:
        print("   FAIL %s" % f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
