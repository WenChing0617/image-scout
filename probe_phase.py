# -*- coding: utf-8 -*-
"""把「扫描 -> 配对 -> 分组 -> 填列表」按段计时，找出 23 秒到底花在哪。

已知（probe_prog.py）：主线程在 `_progress` 上耗的 7.7 秒**不是画控件**，
是等 GIL（静默 300 次 33.8ms vs 4 线程抢 133s）。所以真正要看的是
**后台各阶段自己的墙钟时间**，以及能不能并行。

用法：python probe_phase.py [张数] [--workers N]
"""
from __future__ import annotations

import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import scan                          # noqa: E402
import thumbs                        # noqa: E402

ROOT = os.path.join(os.environ.get("TEMP", "."), "ImageScout-prog")


def make_photo(path, w, h, seed):
    import random
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


def main():
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    n = int(argv[0]) if argv else 400
    workers = 4
    for a in sys.argv[1:]:
        if a.startswith("--workers"):
            workers = int(a.split("=")[1]) if "=" in a else 4
    if not (os.path.isdir(ROOT) and len(os.listdir(ROOT)) >= n):
        shutil.rmtree(ROOT, ignore_errors=True)
        os.makedirs(ROOT, exist_ok=True)
        for i in range(n):
            make_photo(os.path.join(ROOT, "p%04d.png" % i), 800, 600, i // 8)

    files = [os.path.join(ROOT, f) for f in sorted(os.listdir(ROOT))[:n]]
    print("%d 张图，workers=%d" % (len(files), workers))

    # 每次都从空缓存开始量（不然第二次全是缓存命中，量不到解码）
    store = scan.FingerStore(path=os.path.join(
        os.environ["TEMP"], "_phase_cache_%d.json" % time.time()))
    t0 = time.perf_counter()
    descs, stats = scan.describe_paths(files, workers=workers, store=store)
    t1 = time.perf_counter()
    print("  1) 指纹（解码 + crops.describe）: %6.2fs   %s" % (t1 - t0, stats))

    links = scan.find_links(descs)
    t2 = time.perf_counter()
    print("  2) 配对 find_links:              %6.2fs   %d 对"
          % (t2 - t1, len(links)))

    groups, weak = scan.build_groups(descs, links)
    t3 = time.perf_counter()
    print("  3) 分组 build_groups:            %6.2fs   %d 组 / %d 疑似"
          % (t3 - t2, len(groups), len(weak)))
    print("  合计 %.2fs，其中指纹占 %.0f%%"
          % (t3 - t0, (t1 - t0) / max(1e-9, t3 - t0) * 100))

    # ---- 单进程（workers=1）对照：看多线程有没有真加速 ----------------
    store2 = scan.FingerStore(path=os.path.join(
        os.environ["TEMP"], "_phase_cache2_%d.json" % time.time()))
    t0 = time.perf_counter()
    scan.describe_paths(files, workers=1, store=store2)
    t1 = time.perf_counter()
    print("  指纹（workers=1）对照:           %6.2fs" % (t1 - t0))
    print("  -> 4 线程相对 1 线程的加速比：%s"
          % ("（上面两行不同缓存，仅供参考）" if True else ""))


if __name__ == "__main__":
    main()
