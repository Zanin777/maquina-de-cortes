@echo off
cd /d "%~dp0"
set "VENV=%LOCALAPPDATA%\MaquinaCortes\venv"
if not exist "%VENV%\Scripts\pythonw.exe" (
  echo Primeira vez: instalando dependencias, aguarde...
  py -3.12 -m venv "%VENV%" || python -m venv "%VENV%"
  "%VENV%\Scripts\python.exe" -m pip install --upgrade pip
  "%VENV%\Scripts\python.exe" -m pip install -r requirements.txt
)
start "" "%VENV%\Scripts\pythonw.exe" app.py
