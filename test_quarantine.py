# -*- coding: utf-8 -*-
"""测试「隔离文件夹」和「完全重复查找」这两块纯逻辑。

重点核对几件容易出错的事：
  1. 隔离夹到底建在哪个目录（扫描源是文件夹 vs 直接加的图片、嵌套扫描源、
     图片在子目录里、已经躺在隔离夹里）
  2. 重名不覆盖（第二次同名要变成 `xxx-1.png`）
  3. **移动是真移动**：源没了、目标在、字节一致；跨盘退化成复制+删除也不丢数据
  4. 完全重复只认**字节 100% 相同**：大小相同但内容不同的一律不算
  5. 扫描时要跳过 `_隔离`，否则删完再扫又冒出来
所有文件都造在 %TEMP% 下，不碰工程目录。
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import quarantine        # noqa: E402
import scan              # noqa: E402
import thumbs            # noqa: E402

BASE = os.path.join(tempfile.gettempdir(), "ImageScout-quarantine-test")
Q = quarantine.QUARANTINE_NAME

FAIL = []


def check(cond, msg):
    print("   %s %s" % ("OK  " if cond else "FAIL", msg))
    if not cond:
        FAIL.append(msg)
    return cond


def write_png(path, w, h, rgb=(120, 160, 200)):
    """造一张纯色 PNG（要真图，好让 imgsize 能解析出分辨率）。"""
    px = bytes((rgb[2], rgb[1], rgb[0], 255)) * (w * h)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(thumbs.bgra_to_png(w, h, px))
    return path


def write_bytes(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path


def fresh(name):
    d = os.path.join(BASE, name)
    if os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    return d


# ---------------------------------------------------------------------------

def test_dir_choice():
    print("\n=== A. 隔离夹建在哪 ===")
    root = fresh("roots")
    sub = os.path.join(root, "sub", "deep")
    os.makedirs(sub, exist_ok=True)
    a = write_png(os.path.join(root, "a.png"), 8, 8)
    b = write_png(os.path.join(sub, "b.png"), 8, 8)
    single = write_png(os.path.join(fresh("single"), "solo.png"), 8, 8)

    check(quarantine.quarantine_dir_for(a, [root]) == os.path.join(root, Q),
          "图片在扫描目录里 -> 扫描目录/%s" % Q)
    check(quarantine.quarantine_dir_for(b, [root]) == os.path.join(root, Q),
          "图片在扫描目录的子目录里 -> 还是归到扫描目录/%s" % Q)
    check(quarantine.quarantine_dir_for(single, [single])
          == os.path.join(os.path.dirname(single), Q),
          "扫描源是「单独添加的图片」-> 建在它的父目录下")

    loner = write_png(os.path.join(fresh("loner"), "x.png"), 8, 8)
    check(quarantine.quarantine_dir_for(loner, [root])
          == os.path.join(os.path.dirname(loner), Q),
          "哪个扫描源都不属于 -> 退回图片自己所在目录")

    # 嵌套扫描源：取最深的那个
    outer = os.path.join(root, "outer")
    inner = os.path.join(root, "inner")
    p = write_png(os.path.join(inner, "c.png"), 8, 8)
    check(quarantine.quarantine_dir_for(p, [outer, inner, root])
          == os.path.join(inner, Q),
          "有嵌套扫描源时取最深的那个（别删到外层去）")
    check(quarantine.pick_root(a, [root]) == root, "pick_root 认得目录扫描源")

    already = os.path.join(root, Q, "old.png")
    write_png(already, 8, 8)
    check(quarantine.quarantine_dir_for(already, [root])
          == os.path.join(root, Q), "已经在隔离夹里的图不会再套一层")
    check(quarantine.is_quarantined(already), "is_quarantined 认得出来")
    check(not quarantine.is_quarantined(a), "普通图片不算在隔离夹里")


def test_unique_and_isolate():
    print("\n=== B. 移动与重名 ===")
    root = fresh("move")
    src = write_png(os.path.join(root, "pic.png"), 12, 9)
    raw = open(src, "rb").read()
    dest_dir = quarantine.quarantine_dir_for(src, [root])

    # 先放一个同名文件占位
    write_bytes(os.path.join(dest_dir, "pic.png"), b"occupied")
    ok, dest = quarantine.isolate(src, dest_dir)
    check(ok, "移动成功")
    check(not os.path.exists(src), "源文件已经不在了")
    check(os.path.isfile(dest), "落地文件存在：%s" % os.path.basename(dest))
    check(os.path.basename(dest) == "pic-1.png", "重名自动加 -1（不覆盖已有的）")
    check(open(dest, "rb").read() == raw, "落地文件字节与原文件完全一致")
    check(open(os.path.join(dest_dir, "pic.png"), "rb").read() == b"occupied",
          "原来那个同名文件没被动过")

    ok2, why = quarantine.isolate(os.path.join(root, "不存在.png"), dest_dir)
    check(not ok2 and why, "文件不存在时如实报错（%s）" % why)

    # unique_path 连续去重
    p1 = quarantine.unique_path(dest_dir, "pic.png")
    check(os.path.basename(p1) == "pic-2.png", "第三次同名给 -2")


def test_isolate_many():
    print("\n=== C. 批量隔离（多个扫描源）===")
    r1 = fresh("multi1")
    r2 = fresh("multi2")
    f1 = write_png(os.path.join(r1, "one.png"), 8, 8)
    f2 = write_png(os.path.join(r2, "two.png"), 8, 8)
    done, failed = quarantine.isolate_many(
        [f1, f2, os.path.join(r1, "missing.png")], [r1, r2])
    check(len(done) == 2, "两个都移动成功（实际 %d）" % len(done))
    check(len(failed) == 1, "不存在的那张如实失败（实际 %d）" % len(failed))
    check(os.path.isfile(os.path.join(r1, Q, "one.png")),
          "第一张进了各自扫描源的隔离夹（r1）")
    check(os.path.isfile(os.path.join(r2, Q, "two.png")),
          "第二张进了各自扫描源的隔离夹（r2）")


def test_exact_dups():
    print("\n=== D. 完全重复（字节级）===")
    d = fresh("dups")
    # 三份一模一样（不同大小写名），一份分辨率不同，两份「大小相同但内容不同」
    body = open(write_png(os.path.join(d, "src.png"), 40, 30), "rb").read()
    a = write_bytes(os.path.join(d, "copyA.png"), body)
    b = write_bytes(os.path.join(d, "copyB.png"), body)
    c = write_bytes(os.path.join(d, "copyC.png"), body)
    other = write_png(os.path.join(d, "different.png"), 40, 30,
                      rgb=(10, 200, 90))

    same_size_1 = write_bytes(os.path.join(d, "sz1.bin"), b"X" * 4096)
    same_size_2 = write_bytes(os.path.join(d, "sz2.bin"), b"Y" * 4096)

    groups = quarantine.find_exact_dups([a, b, c, other, same_size_1,
                                         same_size_2])
    check(len(groups) == 1, "只有 1 组重复（实际 %d）" % len(groups))
    if groups:
        check(sorted(groups[0]) == sorted([a, b, c]),
              "这一组正好是那三份相同的（%d 个）" % len(groups[0]))
    check(all(other not in g for g in groups), "尺寸相同但内容不同的不算重复")
    check(all(same_size_1 not in g and same_size_2 not in g for g in groups),
          "「文件大小一样」不足以判重复 —— 必须真去比内容")

    # 只有一份的目录不该报重复
    check(quarantine.find_exact_dups([other]) == [], "独一份不报重复")

    print("\n=== E. 保留哪张 ===")
    big = write_png(os.path.join(d, "big.png"), 60, 60)
    small = write_png(os.path.join(d, "small.png"), 20, 20)
    keep, drop = quarantine.split_keep([small, big])
    check(keep == big, "保留分辨率最高的那张")
    check(drop == [small], "其余进待清理列表")
    plan = quarantine.plan_exact_dups([[small, big]])
    if plan:
        k, dr, freed = plan[0]
        check(k == big and dr == [small], "plan 里保留/清理分得清楚")
        check(freed == os.path.getsize(small), "省下的字节数算对了")
    check(quarantine.split_keep([]) == (None, []), "空输入不炸")


def test_scan_skips_quarantine():
    print("\n=== F. 扫描要跳过隔离夹 ===")
    d = fresh("skip")
    keep = write_png(os.path.join(d, "keep.png"), 16, 16)
    hidden = write_png(os.path.join(d, Q, "deleted.png"), 16, 16)
    found = scan.iter_images([d], recursive=True)
    check(keep in found, "正常图片被扫到")
    check(hidden not in found, "隔离夹里的图被跳过（删完再扫不会又冒出来）")

    files = scan.iter_images([d], recursive=False)
    check(hidden not in files, "非递归模式同样不把隔离夹当文件收进来")


def main():
    print("隔离夹名：%s" % Q)
    test_dir_choice()
    test_unique_and_isolate()
    test_isolate_many()
    test_exact_dups()
    test_scan_skips_quarantine()
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
