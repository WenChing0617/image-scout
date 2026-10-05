# -*- coding: utf-8 -*-
"""统一评估最终判据：三类样本放一起看混淆矩阵。

三类：
  CROP  同源裁剪变体（同一底图的 full / 方形 / 16:9 / 3:4 / 裁边 / 偏心 / 缩半）
        -> 必须认出来（强判据）
  SCENE 同场景不同构图（同一大画布上不同取景 + 亮度对比度抖动 + 噪点）
        -> 尽量认出来（宽松判据）
  NEG   不同图案之间（干净反例）
  NOISE 随机噪声图（与谁都无关）

判据（两级）：
  强：corr >= C1 且 哈希 <= H1
  宽松：corr >= C2 且 哈希 <= H2
"""
import itertools
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import crops          # noqa: E402
import makeset        # noqa: E402
import winimg         # noqa: E402

# 样本放 %TEMP%，重跑标定不会把工程目录弄脏
BASE = os.path.join(tempfile.gettempdir(), "ImageScout-tuneset")
CROP_ROOT = os.path.join(BASE, "_tuneset")
SCENE_ROOT = os.path.join(BASE, "_sceneset")
DECODE = 128
TOPK = 3
SLACK = 2

C1, H1 = 0.86, 10      # 强判据
C2, H2 = 0.72, 12      # 宽松判据


def gate_score(da, db):
    a, b = da["cset"], db["cset"]
    if not a or not b:
        return 0.0
    inter = (a & b).bit_count()
    return max(inter / float((a | b).bit_count() or 1),
               inter / float(a.bit_count()), inter / float(b.bit_count()))


def best_pair(da, db):
    """相关最高的候选区域对 -> (corr, hash, i, j)。"""
    _, cand = crops.pair_region_stats(da, db, TOPK, SLACK)
    best = (0.0, 99, 0, 0)
    for d, i, j in cand:
        va, _ = crops.region_ver(da, da["rects"][i])
        vb, _ = crops.region_ver(db, db["rects"][j])
        c = crops.corr(va, vb)
        if c > best[0]:
            best = (c, d, i, j)
    return best


