@echo off
rem phpvm 启动脚本：优先使用 C:\Python312\pythonw.exe（无控制台窗口），否则使用 PATH 中的 pythonw
cd /d %~dp0
set "PYEXE=pythonw"
if exist "C:\Python312\pythonw.exe" set "PYEXE=C:\Python312\pythonw.exe"
start "" "%PYEXE%" main.py
