# -*- coding: utf-8 -*-
"""注入验证：**放宽后的门槛（test_ui 260ms / probe_pan2 300ms）还抓得住真回归？**

⚠️⚠️⚠️ **为什么必须先证明这个，才算把门槛放宽这件事做完**
（v1.7 收尾，主人反复强调「每件事都要有测试钉住」）。

门槛从 140 放宽到 260 是为了消除误报。但**放宽的判据只能证明
「不误报」，不能证明「还抓得住真 bug」** —— 一个宽到 10 秒的门槛
同样「不误报」。所以必须双向验证：
    ① 不误报（基线连跑 N 次全过）    —— 已量：3 次 max135~205ms
    ② **还抓得住**（注入真回归要红）—— 本脚本干这个

## ⚠️⚠️ 上一版注入为什么无效（两次踩坑，务必先读）

**坑1：重复 `render_all` 是缓存命中，不会变慢。**
    第一次注入「补块做两遍」-> 注入后 max 171~217ms，跟基线同档，
    门槛当然抓不住。原因是第二次 `render_all(precise=0.75, only=col)`
    查 `big.photo(pkey)` **直接命中**（实测 0.6~2ms）——
    key 只依赖 `blkW/blkH/twant/zoom/视口位置`，这些都没变。
    ⇒ **要注入「真变慢」，必须让工作真的多做一遍。**

**坑2：`_INJECT_NOCACHE` 那个版本没生效。**
    在 `if img is not None:` 上加 `and not _INJECT_NOCACHE` 看起来对，
    但实测 max 119~167ms（反而略低）——因为 `img is None` 那条路
    最后还是会 `put_photo(pkey, img)` **把结果写回缓存**，
    于是「下一次」照样命中，注入等于没做。
    ⇒ 真要强制重算，得连 `put_photo` 一起跳过（本次的 `INJ` 就是这么做的：
    用 `**kw` 关键字参数控制，**不用环境变量**——环境变量在子进程里
    传不传得对是另一类坑，见下）。

## ⚠️⚠️ 注入必须放**子进程**里验证（这个坑踩过）

改源码文件对**当前进程里已 import 的模块无效**。如果注入后还在
同一个进程里 `importlib.reload` 或直接跑测试，量到的还是**旧代码**，
于是「注入后仍全绿」——**假的**。
    ⇒ 本脚本一律 `subprocess.call([sys.executable, PROBE])` 起子进程，
    子进程重新 import 改过的源码，看到的才是注入后的行为。

## 本脚本注入的三个「真·单帧失控」回归

判据是 `probe_pan2.py` 的 `mx`（四向帧间隔 max，门槛 300ms）和
`test_ui.py` 的 `拖动最慢一步`（门槛 260ms）。要能被抓到，
单帧必须> 门槛，也就是**一次补块的 ~100ms 要变成两三百毫秒**。

| 编号 | 注入内容 | 模拟的真实事故 |
|---|---|---|
| `DOUBLE` | `_pan_fill` 里`render_all` **调两次**，且强制第二次跳过缓存重算 | 「补块逻辑不小心做两遍」 |
| `SYNC` | 拖动补块时**同步解一档基准图**（`self.big.base(...)`） | 「有人把后台预热改回同步解码」 |
| `BOTH` | 两个一起 | 复合事故 |

    用法：
        python probe_inject_threshold.py            # 跑全部
        python probe_inject_threshold.py DOUBLE     # 只跑一个
    ⚠️ **无论跑成功还是失败，脚本最后一定还原源码**
       （`finally` 里 `shutil.copy` 回备份 + 校验字节数一致）。
"""
from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "image_scout.py")
BAK = os.path.join(HERE, "_inject_backup.py")
PROBE = os.path.join(HERE, "probe_pan2.py")
TESTUI = os.path.join(HERE, "test_ui.py")
PY = sys.executable

