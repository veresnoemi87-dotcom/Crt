@echo off
REM CRT language installer for Windows
REM Installs crt-cli so the "crt" command works from any terminal.

setlocal
cd /d "%~dp0"

echo Checking for Python...
where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found on PATH.
    echo Please install Python 3.8+ from https://www.python.org/downloads/
    echo and make sure to check "Add Python to PATH" during setup.
    pause
    exit /b 1
)

echo Installing crt-cli...
python -m pip install --upgrade pip >nul
python -m pip install .

if errorlevel 1 (
    echo.
    echo Install failed. See errors above.
    pause
    exit /b 1
)

echo.
set /p GFX="Install pygame too, for graphics scripts (pygame_init etc.)? [Y/n] "
if /i "%GFX%"=="n" goto skip_gfx
python -m pip install pygame
:skip_gfx

echo.
echo Done! Try running:  crt
echo   crt run yourfile.ct
echo   crt build yourfile.ct -o yourfile.cvm
echo   crt exec yourfile.cvm
echo   crt disasm yourfile.cvm
echo   crt run examples_bounce_demo.ct     (needs pygame)
pause
