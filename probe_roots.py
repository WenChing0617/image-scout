# -*- coding: utf-8 -*-
"""「添加文件夹 / 添加图片」= **换掉**上一次的来源，不是往上再叠一个。

主人 2026-10-08：
    「有问题，重新添加文件夹的时候，要将上次添加的自动清除，不然每次添加
      文件夹会越来越多，所以每次只要扫描本次的文件夹就行了。」

⚠️⚠️ 这个 bug 的坑就在于**表面看没坏**：每点一次「添加文件夹」，确实多出了
   一个目录、也确实把图扫出来了，界面上一片正常。坏的是**上一次的还在**
   —— ``roots`` 悄悄攒成 [A, B, C]，此后扫的是三个目录的**并集**，
   而屏幕上没有任何地方告诉你这件事。所以判据不能只看「有没有扫出图」，
   必须同时钉死两件事：

     ① ``len(app.roots)`` == 1，且内容就是**本次选的那个**目录；
     ② ``app.files`` 里每一张都落在本次那个目录下面（没混进上一轮的）。

   只量 ①会漏掉「根换了、但 files 还留着上一轮的结果」；
   只量 ②会漏掉「根攒成 3 个、恰好第三次选的目录把前两次都盖住了」。

⚠️ 还有一条容易漏的：**点「取消」不许清东西**。老写法是
   ``if d: ...``；如果改成「先清再判断」，那主人点开对话框又反悔，
   辛苦扫出来的结果就没了。这里用 R9 单独钉住。

用法：
    python probe_roots.py
    python probe_roots.py --inject    # 把旧写法（append）放回源码，看判据红不红
"""
from __future__ import annotations

import io
import os
import random
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import image_scout as ui            # noqa: E402
import scan                         # noqa: E402
import thumbs                       # noqa: E402
import uikit                        # noqa: E402

RROOT = os.path.join(os.environ.get("TEMP", "."), "ImageScout-roots")
DIR_A, DIR_B, DIR_C = (os.path.join(RROOT, n) for n in ("A", "B", "C"))
N_A, N_B, N_C = 6, 4, 3
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


def _n(p):
    return os.path.normcase(os.path.abspath(p))


def under(paths, root):
    """`paths` 是否**全部**落在 `root` 底下（且非空）。"""
    r = _n(root) + os.sep
    return bool(paths) and all(_n(p).startswith(r) for p in paths)


def build():
    shutil.rmtree(RROOT, ignore_errors=True)
    for d, n, s0 in ((DIR_A, N_A, 10), (DIR_B, N_B, 80), (DIR_C, N_C, 160)):
        os.makedirs(d, exist_ok=True)
        for i in range(n):
            make_photo(os.path.join(d, "%s%d.png" % (os.path.basename(d), i)),
                       seed=s0 + i)