# 门槛：跟 test_ui.py / probe_pan2.py 里的保持一致
# ⚠️⚠️⚠️ **v1.7 收尾（2026-10-07）：门槛从 max 改成 p95**
#
# 改判据的起因就是这个脚本 —— 上一版「补块做两遍」的注入量到
# max 285ms，门槛 300 抓不住（test_ui 那边 247.6 vs 260 同样溜过去）。
# 查下来根因不是门槛太宽，是**统计量选错了**：`max` 是 460 个样本的
# 极值，基线自身噪声就有 38ms（129~168），门槛放哪都是拍脑袋。
# 换成 `p95` 后判别力碾压：基线最高 27.2ms，注入后最低 131.5ms，
# 中间空档 27~131，门槛 60 落在正中，两侧各留 2 倍余量。
TH_PROBE_P95 = 40.0
TH_PROBE_MAX = 500.0
TH_TESTUI_FILL = 500.0
TH_TESTUI_MAX = 500.0


def read(p):
    with io.open(p, encoding="utf-8") as f:
        return f.read()


def write(p, s):
    with io.open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(s)


# ---------------------------------------------------------------- 注入定义
#
# 统一形态：`(good, bad)` 一对字符串。`good` 是源码里**现在**的写法，
# `bad` 是「把 bug 放回去」的写法。成对定义是为了**缩进一定对得上**
# —— 上一版就是 `old2` 在源码里出现 2 处以上导致 `assertcount==1` 炸掉。
# 「松手后用 precise=True 补最终帧」在**所有** case 里都成立，所以
# 同步解一次基准在正确性上无害—— 正好用来造出「慢但不糊」的真回归。

INJ = {
    # ① 补块做两遍，且**强制第二遍真重算**（跳过缓存写回）
    "DOUBLE": [(
        """        self._pan_redraw_active = True
        try:
            self.render_all(precise=0.75, only=col)
        finally:
            self._pan_redraw_active = False""",
        """        self._pan_redraw_active = True
        try:
            self.render_all(precise=0.75, only=col)
            # ---- INJECT: 补块做两遍 ----
            _hold = self.big
            _pf = _hold.photo
            _hold.photo = lambda k: None
            _pf2 = _hold.put_photo
            _hold.put_photo = lambda k, v, **kw: None
            try:
                self.render_all(precise=0.75, only=col)
            finally:
                _hold.photo = _pf
                _hold.put_photo = _pf2
            # ---- /INJECT ----
        finally:
            self._pan_redraw_active = False""",
    )],
    # ② 拖动补块时同步解一档基准图（模拟「后台预热被改回同步」）
    #
    # ⚠️⚠️⚠️ **必须先逐出缓存，否则这个注入是假的**（同一个坑踩了两次）。
    #   `BigCache.base()` 内部是 `d.get(lv)` 命中就返回 —— 只调 `base()`
    #   的话**只有第一次**真解码（49~95ms），之后全是0ms 命中。
    #   于是「每步都同步解」的意图变成了「整个拖动只多花 95ms 一次」，
    #   探针量出来 max 跟基线同档 —— **我会误判成「门槛抓不住」**，
    #   实际是注入根本没生效。
    #   ⇒ 必须 `self.big._b.pop(_p, None)` 把这一路的档全逐掉，
    #     `base()` 才会真的去调 `winimg.load_pixels`。
    "SYNC": [(
        """        self._pan_redraw_active = True
        try:
            self.render_all(precise=0.75, only=col)
        finally:
            self._pan_redraw_active = False""",
        """        self._pan_redraw_active = True
        try:
            # ---- INJECT: 同步解一档基准（真解码 49~95ms/次）----
            try:
                _p = self.path_a if col == 0 else self.path_b
                if _p and os.path.exists(_p):
                    self.big._b.pop(_p, None)   # ⚠️ 必须逐出，否则是缓存命中
                    self.big.base(_p, 2000, 2000)
            except Exception:
                pass
            # ---- /INJECT ----
            self.render_all(precise=0.75, only=col)
        finally:
            self._pan_redraw_active = False""",
    )],
}

