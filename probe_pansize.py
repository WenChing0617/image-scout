# -*- coding: utf-8 -*-
"""拖动尺寸探针：**拖动过程中画面会不会变大小**。

主人 2026-10-07 反馈「图片拖拽有时候会改变画面大小，不知道什么原因」。

要查的不是「手感」，是一个很具体的问题：**`_view[col]["disp"]` 这个
显示尺寸在拖动全程里变没变**。它一变，用户就会看到图忽然放大/缩小。

候选嫌疑人（都不许靠猜，逐个量）：
  A. `zoom[col]` 被谁改了（滚轮守卫漏了、`_link_*` 串了）
  B. 画布尺寸 `cw/ch` 变了（窗口重排 / 提示条挤压）—— `fit` 直接跟它挂钩
  C. 松手后的精确帧（`_pan_end` -> `render_all(precise=True)`）换了一张
     尺寸不同的图上去
  D. 后台预热 / 基准升级（`_fit_ready` / `_base_prep_go`）在拖动中落地，
     把画面换成另一种尺寸

所以每步都把 **disp / zoom / 画布尺寸 / 图元真实 bbox** 四个量一起记下来，
并标明那一步发生了什么（补块 / 松手 / 后台换图）。哪个量先动，就是它。

用法：
    python probe_pansize.py
"""
from __future__ import annotations

import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import image_scout as ui            # noqa: E402
import scan                         # noqa: E402
import thumbs                       # noqa: E402
import uikit                        # noqa: E402
import winimg                       # noqa: E402

ROOT = os.path.join(os.environ.get("TEMP", "."), "ImageScout-pansize")

STEP = 8                # 一步多少像素（真实一帧的位移）
N_Z = 5                 # 放大几档
OK = []
BAD = []


def check(cond, name, detail=""):
    (OK if cond else BAD).append(name)
    print("   [%s] %s%s" % ("OK" if cond else "XX", name,
                            ("  " + detail) if detail else ""))


def make_photo(path, w=3000, h=2000, seed=7):
    import random
    rnd = random.Random(seed)
    buf = bytearray(rnd.randbytes(w * h * 4))
    for i in range(3, len(buf), 4):
        buf[i] = 255
    with open(path, "wb") as f:
        f.write(thumbs.bgra_to_png(w, h, bytes(buf)))
    return path


class Ev(object):
    def __init__(self, app, d=0, x=None, y=None):
        self.delta = d
        self.x = app.cv_a.winfo_width() // 2 if x is None else x
        self.y = app.cv_a.winfo_height() // 2 if y is None else y


def pump(app, sec):
    t0 = time.time()
    while time.time() - t0 < sec:
        app.update()
        time.sleep(0.004)


def snap(app):
    """当前这一帧要盯的所有量。

    ⚠️ **`imgwh` vs `want` 是关键判据**：块贴到画布上是 1:1 的
    （`create_image` 不带缩放），所以「图元实际像素尺寸」必须等于
    「这块应当代表的显示尺寸」`want=(twant,thwant)`。两者一旦不等，
    块里的内容就会被**拉伸或压缩** —— 那才是用户看到的「画面变了大小」。
    """
    v = app._view.get(0) or {}
    cv = app.cv_a
    it = v.get("item")
    bb = cv.bbox(it) if it is not None else None
    img = v.get("img")
    return {
        "disp": tuple(v.get("disp") or (0, 0)),
        "zoom": round(float(app.zoom[0]), 6),
        "cw": cv.winfo_width(), "ch": cv.winfo_height(),
        "pan": (app.pan[0][0], app.pan[0][1]),
        "bsz": ((bb[2] - bb[0], bb[3] - bb[1]) if bb else (0, 0)),
        "want": tuple(v.get("want") or (0, 0)),
        "blk": tuple(v.get("blk") or (0, 0)),
        # ⚠️ 基准档尺寸 —— `sx = bw/disp_w`。`sx>1` 说明基准像素比显示像素
        #    密，这时「1:1 贴块」等于把内容**放大 sx 倍**。
        "base": tuple(v.get("base") or (0, 0)),
        "sx": round((v.get("base") or (0, 0))[0]
                    / float(max(1, (v.get("disp") or (0, 0))[0])), 5),
        "imgwh": (img.width(), img.height()) if img is not None else (0, 0),
        # 图元落点与「显示图原点」的差 —— 应当恒等于块覆盖范围在画布上的起点
        "ox": v.get("ox", 0), "oy": v.get("oy", 0),
        "bx": v.get("bx", 0), "by": v.get("by", 0),
        "item": it,
    }


