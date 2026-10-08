# -*- coding: utf-8 -*-
"""「说明 / 快捷键」弹窗的判据（v1.11）。

主人 2026-10-07：「右上可以添加一个说明按钮，提供说明弹窗，快捷键功能等」。

⚠️⚠️ 这里**最要紧**的一条不是「弹窗能不能打开」，而是
   **弹窗开着时，后台的快捷键必须让路**。

   原因：`_bind_keys` 是 `root.bind_all("<Key>")`，弹窗只是「盖在前面」，
   完全拦不住键盘事件。要是没有那道门，用户在说明里读到
   「按 A 移除左侧」顺手试一下 —— 后台的图**当场被移走**，
   而弹窗挡住画面，他根本看不见发生了什么。这跟 v1.10 去掉
   删除确认框叠在一起，就是**静默误删**。
   所以 H4 是主判据，H5 是它的注入验证（把门拿掉 -> 必须变红）。

用法：
    python probe_help.py
"""
from __future__ import annotations

import os
import random
import shutil
import sys
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import image_scout as ui            # noqa: E402
import scan                         # noqa: E402
import thumbs                       # noqa: E402
import uikit                        # noqa: E402

HROOT = os.path.join(os.environ.get("TEMP", "."), "ImageScout-help")
OK, BAD = [], []


def check(cond, label, extra=""):
    (OK if cond else BAD).append(label)
    print("   [%s] %s%s" % ("OK" if cond else "XX", label,
                            ("  " + extra) if extra else ""))


def make_photo(path, w, h, seed=1):
    """画一张「8×8 色块」图。

    ⚠️ 不能用纯随机噪声：那样两张图两两都不像，**一组都分不出来**，
       而 H0 需要「当前有一张正在看的图」、H11a 需要「有下一组可换」。
       色块图 + 同尺寸不同 seed = 天然强相似，才分得出组。
    """
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


TIERS = {"poor": (300, 200), "fair": (800, 600), "good": (1600, 1200)}


def build_dir():
    shutil.rmtree(HROOT, ignore_errors=True)
    os.makedirs(HROOT, exist_ok=True)
    k = 0
    for tier, (w, h) in TIERS.items():
        for _ in range(4):
            make_photo(os.path.join(HROOT, "%s-%02d.png" % (tier, k)), w, h,
                       seed=90 + k)
            k += 1


def pump(app, sec):
    t0 = time.time()
    while time.time() - t0 < sec:
        app.update()
        time.sleep(0.004)


def key_event(keysym, char="", state=0):
    """造一个假键盘事件。

    ⚠️ 不能靠 `event_generate("<Key>")` 派发：那是**模拟真实键盘输入**，
       要有焦点链才送得到，探针环境里没有 —— 派发不到就会「测了个寂寞」。
       所以 `_bind_keys` 特意把 `on_key` 挂成 `self._on_key` 供直调。
    """
    return types.SimpleNamespace(keysym=keysym, char=char, state=state,
                                 widget=None)


def all_texts(w):
    """把弹窗里所有可见文字收上来。

    ⚠️ 两路取词：普通 Label 走 `cget("text")`；`RoundButton` 是**自绘
       Canvas**，没有 `text` 选项（取会抛 TclError），文案存在 `_text` 里。
       第一版只走 cget，于是「知道了」那条永远收不到 —— 明明在屏幕上，
       判据却说没有。按控件类型分支，别按「猜它该有 text」。
    """
    out = []
    stack = [w]
    while stack:
        node = stack.pop()
        try:
            t = node.cget("text")
            if t:
                out.append(str(t))
        except Exception:
            t = getattr(node, "_text", None)
            if t:
                out.append(str(t))
        try:
            stack.extend(node.winfo_children())
        except Exception:
            pass
    return out


def fake_group(members):
    return {"members": list(members), "strong_links": [], "weak_links": [],
            "kind": scan.UI_LOOSE, "best": 0.5, "best_strong": 0.0}