# ⚠️ `BOTH` 是**手写的合并版**，不是 `DOUBLE + SYNC` 拼起来。
#   原因：那两个的 `good` 串是同一段代码，逐个替换时第一个改完、
#   第二个必然匹配 0 处 —— 于是停在「改了一半」的半路（2026-10-07 踩到）。
INJ["BOTH"] = [(
    """        self._pan_redraw_active = True
        try:
            self.render_all(precise=0.75, only=col)
        finally:
            self._pan_redraw_active = False""",
    """        self._pan_redraw_active = True
        try:
            # ---- INJECT(SYNC): 同步解一档基准（真解码 49~95ms/次）----
            try:
                _p = self.path_a if col == 0 else self.path_b
                if _p and os.path.exists(_p):
                    self.big._b.pop(_p, None)   # ⚠️ 必须逐出，否则是缓存命中
                    self.big.base(_p, 2000, 2000)
            except Exception:
                pass
            # ---- /INJECT ----
            self.render_all(precise=0.75, only=col)
            # ---- INJECT(DOUBLE): 补块做两遍 ----
            _hold = self.big
            _pf = _hold.photo
            _hold.photo = lambda k: None
            _pf2 = _hold.put_photo
            _hold.put_photo = lambda k, v, **kw: None
            try:
                self.render_all(precise=0.75, only=col)
            finally:
                _hold.photo = _pf
                _hold.put_photo = _pf2
            # ---- /INJECT ----
        finally:
            self._pan_redraw_active = False""",
)]


# ---------------------------------------------------------------- 工具
def apply_inj(tag, pairs):
    """把 `pairs` 里的注入全部打进源码。返回还原函数。

    ⚠️⚠️ **必须先把所有锚点校验完，再动第一处替换**（2026-10-07 踩到）。
    原来是一边校验一边替换：`BOTH` 里的第二对锚点因为第一对已经改过
    同一段代码而匹配 0 处，于是 `raise SystemExit` —— **源码停在
    「第一对已注入、第二对没注入」的半路状态**，而此时 `restore()`
    还没 return、调用方的 `finally` 拿不到它。
    靠 `main()` 里那份独立 BAK 副本兜住了（源码确实没被污染），
    但那是**运气**，不是设计。⇒ 现在先全量校验，全过了才动手。
    """
    src = read(SRC)
    shutil.copyfile(SRC, BAK)
    bak = read(BAK)
    # ---- 第1 步：全量校验，一个都不许少 ----
    for good, bad in pairs:
        c = src.count(good)
        print("    [apply] %-7s 锚点匹配 %d 处%s"
              % (tag, c, "" if c == 1 else "  ⚠️ 不是 1 处！"))
        if c == 0:
            raise SystemExit("❌ 注入锚点没匹配上 —— 源码结构变了，"
                             "锚点得更新（good 串抄错或代码已改）")
    # ---- 第 2 步：校验全过了，才真的替换 ----
    for good, bad in pairs:
        src = src.replace(good, bad)
    write(SRC, src)
    print("    [apply] 共打入 %d 处注入，源码 %d -> %d 字节"
          % (len(pairs), len(bak.encode("utf-8")), len(src.encode("utf-8"))))
    assert "INJECT" in src, "注入没进去"

    def restore():
        shutil.copyfile(BAK, SRC)
        now = read(SRC)
        assert now == bak, "还原后内容与备份不一致 —— 停下，别继续跑！"
        assert "INJECT" not in now, "还原后仍有 INJECT 残留"
        print("    [restore] 已还原并校验（%d 字节，无 INJECT 残留）"
              % len(now.encode("utf-8")))

    return restore


def run(script, timeout=900):
    """跑一个脚本，返回 (exitcode, stdout+stderr)。"""
    env = dict(os.environ)
    t0 = time.time()
    p = subprocess.run([PY, script], cwd=HERE, env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       timeout=timeout)
    return p.returncode, p.stdout.decode("utf-8", "replace"), time.time() - t0


MX_RE = re.compile(r"^(右|左|下|上)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)")


