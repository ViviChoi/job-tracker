@echo off
chcp 65001 >nul
cd /d "%~dp0"

:: 检查 Python（优先 py 启动器，Windows 标准安装自带，最可靠）
py --version >nul 2>&1
if not errorlevel 1 (
    set PYTHON=py
    goto :found_python
)
python --version >nul 2>&1
if not errorlevel 1 (
    set PYTHON=python
    goto :found_python
)
echo.
echo 未找到 Python，请先安装：https://www.python.org
echo 安装时记得勾选 "Add Python to PATH"
echo.
pause
exit /b 1

:found_python

:: 创建虚拟环境（如不存在）
if not exist ".venv\Scripts\python.exe" (
    echo ================================================
    echo   首次启动，正在创建环境，请稍候（约1-2分钟）...
    echo ================================================
    %PYTHON% -m venv .venv
    if errorlevel 1 (
        echo 创建虚拟环境失败，请检查 Python 安装是否正常
        pause
        exit /b 1
    )
    .venv\Scripts\python.exe -m pip install --upgrade pip -q
)

:: 每次确保依赖完整（已安装的包会被跳过，非常快）
.venv\Scripts\python.exe -m pip install -q -r requirements.txt
if errorlevel 1 (
    echo 依赖安装失败，请查看上方错误信息
    pause
    exit /b 1
)

echo 启动 Job Tracker 配置界面...

:: 检查端口是否已在运行
netstat -ano 2>nul | findstr ":8080" >nul 2>&1
if not errorlevel 1 (
    echo 检测到服务已在运行，直接打开浏览器...
    start "" http://localhost:8080
) else (
    start "" http://localhost:8080
    .venv\Scripts\python.exe setup.py
)

pause