def main():
    print("造集")
    crops_set, noise = makeset.build_set(CROP_ROOT)
    scene_set = makeset.build_scene_set(SCENE_ROOT)

    paths, cls, fam = [], {}, {}
    for g, fs in crops_set.items():
        for f in fs:
            paths.append(f)
            cls[f], fam[f] = "CROP", g[3:]
    for f in noise:
        paths.append(f)
        cls[f], fam[f] = "NOISE", "noise_" + os.path.basename(f)
    for g, fs in scene_set.items():
        for f in fs:
            paths.append(f)
            cls[f], fam[f] = "SCENE", g[3:]

    t0 = time.time()
    desc = {}
    for f in paths:
        got = winimg.load_pixels(f, DECODE)
        if not got:
            continue
        w, h, bgra = got
        desc[f] = crops.describe(w, h, bgra)
    dt = time.time() - t0
    print("描述 %d 张，%.1fs (%.1f ms/张)，区域 %d/张，MIN_AREA=%.2f FLAT_STD=%.1f"
          % (len(desc), dt, 1000 * dt / len(desc),
             len(next(iter(desc.values()))["rects"]),
             crops.MIN_AREA, crops.FLAT_STD))

    counts = {}
    rows = []
    t0 = time.time()
    for a, b in itertools.combinations(list(desc), 2):
        # 标注按**图案种类**（fam），不按集合 —— 同一种图案换个尺寸渲染，
        # 内容本来就是同一套（图案是按 w,h 相对参数化的），必须算正例。
        pos = (fam[a] == fam[b] and fam[a] != "noise")
        if pos:
            label = ("CROP" if cls[a] == "CROP" and cls[b] == "CROP"
                     else "SCENE" if cls[a] == "SCENE" and cls[b] == "SCENE"
                     else "CROSS")
        else:
            label = "NEG"
        counts[label] = counts.get(label, 0) + 1
        g = gate_score(desc[a], desc[b])
        c, d, i, j = best_pair(desc[a], desc[b])
        rows.append((label, g, c, d, a, b))
    dt = time.time() - t0
    print("两两 %d 对，%.1fs (%.2f ms/对)  样本分布 %s"
          % (len(rows), dt, 1000 * dt / len(rows), counts))

    def stat(label, idx=None):
        arr = [r for r in rows if r[0] == label]
        return arr

    print("\n=== 最终判据 ===")
    print("  强：corr>=%.2f 且 哈希<=%d     宽松：corr>=%.2f 且 哈希<=%d"
          % (C1, H1, C2, H2))
    for label in ("CROP", "SCENE", "CROSS", "NEG"):
        arr = [r for r in rows if r[0] == label]
        if not arr:
            continue
        strong = [r for r in arr if r[2] >= C1 and r[3] <= H1]
        loose = [r for r in arr if r[2] >= C2 and r[3] <= H2]
        print("  %-6s 共 %5d 对：强档命中 %4d (%.1f%%)，宽松档命中 %4d (%.1f%%)"
              % (label, len(arr), len(strong), 100 * len(strong) / len(arr),
                 len(loose), 100 * len(loose) / len(arr)))

    print("\n=== 按闸门 G 再看一遍（闸门 + 判据 联合）===")
    for G in (0.0, 0.60, 0.70, 0.75, 0.80):
        out = []
        for label in ("CROP", "SCENE", "CROSS", "NEG"):
            tot = counts.get(label, 0)
            if not tot:
                continue
            arr = [r for r in rows if r[0] == label and r[1] >= G]
            strong = sum(1 for r in arr if r[2] >= C1 and r[3] <= H1)
            loose = sum(1 for r in arr if r[2] >= C2 and r[3] <= H2)
            out.append("%s 强%4d 松%4d /%5d" % (label, strong, loose, tot))
        print("  G>=%.2f  %s" % (G, "  ".join(out)))

    print("\n=== 扫判据（P=三类正例合计 360，N=NEG 2961）===")
    P = [r for r in rows if r[0] != "NEG"]
    N = [r for r in rows if r[0] == "NEG"]
    print("    漏掉的真相似对里 corr 大量是 1.000（裁剪不改变结构），")
    print("    卡住它们的是**哈希上限**而不是相关 —— 所以这里把哈希往上扫。")
    for G in (0.0, 0.50, 0.60, 0.70):
        print("  ---- 闸门 G >= %.2f ----" % G)
        print("      C      H=8            H=10           H=14           H=18"
              "           H=22           H=∞")
        for C in (0.86, 0.90, 0.93, 0.95, 0.97):
            cells = []
            for H in (8, 10, 14, 18, 22, 999):
                tp = sum(1 for r in P if r[1] >= G and r[2] >= C and r[3] <= H)
                fp = sum(1 for r in N if r[1] >= G and r[2] >= C and r[3] <= H)
                cells.append("%4d/%-4d" % (tp, fp))
            print("     %.2f   %s" % (C, "  ".join(cells)))

    print("\n=== 分组级评估（真正该看的指标）===")
    print("    分组是 union-find 连出来的：组内只要有若干条边连起来就算对了，")
    print("    所以逐对召回 50% 也可能把整组找回来。")
    families = {}
    for f in desc:
        families.setdefault(fam[f], []).append(f)
    targets = {k: v for k, v in families.items()
               if len(v) > 1 and not k.startswith("noise_")}
    print("    待分组 %d 组（共 %d 张），另外 %d 张噪声图各自成组"
          % (len(targets), sum(len(v) for v in targets.values()),
             sum(1 for k, v in families.items() if k.startswith("noise_"))))

    for G in (0.0, 0.55, 0.60, 0.70):
        for C, H in ((0.86, 10), (0.86, 14), (0.93, 14), (0.93, 18), (0.93, 20),
                     (0.95, 18), (0.97, 20)):
            parent = {f: f for f in desc}

            def find(x):
                while parent[x] != x:
                    parent[x] = parent[parent[x]]
                    x = parent[x]
                return x

            def union(x, y):
                rx, ry = find(x), find(y)
                if rx != ry:
                    parent[rx] = ry

            n_edges = 0
            for r in rows:
                if r[1] >= G and r[2] >= C and r[3] <= H:
                    union(r[4], r[5])
                    n_edges += 1
            comp = {}
            for f in desc:
                comp.setdefault(find(f), []).append(f)
            # 每张图所属的 family 数（= 该连通块里的 family 数）
            fams_of_comp = {k: {fam[x] for x in v} for k, v in comp.items()}
            ok = contam = split = 0
            for name, members in targets.items():
                groups = {find(m) for m in members}
                if len(groups) > 1:
                    split += 1
                elif len(fams_of_comp[next(iter(groups))]) > 1:
                    contam += 1
                else:
                    ok += 1
            noise_merged = sum(1 for k, v in comp.items()
                               if len(v) > 1 and all(fam[x].startswith("noise_")
                                                     for x in v))
            print("    G>=%.2f C>=%.2f H<=%-3s 边%5d | 完整组 %3d/%3d  被拆 %2d  "
                  "被污染 %2d  噪声互相合并 %d"
                  % (G, C, "∞" if H == 999 else H, n_edges, ok, len(targets),
                     split, contam, noise_merged))

    print("\n=== 交付预设（界面里的灵敏度档，与 scan.PRESETS 同一份数字）===")
    import scan as scanmod
    families_all = {}
    for f in desc:
        families_all.setdefault(fam[f], []).append(f)
    tg = {k: v for k, v in families_all.items()
          if len(v) > 1 and not k.startswith("noise_")}
    print("    正例对 %d，反例对 %d，待分组 %d 组"
          % (len(P), len(N), len(tg)))
    print("    %-9s %-6s %-6s %-6s %-6s | %s"
          % ("档", "闸门", "强C/H", "松C/H", "边数", "逐对 强TP/FP  松TP/FP | "
             "分组 完整/被拆/污染/噪声合并"))
    for key, th in scanmod.PRESETS.items():
        G = th["gate"]
        _c1, _h1 = th["corr_strong"], th["hash_max"]
        _c2, _h2 = th["corr_loose"], th["hash_loose"]
        p_strong = sum(1 for r in P if r[1] >= G and r[2] >= _c1 and r[3] <= _h1)
        p_loose = sum(1 for r in P if r[1] >= G and r[2] >= _c2 and r[3] <= _h2)
        f_strong = sum(1 for r in N if r[1] >= G and r[2] >= _c1 and r[3] <= _h1)
        f_loose = sum(1 for r in N if r[1] >= G and r[2] >= _c2 and r[3] <= _h2)

        parent = {f: f for f in desc}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(x, y):
            rx, ry = find(x), find(y)
            if rx != ry:
                parent[rx] = ry

        edges = 0
        for r in rows:
            if r[1] >= G and r[2] >= _c1 and r[3] <= _h1:
                union(r[4], r[5])
                edges += 1
        comp = {}
        for f in desc:
            comp.setdefault(find(f), []).append(f)
        fams_of_comp = {k: {fam[x] for x in v} for k, v in comp.items()}
        ok = contam = split = 0
        for members in tg.values():
            roots = {find(m) for m in members}
            if len(roots) > 1:
                split += 1
            elif len(fams_of_comp[next(iter(roots))]) > 1:
                contam += 1
            else:
                ok += 1
        nm = sum(1 for k, v in comp.items()
                 if len(v) > 1 and all(fam[x].startswith("noise_") for x in v))
        print("    %-9s %.2f   %.2f/%-2d  %.2f/%-2d  %5d | %4d/%-4d %4d/%-4d | "
              "%3d /%2d /%2d /%d"
              % (key, G, _c1, _h1, _c2, _h2, edges, p_strong, f_strong,
                 p_loose, f_loose, ok, split, contam, nm))

    print("\n=== 漏掉的正例（宽松档也不命中）===")
    miss = [r for r in P if not (r[2] >= C2 and r[3] <= H2)]
    for r in sorted(miss, key=lambda r: -r[2])[:12]:
        print("   %-8s corr %+.3f 哈希 %-3d 闸门 %.3f  %s | %s"
              % (r[0], r[2], r[3], r[1],
                 os.path.basename(r[4]), os.path.basename(r[5])))
    print("   共漏 %d / %d" % (len(miss), len(P)))

    print("\n=== 误报的 NEG（强档命中）===")
    fp = [r for r in N if r[2] >= C1 and r[3] <= H1]
    for r in sorted(fp, key=lambda r: -r[2])[:12]:
        print("   corr %+.3f 哈希 %-3d 闸门 %.3f  %s/%s | %s/%s"
              % (r[2], r[3], r[1], fam[r[4]], os.path.basename(r[4]),
                 fam[r[5]], os.path.basename(r[5])))
    print("   共误报 %d / %d" % (len(fp), len(N)))


if __name__ == "__main__":
    main()