def main():
    uikit.enable_dpi_awareness()
    build()

    # ---- 把两个文件对话框打桩成「我说选哪个就选哪个」 ------------------
    # ⚠️ 打桩必须放在构造 App 之前也行，但**必须在点按钮之前**
    #    （`add_folder` 里那句是 `filedialog.askdirectory(...)` 现查现调的）。
    want = {"dir": "", "files": ()}
    ui.filedialog.askdirectory = lambda *a, **k: want["dir"]
    ui.filedialog.askopenfilenames = lambda *a, **k: want["files"]
    # 弹窗一律打桩：探针在无头环境里真弹出来就挂死了
    calls = []
    ui.messagebox.showwarning = lambda *a, **k: calls.append(("warn", a[0]))
    ui.messagebox.showerror = lambda *a, **k: calls.append(("error", a[0]))
    ui.messagebox.showinfo = lambda *a, **k: calls.append(("info", a[0]))

    def pick_dir(app, d):
        want["dir"] = d
        app.add_folder()
        app.update()

    def pick_files(app, fs):
        want["files"] = tuple(fs)
        app.add_files()
        app.update()

    app = ui.App([DIR_A])
    app.store = scan.FingerStore(os.path.join(RROOT, "_cache.json"))
    pump(app, 1.0)

    # ---- R1 起点：命令行给的目录就是唯一来源 ---------------------------
    check(len(app.roots) == 1 and _n(app.roots[0]) == _n(DIR_A),
          "R1a 初始来源只有命令行给的那一个目录", str(app.roots))
    check(len(app.files) == N_A and under(app.files, DIR_A),
          "R1b 列出的是 A 目录里的 %d 张" % N_A, "%d 张" % len(app.files))

    # ---- R2 ★ 再选一个目录 -> 上一个必须被顶掉 -------------------------
    pick_dir(app, DIR_B)
    check(len(app.roots) == 1,
          "R2a 换目录后**只该剩一个根**（攒成 2 个就是这个 bug）",
          "roots=%s" % [os.path.basename(p) for p in app.roots])
    check(len(app.roots) == 1 and _n(app.roots[0]) == _n(DIR_B),
          "R2b 剩下的那个根就是**本次选的** B")
    n_b = len([f for f in app.files if under([f], DIR_B)])
    check(n_b == N_B and len(app.files) == N_B,
          "R2c 列出的只剩 B 的 %d 张（A 的没混进来）" % N_B,
          "共 %d 张，其中 B 的 %d 张" % (len(app.files), n_b))

    # ---- R3 ★ 双向核对：一张 A 的都不许剩 ------------------------------
    leaked = [f for f in app.files if _n(f).startswith(_n(DIR_A) + os.sep)]
    check(not leaked, "R3 上一轮目录的图**一张都没剩**在列表里",
          "漏了 %d 张：%s" % (len(leaked),
                              [os.path.basename(p) for p in leaked[:3]]))

    # ---- R4 同一个目录再点一次：还是 1 个，不会变成 2 个 ---------------
    pick_dir(app, DIR_B)
    check(len(app.roots) == 1 and _n(app.roots[0]) == _n(DIR_B),
          "R4 对着同一个目录点两次，仍然只有 1 个根", str(len(app.roots)))

    # ---- R5 换回 A：真的能换回去（不是「只能加不能减」） ---------------
    pick_dir(app, DIR_A)
    check(len(app.roots) == 1 and _n(app.roots[0]) == _n(DIR_A)
          and len(app.files) == N_A and under(app.files, DIR_A),
          "R5 换回 A：根和列表都跟着回到 A", "%d 张" % len(app.files))

    # ---- R6 先真扫一遍，制造「上一轮的结果」---------------------------
    app.start_scan()
    t0 = time.time()
    while app.busy and time.time() - t0 < 90:
        app.update()
        time.sleep(0.005)
    pump(app, 0.8)
    n_desc = len(app.descs)
    check(n_desc >= N_A, "R6 前置：A 这一轮真的算出了描述子",
          "%d 个" % n_desc)
    if n_desc < N_A:
        print("❌ 前提没建立起来，后面的判据没有意义")
        app.destroy()
        return 1

    # ---- R7 ★ 换目录后，上一轮的扫描结果不许留在内存里 -----------------
    pick_dir(app, DIR_C)
    left = {"descs": len(app.descs), "links": len(app.links),
            "groups_raw": len(app.groups_raw), "groups": len(app.groups)}
    check(not any(left.values()),
          "R7a 换目录后上一轮的描述子/配对/分组**全清空了**",
          str(left))
    check(len(app.files) == N_C and under(app.files, DIR_C),
          "R7b 列表换成 C 的 %d 张" % N_C, "%d 张" % len(app.files))
    stale = [g for g in app.groups if not under(g, DIR_C)]
    check(not stale, "R7c 分组列表里没有上一轮的残留组", "%d 组" % len(stale))

    # ---- R8 ★ 「添加图片」也是换，不是加 -------------------------------
    two_b = sorted(os.path.join(DIR_B, f)
                   for f in os.listdir(DIR_B))[:2]
    pick_files(app, two_b)
    check(len(app.roots) == 2 and {_n(p) for p in app.roots} == {_n(p)
                                                                 for p in two_b},
          "R8a 添加图片：根**只**剩本次挑的这 2 张（目录被顶掉）",
          str([os.path.basename(p) for p in app.roots]))
    check({_n(p) for p in app.files} == {_n(p) for p in two_b},
          "R8b 列表里也只剩这 2 张", "%d 张" % len(app.files))

    # ---- R9 ★ 点「取消」不许清任何东西 ---------------------------------
    before_r, before_f = list(app.roots), list(app.files)
    pick_dir(app, "")                      # 取消 = askdirectory 返回 ""
    check([_n(p) for p in app.roots] == [_n(p) for p in before_r]
          and [_n(p) for p in app.files] == [_n(p) for p in before_f],
          "R9a 文件夹对话框点「取消」-> 根和列表一个字没动",
          "roots=%d files=%d" % (len(app.roots), len(app.files)))
    pick_files(app, ())                    # 取消 = askopenfilenames 返回 ()
    check([_n(p) for p in app.roots] == [_n(p) for p in before_r]
          and [_n(p) for p in app.files] == [_n(p) for p in before_f],
          "R9b 图片对话框点「取消」-> 同样一个字没动")

    # ---- R10 ★ 换目录**不能**把撤销栈清掉 ------------------------------
    # 撤销栈记的是「哪几张图被搬进了隔离夹」—— 那是磁盘上真发生过的事，
    # 跟「这回换扫哪个目录」无关。清掉就等于让主人撤不回刚移走的图。
    pick_dir(app, DIR_A)                   # 先把来源换回一个有内容的目录
    victim = list(app.files)[0]
    app.delete_paths([victim])
    app.update()
    check(app.can_undo, "R10a 前置：移走一张后有可撤销的操作",
          os.path.basename(victim))
    n_undo = len(app.undo_stack)
    pick_dir(app, DIR_C)                   # 换目录
    check(len(app.undo_stack) == n_undo and app.can_undo,
          "R10b 换目录后撤销记录**还在**", "栈里 %d 步" % len(app.undo_stack))
    n_warn = len(calls)
    app.undo()
    app.update()
    check(os.path.isfile(victim),
          "R10c 换完目录再撤销，文件照样搬回原位",
          os.path.basename(victim))
    check(len(calls) == n_warn, "R10d 撤销过程没有报错弹窗",
          str(calls[n_warn:]))

    app.destroy()
    print("\n== 结果：%d 通过 / %d 失败 ==" % (len(OK), len(BAD)))
    for b in BAD:
        print("   XX " + b)
    return 1 if BAD else 0


