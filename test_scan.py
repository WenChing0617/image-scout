# -*- coding: utf-8 -*-
"""端到端测试扫描引擎：造集 -> 找图 -> 描述（含缓存往返）-> 配对 -> 分组。

同时核对两件事：
  1. 缓存 pack/unpack 之后描述子必须与原始完全一致（否则缓存会污染结果）
  2. 分组结果不能把不同图案混到一组（跨类污染必须为 0）
"""
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import crops          # noqa: E402
import makeset        # noqa: E402
import scan           # noqa: E402
import winimg         # noqa: E402

# 样本、缓存全放 %TEMP%，跑测试不会把工程目录弄脏
BASE = os.path.join(tempfile.gettempdir(), "ImageScout-selftest")
CROP_ROOT = os.path.join(BASE, "_tuneset")
SCENE_ROOT = os.path.join(BASE, "_sceneset")
CACHE = os.path.join(BASE, "scan-cache.json")

FAIL = []


def check(cond, msg):
    print("   %s %s" % ("OK  " if cond else "FAIL", msg))
    if not cond:
        FAIL.append(msg)


def fam_of(p):
    parts = p.replace("\\", "/").split("/")
    for i, s in enumerate(parts):
        if s in ("_tuneset", "_sceneset") and i + 1 < len(parts):
            return parts[i + 1][3:]
    return "?"


def main():
    print("=== 造测试集 ===")
    makeset.build_set(CROP_ROOT)
    makeset.build_scene_set(SCENE_ROOT)

    print("\n=== 找图 ===")
    paths = scan.iter_images([CROP_ROOT, SCENE_ROOT])
    print("   找到 %d 个文件" % len(paths))
    check(len(paths) == 82, "文件数应为 82（56 变体 + 2 噪声 + 24 取景），实际 %d"
          % len(paths))

    print("\n=== 缓存往返一致性 ===")
    got = winimg.load_pixels(paths[0], scan.DECODE)
    w, h, bgra = got
    d0 = crops.describe(w, h, bgra)
    d1 = scan.unpack_desc(scan.pack_desc(d0))
    same = (d0["gw"] == d1["gw"] and d0["gh"] == d1["gh"]
            and d0["gray"] == d1["gray"] and d0["hashes"] == d1["hashes"]
            and d0["rects"] == d1["rects"] and d0["cset"] == d1["cset"]
            and d0["hist"] == d1["hist"]
            and d0["buckets"] == d1["buckets"])
    check(same, "pack -> unpack 后描述子完全一致")
    if not same:
        for k in ("gw", "gh", "gray", "hashes", "rects", "cset", "hist",
                  "buckets"):
            print("      %s 一致: %s" % (k, d0[k] == d1[k]))

    print("\n=== 扫描（第一次，全解码）===")
    if os.path.exists(CACHE):
        os.remove(CACHE)
    store = scan.FingerStore(CACHE)
    t0 = time.time()
    descs, stats = scan.describe_paths(paths, workers=4, store=store)
    t1 = time.time()
    print("   %s  用时 %.2fs (%.1f ms/张)"
          % (stats, t1 - t0, 1000 * (t1 - t0) / max(1, len(paths))))
    check(stats["failed"] == 0, "解码失败 0 张（实际 %d）" % stats["failed"])
    check(len(descs) == len(paths), "全部拿到描述子")
    store.save()

    print("\n=== 扫描（第二次，应全部命中缓存）===")
    store2 = scan.FingerStore(CACHE)
    t0 = time.time()
    descs2, stats2 = scan.describe_paths(paths, workers=4, store=store2)
    t1 = time.time()
    print("   %s  用时 %.2fs" % (stats2, t1 - t0))
    check(stats2["cached"] == len(paths) and stats2["decoded"] == 0,
          "第二次全部走缓存")
    same_all = all(descs[p]["hashes"] == descs2[p]["hashes"] for p in paths)
    check(same_all, "缓存回来的指纹与第一次完全一致")

    print("\n=== 配对 ===")
    t0 = time.time()
    links = scan.find_links(descs, on_progress=None)
    t1 = time.time()
    n_pairs = len(descs) * (len(descs) - 1) // 2
    print("   %d 对 -> %d 条边，用时 %.2fs (%.2f ms/对)"
          % (n_pairs, len(links), t1 - t0, 1000 * (t1 - t0) / n_pairs))
    strong_bad = [l for l in links if l[2] == scan.UI_STRONG
                  and fam_of(l[0]) != fam_of(l[1])]
    check(not strong_bad, "跨图案的**强**边 0 条（实际 %d）" % len(strong_bad))
    loose_bad = [l for l in links if l[2] == scan.UI_LOOSE
                 and fam_of(l[0]) != fam_of(l[1])]
    print("   （宽松档跨图案边 %d 条，不参与分组，只在界面里当「疑似」显示）"
          % len(loose_bad))
    for l in loose_bad[:5]:
        print("      %s(%s) | %s(%s)  %s %.3f"
              % (os.path.basename(l[0]), fam_of(l[0]),
                 os.path.basename(l[1]), fam_of(l[1]), l[2], l[3]))

    print("\n=== 分组 ===")
    groups, weak_pairs = scan.build_groups(descs, links)
    print("   得到 %d 组，另有 %d 条未并入的疑似边"
          % (len(groups), len(weak_pairs)))
    for g in groups:
        fams = sorted({fam_of(m) for m in g["members"]})
        print("   [%s] %2d 张  强边 %2d 宽边 %2d  最强 %.3f  图案 %s"
              % (g["kind"], len(g["members"]), len(g["strong_links"]),
                 len(g["weak_links"]), g["best_strong"], ",".join(fams)))
    multi = [g for g in groups if len({fam_of(m) for m in g["members"]}) > 1]
    check(not multi, "没有跨图案的组（实际 %d）" % len(multi))

    fam_members = {}
    for p in paths:
        f = fam_of(p)
        if not f.startswith("noise_"):
            fam_members.setdefault(f, set()).add(p)
    covered = {}
    for g in groups:
        for m in g["members"]:
            covered.setdefault(fam_of(m), set()).add(m)
    print("\n   每个图案被分进组的张数：")
    for f in sorted(fam_members):
        print("      %-10s %d / %d" % (f, len(covered.get(f, ())),
                                       len(fam_members[f])))

    print("\n=== 汇总 ===")
    if FAIL:
        print("   %d 项失败：" % len(FAIL))
        for m in FAIL:
            print("     - %s" % m)
        return 1
    print("   全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
