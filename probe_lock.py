# -*- coding: utf-8 -*-
"""「文件被占用时别立刻报错」—— 主人 2026-10-08 报的「删除过快就弹移动失败」。

主人原话（附截图）：
    当删除过快就会这个弹窗
    PermissionError: [WinError 32] 另一个程序正在使用此文件，进程无法访问。
    :'D:\\素材\\图片\\新建文件夹\\20220804_194744.jpg'

⚠️⚠️ 病根：Windows 上**任何人**拿着文件句柄（**包括本程序自己的背景线程**），
   `shutil.move`（改名）就报 WinError 32。而本程序有三条后台线程在解图：
   预热 / 基准像素 / NIQE(GDI+)。主人手一快连删，就撞上
   「后台正在解这张、你正要把这张移走」。

⚠️ 关键认识：**这不是「删不掉」，是「晚了几十毫秒」**（后台解一张 640px 的图
   只要几十毫秒）。所以正确做法是等一下再试，不是立刻糊用户一脸错误。

⚠️ 造这个场景必须**真的拿一个文件句柄**：实测（`open(p,'rb')` 不关）
   `shutil.move` / `os.remove` 都必报那句 WinError 32 原话。
   不真拿句柄的话，判据测的就是一个不存在的场景。

用法：
    python probe_lock.py
    python probe_lock.py --inject    # 把重试关掉（_TRIES=1），看判据红不红
"""
from __future__ import annotations

import io
import os
import random
import shutil
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import image_scout as ui            # noqa: E402
import quarantine                   # noqa: E402
import scan                         # noqa: E402
import thumbs                       # noqa: E402
import uikit                        # noqa: E402

LROOT = os.path.join(os.environ.get("TEMP", "."), "ImageScout-lock")
OK, BAD = [], []


def check(cond, label, extra=""):
    (OK if cond else BAD).append(label)
    print("   [%s] %s%s" % ("OK" if cond else "XX", label,
                            ("  " + extra) if extra else ""))


def make_photo(path, w=300, h=200, seed=1):
    rnd = random.Random(seed)
    buf = bytearray(rnd.randbytes(w * h * 4))
    for i in range(3, len(buf), 4):
        buf[i] = 255
    with open(path, "wb") as f:
        f.write(thumbs.bgra_to_png(w, h, bytes(buf)))


def pump(app, sec):
    t0 = time.time()
    while time.time() - t0 < sec:
        app.update()
        time.sleep(0.004)


class Holder:
    """在**另一个线程**里拿住一个文件句柄，模拟「后台正在解这张图」。

    ⚠️ 必须 `open()` 真读一下再停住 —— 只是建个 File 对象不读也可能不占句柄。
       并且要用 `ready` 事件确认「句柄真的拿住了」再往下走，
       否则判据会和线程调度赛跑（有时候占用还没生效就先把文件移走了）。
    """

    def __init__(self, path):
        self.path = path
        self.ready = threading.Event()
        self.release = threading.Event()
        self.err = None
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()
        if not self.ready.wait(3.0):
            raise RuntimeError("Holder 没能拿住句柄：%s" % path)

    def _run(self):
        f = None
        try:
            f = open(self.path, "rb")
            f.read(1)
            self.ready.set()
            self.release.wait(15.0)
        except Exception as e:                       # noqa: BLE001
            self.err = e
            self.ready.set()
        finally:
            if f is not None:
                try:
                    f.close()
                except Exception:                    # noqa: BLE001
                    pass

    def let_go_after(self, sec):
        threading.Timer(sec, self.release.set).start()

    def done(self):
        self.release.set()
        self.t.join(timeout=3.0)


