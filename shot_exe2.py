# -*- coding: utf-8 -*-
"""验收打包好的 exe：启动 -> 用鼠标事件点「开始扫描」-> 抓界面。

⚠️ 为什么必须点：命令行传入的文件夹只会「列出图片」，不会自动扫。
   而按钮是自绘 Canvas，发 `WM_COMMAND` 没用（没有 command id），
   所以只能用**真实鼠标事件**（SetCursorPos + mouse_event）。

⚠️ 会短暂占用系统光标位置 —— 结束时**恢复原位置**。

用法：python shot_exe2.py <exe 路径> <图片目录> <输出前缀>
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

import uikit            # noqa: E402
import uishot           # noqa: E402

user32 = ctypes.windll.user32
user32.FindWindowW.restype = ctypes.c_void_p
user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
user32.BringWindowToTop.argtypes = [ctypes.c_void_p]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.GetCursorPos.argtypes = [ctypes.c_void_p]

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004


def rect(hwnd):
    rc = (ctypes.c_long * 4)()
    user32.GetWindowRect(hwnd, ctypes.byref(rc))
    return rc[0], rc[1], rc[2], rc[3]


def click(x, y):
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.15)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.06)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(0.25)


def find_window(title="图片查重 · ImageScout", timeout=30.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        h = user32.FindWindowW(None, title)
        if h:
            return h
        time.sleep(0.2)
    return None


def window_pid(hwnd):
    pid = ctypes.c_ulong(0)
    user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
    return pid.value


def kill_stale(name):
    """杀干净同名的残留进程。

    ⚠️⚠️ **PyInstaller 单文件 exe 是「引导器 + 子进程」两个进程**，
       `Popen.kill()` 只杀掉引导器，**真正的窗口属于子进程**，会继续活着。
       于是下一次 `FindWindowW` 抓到的是**上一次的旧窗口** —— 我第一版就中招了：
       点击发给了旧窗口，截出来是「一个刚启动、还没列图的空界面」，
       看着像「点击没生效」，其实是找错了对象。
    """
    subprocess.run(["taskkill", "/F", "/T", "/IM", name],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.8)


def main():
    exe = os.path.abspath(sys.argv[1])
    folder = os.path.abspath(sys.argv[2])
    prefix = os.path.abspath(sys.argv[3])
    uikit.enable_dpi_awareness()

    pos = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pos))
    old = (pos.x, pos.y)

    exe_name = os.path.basename(exe)
    kill_stale(exe_name)
    p = subprocess.Popen([exe, folder])
    print("已启动 %s（引导器 pid %d）" % (exe_name, p.pid))
    hwnd = find_window()
    if not hwnd:
        print("✗ 没等到窗口")
        p.kill()
        return 1
    time.sleep(3.0)
    wp = window_pid(hwnd)
    print("窗口 hwnd=%s 属于 pid %d" % (hwnd, wp))
    if wp == p.pid:
        print("⚠️ 窗口属于引导器？单文件 exe 一般不是这样，先继续")
    L, T, R, B = rect(hwnd)
    W, H = R - L, B - T
    print("窗口 %dx%d @ (%d,%d)" % (W, H, L, T))

    def at(fx, fy):
        return L + W * fx, T + H * fy

    # ⚠️ **先点一次把窗口激活**：Windows 会把「激活未聚焦窗口」的第一次点击
    #    吃掉（不转给控件）。第一次跑碰巧窗口已经在前台，所以一次就成；
    #    第二次换了目录重跑就失效了 —— 只点一次是不可靠的。
    # 强制置前 + 点两次（第一次可能只被用来激活窗口）
    user32.SetForegroundWindow(ctypes.c_void_p(hwnd))
    user32.BringWindowToTop(ctypes.c_void_p(hwnd))
    time.sleep(0.6)
    click(*at(0.5, 0.30))
    time.sleep(0.4)
    click(*at(0.748, 0.084))
    time.sleep(0.4)
    click(*at(0.748, 0.084))
    print("已点「开始扫描」，等扫描跑完…")
    t0 = time.time()
    while time.time() - t0 < 90:
        time.sleep(2.0)
        # 扫描完成 -> 左侧会出现分组行；粗略判据：等够时间就抓
        if time.time() - t0 > 25:
            break
    time.sleep(2.0)
    w, h = uishot.grab(hwnd, prefix + "-1-groups.png")
    print("OK %dx%d -> %s-1-groups.png (%d 字节）"
          % (w, h, os.path.basename(prefix), os.path.getsize(
              prefix + "-1-groups.png")))

    # 「排列 ↓」切一次
    click(*at(0.214, 0.220))
    time.sleep(1.2)
    uishot.grab(hwnd, prefix + "-2-sorted.png")
    print("OK -> %s-2-sorted.png" % os.path.basename(prefix))

    # 「全部图片」
    click(*at(0.114, 0.220))
    time.sleep(1.5)
    # 往下滚一下，验证下拉条
    user32.SetCursorPos(int(L + W * 0.08), int(T + H * 0.45))
    time.sleep(0.2)
    for _ in range(12):
        user32.mouse_event(0x0800, 0, 0, -120, 0)   # MOUSEEVENTF_WHEEL
        time.sleep(0.06)
    time.sleep(0.8)
    uishot.grab(hwnd, prefix + "-3-all.png")
    print("OK -> %s-3-all.png" % os.path.basename(prefix))

    user32.SetCursorPos(old[0], old[1])
    p.kill()
    kill_stale(exe_name)     # 子进程也要收掉，别留给下一次
    print("已恢复光标位置")
    return 0


if __name__ == "__main__":
    sys.exit(main())
