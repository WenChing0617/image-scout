# -*- coding: utf-8 -*-
"""隔离文件夹 + 完全重复查找 —— 纯逻辑，零第三方依赖。

**删除不是真删。** 界面上的「删除」一律是把文件**移动**到扫描文件夹下的
`_隔离` 子目录：

    扫描文件夹/
      照片.jpg
      _隔离/            <- 删掉的都进这里
        照片.jpg
        照片-1.jpg      <- 重名自动加序号

这么设计有三个好处：出错了能拖回来、不需要回收站权限、跟「扫描文件夹」
放在一起（用户明说了要放这儿）。扫描时 `_隔离` 会被自动跳过
（见 `scan.iter_images`），所以删完再扫一遍不会又冒出来。

完全重复的判据是**文件内容哈希（sha256）**，也就是字节级 100% 相同 ——
只保证「一模一样」的才叫重复，重新编码过的（同一张图存成 jpg 和 png）
不会被算进来，宁可少删不可误删。为了不做无用的全量读盘，
先按**文件大小**分组，只有大小相同的才去算哈希。
"""

from __future__ import annotations

import hashlib
import os
import shutil
from concurrent.futures import ThreadPoolExecutor

import imgsize

#: 隔离目录名（放在扫描根目录下）。也用于扫描时跳过。
QUARANTINE_NAME = "_隔离"

_CHUNK = 1 << 20            # 算哈希时的分块（1 MB）


# ---------------------------------------------------------------------------
# 隔离目录
# ---------------------------------------------------------------------------

def pick_root(path: str, roots) -> str | None:
    """找出这张图属于哪个扫描源。

    规则：文件自己就是一个扫描源 → 用它的父目录；在某目录扫描源之下
    （递归扫子目录时很常见）→ 用那个目录扫描源。都匹配不上返回 None。
    """
    if not path:
        return None
    ap = os.path.abspath(path)
    best = None
    for r in roots or ():
        ar = os.path.abspath(r)
        if os.path.isfile(ar):
            if os.path.normcase(ar) == os.path.normcase(ap):
                cand = os.path.dirname(ar)
            else:
                continue
        else:
            if os.path.normcase(ap).startswith(
                    os.path.normcase(ar + os.sep)):
                cand = ar
            else:
                continue
        # 嵌套的扫描源取最深的那个（不然删到外层的隔离夹里去）
        if best is None or len(cand) > len(best):
            best = cand
    return best


def quarantine_dir_for(path: str, roots) -> str:
    """这张图该进哪个隔离夹。匹配不上就退回图片自己所在目录。"""
    root = pick_root(path, roots) or os.path.dirname(os.path.abspath(path))
    if os.path.basename(root) == QUARANTINE_NAME:
        return root                      # 已经在隔离夹里了，别再套一层
    return os.path.join(root, QUARANTINE_NAME)


def is_quarantined(path: str) -> bool:
    """路径是否已经在某个隔离夹里。"""
    parts = os.path.normcase(os.path.abspath(path)).replace("/", "\\").split("\\")
    return os.path.normcase(QUARANTINE_NAME) in parts


def unique_path(dest_dir: str, name: str) -> str:
    """目标重名时加 `-1` / `-2`…，别把已有的文件覆盖掉。"""
    dest = os.path.join(dest_dir, name)
    if not os.path.exists(dest):
        return dest
    stem, ext = os.path.splitext(name)
    n = 1
    while True:
        cand = os.path.join(dest_dir, "%s-%d%s" % (stem, n, ext))
        if not os.path.exists(cand):
            return cand
        n += 1


def isolate(path: str, dest_dir: str):
    """把一张图移进隔离夹。返回 (是否成功, 落地路径或错误原因)。"""
    if not path or not os.path.isfile(path):
        return False, "文件不在了"
    name = os.path.basename(path)
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError as e:
        return False, "建不了隔离夹：%s" % e
    dest = unique_path(dest_dir, name)
    if os.path.normcase(os.path.abspath(dest)) == \
            os.path.normcase(os.path.abspath(path)):
        return False, "源和目标是同一个文件"
    try:
        shutil.move(path, dest)          # 同盘是改名，秒完
        return True, dest
    except Exception:
        pass
    # 跨盘 / 被占用：退化成「复制 + 删源」。先复制成功再删，绝不先删。
    try:
        shutil.copy2(path, dest)
        os.remove(path)
        return True, dest
    except Exception as e:
        if os.path.exists(dest):
            try:
                os.remove(dest)          # 复制到一半的残file别留在隔离夹里
            except OSError:
                pass
        return False, "%s: %s" % (type(e).__name__, e)