if __name__ == "__main__":
    if "--inject" in sys.argv:
        # ---- 注入：把 v1.12 的旧写法（append）放回 `_replace_roots` ----
        #
        # 这就是主人报的那个 bug 的**原始形态**：每次点「添加文件夹」只是
        # `self.roots.append(...)` 一下，旧的根永远不走。
        #
        # ⚠️ 注入必须**精确对准那个 bug**：只把「先清空再赋值」换成
        #    「逐个 append」，别的（`clear_all` / `_refresh_roots`）一律不动。
        #    要是顺手把 `_refresh_roots()` 也去掉，红的是「列表没更新」，
        #    而「根会攒起来」这个真场景压根没被构造出来。
        SRC = os.path.join(HERE, "image_scout.py")
        BAK = os.path.join(HERE, "_roots_bak.py")
        good = ("        new = [os.path.abspath(p) for p in new]\n"
                "        # `clear_all()` 会把 `self.roots` 也清空，"
                "所以赋值必须排在它后面。\n"
                "        self.clear_all()\n"
                "        self.roots = new\n")
        bad = ("        for _p in list(new):                     # INJECT\n"
               "            self.roots.append(os.path.abspath(_p))  # INJECT\n")
        s = io.open(SRC, encoding="utf-8").read()
        c = s.count(good)
        print("注入 [--inject] 「先清空再赋值」-> 「逐个 append」")
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
        # 还原必须做**字节级**校验（只 grep 片段会自己骗自己）
        shutil.copyfile(BAK, SRC)
        now = io.open(SRC, encoding="utf-8").read()
        assert now == bak, "还原后与备份不一致 —— 停下，别继续跑！"
        assert "INJECT" not in now, "还原后仍有注入残留"
        os.remove(BAK)
        print("\n[restore] 已还原并校验（%d 字节，与备份逐字节一致）"
              % len(now.encode("utf-8")))
        # ⚠️ 光看「子进程 rc != 0」还不够 —— 得确认红的**就是**这几条
        #    「根没被清」的判据，而不是探针自己崩了、或者别的判据替它红了。
        want_red = ["R2a", "R2c", "R3", "R7a", "R8a"]
        missed = [w for w in want_red if ("XX " + w) not in out]
        if p.returncode != 0 and not missed:
            print("[inject] ✅ 注入后 %d 条关键判据**全部**变红：%s"
                  % (len(want_red), "、".join(want_red)))
            sys.exit(0)
        print("[inject] ❌ 注入没被抓全：rc=%d 没红的有 %s"
              % (p.returncode, missed or "无"))
        sys.exit(1)
    sys.exit(main())