def main():
    uikit.enable_dpi_awareness()
    shutil.rmtree(LROOT, ignore_errors=True)
    os.makedirs(LROOT, exist_ok=True)
    qdir = os.path.join(LROOT, quarantine.QUARANTINE_NAME)

    # ---- L1 文件正被开着（120ms 后松手）-> 等一下重试就成，不该报错 -------
    p1 = os.path.join(LROOT, "busy_brief.jpg")
    make_photo(p1, seed=11)
    raw1 = open(p1, "rb").read()
    h1 = Holder(p1)
    h1.let_go_after(0.12)
    t0 = time.time()
    ok, info = quarantine.isolate(p1, qdir)
    dt = time.time() - t0
    h1.done()
    check(ok, "L1a 短暂被占用 -> 重试后移成功（原来直接报 WinError 32）",
          "%.2fs  %s" % (dt, "" if ok else info))
    check(ok and os.path.isfile(info) and open(info, "rb").read() == raw1,
          "L1b 落地的那份**字节完全一致**（不是半截拷贝）",
          os.path.basename(info) if ok else "-")
    check(ok and not os.path.exists(p1), "L1c 原位置已经没有了")
    check(dt >= 0.10, "L1d 确实等了（不是侥幸一次就成了）", "%.2fs" % dt)

    # ---- L2 一直被占用 -> 老实报「被占用」，别留下半截东西 ---------------
    p2 = os.path.join(LROOT, "busy_forever.jpg")
    make_photo(p2, seed=12)
    h2 = Holder(p2)
    t0 = time.time()
    ok2, info2 = quarantine.isolate(p2, qdir)
    dt2 = time.time() - t0
    check(not ok2, "L2a 从头到尾被占用 -> 如实返回失败（不假装成功）",
          "%.2fs" % dt2)
    check("占用" in str(info2), "L2b 原因写清楚了「被占用」", str(info2)[:60])
    check(os.path.isfile(p2), "L2c 源文件还在原位（没被误删）")
    check(not os.path.exists(os.path.join(qdir, os.path.basename(p2))),
          "L2d 隔离夹里**没有**留下半截拷贝")
    check(dt2 < 6.0, "L2e 不会死等（有时限，不是无限重试）", "%.2fs" % dt2)
    h2.done()

    # ---- L3 没人碰 -> 该是一次就成，别凭空变慢 ---------------------------
    p3 = os.path.join(LROOT, "free.jpg")
    make_photo(p3, seed=13)
    t0 = time.time()
    ok3, info3 = quarantine.isolate(p3, qdir)
    dt3 = time.time() - t0
    check(ok3 and dt3 < 0.08, "L3 没被占用时一次移成、没有白等",
          "%.3fs" % dt3)

    # ---- L4 「被占用」和「真不行」要分得开 -------------------------------
    e32 = PermissionError(13, "另一个程序正在使用此文件，进程无法访问。")
    e32.winerror = 32
    e2 = FileNotFoundError(2, "系统找不到指定的文件。")
    e2.winerror = 2
    check(quarantine._is_busy(e32) is True,
          "L4a WinError 32 认得出是「被占用」（值得重试）")
    check(quarantine._is_busy(e2) is False,
          "L4b 「找不到文件」不该重试（白等 0.9s 才报错）")

    # ---- L5 ★ 主人的真实场景：删过快，UI 层不再弹「移动失败」 ----------
    for i in range(5):
        make_photo(os.path.join(LROOT, "app%d.png" % i), seed=30 + i)
    calls = []
    ui.messagebox.showwarning = lambda *a, **k: calls.append(("warn", a[0]))
    ui.messagebox.showerror = lambda *a, **k: calls.append(("error", a[0]))
    ui.messagebox.showinfo = lambda *a, **k: calls.append(("info", a[0]))

    app = ui.App([LROOT])
    app.store = scan.FingerStore(os.path.join(LROOT, "_cache.json"))
    pump(app, 0.8)
    app.start_scan()
    t0 = time.time()
    while app.busy and time.time() - t0 < 60:
        app.update()
        time.sleep(0.005)
    pump(app, 0.8)
    n_files = len(app.files)
    check(n_files >= 4, "L5 前置：列表里有图可删", "%d 张" % n_files)
    if n_files < 4:
        app.destroy()
        print("❌ 前提没建立起来")
        return 1

    victim = app.files[0]
    hv = Holder(victim)
    hv.let_go_after(0.15)
    n_warn = len(calls)
    got = app.delete_paths([victim])
    app.update()
    hv.done()
    check(got == 1, "L5a ★「删得快」时那张图照样移走了（返回 1）",
          "返回 %s" % got)
    check(len(calls) == n_warn,
          "L5b ★ 没有弹「移动失败」框",
          str(calls[n_warn:]) if len(calls) > n_warn else "零弹窗")
    check(not os.path.isfile(victim), "L5c 原位置没了")
    check(os.path.isfile(os.path.join(qdir, os.path.basename(victim))),
          "L5d 它躺在隔离夹里")
    check(len(app.files) == n_files - 1, "L5e 列表里少了一张",
          "%d -> %d" % (n_files, len(app.files)))

    # ---- L6 删之前会把这几张从「后台待办」里摘掉 -------------------------
    a, b = app.files[0], app.files[1]
    c = app.files[2]
    app._prewarm_q = [a, b, c, a]
    app._prewarm_pause = 0.0
    app._quiesce_for([b])
    check(b not in app._prewarm_q, "L6a 要删的那张已从预热队列里摘掉",
          str([os.path.basename(x) for x in app._prewarm_q]))
    check(a in app._prewarm_q and c in app._prewarm_q,
          "L6b 别的图不受影响（别把整个队列清了）")
    check(app._prewarm_pause > time.time(),
          "L6c 删的这一刻预热先让路（不再起新的解码）")
    # ⚠️ `_base_want` 的键是 **(路径, 档位)**（见 `_base_prep` 里的 `key`），
    #    我第一版随手写成 `("k", 0)` —— 于是 `_quiesce_for` 拿 `k[0]` 去比
    #    当然不相干，判据报红。**红的是判据自己的假数据，不是源码**。
    app._base_want = {(b, 100): (0, b, 100)}
    app._quiesce_for([b])
    check(not app._base_want, "L6d 基准像素的待办里也摘掉了")

    # ---- L7 NIQE 让路 + 能恢复 ------------------------------------------
    app.niqec.cancel({c})
    check(c in app.niqec._cancel, "L7a 叫停之后 NIQE 那边记下了这张")
    app.niqec.request(c, lambda *a: None)
    check(c not in app.niqec._cancel,
          "L7b 再请求时让路标记被摘掉（否则永远停在「计算中…」）")

    app.destroy()
    print("\n== 结果：%d 通过 / %d 失败 ==" % (len(OK), len(BAD)))
    for x in BAD:
        print("   XX " + x)
    return 1 if BAD else 0


