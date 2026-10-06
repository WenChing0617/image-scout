# -*- coding: utf-8 -*-
"""⚠️⚠️ **端到端清晰度验收** —— 主人的三次反馈全部由这个判据拦住。

## 它量什么

真实启动 ImageScout、载入一张 **3000×2000 纯噪声**图（最坏输入：
全高频细节、PNG 零压缩），滚轮放大到各档，然后**从画布上真的把像素读回来**，
统计两个量：

1. **放大后横向的独立取值数 / 欠采样倍数**：
   噪声图每个像素都不同，所以放大后「有多少个不同的取值」直接反映
   有多少真实信息被保留下来。
   - 真插值（GDI+）：取值数跟源图同量级 → 清晰
   - 先 `subsample` 再 `zoom`：取值数掉到 1/q → **色块**
2. **块宽**：沿行扫描平均同色段长度。噪声图不该有同色段；
   一旦出现 >2px 的段就是有像素被复制（马赛克）。

## 为什么不用 test_ui 里那套

`test_ui` 用的是「显示尺寸 vs 基准档」的**间接**判据（over 倍数）。
它是必要的回归网，但**证不了「屏幕上没有色块」** ——
间接判据全绿、实际像素是块的情况，主人在 v1.4/v1.5 上就遇到过。
这个探针**直接读画布像素**，是唯一能当场证明「没色块」的东西。

## 跑法

```
D:/python/python-3.13.5/python.exe probe_e2e.py            # 3~5 张大图
D:/python/python-3.13.5/python.exe probe_e2e.py 2>&1 | tee out.txt
```
"""
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import thumbs                  # noqa: E402
import tkinter as tk            # noqa: E402  —— break_rendering 里的旧实现要用
import winimg                  # noqa: E402


def pump(app, until, timeout=120):
    """照抄 test_ui 的等待法：反复 update 直到条件成立或超时。

    ⚠️ 别用 `time.sleep` 干等 —— Tk 的定时器（去抖重画、异步块回调）
    只在 `update()` 里才跑，不泵就永远等不到它们。
    """
    end = time.time() + timeout
    while time.time() < end:
        try:
            app.update()
        except Exception:
            return False
        if until():
            return True
        time.sleep(0.01)
    return False


def settle_render(app, timeout=60):
    """等到这一帧真的画完：异步块就位 **且** 预热队列空了。

    ⚠️⚠️ **预热队列也必须等**：`_on_wheel` 会 `render_all(precise=0.75,
    only=col)` 先出快速帧，再排 170ms 后的精确帧；而预热线程还在后台
    解基准档。我在第一版只等 `_fit_pend`，于是预热一完成就触发
    `render_all()`（`only=None`）——**那一版会把 `_view` 整个清掉**，
    接着我的量测就读不到图元了。8 档全跳、探针却差点报「通过」。
    这跟真实使用不一样：主人是手动一档一档滚，每档之间隔了几百毫秒。
    """
    pump(app, until=lambda: not any(app._fit_pend.values()), timeout=timeout)
    for _ in range(3):
        pump(app, until=lambda: not getattr(app, "_prewarm_q", [])
             and not getattr(app, "_prewarm_busy", False), timeout=timeout)
    # ⚠️ 最后再确认画布上真的有图元。上面那个 `until` 只管「活干完了」，
    #    不管「画出来了」—— 之前那版我写成
    #    `until=lambda: not item or disp`，右边只要 `_view` 非空就为真，
    #    等于**什么都没等**，是个假等待。判据必须是「图元存在且 bbox 非空」。
    def painted():
        v = app._view.get(0) or {}
        it = v.get("item")
        return bool(it) and bool(app.cv_a.bbox(it))
    pump(app, until=painted, timeout=timeout)


