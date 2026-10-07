# -*- coding: utf-8 -*-
"""NIQE 那段文字为什么是空的 —— 整条链实测（2026-10-06 主人报的）。

跑法：

    python probe_niqe.py

主人截图：信息卡上「质量 清晰 · 清晰度」后面**完全空白**，
连「计算中…」都没有，下面还留着空位。

怀疑链条有三个，逐个量：
  A. `niqe_score` 算不出来（返回 score=None，或者异常）
  B. 算出来了但 `_niqe_done` 的守卫 `got[0] != path` 把它丢了
  C. 压根没 request（`nq is None` 那个分支没进去）

顺带量「预留行」逻辑：`_fit_card` 会因为 NIQE 没算完而多留一行 linespace，
如果 NIQE 一直算不完，**那一行就永远空着** —— 这正好解释「下面还有点空位」。
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
import niqe                         # noqa: E402
import thumbs                      # noqa: E402
import uikit                       # noqa: E402
import winimg                       # noqa: E402

ROOT = os.path.join(os.environ.get("TEMP", "."), "ImageScout-niqe")


def make_photo(path, w, h, seed=3):
    """造一张有真实高频细节的噪声图（NIQE 对它应该给「良好」左右）。

    ⚠️ 尺寸照主人截图那张来：1080×1330（1.4 MP）。
    格式用 PNG（`bgra_to_png` 是现成的、零依赖）—— 主人那张是 JPEG，
    但 JPEG 与 PNG 走的是**同一个** `niqe_score`（它只管解码拿像素），
    格式差异由 `imgsize`/`winimg` 负责，不影响 NIQE 这一段。
    真要验证 JPEG 再说，别在这里凭空造一个 JPEG 编码器。
    """
    rnd = random.Random(seed)
    buf = bytearray(w * h * 4)
    buf[:] = rnd.randbytes(w * h * 4)
    for i in range(3, len(buf), 4):
        buf[i] = 255
    with open(path, "wb") as f:
        f.write(thumbs.bgra_to_png(w, h, bytes(buf)))
    return path


def main():
    uikit.enable_dpi_awareness()
    shutil.rmtree(ROOT, ignore_errors=True)
    os.makedirs(ROOT, exist_ok=True)
    # ⚠️ 尺寸照主人截图那张来：1080×1330（1.4 MP）
    a = make_photo(os.path.join(ROOT, "photo_a.png"), 1080, 1330)
    got = winimg.load_pixels(a, 2000, winimg.SIIGBF_RESIZETOFIT, raw=True)
    w, h, px = got
    _w, _h, cut = thumbs.crop_bgra(w, h, px, int(w * 0.1), int(h * 0.1),
                                   int(w * 0.8), int(h * 0.8))
    with open(os.path.join(ROOT, "photo_b.png"), "wb") as f:
        f.write(thumbs.bgra_to_png(_w, _h, cut))

    print("=== A. niqe_score 直接单测（不经界面）===")
    t0 = time.perf_counter()
    res = ui.niqe_score(a)
    print("  耗时 %.0f ms  ->  %r" % ((time.perf_counter() - t0) * 1000, res))
    print("  niqe_field 显示成：%s" % ui.niqe_field(a, res))
    print("  MIN_BLOCKS=%d SOLID_BLOCKS=%d BLOCK=%d"
          % (niqe.MIN_BLOCKS, niqe.SOLID_BLOCKS, niqe.BLOCK))

    print("\n=== B. 起真实 App，看信息卡那一行 ===")
    uikit.clear_photo_caches()
    app = ui.App([ROOT])
    app.update()
    app.start_scan()
    t0 = time.time()
    while time.time() - t0 < 300:
        app.update()
        if not app.busy and app.files:
            break
        time.sleep(0.01)
    app.set_view("all")
    row = next(i for i, it in enumerate(app.glist.items) if it["tag"] == a)
    app.glist.select(row)
    app.update()

    def warm_idle():
        return ((not getattr(app, "_prewarm_q", []))
                and not getattr(app, "_prewarm_busy", False))

    t0 = time.time()
    while time.time() - t0 < 180:
        app.update()
        if warm_idle():
            break
        time.sleep(0.01)

    def card_text():
        """把信息卡里两个 Label 的实际文本抓出来。"""
        out = []
        for ch in app.info_card.body.winfo_children():
            for sub in ch.winfo_children():
                for lb in sub.winfo_children():
                    try:
                        if lb.winfo_class() == "Label":
                            t = lb.cget("text")
                            if t:
                                out.append(t)
                    except Exception:
                        pass
            try:
                if ch.winfo_class() == "Label" and ch.cget("text"):
                    out.append(ch.cget("text"))
            except Exception:
                pass
        return out

    # ⚠️ v1.7：`_niqe_lbl` 的元组从 `(path, lbl, 质量, base)` 缩成了
    #    `(path, lbl)` —— 「清晰度」拆成了独立 Label，回填只改那一行。
    print("  刚选完：_niqe_lbl = %r" % ({k: (v[0][-12:], v[1].cget("text")[-24:])
                                     for k, v in app._niqe_lbl.items()},))
    for t in card_text():
        if "质量" in t:
            print("  卡片文本：%r" % t)
            break

    print("  等 NIQE 算完（最多 20s）…")
    t0 = time.time()
    got_niqe = False
    while time.time() - t0 < 20:
        app.update()
        time.sleep(0.05)
        if app.niqec.cached(a) is not None:
            got_niqe = True
            break
    print("  cached(a) = %r  （等了 %.1fs）"
          % (app.niqec.cached(a), time.time() - t0))
    print("  _niqe_lbl = %r" % ({k: (v[0][-12:], v[1].cget("text")[-24:])
                                for k, v in app._niqe_lbl.items()},))
    for t in card_text():
        if "质量" in t:
            print("  卡片文本：%r" % t)
            break

    print("\n=== C. 布局：预留行是不是空着的 ===")
    # ⚠️ 别调 `_fill_card`（那是模块级函数名，App 上是 `fit_to_content`）。
    #    这里直接量信息卡实际高度和那几行的换行情况 ——
    #    主人的截图是「清晰度后空白 + 下面有空位」，
    #    最可能是**文字折行了、但卡片高度只按折行前算**。
    app.info_card.fit_to_content()
    app.update()
    print("  info_card.min_h=%s  实际高=%d  body.reqheight=%d"
          % (app.info_card.min_h,
             app.info_card.winfo_height(),
             app.info_card.body.winfo_reqheight()))
    for ch in app.info_card.body.winfo_children():
        for lb in ch.winfo_children():
            try:
                if lb.winfo_class() != "Label":
                    continue
                t = lb.cget("text")
                if not t:
                    continue
                print("  Label wraplength=%s 宽=%d 高=%d  %r"
                      % (lb.cget("wraplength"), lb.winfo_width(),
                         lb.winfo_height(), t[:60]))
            except Exception as e:
                pass

    app.destroy()
    ok = got_niqe
    print("\n%s" % ("✅ NIQE 算完并回填了" if ok else
                    "❌ NIQE 没算完 —— 空白就是这个原因"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