if __name__ == "__main__":
    if "--inject" in sys.argv:
        # ---- 注入：把重试关掉（回到「撞上就报错」的老行为）----
        #
        # ⚠️ 只改 `_TRIES` 这一个数，别的（`_is_busy` / `_quiesce_for` / 让路）
        #    一个字都不动 —— 注入必须**精确对准要模拟的那一个 bug**，
        #    改多了红的是别的东西，等于没测到这个场景。
        SRC = os.path.join(HERE, "quarantine.py")
        BAK = os.path.join(HERE, "_lock_bak.py")
        good = "_TRIES = 6\n"
        bad = "_TRIES = 1   # INJECT\n"
        s = io.open(SRC, encoding="utf-8").read()
        c = s.count(good)
        print("注入 [--inject] 重试 6 次 -> 1 次（撞上就报错）")
        print("锚点匹配 %d 处%s" % (c, "" if c == 1 else "  <- 不是 1 处！"))
        if c != 1:
            print("X 锚点对不上，注入作废（源码结构变了？）")
            sys.exit(2)
        shutil.copyfile(SRC, BAK)
        bak = io.open(BAK, encoding="utf-8").read()
        io.open(SRC, "w", encoding="utf-8", newline="\n").write(
            s.replace(good, bad))
        print("已注入，子进程重跑…\n")
        p = subprocess.run([sys.executable, os.path.abspath(__file__)],
                           capture_output=True)
        out = (p.stdout or b"").decode("utf-8", "replace")
        print(out)
        shutil.copyfile(BAK, SRC)
        now = io.open(SRC, encoding="utf-8").read()
        assert now == bak, "还原后与备份不一致 —— 停下，别继续跑！"
        assert "INJECT" not in now, "还原后仍有注入残留"
        os.remove(BAK)
        print("\n[restore] 已还原并校验（%d 字节，与备份逐字节一致）"
              % len(now.encode("utf-8")))
        # ⚠️ 光看 rc != 0 不够：得确认红的**正是**「被占用时不再重试」那几条
        want_red = ["L1a", "L1b", "L1c", "L5a", "L5b"]
        missed = [w for w in want_red if ("XX " + w) not in out]
        if p.returncode != 0 and not missed:
            print("[inject] ✅ 注入后 %d 条关键判据**全部**变红：%s"
                  % (len(want_red), "、".join(want_red)))
            sys.exit(0)
        print("[inject] ❌ 注入没被抓全：rc=%d 没红的有 %s"
              % (p.returncode, missed or "无"))
        sys.exit(1)
    sys.exit(main())