def run_case(app, tag, zoom_steps, mode="plain"):
    """跑一个档位：放大 N 档，然后横向拖，再松手等精确帧。

    `mode`：
      `plain`      —— 老实拖完一步一帧，然后松手
      `continuous` —— 松手后**立刻**再按（不等的精确帧落地）。主人说的
                      「有时候」最可能就是这个时序：`_pan_end` 排的精确帧
                      （`after(90ms)`）在**下一次拖动期间**才渲染，而它用的是
                      渲染那一刻的 pan —— 画面会跳一下。
      `withwheel`  —— 拖到一半插一次滚轮，看 zoom 守卫漏没漏。
    """
    app.reset_zoom()
    pump(app, 0.25)
    for _ in range(zoom_steps):
        app._on_wheel(0, Ev(app, 120))
    pump(app, 0.6)

    base = snap(app)
    cw, ch = base["cw"], base["ch"]
    dw, dh = base["disp"]
    # ⚠️ 判「拖不动」必须两维都塞得下才成立。用 `or` 会把
    #   「一维能拖」的档位（最常见的就是 zoom=1.0 适应窗口，
    #   图 1380x920 / 视口 1515x757 —— 纵向还有 163px 余量）整个跳过，
    #   而那正是主人最常用的那一档。
    if dw <= cw and dh <= ch:
        print("   -- %s：显示图 %s 两维都不超过视口 %sx%s，跳过"
              % (tag, base["disp"], cw, ch))
        return None
    # 挑「有余量」的那一维拖，两维都有就拖横向
    room_x = max(0, dw - cw) // 2
    room_y = max(0, dh - ch) // 2
    horiz = room_x >= room_y and room_x > 0
    room = room_x if horiz else room_y
    print("   %s：zoom=%.2f 显示图 %dx%d 画布 %dx%d 拖%s(余量%d)\n"
          "        基准档 %s  sx=bw/disp=%.4f  块(实) %s  声称(twant) %s"
          % (tag, base["zoom"], dw, dh, cw, ch,
             "横向" if horiz else "纵向", room,
             base["base"], base["sx"], base["imgwh"], base["want"]))

    evs = []
    app._pan_start(0, Ev(app, 0, cw // 2, ch // 2))
    bx, by = app._drag[1], app._drag[2]
    total = max(6, min(60, room // STEP))
    dx_sign = STEP if horiz else 0
    dy_sign = 0 if horiz else STEP

    def drag(from_k, n, x0, y0):
        for k in range(n):
            kn = from_k + k
            app._pan_move(0, Ev(app, 0, x0 + dx_sign * (k + 1),
                                y0 + dy_sign * (k + 1)))
            app.update()
            evs.append((kn, "drag", snap(app)))

    if mode == "continuous":
        drag(0, total // 2, bx, by)
        app._pan_end()                  # 松手，精确帧排进 90ms 后
        app.update()                    # ⚠️ 只推一帧，**故意不等那个精确帧**
        x1 = bx + dx_sign * (total // 2)
        y1 = by + dy_sign * (total // 2)
        app._pan_start(0, Ev(app, 0, x1, y1))
        drag(total // 2, total // 2, x1, y1)
        app._pan_end()
    else:
        drag(0, total, bx, by)
        if mode == "withwheel":
            app._on_wheel(0, Ev(app, 120))     # 拖动中滚一格
            app.update()
            evs.append((total, "wheel", snap(app)))
        app._pan_end()
    pump(app, 1.2)
    evs.append((total, "release", snap(app)))

    def first_change(field):
        for i, (k, why, s) in enumerate(evs):
            if s[field] != evs[0][2][field]:
                return i, k, why, evs[0][2][field], s[field]
        return None

    out = {}
    for f in ("disp", "zoom", "cw", "bsz"):
        out[f] = first_change(f)
    out["final_disp"] = evs[-1][2]["disp"]
    out["final_bsz"] = evs[-1][2]["bsz"]
    out["base_bsz"] = base["bsz"]
    out["disp_changed"] = (out["final_disp"] != base["disp"])
    out["bsz_changed"] = (out["final_bsz"] != base["bsz"])
    # ⚠️ 核心判据：块「实际像素尺寸」必须等于「它声称代表的显示尺寸」。
    #    不等 = 贴上去的内容被拉伸 = 用户眼里的「画面变了大小」。
    bad = [(k, why, s["imgwh"], s["want"])
           for k, why, s in evs if s["imgwh"] != s["want"]]
    out["bad_scale"] = bad
    # 图元在画布上的实际占用必须等于图尺寸（1:1 贴图）
    bad_bb = [(k, why, s["bsz"], s["imgwh"])
              for k, why, s in evs if s["bsz"] != s["imgwh"]]
    out["bad_bbox"] = bad_bb
    out["wheel"] = (mode == "withwheel")
    return out


def main():
    uikit.enable_dpi_awareness()
    shutil.rmtree(ROOT, ignore_errors=True)
    os.makedirs(ROOT, exist_ok=True)
    a = make_photo(os.path.join(ROOT, "photo_a.png"))
    got = winimg.load_pixels(a, 2000, winimg.SIIGBF_RESIZETOFIT, raw=True)
    w, h, px = got
    _w, _h, cut = thumbs.crop_bgra(w, h, px, int(w * .1), int(h * .1),
                                   int(w * .8), int(h * .8))
    with open(os.path.join(ROOT, "photo_b.png"), "wb") as f:
        f.write(thumbs.bgra_to_png(_w, _h, cut))

    app = ui.App([ROOT])
    app.store = scan.FingerStore(os.path.join(ROOT, "cache.json"))
    pump(app, 1.5)
    t0 = time.time()
    app.start_scan()
    while app.busy and time.time() - t0 < 60:
        app.update()
        time.sleep(0.005)
    pump(app, 1.5)
    print("扫出 %d 组 / %d 文件" % (len(app.groups), len(app.files)))
    app.set_view("all")
    row = next((i for i, it in enumerate(app.glist.items)
                if it["tag"] == a), None)
    if row is None:
        print("❌ 找不到 a 那一行，探针失效")
        return 1
    app.glist.select(row)
    pump(app, 1.0)

    print("\n=== 拖动全程：哪个量先变 ===")
    row_out = {}
    for tag, z, mode in (("z1-plain", 1, "plain"),
                         ("z3-plain", 3, "plain"),
                         ("z5-plain", N_Z, "plain"),
                         ("z3-cont", 3, "continuous"),
                         ("z5-cont", N_Z, "continuous"),
                         ("z3-wheel", 3, "withwheel")):
        r = run_case(app, tag, z, mode)
        if r:
            row_out[tag] = r

    if not row_out:
        print("❌ 所有档位都拖不动，数据作废")
        return 1

    print("\n=== 判定 ===")
    for tag, r in row_out.items():
        # ⚠️ `withwheel` 里用户**主动滚轮**缩放，`zoom`/`disp` 本来就该变，
        #    不算 bug —— 那个场景只是看「拖动中滚轮会不会把 pan 算错」。
        # ⚠️ `bsz`（块像素尺寸）**不再判定**：拖动补块时缓冲从 0.55 档
        #    放大到 1.0 档（`_pad_scale`）是 v1.7 有意的设计，块覆盖更大
        #    范围。它会不会毁掉画面，看的是下面「块像素 == 声称尺寸」
        #    那条 —— 只要两者相等，块变大只是多画了一圈缓冲。
        for f, label in (("disp", "显示尺寸 disp"),
                         ("zoom", "缩放倍率 zoom"),
                         ("cw", "画布宽 cw")):
            if r["wheel"] and f in ("disp", "zoom"):
                continue
            ch_ = r[f]
            if ch_ is None:
                check(True, "P-%s：全程 %s 不变" % (tag, label))
            else:
                i, k, why, b, aft = ch_
                check(False, "P-%s：全程 %s 不变" % (tag, label),
                      "第 %d 步(%s)就变了 %s -> %s" % (i, why, b, aft))
        if not r["wheel"]:
            check(not r["disp_changed"],
                  "P-%s：松手后不改变显示尺寸" % tag,
                  "" if not r["disp_changed"] else "末帧 %s"
                  % (r["final_disp"],))
        # ⚠️ 这两条是「画面会不会被拉伸」的直接判据
        if r["bad_scale"]:
            k, why, got, want = r["bad_scale"][0]
            check(False, "P-%s：每帧块像素 == 声称显示尺寸" % tag,
                  "%d/%d 帧不符，首帧 第%d步(%s) 实 %s != 应 %s"
                  % (len(r["bad_scale"]), len(r["bad_scale"]) + 1, k, why,
                     got, want))
        else:
            check(True, "P-%s：每帧块像素 == 声称显示尺寸" % tag)
        if r["bad_bbox"]:
            k, why, got, want = r["bad_bbox"][0]
            check(False, "P-%s：图元占画布尺寸 == 图尺寸" % tag,
                  "第%d步(%s) 实 %s != 图 %s" % (k, why, got, want))
        else:
            check(True, "P-%s：图元占画布尺寸 == 图尺寸" % tag)

    app.destroy()
    print("\n== 结果：%d 通过 / %d 失败 ==" % (len(OK), len(BAD)))
    for b in BAD:
        print("   XX " + b)
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
