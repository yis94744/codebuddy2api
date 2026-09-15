@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem ============================================================
rem  CodeBuddy2API 打包脚本
rem  产出: dist\CodeBuddy2API.exe （onefile，内嵌 WorkBuddy 客户端图标）
rem  之后可用 Inno Setup 编译 setup.iss 生成安装包
rem
rem  可选：先 set PYTHON=C:\path\to\python.exe 指定用于打包的解释器
rem
rem  ── 关于下面那串 --collect-* / --add-data（别删，都是踩过坑补的）──
rem  PyInstaller 的静态分析看不见「动态导入」和「数据文件」，源码跑得好好的
rem  打包后就炸。已记录的四个：
rem    1) uvicorn.protocols.http.auto   → 服务起不来（uvicorn 内部用
rem       importlib 选协议实现）
rem    2) anyio._backends._asyncio      → 每个请求 500（anyio 动态选后端，
rem       FastAPI 的同步端点全走 starlette → anyio.run_sync）
rem    3) certifi 的 cacert.pem         → 所有 httpx 请求 Errno 2
rem       （证书属数据文件，静态分析不收集）→ --collect-data certifi
rem    4) assets/ 目录                  → exe 图标正常但窗口/任务栏图标
rem       退回 tkinter 默认羽毛（--icon 只改 exe 资源，运行时要读
rem       assets/icon.ico）→ --add-data "assets;assets"
rem  打包后脚本会自动跑一次 --selfcheck，缺件会直接打印出来。
rem ============================================================

rem -- 1. 挑一个装了 PyInstaller 的解释器 ------------------------
if not "%PYTHON%"=="" set "PY=%PYTHON%"
if not defined PY set "PY=%~dp0.venv\Scripts\python.exe"

"%PY%" -c "import PyInstaller" >nul 2>&1
if errorlevel 1 set "PY=%LOCALAPPDATA%\Programs\Python\Python39\python.exe"

"%PY%" -c "import PyInstaller" >nul 2>&1
if errorlevel 1 set "PY=python"

"%PY%" -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo [错误] 找不到可用的 PyInstaller。
    echo        请先安装:  pip install pyinstaller
    echo        或用 set PYTHON=^<解释器路径^> 指定一个已装 PyInstaller 的 Python。
    exit /b 1
)

echo [1/4] 解释器: %PY%

rem -- 2. 校验图标 ----------------------------------------------
if not exist "assets\icon.ico" (
    echo [错误] 缺少图标文件 assets\icon.ico
    exit /b 1
)
echo [2/4] 图标: assets\icon.ico

rem -- 3. 打包 --------------------------------------------------
echo [3/4] 开始打包 ...

rem 保留上一版生成的 config.json（用户可能改过端口/key），打包完再放回去。
rem 若直接连 dist 一起删，打包版启动时会退回默认端口，白折腾一轮。
set "CFG_BAK=%TEMP%\cb2a_cfg_backup.json"
if exist "dist\config.json" copy /y "dist\config.json" "%CFG_BAK%" >nul

rem 账号自定义名映射（uid -> 名称）同样要保留：它只在 dist 目录里，
rem 打包连带删除会让面板里的自定义名全部回退成登录昵称。
set "NAMES_BAK=%TEMP%\cb2a_names_backup.json"
if exist "dist\account_names.json" copy /y "dist\account_names.json" "%NAMES_BAK%" >nul

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

"%PY%" -m PyInstaller ^
  --noconfirm --clean --onefile --noconsole ^
  --name CodeBuddy2API ^
  --icon "assets\icon.ico" ^
  --add-data "ui;ui" ^
  --add-data "assets;assets" ^
  --collect-data certifi ^
  --collect-all customtkinter ^
  --collect-submodules uvicorn ^
  --collect-submodules anyio ^
  --collect-submodules httpcore ^
  --collect-submodules starlette ^
  --hidden-import converter ^
  --hidden-import account_pool ^
  --hidden-import ui_admin ^
  --hidden-import billing ^
  --hidden-import cn_importer ^
  --hidden-import desensitize ^
  --hidden-import responses_adapter ^
  --hidden-import responses_projection ^
  --hidden-import anthropic_adapter ^
  --hidden-import growth ^
  --hidden-import ssl_bootstrap ^
  --hidden-import netenv ^
  --hidden-import selfcheck ^
  --exclude-module PySide6 ^
  --exclude-module shiboken6 ^
  --exclude-module PyQt5 ^
  --exclude-module PyQt6 ^
  --exclude-module numpy ^
  --exclude-module scipy ^
  --exclude-module pandas ^
  --exclude-module matplotlib ^
  app.py

if errorlevel 1 (
    echo.
    echo [失败] 打包出错，请查看上方输出。
    exit /b 1
)

if not exist "dist\CodeBuddy2API.exe" (
    echo.
    echo [失败] 未生成 dist\CodeBuddy2API.exe
    exit /b 1
)

if exist "%CFG_BAK%" (
    copy /y "%CFG_BAK%" "dist\config.json" >nul
    del "%CFG_BAK%" >nul 2>&1
)
if exist "%NAMES_BAK%" (
    copy /y "%NAMES_BAK%" "dist\account_names.json" >nul
    del "%NAMES_BAK%" >nul 2>&1
)

rem -- 4. 自检 --------------------------------------------------
rem onefile 的 exe 启动要解压，这里等久一点。start /wait 才能挡住 GUI 子
rem 系统进程（--noconsole 是 windows 子系统，直接调用不会等待）。
echo [4/4] 运行打包自检 ...
start /wait "" "dist\CodeBuddy2API.exe" --selfcheck

if exist "dist\selfcheck.log" (
    type "dist\selfcheck.log"
) else (
    echo [警告] 未生成 selfcheck.log，无法确认打包完整性。
)

echo.
echo [完成] dist\CodeBuddy2API.exe
echo        下一步: ISCC.exe setup.iss  生成安装包
exit /b 0