def isolate_many(paths, roots, on_progress=None, should_stop=None):
    """批量隔离。返回 (成功列表[(源, 落地)], 失败列表[(源, 原因)])。

    同一批里可能有来自不同扫描文件夹的图，所以逐个算隔离目录。
    """
    done, failed = [], []
    total = len(paths)
    for i, p in enumerate(paths):
        if should_stop and should_stop():
            break
        ok, info = isolate(p, quarantine_dir_for(p, roots))
        (done if ok else failed).append((p, info))
        if on_progress:
            on_progress(i + 1, total)
    return done, failed


# ---------------------------------------------------------------------------
# 完全重复
# ---------------------------------------------------------------------------

def file_hash(path: str, chunk: int = _CHUNK):
    """文件内容的 sha256（十六进制）。读不出来返回 None。"""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                b = f.read(chunk)
                if not b:
                    break
                h.update(b)
    except OSError:
        return None
    return h.hexdigest()


def find_exact_dups(paths, on_progress=None, should_stop=None, workers: int = 4):
    """找出**内容完全相同**的文件。返回 [[路径, 路径, ...], ...]（每组 >= 2 个）。

    ⚠️ 先按文件大小分组：大小不同的文件**不可能**字节相同，直接跳过 ——
    真实场景里这一步能省掉 99% 的读盘。只有同大小的才去算 sha256。
    """
    by_size = {}
    for p in paths:
        if should_stop and should_stop():
            return []
        try:
            by_size.setdefault(os.path.getsize(p), []).append(p)
        except OSError:
            continue
    cand = [v for v in by_size.values() if len(v) > 1]
    todo = [p for v in cand for p in v]
    total = len(todo)
    if on_progress:
        on_progress(0, total)

    hashes = {}
    if todo:
        nw = max(1, min(int(workers or 1), len(todo)))
        done = 0
        with ThreadPoolExecutor(max_workers=nw) as ex:
            for p, h in zip(todo, ex.map(file_hash, todo)):
                if should_stop and should_stop():
                    break
                done += 1
                if h:
                    hashes.setdefault(h, []).append(p)
                if on_progress:
                    on_progress(done, total)

    groups = [v for v in hashes.values() if len(v) > 1]
    for v in groups:
        v.sort()
    groups.sort(key=lambda v: (-len(v), v[0]))
    return groups


def rank_for_keeping(path: str):
    """「保留哪张」的排序键（越大越该留）。

    先看像素总量（分辨率高的清楚），再比文件大小，再看修改时间，最后
    路径短的优先（通常是自己整理的目录，而不是某个深层缓存）。
    """
    px = 0
    wh = None
    try:
        wh = imgsize.size_of_file(path)
        px = int(wh[0]) * int(wh[1])
    except Exception:
        pass
    try:
        st = os.stat(path)
        size, mt = int(st.st_size), float(st.st_mtime)
    except OSError:
        size, mt = 0, 0.0
    return (px, size, mt, -len(path))


def split_keep(paths):
    """把一组完全重复的文件拆成 (保留的那张, [其余])。"""
    if not paths:
        return None, []
    ordered = sorted(paths, key=rank_for_keeping, reverse=True)
    return ordered[0], ordered[1:]


def plan_exact_dups(groups):
    """把 `find_exact_dups` 的结果整理成 [(保留, [删除...], 省下字节数)]。"""
    out = []
    for g in groups:
        keep, drop = split_keep(g)
        freed = 0
        for p in drop:
            try:
                freed += os.path.getsize(p)
            except OSError:
                pass
        out.append((keep, drop, freed))
    return out
