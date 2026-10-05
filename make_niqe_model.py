# -*- coding: utf-8 -*-
"""把官方 NIQE 参数包（`niqe_pris_params.npz`）转成纯 Python 字面量模块。

为什么要有这一步：NIQE 的分数是「图像特征到**官方 pristine 模型**的马氏距离」，
那个模型（36 维均值 + 36×36 协方差）是论文作者在 pristine 图像库上标定出来的，
**不能自己造** —— 造一套出来的分数看着像模像样，其实没有任何意义。
所以参数必须原封不动地来自官方发布包，而本工程是零第三方依赖（没有 numpy），
`npz` 又是 numpy 的格式，于是这里用纯标准库把 `npz`（本质是 zip）里的
`.npy`（一个很简单的头 + 裸数据）解出来，写成 Python 字面量。

参数来源：BasicSR 仓库 `basicsr/metrics/niqe_pris_params.npz`
（就是官方 NIQE release 里的 `niqe_modelparameters.mat` 转过来的，
BasicSR 注明了「我们用的是从 pristine 数据集估出来的**官方**参数」）。
    https://github.com/XPixelGroup/BasicSR/blob/master/basicsr/metrics/niqe_pris_params.npz

⚠️ **别用 numpy 的 `np.save` 顺序去读**：这三个数组都有 `fortran_order` 标志，
协方差和 7×7 高斯窗是**列优先**存的。按行优先去读会得到一个对称性被破坏的矩阵，
NIQE 分数会错得看不出来（协方差错了但数值仍在同一量级）。

用法：
    python make_niqe_model.py            # 读同目录的 npz，重写 niqe_model.py
"""
import ast
import os
import struct
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
NPZ = os.path.join(HERE, "niqe_pris_params.npz")
OUT = os.path.join(HERE, "niqe_model.py")

# 官方参考值：BasicSR 记录同一张图在 MATLAB R2021a 下的结果是 5.72957338，
# 它自己的复现是 5.7295763。test_niqe.py 拿这个当验收锚点。
GOLDEN = "baboon.png 492×480 -> 5.7296"


def read_npy(data):
    """解一个 .npy，返回 (形状元组, 按 C 序展开的 float 列表)。

    只支持 '<f8'（float64）—— 官方包里就这一种，多支持没有意义。
    """
    if not data.startswith(b"\x93NUMPY"):
        raise ValueError("不是 .npy（magic 不对）")
    major = data[6]
    if major == 1:
        hlen = struct.unpack_from("<H", data, 8)[0]
        off = 10
    else:                                    # v2/v3：头长度改成 4 字节
        hlen = struct.unpack_from("<I", data, 8)[0]
        off = 12
    hdr = ast.literal_eval(data[off:off + hlen].decode("latin1"))
    if hdr["descr"] != "<f8":
        raise ValueError("只支持 float64，拿到 %r" % (hdr["descr"],))
    shape = tuple(int(v) for v in hdr["shape"])
    n = 1
    for v in shape:
        n *= v
    body = data[off + hlen:]
    flat = list(struct.unpack_from("<%dd" % n, body, 0))
    if hdr["fortran_order"] and len(shape) == 2:
        rows, cols = shape
        # 列优先：第 (i, j) 个元素存在 i + j*rows
        flat = [flat[i + j * rows] for j in range(cols) for i in range(rows)]
    return shape, flat


def load_params(path=NPZ):
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        need = ("mu_pris_param.npy", "cov_pris_param.npy",
                "gaussian_window.npy")
        missing = [n for n in need if n not in names]
        if missing:
            raise ValueError("参数包里缺 %s（实际有 %s）"
                             % (missing, sorted(names)))
        mu_shape, mu = read_npy(z.read("mu_pris_param.npy"))
        cov_shape, cov = read_npy(z.read("cov_pris_param.npy"))
        gw_shape, gw = read_npy(z.read("gaussian_window.npy"))
    if len(mu) != 36:
        raise ValueError("mu_pris_param 应该是 36 个，实际 %d" % len(mu))
    if cov_shape != (36, 36):
        raise ValueError("cov_pris_param 应该是 36x36，实际 %s" % (cov_shape,))
    if gw_shape != (7, 7):
        raise ValueError("gaussian_window 应该是 7x7，实际 %s" % (gw_shape,))
    if mu_shape not in ((1, 36), (36,)):
        raise ValueError("mu_pris_param 形状意外：%s" % (mu_shape,))
    return mu, [cov[i * 36:(i + 1) * 36] for i in range(36)], gw


def fmt(v):
    s = "%.17g" % v
    if "." not in s and "e" not in s and "E" not in s:
        s += ".0"
    return s


def emit(mu, cov, gw, path=OUT):
    L = []
    a = L.append
    a('# -*- coding: utf-8 -*-')
    a('"""NIQE 的 pristine 模型参数（官方参数包的纯 Python 版本）。')
    a('')
    a('⚠️ **这个文件是生成的，不要手改** —— 手改一个数字，NIQE 分数就悄悄不对了，')
    a('而且不会有任何报错。要更新就跑 `python make_niqe_model.py`，')
    a('它会从同目录的 `niqe_pris_params.npz` 重新生成。')
    a('')
    a('来源：BasicSR 的 `basicsr/metrics/niqe_pris_params.npz`（官方 NIQE release 的参数）。')
    a('这套参数是论文作者在 pristine 图像库上估出来的，**不能自己造**：')
    a('NIQE 分数 = 图像特征到这套模型的马氏距离，模型换了分数就失去意义。')
    a('')
    a('`test_niqe.py` 里有两道保险：')
    a('  1. 从 npz 重新解析一遍，逐个 float 断言与本文件一致（防手改 / 防转错）')
    a('  2. 官方参考图 %s' % GOLDEN)
    a('"""')
    a('')
    a('MU_PRIS = (')
    for i in range(0, 36, 4):
        a('    ' + ' '.join(fmt(v) + ',' for v in mu[i:i + 4]))
    a(')')
    a('')
    a('COV_PRIS = (')
    for r in cov:
        a('    (' + ' '.join(fmt(v) + ',' for v in r) + '),')
    a(')')
    a('')
    a('GAUSSIAN_WINDOW = (')
    for i in range(0, 49, 7):
        a('    (' + ' '.join(fmt(v) + ',' for v in gw[i:i + 7]) + '),')
    a(')')
    a('')
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(L))
    return path


def main():
    mu, cov, gw = load_params()
    p = emit(mu, cov, gw)
    print("已写出 %s" % p)
    print("  mu_pris_param     36 个  前 4 个：%s" % [round(v, 6) for v in mu[:4]])
    print("  cov_pris_param    36x36  对角线前 4 个：%s"
          % [round(cov[i][i], 6) for i in range(4)])
    print("  gaussian_window   7x7    最小值 %.6g 最大值 %.6g"
          % (min(gw), max(gw)))
    # 协方差必须是对称的，否则一定是读错了（fortran_order 踩坑点）
    bad = [(i, j) for i in range(36) for j in range(i + 1, 36)
           if abs(cov[i][j] - cov[j][i]) > 1e-9]
    if bad:
        print("!! 协方差不对称，%d 处（前 3 处 %s）—— 多半是 fortran_order 读错了"
              % (len(bad), bad[:3]))
        return 1
    print("  协方差对称性检查：通过（36x36 全部 %d 对）" % (36 * 35 // 2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
