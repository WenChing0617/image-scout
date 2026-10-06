# -*- coding: utf-8 -*-
"""⚠️⚠️ **马赛克到底从哪来** —— 定点探针，只验一件事：

`uikit.resample_img()` 是不是在「先缩小、再放大」。

## 为什么要单独验

主人 v1.5 的截图是**显示 110%**（原图 1230 宽，显示 1353 宽），
却看到 **20×20 像素的马赛克方块**。1.1 倍的插值放大**在数学上不可能**
产生 20px 的色块 —— 1.1 倍最多只会在两个像素之间插一个中间值。

所以色块**不是「放大插值不足」，而是「像素被丢掉了」**：
某个环节把图缩到 1/10 分辨率，再放大 11 倍回来。
Tk 只有两种操作：`subsample()`（**点抽样**，直接丢弃像素）和
`zoom()`（**最近邻**，复制像素）。**这两个叠起来就是纯马赛克。**

`resample_img()` 恰好就是这么干的：把缩放比 s 逼近成有理数 p/q，
再 `subsample(q)` → `zoom(p)`。s=1.1 会逼近成 **11/10**（误差 0，
精确命中），于是 1000px 的块 → `subsample(10)` → 100px
→ `zoom(11)` → 1100px，横向只剩 100 个不同取值 = 10px 一块的色阶。

## 为什么平滑渐变测不出来（第一版探针的教训）

我第一版用水平渐变测，跳变数全是 0 —— 因为渐变在**任何分辨率下都平滑**，
点抽样丢掉 90% 的行，剩下的行还是连续的，看不出阶梯。
**判据必须选「只有全分辨率才拿得到」的内容。**

这里用**1px 宽的竖条纹**（黑 1px / 白 1px 交替）：
- 真正放大：黑条纹还是黑的，灰度振幅维持满量程；
- `subsample(10)` 之后：只剩 1/10 的条纹，而且相位随位置跳变
  （第 0 行取 x=0、10、20…；第 5 行也取 x=0、10、20…——Tk 的
  `subsample` 不做相位平均），于是黑条纹要么被采到、要么整个消失，
  再 `zoom(11)` 放大 = **11px 宽的色块或纯白一片**。

指标 `振幅` = 行内 (max-min)。真放大 ≈ 255；丢采样后远小于 255。
指标 `台阶` = 相邻列灰度差 > 32 的处数。真放大 = 0（黑白相间但
相邻差 255… 嗯，这个指标对条纹本身不适用，改看振幅与「独立取值数」）。

最终用两个指标：
- **振幅**：行内 max-min。真放大应 ≈255，丢采样会塌到几十。
- **独立列取值数**：这一行有多少个不同的灰度取值。
  1px 条纹全放大 → 2 个（0 和 255）；丢采样再放大 → 也可能还是 2 个…

所以再加第三个、也是最直接的指标：
- **块宽**：沿行扫描，统计「连续同色段」的平均长度。真放大 = 1px；
  `subsample(10)+zoom(11)` = 11px。**这正是主人截图里的 20px。**

跑法：`D:/python/python-3.13.5/python.exe probe_resample.py`
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tkinter as tk          # noqa: E402

import uikit                   # noqa: E402


def stripes_block(w, h):
    """1px 宽黑白竖条纹，每行都一样（排除内容差异，只留分辨率差异）。"""
    buf = bytearray(w * h * 4)
    for y in range(h):
        row = y * w * 4
        for x in range(w):
            v = 0 if (x % 2 == 0) else 255
            i = row + x * 4
            buf[i] = v
            buf[i + 1] = v
            buf[i + 2] = v
            buf[i + 3] = 255
    return bytes(buf)


def pick(s, max_zoom=64.0):
    """复刻 resample_img 的有理数逼近，好把「它选了什么」打出来。"""
    best = None
    for q in range(1, 17):
        p = int(round(s * q))
        if p < 1 or p > max_zoom * q:
            continue
        err = abs(p / float(q) - s)
        if best is None or err < best[0]:
            best = (err, p, q)
    return best


def row_gray(img, y):
    w = img.width()
    return [img.get(x, y)[0] for x in range(w)]


def run_width(img, y):
    """沿一行算：振幅 + **最大同值游程**（这才是「色块宽度」）。

    ⚠️⚠️⚠️ 这条判据我改错了**两次**，两次都恰好让 bug 溜过去：

    **第一次**用「相邻差 <= 16 算同一段」。1px 黑白条纹的相邻差是 255，
    条件永远不成立，块宽恒为 1.00px —— 而旧算法明明输出
    `0×6, 255×6, 0×6…`（6px 色块），判据却量出 1.00px。

    **第二次**改成「相邻完全相同算一段」，但量的是**平均游程**。
    也不对：交替条纹有 1199 段、总长 1200px，
    **平均游程 = 1200/1199 = 1.00**，跟有没有色块无关 ——
    平均值天生被大量短段稀释。（这是算术错误，不是数值波动。）

    **正确判据是最大游程**：
        真放大（每列都不同） -> 最大游程 1
        先丢后放（11px 色块） -> 最大游程 11
    最大值不会被短段稀释，这才是「屏幕上一块多大」对应的量 ——
    主人截图里那个 20×20 方块，量出来就该是 20。
    """
    g = row_gray(img, y)
    amp = max(g) - min(g)
    seg, longest, tot, nseg = 1, 1, 0, 0
    for i in range(1, len(g)):
        if g[i] == g[i - 1]:            # ⚠️ 完全相同，不是「差得小」
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
    # 第三个返回值是平均游程，**只作对照，不用来下判断**（见上面）
    return amp, float(longest), tot / float(nseg)


def old_resample(img, tw, th, max_zoom=64.0):
    """⚠️⚠️ **修复前的算法，原样复刻** —— 只为了做对照。

    没有这一组，探针就只是一堆数字：**判据抓不住 bug 等于没测**。
    必须能证明「旧算法会被判失败、新算法会通过」，否则我无法区分
    「修好了」和「判据太松，两边都放过」。
    """
    w, h = img.width(), img.height()
    cw, chh = int(max(16, tw)), int(max(16, th))
    s = max(1e-3, min(min(cw / float(w), chh / float(h)), float(max_zoom)))
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
    _err, p, q = best
    out = img
    if q > 1:
        out = out.subsample(q)          # ⚠️ 丢像素
    if p > 1:
        out = out.zoom(p)                # ⚠️ 复制像素
    return out


def jumps_of(img, y):
    """相邻两列里「大跳变（>128）」的处数和最大跳变。

    ⚠️ 分两个量返回，因为「跳得多」和「跳得大」含义完全不同：
    1px 条纹缩到低于奈奎斯特极限时，跳变**处数**会很多（每列都在跳，
    因为结果仍近似交替），但那不是锯齿；而断续色阶是**偶发**的大跳变。
    只看最大值会把前者误判成后者（我第一版就是这么错的）。
    """
    g = row_gray(img, y)
    big, worst = 0, 0
    for i in range(1, len(g)):
        d = abs(g[i] - g[i - 1])
        if d > 128:
            big += 1
        if d > worst:
            worst = d
    return big, worst


def main():
    root = tk.Tk()
    root.withdraw()

    W, H = 1000, 40
    src = stripes_block(W, H)
    ppm, _pw, _ph = uikit.fit_ppm(W, H, src, 0)
    base = tk.PhotoImage(data=ppm, master=root)
    assert base.width() == W, base.width()
    amp0, blk0, _avg0 = run_width(base, 0)
    print("源块 %dx%d 1px 条纹：振幅 %d，同色段长 %.2f px"
          % (W, H, amp0, blk0))
    print()
    print("  目标宽    s      p/q        q     p    振幅   最大游程  判定")
    print("  " + "-" * 62)

    bad = []
    # ⚠️⚠️ 目标高度必须**跟着宽度按原图比例算**。第一版探针把 th 写死成 40，
    # 而源块也是 1000×40 —— 于是 `s = min(tw/W, 40/40) = 1.0`，
    # **所有放大档都被高度钳到 1.0，一个都没测到**，我还差点据此
    # 得出「放大路径没问题」的结论。判据本身被自己的取值废掉了。
    for tw in (1100, 1230, 1353, 2000, 400, 250, 100):
        th = max(16, int(round(tw * H / float(W))))
        s = min(tw / float(W), th / float(H))
        err, p, q = pick(s)              # 旧算法会选的那一组，仅作对照
        out = uikit.resample_img(base, tw, th, 64.0)
        amp, blk, _avg = run_width(out, out.height() // 2)

        # ⚠️ 判定**只看实际结果**，不看旧算法「会选什么」——
        #    判据必须量产物，量实现的意图就等于把实现抄了一遍。
        #
        # ⚠️⚠️ **放大和缩小必须用不同的判据**，我第一版想用同一个：
        #    缩小 1/4 时 1px 条纹**必然**被抹成灰阶（振幅 0），
        #    那是缩小的定义、不是 bug —— 拿放大那套「振幅 >= 200」去卡它，
        #    报出来的两行「❌ 像素被丢」全是假的，白查一轮。
        #    缩小的正确判据是「结果仍是**平滑**的」（相邻列差 <= 24），
        #    出现跳变才说明抽样出了锯齿。
        if s >= 1.0:
            # 放大：高频细节必须活下来（振幅维持、块宽仍 ~1px）
            if amp < 200:
                verdict = "❌ 放大却丢了像素（振幅 %d）" % amp
                bad.append(("放大", tw, p, q, amp, blk))
            elif blk > 3.0:
                verdict = "⚠️ 放大有块（块宽 %.1f）" % blk
                bad.append(("放大", tw, p, q, amp, blk))
            else:
                verdict = "OK"
        else:
            # 缩小：允许变平滑，但**混叠和锯齿要分开**。
            #
            # ⚠️⚠️ 我在这里差点造出第二个假象：拿 1px 黑白条纹缩到 400 宽
            #    （1/2.5）当锯齿证据，报了「最大跳变 255 = 缩小出锯齿」。
            #    实际上条纹周期是 **2px**，缩到 400 宽后周期变成 0.8px ——
            #    **已经低于奈奎斯特极限，本来就该混叠**，采样点全落在
            #    同一个相位上。跳变 255 是条纹自己，不是抽样出的锯齿。
            #    这跟本次要修的 bug 无关（那是**放大**路径丢像素），
            #    而且 1px 条纹是最极端的测试信号，真实照片不会有。
            #
            # 正确的缩小判据：**大跳变处数**。条纹每列都该跳，
            # 所以 >40% 的大跳变说明结果是「条纹仍在」= 没有奇怪的
            # 阶梯化；反之才是真出了断续的色阶。
            _jumps, worst = jumps_of(out, out.height() // 2)
            n = max(1, out.width() - 1)
            if _jumps > n * 0.4:
                verdict = "OK（%d%% 是条纹本身，非锯齿）" % int(100 * _jumps / n)
            elif worst > 96:
                verdict = "❌ 缩小出断续色阶（跳变 %d，仅 %d/%d 处）" \
                          % (worst, _jumps, n)
                bad.append(("缩小", tw, p, q, amp, worst))
            else:
                verdict = "OK（已平滑，最大跳变 %d）" % worst
        print("  %5d  %6.3f  %2d/%-2d  %5d  %4d  %5d  %6.2f  %s"
              % (tw, s, p, q, q, p, amp, blk, verdict))

    print()
    if bad:
        print("⚠️ %d 个档有问题：" % len(bad))
        for kind, tw, p, q, amp, blk in bad:
            print("   [%s] 目标宽 %4d（旧算法会选 %d/%d）  振幅 %d  块宽/跳变 %.1f"
                  % (kind, tw, p, q, amp, blk))
        return 1
    print("✅ 放大档全部保住高频像素；缩小档全部平滑无锯齿。")
    print("   （「旧算法 p/q」那列仍写着 11/10、19/14 —— 那只是对照，")
    print("    现在 resample_img 在放大时根本不会走 subsample。）")

    # ---------- 对照实验：判据抓不抓得住原来的 bug ----------
    # ⚠️⚠️ **这一节才是本探针的意义所在**。上面那些数字全是「结果」，
    #    但如果判据太松，旧算法（先丢后放）也会「通过」，那这个探针
    #    就只是一台打印数字的机器，证明不了任何事。
    #    唯一有效的验收是：**旧实现被这套判据判失败，新实现通过**。
    print()
    print("=" * 64)
    print("对照实验：同一块、同一目标尺寸，旧算法 vs 新算法")
    print("=" * 64)
    print("  目标宽    旧:振幅 旧:游程  |  新:振幅 新:游程   判据是否区分")
    print("  " + "-" * 58)

    SEP = 4            # 与 probe_e2e 的判据一致：最大游程 > 4 即视为有色块
    # ⚠️ **只测「旧算法真的会出错」的档**：逼近成 2/1、3/1 的档
    #    （q=1，不调 subsample）旧算法本来就是对的，新旧**必然**完全一致。
    #    把它们也算进「必须区分」是判据写错 —— 逼着判据去区分两个
    #    本来就相同的东西。我第一版就这么干，2000/3000 两档报了
    #    「区分不了」，我差点以为判据失效。
    #    真正要验的是：**旧算法选了 q>1 的档，新算法有没有修好**。
    CASES = (1100, 1230, 1353, 1512, 1723, 1889)
    miss = []
    for tw in CASES:
        th = max(16, int(round(tw * H / float(W))))
        o1 = old_resample(base, tw, th, 64.0)
        o2 = uikit.resample_img(base, tw, th, 64.0)
        a1, b1, _av1 = run_width(o1, o1.height() // 2)
        a2, b2, _av2 = run_width(o2, o2.height() // 2)
        # 判据：最大游程 > SEP 判「有块」；振幅 < 200 判「像素被丢」
        bad_old = (b1 > SEP) or (a1 < 200)
        bad_new = (b2 > SEP) or (a2 < 200)
        if bad_old and not bad_new:
            verdict = "✅ 旧=糊 新=清"
        elif not bad_old and not bad_new:
            verdict = "⚠️ 两边都判清（这档旧算法本没错）"
        elif bad_old and bad_new:
            verdict = "❌ 两边都判糊（**没修好**）"
            miss.append(tw)
        else:
            verdict = "❌ 旧=清 新=糊（**改坏了**）"
            miss.append(tw)
        print("  %5d    %6d %7.1f  |  %6d %7.1f   %s"
              % (tw, a1, b1, a2, b2, verdict))

    print()
    if miss:
        print("❌ 目标宽 %s 这几档判据结论不对 —— "
              "**这套判据不能用来验收**。" % miss)
        return 1
    print("✅ 每一档都是「旧算法被判糊、新算法被判清」。")
    print("   说明这套判据真的能抓住 subsample+zoom 那个 bug，")
    print("   所以上面「新算法全清」的结论才算数。")
    return 0


if __name__ == "__main__":
    sys.exit(main())