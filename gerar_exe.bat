@echo off
rem Gera dist\MaquinaDeCortes.exe (um arquivo so, com Python e bibliotecas dentro).
cd /d "%~dp0"
set "VENV=%LOCALAPPDATA%\MaquinaCortes\venv"
if not exist "%VENV%\Scripts\python.exe" (
  py -3.12 -m venv "%VENV%" || python -m venv "%VENV%"
  "%VENV%\Scripts\python.exe" -m pip install -r requirements.txt
)
"%VENV%\Scripts\python.exe" -m pip install --upgrade pyinstaller
"%VENV%\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name MaquinaDeCortes ^
  --collect-data faster_whisper ^
  --collect-binaries ctranslate2 ^
  --collect-data cv2 ^
  --collect-data googleapiclient ^
  --hidden-import publicador ^
  --hidden-import aprendizado ^
  --collect-submodules yt_dlp ^
  --add-binary "C:\Windows\System32\msvcp140.dll;." ^
  --add-binary "C:\Windows\System32\vcruntime140.dll;." ^
  --add-binary "C:\Windows\System32\vcruntime140_1.dll;." ^
  --exclude-module torch --exclude-module matplotlib --exclude-module pandas ^
  --distpath dist --workpath build ^
  app.py
echo.
echo Pronto: dist\MaquinaDeCortes.exe
