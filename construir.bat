@echo off
setlocal
set "ROOT=%~dp0"
set "VENV=%TEMP%\vc\venv"
if not exist "%VENV%\Scripts\pyinstaller.exe" (
    python -m venv "%VENV%" || exit /b 1
    "%VENV%\Scripts\python.exe" -m pip install -r "%ROOT%requirements.txt" || exit /b 1
)
"%VENV%\Scripts\pyinstaller.exe" --noconfirm --onedir --windowed --name VisorCamara --collect-all av --collect-all sounddevice --workpath "%TEMP%\vc\build" --specpath "%TEMP%\vc" --distpath "%ROOT%dist" "%ROOT%visor_camara.py" || exit /b 1
"%VENV%\Scripts\pyinstaller.exe" --noconfirm --onedir --windowed --name ServidorIphone --collect-all av --exclude-module PySide6 --exclude-module sounddevice --workpath "%TEMP%\vc\build3" --specpath "%TEMP%\vc" --distpath "%ROOT%dist" "%ROOT%servidor_iphone.py"
exit /b %ERRORLEVEL%
