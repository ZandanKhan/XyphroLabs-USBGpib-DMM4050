@echo off
setlocal
title Tektronix DMM4050 Measurement Console R05

cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python launcher "py" was not found.
    echo Install Python for Windows and enable the Python launcher.
    echo.
    pause
    exit /b 1
)

py -c "import pyvisa, matplotlib" >nul 2>nul
if errorlevel 1 (
    echo Required Python packages are missing.
    echo Installing PyVISA and Matplotlib...
    py -m pip install pyvisa matplotlib
    if errorlevel 1 (
        echo.
        echo [ERROR] Package installation failed.
        pause
        exit /b 1
    )
)

py "%~dp0dmm4050_gui_gpib10_R05.py"
if errorlevel 1 (
    echo.
    echo [ERROR] The DMM4050 application ended with an error.
    pause
)

endlocal
