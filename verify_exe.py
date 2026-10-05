# -*- coding: utf-8 -*-
"""打包验收：启动 `ImageScout.exe`，等窗口出现，PrintWindow 抓成 PNG。

⚠️ onefile 是**双进程**（bootloader + 真实应用），返回的 Popen.pid 是 bootloader 的，
拿它去匹配窗口 PID 会扑空 —— 所以按**窗口类名 == 'TkTopLevel'** 枚举匹配。

用法：python verify_exe.py [exe路径] [输出png]
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import uikit                                    # noqa: E402  (只借 DPI 感知)
from uishot import grab                         # noqa: E402  (复用现成抓图)

user32 = ctypes.windll.user32

EnumWindows = user32.EnumWindows
EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                           wintypes.LPARAM), wintypes.LPARAM]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                wintypes.UINT]
user32.SetWindowPos.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                            ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD


def find_tk_windows():
    """返回 [(hwnd, title, pid)]：所有可见的 TkTopLevel 顶层窗口。"""
    out = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        if buf.value != "TkTopLevel":
            return True
        user32.GetWindowTextW(hwnd, buf, 256)
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        out.append((hwnd, buf.value, pid.value))
        return True

    EnumWindows(cb, 0)
    return out


def main():
    exe = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "ImageScout.exe")
    out_png = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "shot-exe.png")

    # 开 DPI 感知：不开的话抓到的窗口被系统拉伸过，字是糊的
    uikit.enable_dpi_awareness()

    before = {h for h, _, _ in find_tk_windows()}
    proc = subprocess.Popen([exe], cwd=os.path.dirname(exe))
    print("exe 已启动 pid=%s  -> %s" % (proc.pid, exe))

    hwnd = None
    t0 = time.time()
    while time.time() - t0 < 60:                # onefile 首跑要解包，给足时间
        time.sleep(0.5)
        now = [(h, t, p) for h, t, p in find_tk_windows() if h not in before]
        if now:
            hwnd, title, _ = now[0]
            break
    if hwnd is None:
        print("FAIL：60 秒内没等到 TkTopLevel 窗口")
        return 1

    print("窗口出现：hwnd=%s title=%r  （等界面画完）" % (hwnd, title))
    # SWP_NOMOVE|SWP_NOSIZE|SWP_SHOWWINDOW，置顶一次，避免被别的窗口挡住
    user32.SetWindowPos(hwnd, wintypes.HWND(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040)
    user32.SetForegroundWindow(hwnd)
    time.sleep(3.0)                             # 首帧绘制 + 缩略图缓存建好

    w, h = grab(hwnd, out_png)
    print("OK %dx%d -> %s (%d 字节)" % (w, h, out_png, os.path.getsize(out_png)))

    # 验证完杀掉（onefile 双进程：只杀 Popen 那个可能留下真进程，两个都试）
    subprocess.run(["taskkill", "/F", "/IM", "ImageScout.exe"],
                   capture_output=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