def make_noise(path, w=3000, h=2000, seed=7):
    """纯噪声图：全高频、零结构 —— 马赛克和欠采样在这里无处可藏。

    ⚠️ 千万别用规则条纹 / 正弦波当清晰度测试图（v1.4/v1.5 的教训）：
    条纹欠采样 1.9 倍之后**看着还是条纹**，断言全绿而主人满屏马赛克。
    噪声是唯一能让「信息丢了」直接体现在取值数上的信号。
    """
    rnd = random.Random(seed)
    buf = bytearray(w * h * 4)
    for y in range(h):
        row = y * w * 4
        for x in range(w):
            i = row + x * 4
            v = rnd.randrange(256)
            g = 128 + int(80 * ((x - w / 2.0) / (w / 2.0)))
            buf[i] = (v + g) // 2
            buf[i + 1] = v
            buf[i + 2] = (255 - g) // 2
            buf[i + 3] = 255
    with open(path, "wb") as f:
        f.write(thumbs.bgra_to_png(w, h, bytes(buf)))
    return path


def canvas_stats(cv, item, img=None):
    """从画布上把图元那一块**真的读回来**，统计取值数和块宽。

    ⚠️⚠️ **Tk 的 Canvas 没有 `get(x, y)`**（我第一版照着 PhotoImage 的
    API 写，`AttributeError: 'Canvas' object has no attribute 'get'`）。
    Canvas 的图元只存一个**图元名**，读像素得两步：
        img_name = cv.itemcget(item, "image")   # -> "pyimage50"
        cv.tk.call(img_name, "get", x, y)       # -> (r, g, b)
    `get` 的坐标是**图片自身**的坐标（0..图片宽），不是画布坐标。

    ⚠️⚠️⚠️ **尺寸不能靠 `tk.call(name, "cget", "-width")`** ——
    实测它对画布上的这些图元**返回 0**（于是我的判据 `iw < 16` 每次
    都成立、`canvas_stats` 每次都返回 None，8 档全「读不到画面」，
    而渲染其实完全正常：`_view[0]` 齐全、item 存在、bbox 有效）。
    原因大概是这些 PhotoImage 是**空壳对象**（数据由 Tk 侧另存，
    `cget -width` 读不到），Tk 的 Tcl 子命令里也确实**没有**
    `width`/`height`（`bad option "width"` 那串报错里列的就是真正的
    子命令：cget/get/put/read/write…）。

    **可靠做法**：直接把 `_view[col]["img"]` 那个 tkinter.PhotoImage
    对象传进来，用它的 `.width()` / `.height()` **方法** ——
    这才是「画布上正在显示的那张图」，不会读到别处的缓存。
    """
    bb = cv.bbox(item)
    if not bb:
        return None
    if img is None:
        img_name = cv.itemcget(item, "image")
        if not img_name:
            return None
        iw = ih = None
    else:
        iw, ih = img.width(), img.height()
    if iw is None or iw < 16 or ih < 16:
        return None
    # 取图元**自身**范围内的中间一行（不受画布边界裁剪影响）
    y = ih // 2
    # ⚠️⚠️ **必须读 G 通道（索引 1），不能读 R（索引 0）** ——
    #    我第一版读 R，量出来「独立取值只有 8~13%」，一路以为还是糊，
    #    白查了好几轮。根因在**测试图自己**：`make_noise` 的 R 通道是
    #    `(255 - g) // 2`，而 g 只跟 x 成正比 —— **R 通道本身就是一条
    #    平滑渐变**，整行只有几十个取值，跟「有没有马赛克」毫无关系。
    #    G 通道才是 `v`（真噪声）。
    #
    #    ⚠️ 这是本项目第 N 次「判据/测试图自己有问题，把结论带偏」：
    #    正弦条纹（v1.4/v1.5 隐藏了两个 bug）→ 目标高度写死（放大档一个
    #    都没测到）→ 跳过档位算通过（假绿）→ 现在读错通道。
    #    **判据必须先在「已知有问题的输入」上确认它会报警**，
    #    才准拿去验收。
    grey = [img.get(x, y)[1] for x in range(iw)]
    # ⚠️⚠️ **必须量「最大同值游程」，不能量平均、也不能用「差<=4」当同段**
    #（这是 probe_resample.py 里踩了两次的坑，详见那里的注释）：
    #   -「差<=4」：噪声相邻差本来就大，条件永不成立 -> 恒为 1，抓不到块
    #   -「平均游程」：被大量 1px 段稀释 -> 恒为 1，抓不到块
    # 只有**最大值**不会被短段稀释：噪声图真放大时恒为 1，
    # 一旦有像素被复制（马赛克）就会飙到 zoom 倍数那个量级
    #（20 倍放大 -> 20px，正是主人截图里的方块尺寸）。
    seg, longest, tot, nseg = 1, 1, 0, 0
    for i in range(1, len(grey)):
        if grey[i] == grey[i - 1]:
            seg += 1
        else:
            tot += seg
            nseg += 1
            if seg > longest:
                longest = seg
            seg = 1
    tot += seg
    nseg += 1
    if seg > longest:
        longest = seg
    return {
        "w": iw,
        "h": ih,
        "uniq": len(set(grey)),
        "blk": float(longest),
        "min": min(grey),
        "max": max(grey),
        "bb": bb,
    }


