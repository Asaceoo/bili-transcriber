@echo off
setlocal
chcp 65001 >nul
echo ===== bili-transcriber 打包脚本 =====

set VENV=.venv
set BUILD_DIR=build
set DIST_DIR=dist

:: 1. 安装 pyinstaller
echo [1/3] 安装 PyInstaller ...
call %VENV%\Scripts\pip.exe install pyinstaller --quiet -q

:: 2. PyInstaller 打包 (--onedir 便携版)
echo [2/3] PyInstaller 打包中 (nvidia DLL 约 2 GB,请耐心等待) ...
%VENV%\Scripts\pyinstaller.exe --clean --noconfirm %BUILD_DIR%\bili-transcriber.spec
if errorlevel 1 (
    echo !! PyInstaller 打包失败
    exit /b 1
)

:: 3. Inno Setup 生成安装包
set ISCC="C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if exist %ISCC% (
    echo [3/3] Inno Setup 生成安装包 ...
    %ISCC% %BUILD_DIR%\installer.iss
    if errorlevel 1 (
        echo !! Inno Setup 编译失败
        exit /b 1
    )
    echo.
    echo ===== 打包完成 =====
    echo 便携版: %DIST_DIR%\bili-transcriber\
    echo 安装包: %DIST_DIR%\bili-transcriber-setup.exe
) else (
    echo [3/3] 未找到 Inno Setup,跳过安装包生成
    echo   便携版已生成: %DIST_DIR%\bili-transcriber\
    echo   如需安装包请安装 Inno Setup 6
)

echo.
pause
