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
import time
from concurrent.futures import ThreadPoolExecutor

import imgsize

#: 隔离目录名（放在扫描根目录下）。也用于扫描时跳过。
QUARANTINE_NAME = "_隔离"

_CHUNK = 1 << 20            # 算哈希时的分块（1 MB）

# ---------------------------------------------------------------------------
# 「文件被占用」重试（2026-10-08，主人报的「删除过快就弹移动失败」）
# ---------------------------------------------------------------------------
#
# ⚠️⚠️ 病根：Windows 上只要**任何人**拿着文件句柄，`shutil.move`（改名）就会
#    `PermissionError: [WinError 32] 另一个程序正在使用此文件`。
#    而本程序自己就有三条后台线程在解图 —— 预热（`_prewarm_tick`）、
#    基准像素（`_base_prep_go` → `BigCache.base`）、NIQE（走 GDI+）。
#    主线程手一快（连按 D / Delete、一键清理重复），就会正好撞上
#    「后台正在解这张图、你正要把这张图移走」—— 撞了就报错弹窗。
#
#   实测复现（`probe_lock.py`）：`open(p,'rb')` 拿住句柄不关，
#    `shutil.move` 必报上面那个 WinError 32 原话。
#
# ⚠️ 关键认识：**这不是「删不掉」，是「晚了几十毫秒」。**
#    后台解一张 640px 的图只要几十毫秒，句柄随即释放。
#    所以正确做法是**等一下再试**，而不是立刻把失败糊到用户脸上。
_TRIES = 6
_WAITS = (0.03, 0.06, 0.12, 0.24, 0.45)     # 累计约 0.90s（GDI+ 解一张大图要几百 ms）
#: 「被占用」的 WinError 码：32=SHARING_VIOLATION、33=LOCK_VIOLATION、5=ACCESS_DENIED
_BUSY_WINERR = (32, 33, 5)


def _is_busy(exc) -> bool:
    """这是「文件正被占用」（等一下就好），还是「真不行」（重试无意义）？

    ⚠️ 必须分清：权限不足 / 路径非法这类错误**重试 6 次也是白等**，
       只会让用户多卡 0.6 秒才看到错误。所以只有「被占用」才值得重试。
    """
    if getattr(exc, "winerror", None) in _BUSY_WINERR:
        return True
    s = str(exc)
    return ("另一个程序正在使用此文件" in s
            or "being used by another process" in s
            or "正被另一进程使用" in s)


def _move_once(path: str, dest: str):
    """试一次搬运。返回 (是否成功, 异常或 None)。

    ⚠️ **「被占用」时不再退化成「复制 + 删源」**：`os.remove(源)` 会报同一个
       WinError 32，白拷一份整个文件（20MB 的图拷 6 次就是 120MB 无效 IO）。
       那种情况直接交给调用方睡一下重试。
    """
    try:
        shutil.move(path, dest)     # 同盘=改名；跨盘 `shutil.move` 自己会 copy2+unlink
        return True, None
    except Exception as e:
        first = e
    if _is_busy(first):
        return False, first
    # 不是「被占用」才值得试「复制 + 删源」（跨盘等）。先复制成功再删，绝不先删。
    try:
        shutil.copy2(path, dest)
        os.remove(path)
        return True, None
    except Exception as e:
        if os.path.exists(dest):
            try:
                os.remove(dest)          # 复制到一半的残file别留在隔离夹里
            except OSError:
                pass
        return False, (e if _is_busy(e) else first)


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
    """把一张图移进隔离夹。返回 (是否成功, 落地路径或错误原因)。

    **被占用时会自动等一下再试**（见文件顶部那段）。这也是为什么它可能要花
    最多 ~0.6 秒才返回 —— 换来的是一般情况下不再弹「移动失败」。
    """
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

    last = None
    for i in range(_TRIES):
        ok, err = _move_once(path, dest)
        if ok:
            return True, dest
        last = err
        if not _is_busy(err):
            break                       # 不是「被占用」=> 重试也是白等，早点报错
        if i >= _TRIES - 1:
            break
        time.sleep(_WAITS[min(i, len(_WAITS) - 1)])

    # 收拾残局：多次尝试可能在隔离夹里留下半截拷贝（源还在 = 没搬成）
    # ⚠️ 只删 `dest`（`unique_path` 保证它原本不存在），别碰别的。
    if os.path.isfile(path) and os.path.isfile(dest):
        try:
            os.remove(dest)
        except OSError:
            pass

    if _is_busy(last):
        return False, ("文件一直被占用着（等了 %.1f 秒、试了 %d 次）—— "
                       "多半是正在看这张图、或别的程序开着它。"
                       "稍等一下再删一次通常就好了。"
                       % (sum(_WAITS[:max(0, _TRIES - 1)]), _TRIES))
    return False, "%s: %s" % (type(last).__name__, last)


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