def parse_mx(out):
    """从 probe_pan2 输出里取四向 (p50, p95, max)。"""
    v = []
    for ln in out.splitlines():
        m = MX_RE.match(ln.strip())
        if m:
            v.append((float(m.group(2)), float(m.group(3)),
                      float(m.group(4))))
    return v


def parse_ui(out):
    """从 test_ui 输出里取「补块步最慢」和「全样本最慢」。

    ⚠️ `test_ui` 场景里补块只占 3%（2 次 / 约 60 步），
    所以分位数抓不到它 —— 判据量的是 `fill_pans`（补块那几步）自己。
    解析口径要跟着判据走，否则量到的数不是同一个东西。
    """
    fill = re.search(r"补块那一步无极端失控（补块 (\d+) 步，最慢 ([\d.]+) ms",
                     out)
    mx = re.search(r"拖动无极端单帧（最慢 ([\d.]+) ms", out)
    return (float(fill.group(2)) if fill else None,
            float(mx.group(1)) if mx else None)


def show(out, keep):
    for ln in out.splitlines():
        s = ln.strip()
        if any(k in s for k in keep):
            print("| %s" % s[:150])


# ---------------------------------------------------------------- 主流程
def one(tag, pairs):
    print("\n" + "=" * 70)
    print("### 注入 [%s] —— 期望：门槛变红" % tag)
    print("=" * 70)
    restore = apply_inj(tag, pairs)
    try:
        rc_p, out_p, t_p = run(PROBE)
        mx = parse_mx(out_p)
        print("\n  [probe_pan2] exit=%d  用时 %.0fs" % (rc_p, t_p))
        if mx:
            print("  [probe_pan2] 四向 p95 = %s  门槛 %.0f"
                  % (" / ".join("%.1f" % x[1] for x in mx), TH_PROBE_P95))
            print("  [probe_pan2] 四向 max = %s  门槛 %.0f"
                  % (" / ".join("%.1f" % x[2] for x in mx), TH_PROBE_MAX))
            caught_p = (max(x[1] for x in mx) > TH_PROBE_P95
                        or max(x[2] for x in mx) > TH_PROBE_MAX)
        else:
            print("  [probe_pan2] ⚠️ 没解析到四向数据：")
            show(out_p, ["Traceback", "Error", "assert", "❌"])
            caught_p = rc_p != 0

        rc_u, out_u, t_u = run(TESTUI)
        uifi, uimx = parse_ui(out_u)
        print("\n  [test_ui]    exit=%d  用时 %.0fs" % (rc_u, t_u))
        if uifi is not None:
            print("  [test_ui]    补块步最慢 = %.1f ms  门槛 %.0f"
                  % (uifi, TH_TESTUI_FILL))
            print("  [test_ui]    全样本最慢 = %s ms  门槛 %.0f"
                  % (uimx, TH_TESTUI_MAX))
            caught_u = (uifi > TH_TESTUI_FILL or
                        (uimx is not None and uimx > TH_TESTUI_MAX))
        else:
            print("  [test_ui]    ⚠️ 没跑到那条判据（可能更早就红了）：")
            show(out_u, ["FAIL", "❌", "Traceback"])
            caught_u = rc_u != 0
    finally:
        restore()

    caught = caught_p or caught_u
    verdict = ("✅ 门槛抓住了" if caught else
               "❌❌ 门槛抓不住 —— 判据是空的！")
    print("\n  ==> %s（probe_pan2 %s / test_ui %s）"
          % (verdict, "红" if caught_p else "绿",
             "红" if caught_u else "绿"))
    return caught


