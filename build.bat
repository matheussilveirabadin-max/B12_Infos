@echo off
REM Gera dist\AvisoSonoro\AvisoSonoro.exe  (requer Python 3.10-3.12 de 64 bits instalado)
setlocal
cd /d "%~dp0"

python -m venv .venv || goto :erro
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt pyinstaller==6.22.3 || goto :erro

pyinstaller --noconfirm --clean --onefile --windowed ^
  --name AvisoSonoro ^
  --hidden-import pystray._win32 ^
  --collect-submodules pycaw ^
  aviso_sonoro.py || goto :erro

REM Monta a pasta pronta para distribuir
if exist dist\AvisoSonoro rmdir /s /q dist\AvisoSonoro
mkdir dist\AvisoSonoro\audios
move /y dist\AvisoSonoro.exe dist\AvisoSonoro\ >nul
copy /y config.json dist\AvisoSonoro\ >nul
copy /y audios\*.* dist\AvisoSonoro\audios\ >nul 2>&1
copy /y LEIA-ME.md dist\AvisoSonoro\ >nul
copy /y "COMO USAR.txt" dist\AvisoSonoro\ >nul

echo.
echo Pronto: dist\AvisoSonoro\AvisoSonoro.exe
exit /b 0

:erro
echo.
echo Falha no build. Veja as mensagens acima.
exit /b 1
