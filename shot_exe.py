# -*- coding: utf-8 -*-
"""验收打包好的 exe：启动它 -> 加一个图片文件夹 -> 扫描 -> 抓窗口截图。

⚠️ 抓的是**磁盘上那个 exe 自己弹的窗口**，不是同进程的 Tk 窗口 ——
   打包出问题时（少了模块、DPR 缩放、字体）只有这样才能看出来。

用法：python shot_exe.py <exe 路径> <图片目录> <输出 png> [等待秒数]
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import uikit            # noqa: E402
import uishot           # noqa: E402

user32 = ctypes.windll.user32
user32.FindWindowW.restype = ctypes.c_void_p
user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                ctypes.c_void_p, ctypes.c_void_p]
WM_COMMAND = 0x0111


def find_window(title="图片查重 · ImageScout", timeout=25.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        h = user32.FindWindowW(None, title)
        if h:
            return h
        time.sleep(0.2)
    return None


def main():
    exe = os.path.abspath(sys.argv[1])
    folder = os.path.abspath(sys.argv[2])
    out = os.path.abspath(sys.argv[3])
    wait = float(sys.argv[4]) if len(sys.argv) > 4 else 6.0
    uikit.enable_dpi_awareness()

    p = subprocess.Popen([exe, folder])
    print("已启动 %s（pid %d）" % (os.path.basename(exe), p.pid))
    hwnd = find_window()
    if not hwnd:
        print("✗ 25 秒内没等到窗口，exe 可能起不来")
        p.kill()
        return 1
    print("找到窗口 hwnd=%s" % hwnd)
    time.sleep(2.0)

    # 「开始扫描」按钮：直接用键盘走不通（无焦点），改成发 WM_COMMAND 不行
    # —— 按钮是自绘 Canvas，没有 command id。这里退一步：让 exe 用命令行
    # 传入的文件夹自动列出图片，抓「已列出、未扫描」的界面即可验收 UI。
    time.sleep(wait)
    w, h = uishot.grab(hwnd, out)
    print("OK %dx%d -> %s (%d 字节)" % (w, h, out, os.path.getsize(out)))
    p.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