def break_rendering():
    """⚠️ 把渲染**故意装回修复前的行为**，用来校准判据门槛。

    ## 为什么必须有这个模式

    判据「最大游程 <= N」里的 N 到底该定几？我第一版定了 2，
    结果 8 档里 2 档报「有色块」（游程 3px）。但那 3px 是**真色块**，
    还是「双三次把平滑区四舍五入成同一个 8 位灰度」的假象？
    **凭感觉定门槛 = 又一次假红或假绿。**

    唯一靠得住的办法：**同一张图、同一个 zoom 分别走新旧两条路**，
    看两个分布差多远 ——
        新路径（真插值）的游程 = 「正常水平」
        旧路径（先丢后放）的游程 = 「有马赛克」的水平
    N 取两者之间。

    装旧的方法两步：① 让 GDI+ 插值失效（返回原尺寸）；
    ② 把 `uikit.resample_img` 换回先 subsample 再 zoom 的旧实现。
    """
    import uikit

    winimg._gdip_scale_argb = lambda w, h, bgra, ow, oh: (w, h, bgra)

    # ⚠️ `_render_tile` 现在**不再调 resample_img**（插值改成在像素层
    #    调 `_gdip_scale_argb` 了），所以只换 `resample_img` 不够 ——
    #    上面把 GDI+ 打掉之后，`scaled` 恒为 None，代码会走
    #    「不缩放」分支直接贴原尺寸块，等于变成 1:1，量不到旧算法的马赛克。
    #    必须**同时**把 `_render_tile` 换成一个「先建 Tk 图再 resample」的版本。
    import image_scout as IS

    def old_render_tile(self, col, cv, cap, path, box, zoom, precise=True):
        """修复前的 `_render_tile`：Tk 图 + resample_img（先丢后放）。"""
        wh = IS.META.of(path)["wh"]
        if not wh or wh[0] <= 0 or wh[1] <= 0:
            return False
        cw = max(1, cv.winfo_width())
        ch = max(1, cv.winfo_height())
        fit = min(box[0] / float(wh[0]), box[1] / float(wh[1]))
        disp_w = max(1, int(round(wh[0] * fit * zoom)))
        disp_h = max(1, int(round(wh[1] * fit * zoom)))
        ox = (cw - disp_w) // 2 + self.pan[col][0]
        oy = (ch - disp_h) // 2 + self.pan[col][1]
        need_side = max(disp_w, disp_h)
        native = max(wh)
        if precise >= 1:
            got = self.big.peek_base(path, need_side, native) \
                or self.big.base(path, need_side, native)
        else:
            got = (self.big.peek_best(path, max(256, int(need_side * 0.75)),
                                      native)
                   or self.big.peek_any(path))
        if not got:
            return False
        bw, bh, bbgra = got
        import math
        padx = int(min(disp_w, cw) * 0.10)
        pady = int(min(disp_h, ch) * 0.10)
        vx0 = max(0, min(disp_w - 1, -ox - padx))
        vy0 = max(0, min(disp_h - 1, -oy - pady))
        vx1 = max(vx0 + 8, min(disp_w, cw - ox + padx))
        vy1 = max(vy0 + 8, min(disp_h, ch - oy + pady))
        sx = bw / float(disp_w)
        sy = bh / float(disp_h)
        px0 = max(0, int(vx0 * sx))
        py0 = max(0, int(vy0 * sy))
        px1 = min(bw, max(px0 + 8, int(math.ceil(vx1 * sx))))
        py1 = min(bh, max(py0 + 8, int(math.ceil(vy1 * sy))))
        blkW, blkH = px1 - px0, py1 - py0
        import thumbs
        _w, _h, cbgra = thumbs.crop_bgra(bw, bh, bbgra, px0, py0, blkW, blkH)
        ppm, _pw, _ph = IS.uikit.fit_ppm(blkW, blkH, cbgra, 0)
        img = tk.PhotoImage(data=ppm, master=self)
        # ⚠️ 这一行就是马赛克的来源：放大 1.1 倍也会走 subsample(10)+zoom(11)
        img = IS.uikit.resample_img(img, int(round(blkW / sx)),
                                    int(round(blkH / sy)), 64.0)
        self._keep.append(img)
        cv.delete("all")
        item = cv.create_image(ox + int(round(px0 / sx)),
                               oy + int(round(py0 / sy)),
                               anchor="nw", image=img)
        self._view[col] = {"cv": cv, "item": item, "ox": ox, "oy": oy,
                           "px": self.pan[col][0], "py": self.pan[col][1],
                           "disp": (disp_w, disp_h), "base": (bw, bh),
                           "blk": (blkW, blkH), "img": img}
        self._render_caption(cap, path, disp_w)
        return True

    IS.App._render_tile = old_render_tile

    def old(img, tw, th, max_zoom=64.0):
        w, h = img.width(), img.height()
        cw, chh = int(max(16, tw)), int(max(16, th))
        s = max(1e-3, min(min(cw / float(w), chh / float(h)),
                          float(max_zoom)))
        if 0.985 <= s <= 1.015:
            return img
        best = None
        for q in range(1, 17):
            p = int(round(s * q))
            if p < 1 or p > max_zoom * q:
                continue
            err = abs(p / float(q) - s)
            if best is None or err < best[0]:
                best = (err, p, q)
        if best is None:
            return img
        _e, p, q = best
        out = img
        if q > 1:
            out = out.subsample(q)      # ⚠️ 丢像素
        if p > 1:
            out = out.zoom(p)            # ⚠️ 复制像素
        return out
    uikit.resample_img = old


