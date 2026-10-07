# -*- coding: utf-8 -*-
"""撤销（移入隔离夹 -> 搬回来）的判据。

主人 2026-10-07：「添加撤销功能，当误删时可以撤销」。

⚠️ 撤销和「去掉删除确认框」是**同一件事的两半**：v1.11 起
   `delete_side` 不再弹确认（主人明确要求去掉那道帘子），兜底就
   全靠撤销了。所以这里的判据要覆盖「删错了能救回来」这条主线，
   而不只是「栈里有没有东西」。

⚠️ 全程在 `%TEMP%` 下的临时目录里跑 —— 隔离夹是 `<root>/_隔离`，
   被删的图和隔离夹都在临时目录内，绝不会碰真实照片。

用法：
    python probe_undo.py
"""
from __future__ import annotations

import os
import random
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import image_scout as ui            # noqa: E402
import quarantine                   # noqa: E402
import scan                         # noqa: E402
import thumbs                       # noqa: E402
import uikit                        # noqa: E402

QROOT = os.path.join(os.environ.get("TEMP", "."), "ImageScout-undo")
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


def main():
    uikit.enable_dpi_awareness()
    shutil.rmtree(QROOT, ignore_errors=True)
    os.makedirs(QROOT, exist_ok=True)
    for i in range(6):
        make_photo(os.path.join(QROOT, "p%d.png" % i), seed=50 + i)

    # ⚠️ 必须打桩掉弹窗：失败路径会 `showwarning`，真弹出来探针就挂死了
    #    （无头环境没人点「确定」）。打桩后 `_cap` 还能顺便记录调用。
    calls = []
    ui.messagebox.showwarning = lambda *a, **k: calls.append(("warn", a[0]))
    ui.messagebox.showerror = lambda *a, **k: calls.append(("error", a[0]))
    ui.messagebox.showinfo = lambda *a, **k: calls.append(("info", a[0]))

    app = ui.App([QROOT])
    app.store = scan.FingerStore(os.path.join(QROOT, "_cache.json"))
    pump(app, 1.0)
    app.start_scan()
    t0 = time.time()
    while app.busy and time.time() - t0 < 90:
        app.update()
        time.sleep(0.005)
    pump(app, 1.2)
    n_files = len(app.files)
    n_desc = len(app.descs)
    print("扫出 %d 张 / %d 个描述子 / %d 组"
          % (n_files, n_desc, len(app.groups_raw)))
    if n_files < 4:
        print("❌ 样本太少，探针作废")
        return 1

    # ---- U1 刚启动时没有可撤销的东西 ----------------------------------
    check(not app.can_undo, "U1 初始状态没有可撤销的操作")
    check(app.btn_undo._state == "disabled", "U1b 撤销按钮初始是灰的",
          "state=%s" % app.btn_undo._state)

    # ---- U2 删一张 -> 进隔离夹 -----------------------------------------
    victim = app.files[0]
    files_before = list(app.files)
    app.delete_paths([victim])
    app.update()
    dest = os.path.join(QROOT, quarantine.QUARANTINE_NAME,
                        os.path.basename(victim))
    check(not os.path.isfile(victim), "U2a 原位置已经没有这张图了",
          victim)
    check(os.path.isfile(dest), "U2b 文件确实躺在隔离夹里",
          os.path.relpath(dest, QROOT))
    check(app.can_undo, "U2c 现在有可撤销的操作了")
    check(len(app.files) == n_files - 1, "U2d 列表里少了一张",
          "%d -> %d" % (n_files, len(app.files)))

    # ---- U3 撤销 -> 搬回来 --------------------------------------------
    app.undo()
    app.update()
    check(os.path.isfile(victim), "U3a 撤销后文件回到原位")
    check(not os.path.isfile(dest), "U3b 隔离夹里那份已经搬走了")
    check(list(app.files) == files_before,
          "U3c 列表内容与**顺序**都还原了",
          "%d 张" % len(app.files))
    check(not app.can_undo, "U3d 撤销完栈空了")

    # ---- U4 撤销之后还能正常重新分组 ----------------------------------
    app.groups_raw, app.weak = scan.build_groups(app.descs, app.links)
    app._make_view_groups()
    check(len(app.descs) == n_desc, "U4a 描述子全部回来了",
          "%d / %d" % (len(app.descs), n_desc))

    # ---- U5 多步撤销 --------------------------------------------------
    v1, v2 = app.files[0], app.files[1]
    app.delete_paths([v1])
    app.update()
    app.delete_paths([v2])
    app.update()
    check(len(app.undo_stack) == 2, "U5a 连删两次 -> 栈里两步",
          "%d 步" % len(app.undo_stack))
    app.undo()
    app.update()
    check(os.path.isfile(v2) and not os.path.isfile(v1),
          "U5b 第一次撤销只搬回**后删的那张**")
    app.undo()
    app.update()
    check(os.path.isfile(v1) and os.path.isfile(v2),
          "U5c 第二次撤销把另一张也搬回来")
    check(not app.can_undo, "U5d 撤干净了，栈空")

    # ---- U6 栈空时再撤销：不崩、有提示 ---------------------------------
    n_warn = len(calls)
    app.undo()
    app.update()
    check(len(calls) == n_warn, "U6 栈空时撤销不弹错误框（只 toast）")
    check(not app.can_undo, "U6b 状态没被搞坏")

    # ---- U7 原位有同名文件 -> 拒绝覆盖，且记录不丢 ---------------------
    v3 = app.files[0]
    app.delete_paths([v3])
    app.update()
    with open(v3, "w", encoding="utf-8") as f:
        f.write("我自己放回来的占位")
    n_before = len(calls)
    app.undo()
    app.update()
    with open(v3, encoding="utf-8") as f:
        body = f.read()
    check(body == "我自己放回来的占位", "U7a 不覆盖已在原位的同名文件")
    check(len(calls) > n_before, "U7b 明确告诉了用户「撤不回来」")
    check(app.can_undo, "U7c 失败后**记录没丢**，处理完还能再撤一次",
          "栈里 %d 步" % len(app.undo_stack))

    # 把占位删掉，再撤一次应该成功
    os.remove(v3)
    app.undo()
    app.update()
    check(os.path.isfile(v3), "U7d 冲突解决后再撤就成功了")
    check(not app.can_undo, "U7e 这次真的撤干净了")

    # ---- U8 撤销按钮的可用态跟着栈走 ----------------------------------
    # ⚠️ 按钮是自绘 Canvas，状态存在 `_state` 里（`configure_state` 写的）。
    #    第一版这里写成 `... or True` 的恒真式，等于没测 —— 那种判据
    #    注入什么都不会红。
    st0 = app.btn_undo._state
    v4 = app.files[0]
    app.delete_paths([v4])
    app.update()
    st1 = app.btn_undo._state
    app.undo()
    app.update()
    st2 = app.btn_undo._state
    check(st0 == "disabled" and st1 != "disabled" and st2 == "disabled",
          "U8 按钮可用态跟着撤销栈走",
          "初始 %s / 有货 %s / 撤完 %s" % (st0, st1, st2))

    # ---- U9 隔离夹里的文件被手动删了 -> 老实报错，不假装成功 -----------
    v5 = app.files[0]
    app.delete_paths([v5])
    app.update()
    d5 = os.path.join(QROOT, quarantine.QUARANTINE_NAME,
                      os.path.basename(v5))
    if os.path.isfile(d5):
        os.remove(d5)                    # 模拟用户手动清了隔离夹
    n_warn = len(calls)
    app.undo()
    app.update()
    check(len(calls) > n_warn, "U9a 隔离夹里没了 -> 明确报错")
    check(not os.path.isfile(v5), "U9b 没有凭空造出一个文件")
    check(app.can_undo, "U9c 记录仍然留着")

    app.destroy()
    print("\n== 结果：%d 通过 / %d 失败 ==" % (len(OK), len(BAD)))
    for b in BAD:
        print("   XX " + b)
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