def arm(app):
    """把现场重新摆成「有分组、且当前有一张正在看的图」。

    ⚠️ 必须反复调：**删图 / 撤销都会重算分组**（`scan.build_groups`），
       真实相似度低的样本重算出来是 0 组，于是 `groups` 空、
       `path_a` 被清成 None —— 后面按 Delete 只会听到一句「还没有图可删」，
       判据就变成「测了个空场景」（H6c 第一版就是这么假红的）。
    """
    if len(app.groups_raw) < 2:
        picks = [app.files[0:4], app.files[4:7], app.files[7:9]]
        app.groups_raw = [fake_group(m) for m in picks if m]
        app.weak = []
    app._make_view_groups()
    app.view = "groups"
    if app.path_a is None and app.groups:
        app.gidx = -1
        app._fill_group_list(select=0)
    app.update()


def toplevels(app):
    out = []
    for c in app.winfo_children():
        if isinstance(c, ui.tk.Toplevel):
            out.append(c)
    return out


def main():
    uikit.enable_dpi_awareness()
    build_dir()

    # 弹窗相关路径不会弹 messagebox，但删图路径的 toast/报错会 —— 一并打桩
    calls = []
    ui.messagebox.showwarning = lambda *a, **k: calls.append(("warn", a[0]))
    ui.messagebox.showerror = lambda *a, **k: calls.append(("error", a[0]))
    ui.messagebox.showinfo = lambda *a, **k: calls.append(("info", a[0]))

    app = ui.App([HROOT])
    app.store = scan.FingerStore(os.path.join(HROOT, "_cache.json"))
    pump(app, 1.0)
    app.start_scan()
    t0 = time.time()
    while app.busy and time.time() - t0 < 90:
        app.update()
        time.sleep(0.005)
    pump(app, 1.2)
    n_files = len(app.files)
    print("扫出 %d 张 / %d 个描述子 / %d 组"
          % (n_files, len(app.descs), len(app.groups_raw)))
    if n_files < 4:
        print("❌ 样本太少，探针作废")
        return 1

    # ⚠️ 别指望随机色块图能**自己**分出组来（实测 12 张 0 组：每张的色块
    #    都是各自随机的，两两都不像）。这里要的是「有一组、有下一组可换」，
    #    所以直接按 `scan.UI_LOOSE` 的组结构手工造，和 probe_pick 一个路子。
    arm(app)
    have_a = bool(app.path_a)
    check(have_a, "H0 前置：当前有一张「正在看」的图", str(app.path_a))
    if not have_a:
        app.destroy()
        return 1

    # ---- H1 初始状态：按钮在，弹窗没开 ---------------------------------
    check(getattr(app, "_help_win", None) is None, "H1a 初始没有说明弹窗")
    btn = getattr(app, "btn_help", None)
    check(btn is not None, "H1b 顶栏有「说明」按钮")
    # ⚠️ RoundButton 是自绘 Canvas，**没有 `text` / `command` 属性** ——
    #    文案存 `_text`、回调存 `_cmd`。按普通 ttk 按钮去读会永远拿到 None。
    check(btn is not None and "说明" in str(getattr(btn, "_text", "")),
          "H1c 按钮文案里写了「说明」", repr(getattr(btn, "_text", None)))
    check(btn is not None and getattr(btn, "_cmd", None) == app.show_help,
          "H1d 按钮真的挂在 show_help 上")
    check(btn is not None and getattr(btn, "_state", "") != "disabled",
          "H1e 说明按钮是可点的", "state=%s" % getattr(btn, "_state", None))

    # ---- H2 打开弹窗 ---------------------------------------------------
    n_top0 = len(toplevels(app))
    app.show_help()
    app.update()
    w = getattr(app, "_help_win", None)
    check(w is not None, "H2a show_help 之后 _help_win 有值")
    check(w is not None and isinstance(w, ui.tk.Toplevel),
          "H2b 说明是一个独立的顶层窗口")
    check(w is not None and w.title() == "说明 / 快捷键", "H2c 窗口标题正确",
          repr(w.title()) if w is not None else "-")
    check(len(toplevels(app)) == n_top0 + 1, "H2d 多出来一个顶层窗口",
          "%d -> %d" % (n_top0, len(toplevels(app))))

    # ---- H3 内容：快捷键 + 功能两栏都在，条目够数 -----------------------
    texts = all_texts(w) if w is not None else []
    joined = "\n".join(texts)
    for need in ["快捷键", "功能", "Ctrl + Z", "Delete", "← / A", "→ / D",
                 "0", "滚轮", "双击图片", "撤销", "清理完全重复",
                 "本组成员", "知道了",
                 # ⚠️ v1.14：来源按钮的语义从「加」改成了「换」（主人 10-08
                 #    「每次只要扫描本次的文件夹就行了」），说明文案必须跟着改 ——
                 #    留着「可以加多个」就是在教用户做已经不存在的事。
                 "添加文件夹", "换掉上一个"]:
        check(need in joined, "H3 说明里提到了「%s」" % need)
    n_items = len([t for t in texts if t.startswith(("↑", "↓", "J", "←", "→",
                                                     "Delete", "Ctrl", "0",
                                                     "滚轮", "按住", "双击"))])
    check(n_items >= 10, "H3b 快键条目数够（>=10）", "数到 %d 条" % n_items)
    check("_隔离" in joined, "H3c 讲清楚了「移除」是进隔离夹不是删除")

    # ---- H4 ★ 弹窗开着时，快捷键必须让路 --------------------------------
    before_files = list(app.files)
    before_a = app.path_a
    # ⚠️⚠️ **每个键按完立刻断言**，不能循环跑完再统一比一次快照。
    #    第一版就是统一比快照 —— 而最后一个键是 Ctrl+Z，它把前面
    #    「按 a」造成的误删**顺手撤销了**，快照又对上了，H4a 报 OK。
    #    误删确实发生（files 12->11），判据却说「一张没动」。
    #    这是源码注入验证时抓出来的：不看中间态就等于没测。
    hits = []
    for ks, ch, st in [("a", "a", 0), ("d", "d", 0), ("Delete", "", 0),
                       ("BackSpace", "", 0), ("z", "z", 0x4)]:
        n0, a0, u0 = len(app.files), app.path_a, len(app.undo_stack)
        app._on_key(key_event(ks, ch, st))
        app.update()
        why = []
        if len(app.files) != n0:
            why.append("图 %d->%d" % (n0, len(app.files)))
        if app.path_a != a0:
            why.append("当前这张被换掉")
        if len(app.undo_stack) != u0:
            why.append("撤销栈 %d->%d" % (u0, len(app.undo_stack)))
        if why:
            hits.append("%s(%s)" % (ks, "/".join(why)))
    check(not hits, "H4a 弹窗开着时 A/D/Delete/Ctrl+Z **逐键**都不动后台",
          ("漏了：" + "，".join(hits)) if hits else "5 个键全无副作用")
    check(len(app.files) == len(before_files),
          "H4b 一轮按完，图的张数还是原样", "%d 张" % len(app.files))
    check(getattr(app, "_help_win", None) is not None, "H4c 弹窗还开着（没被顺手关掉）")
    check(app.path_a == before_a, "H4d 当前看的那张没被换掉")
    gone = [p for p in before_files if not os.path.isfile(p)]
    check(not gone, "H4e 磁盘上一个文件都没少", str(gone[:2]))

    # ---- H5 ★ 注入验证：把「让路」那道门拿掉 -> H4 必须变红 --------------
    # 这一步是**判据自证**：证明 H4 不是在「测了个空场景」。
    # 拿掉门之后同一串按键必须真的把图删掉，否则说明 H4 的按键
    # 根本没走到删除那条路上，[OK] 是假的。
    n_now = len(app.files)
    # ⚠️ 一律用 getattr 取/存：这个属性只在开过弹窗之后才存在。
    #    源码级注入（把 `show_help` 里那句赋值拿掉）时它是**没有**的，
    #    直接 `app._help_win` 会让探针自己崩掉 —— 那只能看到半截红名单，
    #    分不清「判据红了」和「探针坏了」。
    saved = getattr(app, "_help_win", None)
    app._help_win = None                    # <<< 注入：相当于删掉 on_key 里那道判断
    app._on_key(key_event("Delete", "", 0))
    app.update()
    injected_hit = len(app.files) < n_now
    if saved is None:                       # 恢复
        try:
            del app._help_win
        except Exception:                   # noqa: BLE001
            app._help_win = None
    else:
        app._help_win = saved
    check(injected_hit,
          "H5 注入验证：拿掉让路判断后，Delete 确实会删到后台的图",
          "%d -> %d 张" % (n_now, len(app.files)))
    if injected_hit:
        app.undo()                          # 把注入验出来的那张搬回去
        app.update()
        check(len(app.files) == n_now, "H5b 注入造成的误删已撤销回来")

    # ---- H6 关闭弹窗 -> 快捷键恢复 -------------------------------------
    app._close_help()
    app.update()
    check(getattr(app, "_help_win", None) is None, "H6a 关掉后 _help_win 清空")
    check(len(toplevels(app)) == n_top0, "H6b 顶层窗口数量还原",
          "%d" % len(toplevels(app)))
    # 删过图之后分组被重算清空、path_a 也没了 —— 不重新摆现场，
    # 下面这一下按 Delete 只会撞上「还没有图可删」，判据就假红了。
    arm(app)
    check(bool(app.path_a), "H6b2 现场已复位（有图可删）",
          str(os.path.basename(app.path_a or "-")))
    n_now = len(app.files)
    app._on_key(key_event("Delete", "", 0))
    app.update()
    check(len(app.files) == n_now - 1,
          "H6c 关掉之后 Delete 又生效了（说明让路是**临时**的，不是把键禁死）",
          "%d -> %d 张" % (n_now, len(app.files)))
    app.undo()
    app.update()

    # ---- H7 单例：连点两次不开两个窗 -----------------------------------
    app.show_help()
    app.update()
    w1 = getattr(app, "_help_win", None)
    n_top1 = len(toplevels(app))
    app.show_help()
    app.update()
    w2 = getattr(app, "_help_win", None)
    check(w1 is not None and w1 is w2, "H7a 连点两次还是同一个窗口")
    check(len(toplevels(app)) == n_top1, "H7b 没有多出第二个说明窗口",
          "%d" % len(toplevels(app)))

    # ---- H8 Escape 能关 -------------------------------------------------
    hit_esc = False
    try:
        w2.event_generate("<Escape>", when="now")
        app.update()
        hit_esc = getattr(app, "_help_win", None) is None
    except Exception as e:                                   # noqa: BLE001
        print("   .. Escape 派发失败：%r" % (e,))
    check(hit_esc, "H8 按 Escape 能关掉说明")

    # ---- H9 窗口被外力销毁后，再开能重建 -------------------------------
    app.show_help()
    app.update()
    wk = getattr(app, "_help_win", None)
    try:
        wk.destroy()                    # 模拟弹窗被别的东西干掉
    except Exception:
        pass
    app.update()
    app.show_help()                     # 不该因为指向死窗口而报错
    app.update()
    wk2 = getattr(app, "_help_win", None)
    check(wk2 is not None and wk2 is not wk, "H9 旧窗口没了之后能重新开一个")
    app._close_help()
    app.update()

    # ---- H10 没开过窗时直接调 _close_help 不炸 --------------------------
    ok10 = True
    try:
        app._close_help()
        app._close_help()
    except Exception as e:                                   # noqa: BLE001
        ok10 = False
        print("   .. %r" % (e,))
    check(ok10, "H10 反复关（含从没开过）不抛异常")

    # ---- H11 说明里承诺的键，实际都真绑上了 ----------------------------
    # 只挑「能安全观测」的几个：↑ ↓ 0。A/D/Delete 会删图，前面已单独验过。
    app._close_help()
    app.update()
    arm(app)
    g0 = app.gidx
    app._on_key(key_event("Down", "", 0))
    app.update()
    # ⚠️ 别写 `moved or len(...) < 2` 这种「不够就放行」的兜底 ——
    #    那正是「恒真判据」的软版本：样本不够时它默默变 OK，看起来全绿。
    #    这里直接要求「组数 >= 2 **且** 真的动了」，样本不足就该红。
    check(len(app.groups) >= 2 and app.gidx != g0,
          "H11a ↓ 真能换下一组（说明里承诺了）",
          "组 %d -> %d / 共 %d 组" % (g0, app.gidx, len(app.groups)))
    # ⚠️ 别写成 `... or True` —— 恒真判据注入什么都不会红，等于没测。
    #    先把缩放**人为推离** 1.0，再验 0 键能不能把它按回去。
    if app.path_a:
        app.zoom = [2.0, 2.0]
        app.pan = [[40, 30], [40, 30]]
        app._on_key(key_event("0", "0", 0))
        app.update()
        check(app.zoom == [1.0, 1.0] and app.pan == [[0, 0], [0, 0]],
              "H11b 0 键真把缩放/平移打回「适应窗口」",
              "zoom=%s pan=%s" % (app.zoom, app.pan))

    app.destroy()
    print("\n== 结果：%d 通过 / %d 失败 ==" % (len(OK), len(BAD)))
    for b in BAD:
        print("   XX " + b)
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