def main():
    broken = "--broken" in sys.argv
    if broken:
        break_rendering()
        print("⚠️ 已装回修复前的渲染（subsample + zoom）—— "
              "下面量到的游程就是「有马赛克」的水平\n")
    else:
        print("正常模式：GDI+ 真插值\n")

    import image_scout as S

    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "_e2e")
    # ⚠️ **必须有真重复才能扫出组**：三张互不相同的噪声图会被判成
    #    「三个单张」，groups 为空，探针直接卡在第一步。
    #    照 test_ui 的做法：一张大图 + 它裁掉 10%~90% 的版本 = 同一组。
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    a = make_noise(os.path.join(tmp, "a.png"), 3000, 2000, 7)
    got = winimg.load_pixels(a, 2000, winimg.SIIGBF_RESIZETOFIT, raw=True)
    w, h, px = got
    _w, _h, cut = thumbs.crop_bgra(w, h, px, int(w * 0.1), int(h * 0.1),
                                   int(w * 0.8), int(h * 0.8))
    with open(os.path.join(tmp, "b.png"), "wb") as f:
        f.write(thumbs.bgra_to_png(_w, _h, cut))
    print("造了 1 组：3000×2000 噪声图 + 它的裁剪版\n")

    # ⚠️ App **自己就是** tk 根窗口（它 `tk.Tk()` 建根再扫 argv），
    #    不是「包装了一个根窗口」—— 所以是 `app.update()`，没有 app.root。
    app = S.App([tmp])
    app.geometry("1500x950+0+0")
    app.update()

    # ⚠️ 走**真实入口**（start_scan），不要直接往 groups 里塞 ——
    #    预热队列、基准档缓存、缩略图缓存都是在扫描流程里建起来的，
    #    绕过去的话测的不是主人真正用的那条路径（v1.4 的教训）。
    app.start_scan()
    ok = pump(app, until=lambda: not app.busy and bool(app.files), timeout=300)
    if not ok:
        print("❌ 扫描没跑完（files=%d busy=%s）" % (len(app.files), app.busy))
        return 1
    print("扫到 %d 个文件，%d 组\n" % (len(app.files), len(app.groups)))

    # ⚠️ **别用 `select_group`**：默认视图是「组」，可这张图没被判成组
    #    （噪声图 + 裁剪版的相似度没过阈值），`groups` 是空的，
    #    `select_group(0)` 什么也不做，画布上还是空的。
    #    照 test_ui 的走法：切「全部」视图，再在列表里**按文件名选中**
    #    那张大图 —— 选中才会把它放到 cv_a 上。
    app.set_view("all")
    row = next((i for i, it in enumerate(app.glist.items)
                if it["tag"] == a), None)
    if row is None:
        print("❌ 列表里找不到 %s（items=%d）" % (os.path.basename(a),
                                                len(app.glist.items)))
        return 1
    app.glist.select(row)
    app.update()
    settle_render(app)
    settle_render(app)
    v = app._view.get(0)
    if not v or not v.get("item"):
        print("❌ 选中后画布还是空的（_view=%r）" % (list(app._view),))
        return 1
    print("画布 %dx%d，已显示 %s（显示 %dx%d）\n"
          % (app.cv_a.winfo_width(), app.cv_a.winfo_height(),
             os.path.basename(a), v["disp"][0], v["disp"][1]))

    worst_blk = 0.0
    worst_uniq_ratio = 1.0
    bad = []
    skipped = []
    measured = 0

    class Ev(object):
        """仿造 Tk 的滚轮事件（只带 `_on_wheel` 真正读的字段）。

        ⚠️ **必须走 `_on_wheel`，不能直接改 `app.zoom[0]`**：
        `_on_wheel` 除了改 zoom，还会 `pan[col] = [0,0]`、调
        `_prewarm_yield()`（暂停预热让路）、并 `_later(...)` 排一个
        170ms 后的精确帧。直接赋值只做了第一件事 ——
        预热线程还在抢 GIL、精确帧也没排上，量到的是半就绪的画面。
        """
        def __init__(self, d):
            self.delta = d
            self.x = 10
            self.y = 10

    app.reset_zoom()
    settle_render(app)
    measured_up = 0           # 量到的「真插值放大档」数（收尾要查它 >=2）

    for _ in range(8):
        t0 = time.time()
        app._on_wheel(0, Ev(120))
        app.update()
        settle_render(app)
        dt = (time.time() - t0) * 1000
        v = app._view.get(0)
        st = (canvas_stats(app.cv_a, v["item"], v.get("img"))
              if v and v.get("item") else None)
        if not st:
            skipped.append(round(app.zoom[0], 2))
            print("  zoom %4.2f  ❌ 读不到画面。"
                  "\n        _view[0] = %r"
                  "\n        cv_a items = %r  bbox = %r"
                  "\n        canvas %dx%d  zoom=%r  mode=%r"
                  % (app.zoom[0], app._view.get(0),
                     app.cv_a.find_all(),
                     app.cv_a.bbox(app._view[0].get("item"))
                     if app._view.get(0, {}).get("item") else None,
                     app.cv_a.winfo_width(), app.cv_a.winfo_height(),
                     app.zoom, app.mode))
            continue
        measured += 1          # 这一档量到了（不管是不是放大档）
        # ⚠️⚠️ **必须区分「放大档」和「1:1 / 缩小档」，两者不能用同一个门槛。**
        #
        # 实测数据说明了为什么（3000×2000 噪声图，窗口 975×432）：
        #     zoom 1.25  img  780x 520   取值  23%   <- blk == img，**没放大**
        #     zoom 1.95  img 1474x 654   取值  46%   <- 同上
        #     zoom 4.77  img 1180x 524   取值 101%   <- 插值放大了
        #     zoom 5.96  img 1170x 518   取值  87%   <- 插值放大了
        #
        # 低倍档取值少**不是糊**，是**缩小的必然结果**：那几档的基准档是
        # 768 / 1024 / 1536，噪声图从 3000 缩下来时高频细节被 GDI+ 抹平了，
        # 一行里本来就没那么多不同取值。而放大档（img > blk）才是真的
        # 在做插值，取值比 87~101% 说明**像素几乎全部保留**。
        #
        # 判据：**放大档**才查「取值比 >=55%」；所有档都查「最大游程 <=2px」
        #（色块是主人真正报的 bug，缩小档也一样不能有）。
        # ⚠️ 我第一版对所有档用同一个 35% 门槛，8 档全「失败」，
        #    其中 4 档其实清晰无误 —— 门槛不分档 = 假红。
        v = app._view.get(0) or {}
        blk = v.get("blk") or (0, 0)
        # ⚠️⚠️ **只有「放大倍数够大」才查取值比**。
        #
        # 实测 zoom 1.25 那档：blk 768x512 -> img 780x520，只有 **1.016 倍**。
        # 它量到 23%，但这个 23% 是**基准档自己**的属性 —— 噪声图从 3000
        # 缩到 768 时高频细节已经被 GDI+ 抹平了，一行里本来就只有几十个
        # 取值。1.6% 的放大**不可能**造成这种损失，是判据把它算在了
        # 放大头上（我第一版按 `img > blk` 就查，又是一次假红）。
        #
        # 门槛取 **1.3 倍**：低于这个量级，双三次插值本来就只动一点点，
        # 取值比的变化淹没在基准档自身的差异里；高于它，取值比掉下来
        # 就一定是插值出了问题。
        #
        # 「最大游程」那一项**不分档** —— 色块是主人真正报的 bug，
        # 任何档都不许有（这一项全程 1.00px，即零色块）。
        #
        # ⚠️⚠️ 判定「是不是放大档」要看 **`disp` 对 `base`**，不能看
        #    `img` 对 `blk`。`blk` 是**从基准档裁下来的那一块**，它跟显示
        #    图几乎同尺寸（实测比值只有 1.02~1.24），所以拿它当分母
        #    的话 `up >= 1.3` 一档都过不了 —— 我第一版就是这么写的，
        #    探针最后报「只量到 0 个放大档」才发现。
        #
        # 真正的放大关系是：**显示图需要多少像素 / 基准档有多少像素**。
        # 基准档按 256 量化、要多少解多少，所以 `disp` 通常 ≤ `base`
        # （这时是 1:1，画的就是基准像素本身）；
        # 只有 `disp` 超过 `base`（比如原图就那么大、再放大就没像素了）
        # 才是真的在插值放大。实测 8 档里 5 档是 1:1、1 档放大 1.24x。
        base = v.get("base") or (0, 0)
        disp = v.get("disp") or (0, 0)
        up = (float(max(disp)) / base[0]) if base[0] else 1.0
        upscaled = up >= 1.15
        if upscaled:
            measured_up += 1
        n = st["w"]
        ceiling = 256.0 * (1.0 - (255.0 / 256.0) ** n)
        ratio = st["uniq"] / ceiling
        worst_blk = max(worst_blk, st["blk"])
        if upscaled:
            worst_uniq_ratio = min(worst_uniq_ratio, ratio)
        flag = ""
        # ⚠️ 门槛 **6px**，不是拍脑袋定的 —— 是用 `--broken` 对照量出来的：
        #     旧算法（先丢后放）实测游程 **32 / 38 / 34 / 32 px**
        #     新算法（GDI+ 真插值）实测游程 **2 / 3 px**
        # 差一个数量级，所以 6 两侧都安全。我第一版定 2，把新算法里
        # 「双三次把平滑区四舍五入成同一个灰度」量出的 2~3px 误判成
        # 色块（8 档里 2 档假红）。
        if st["blk"] > 6.0:
            flag = "  <== 有色块！"
            bad.append(app.zoom[0])
        elif upscaled and ratio < 0.55:
            # ⚠️ 门槛 0.55：真插值放大时相邻像素高度相关（双三次会把邻域
            #   平均掉），实测清晰档落在 0.87~1.01；而「先丢后放」
            #   （subsample(10)+zoom(11)）只有 0.1~0.2，两者差一个数量级，
            #   门槛放中间足够宽，也不会放过真糊的。
            flag = "  <== 放大档取值太少（信息丢了）"
            bad.append(app.zoom[0])
        print("  zoom %4.2f  画面 %4dx%4d  块 %4dx%-4d %s  独立取值 %4d"
              " / 上限 %5.1f (%3.0f%%)  最大游程 %.2f px  %4.0f ms%s"
              % (app.zoom[0], st["w"], st["h"], blk[0], blk[1],
                 ("放大%.2fx" % up) if up > 1.02 else "1:1  ",
                 st["uniq"], ceiling, ratio * 100, st["blk"], dt, flag))

    print()
    if skipped:
        print("❌ 有 %d 档没量到（zoom %s）—— 探针没走到渲染路径，"
              "**这次不算通过**。" % (len(skipped), skipped))
    if bad:
        print("❌ zoom %s 这几档有色块或信息丢失。" % bad)
    # ⚠️⚠️ **别再要求「至少 N 个放大档」** —— 我加过这条，探针直接报
    #    「只量到 1 个放大档」，查了半天才发现**这条要求本身是错的**：
    #    修复之后基准档是**按显示尺寸取的**（`need_side` 取自 `disp_w`），
    #    所以 `disp <= base`、绝大多数档就是 **1:1**（画的就是基准像素本身，
    #    根本不需要插值）。实测 8 档里 7 档 1:1、1 档放大 1.24x ——
    #    **这恰恰是修对了的表现**。
    #    「放大档少」不是缺陷；判据只需保证**每一档**（不管 1:1 还是放大）
    #    都没有色块。
    if skipped or bad or measured == 0:
        print()
        print("   量了 %d 档（其中放大档 %d 个），"
              "最差最大游程 %.2f px，最差取值比 %.0f%%"
              % (measured, measured_up, worst_blk,
                 worst_uniq_ratio * 100))
        return 1
    print("✅ 每一档最大游程都 <= 6px（零色块，门槛用 --broken 对照校准过："
          "旧算法 32~38px），放大档独立取值 >=55% 理论上限")
    print("   量了 %d 档（其中基准档不足需插值的 %d 个），"
          "最差最大游程 %.2f px，放大档最差取值比 %.0f%%"
          % (measured, measured_up, worst_blk, worst_uniq_ratio * 100))
    return 0


if __name__ == "__main__":
    sys.exit(main())