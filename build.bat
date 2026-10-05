@echo off
cd /d "%~dp0"
python -m PyInstaller --noconfirm --onefile --noconsole --name AsistenteTexto --collect-all google.genai assistant.py
if errorlevel 1 exit /b 1
if not exist release mkdir release
copy /y dist\AsistenteTexto.exe release\ >nul
copy /y config.json release\ >nul
if not exist release\.env copy /y .env release\.env >nul
echo Listo: carpeta release
