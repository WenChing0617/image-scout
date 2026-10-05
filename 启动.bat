@echo off
setlocal
cd /d "%~dp0"

rem 打包过 exe 就直接用 exe
if exist "ImageScout.exe" (
  start "" "ImageScout.exe" %*
  exit /b 0
)

rem 找一个**带 Tkinter** 的 Python（PATH 上的 python 可能是精简版，没带 Tk）
set "PY="
call :probe pythonw.exe
if not defined PY call :probe python.exe
if not defined PY if exist "D:\python\python-3.13.5\pythonw.exe" set "PY=D:\python\python-3.13.5\pythonw.exe"

if not defined PY (
  echo.
  echo  [x] 没找到带 Tkinter 的 Python。
  echo      装一个 python.org 的官方版即可（安装时不用特意取消 tcl/tk）。
  echo.
  pause
  exit /b 1
)

start "" "%PY%" "image_scout.py" %*
exit /b 0

:probe
for /f "delims=" %%X in ('where %1 2^>nul') do (
  if not defined PY (
    "%%X" -c "import tkinter" >nul 2>nul && set "PY=%%X"
  )
)
exit /b 0
