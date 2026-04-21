@echo off
chcp 65001 >nul
echo ====================================
echo  Building Plaud Registration Tools
echo ====================================
cd /d "%~dp0"

echo [1/4] Installing dependencies...
python -m pip install requests rich coincurve pycryptodome flask pyinstaller charset-normalizer --quiet

echo [2/4] Building plaud_web.exe (Web UI)...
python -m PyInstaller --onefile --console --name "plaud_web" ^
    --collect-all "charset_normalizer" ^
    --hidden-import "coincurve" ^
    --hidden-import "Crypto.Cipher.AES" ^
    --hidden-import "Crypto.Util.Padding" ^
    --hidden-import "flask" ^
    --hidden-import "werkzeug" ^
    --hidden-import "jinja2" ^
    --hidden-import "click" ^
    plaud_web.py

echo [3/4] Building plaud_register.exe (CLI)...
python -m PyInstaller --onefile --console --name "plaud_register" ^
    --collect-all "rich" ^
    --collect-all "charset_normalizer" ^
    --hidden-import "coincurve" ^
    --hidden-import "Crypto.Cipher.AES" ^
    --hidden-import "Crypto.Util.Padding" ^
    plaud_register.py

echo [4/4] Finalizing...
if exist dist\plaud_web.exe (
    copy /Y dist\plaud_web.exe "%~dp0plaud_web.exe" >nul
    echo   plaud_web.exe      OK
) else ( echo   plaud_web.exe      FAILED )

if exist dist\plaud_register.exe (
    copy /Y dist\plaud_register.exe "%~dp0plaud_register.exe" >nul
    echo   plaud_register.exe OK
) else ( echo   plaud_register.exe FAILED )

echo.
echo Done! Run plaud_web.exe to open the web UI.
pause