def baseline(n=1):
    print("\n" + "=" * 70)
    print("### 基线（无注入，%d 次）—— 期望：全绿" % n)
    print("=" * 70)
    src = read(SRC)
    assert "INJECT" not in src, "跑基线前源码里不该有 INJECT"
    allp95, allmax, allui = [], [], []
    ok = True
    for i in range(n):
        rc, out, t = run(PROBE)
        mx = parse_mx(out)
        rc_u, out_u, _ = run(TESTUI)
        uifi, uimx = parse_ui(out_u)
        if mx:
            allp95 += [x[1] for x in mx]
            allmax += [x[2] for x in mx]
        if uifi is not None:
            allui.append(uifi)
        print("  run%d: probe exit=%d 四向 p95=%s | test_ui exit=%d p95=%s"
              % (i + 1, rc,
                 "/".join("%.0f" % x[1] for x in mx) if mx else "无",
                 rc_u, ("%.0fms" % uifi) if uifi else "未跑到"))
        if rc != 0 or rc_u != 0 or not mx or uifi is None:
            ok = False
            show(out, ["❌", "FAIL", "Traceback"])
            show(out_u, ["❌", "FAIL", "Traceback"])
    if allp95:
        print("\n  基线 %d 次共 %d 个 p95：min %.1f / 中位 %.1f / max %.1f"
              % (n, len(allp95), min(allp95),
                 sorted(allp95)[len(allp95) // 2], max(allp95)))
        print("  基线 %d 次共 %d 个 max：min %.1f / 中位 %.1f / max %.1f"
              % (n, len(allmax), min(allmax),
                 sorted(allmax)[len(allmax) // 2], max(allmax)))
        if allui:
            print("  test_ui 补块步最慢：%s（门槛 %.0f）"
                  % (" / ".join("%.1f" % x for x in allui), TH_TESTUI_FILL))
        print("  p95 门槛余量：%.0f - %.1f = %.1f ms  ← 判别空档的下沿"
              % (TH_PROBE_P95, max(allp95), TH_PROBE_P95 - max(allp95)))
    print("  ==> 基线 %s" % ("全绿 ✅" if ok else "有红 ❌"))
    return ok, (max(allp95) if allp95 else 0.0)


def main():
    want = sys.argv[1:] or ["BASELINE", "DOUBLE", "SYNC", "BOTH"]
    print("python=%s" % PY.replace("\\", "/"))
    print("源码 = %s" % SRC)
    results = {}
    try:
        for w in want:
            if w.upper() == "BASELINE":
                # ⚠️⚠️ **必须连跑 3 次**，不是 1 次。
                # 单次量到的分布说明不了「门槛卡在分布外」——
                # 上一轮就是单次跑，看到 max 152 就以为 220 能用，
                # 结果连跑 3 次才发现 152~256 跨了一倍。
                results["BASELINE"] = baseline(3)
            else:
                key = w.upper()
                if key not in INJ:
                    raise SystemExit("❌ 未知注入 %r，可选：%s / BASELINE"
                                     % (w, list(INJ)))
                results[key] = one(key, INJ[key])
    finally:
        # ⚠️⚠️ **无论成败一定还原**（上次就是炸在半路留了一地注入）
        if os.path.exists(BAK):
            cur = read(SRC)
            if "INJECT" in cur:
                shutil.copyfile(BAK, SRC)
                print("\n[finally]检测到源码有INJECT，已强制还原")
        try:
            os.remove(BAK)
        except OSError:
            pass

    print("\n" + "=" * 70)
    print("### 汇总")
    print("=" * 70)
    for k, v in results.items():
        if k == "BASELINE":
            print("  %-10s %s (基线 max %.1fms)"
                  % (k, "全绿 ✅" if v[0] else "有红 ❌", v[1]))
        else:
            print("  %-10s %s" % (k, "门槛抓住了 ✅" if v else "抓不住 ❌"))
    b = results.get("BASELINE")
    inj = [k for k in results if k != "BASELINE"]
    print("\n  结论：%s"
          % ("双向验证完成 —— 不误报且抓得住真回归，"
             "p95 门槛 40ms / 极端单帧 500ms"
             if (b and b[0] and all(results[k] for k in inj))
             else "❌ 有未通过的项，别收工"))
    print("\n  ⚠️ 门槛历史（全靠拍脑袋 → 全靠数据）：")
    print("     120 -> 140 -> 260/300 -> 60 -> 40")
    print("     前三次都卡在分布中间；60 那次卡在**注入分布**中间")
    print("     （SYNC 量到 59.4/ 61.3，一半红一半绿）；40 是空档中点。")


if __name__ == "__main__":
    main()
