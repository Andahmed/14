@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Istakoza POS - Build

echo ========================================
echo   Istakoza POS v12 - One-click Build
echo ========================================
echo.

set "PYVER=3.11.9"
set "PYURL=https://www.python.org/ftp/python/%PYVER%/python-%PYVER%-embed-amd64.zip"

if not exist server.py (
  echo [X] server.py not found next to build.bat
  goto :fail
)
if not exist pos_istakoza.html (
  echo [X] pos_istakoza.html not found next to build.bat
  goto :fail
)

REM ---- 1) Python runtime: use existing py folder or download it automatically ----
if exist "py\python.exe" goto :have_py
if exist "..\py\python.exe" (
  echo [i] Using ..\py
  xcopy /E /I /Y /Q "..\py" "py" >nul
  goto :have_py
)
echo [1/5] Downloading portable Python %PYVER% ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -UseBasicParsing '%PYURL%' -OutFile 'py_embed.zip'"
if errorlevel 1 goto :dl_fail
if not exist py_embed.zip goto :dl_fail
powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -Force 'py_embed.zip' 'py'"
if errorlevel 1 goto :dl_fail
del /q py_embed.zip >nul 2>&1
:have_py
if not exist "py\python.exe" goto :dl_fail
echo [OK] Python runtime ready: py\

REM ---- 2) clean output ----
if exist dist rmdir /s /q dist
mkdir "dist\Cashier"
mkdir "dist\Backoffice"

REM ---- 3) copy files ----
echo [2/5] Building Cashier ...
xcopy /E /I /Y /Q "py" "dist\Cashier\py" >nul
copy /Y server.py "dist\Cashier\" >nul
copy /Y pos_istakoza.html "dist\Cashier\" >nul

echo [3/5] Building Backoffice ...
xcopy /E /I /Y /Q "py" "dist\Backoffice\py" >nul
copy /Y server.py "dist\Backoffice\" >nul
copy /Y pos_istakoza.html "dist\Backoffice\" >nul

REM ---- 4) launchers ----
echo [4/5] Creating launchers ...
call :make_launcher Cashier cashier 8080
call :make_launcher Backoffice backoffice 8081

REM ---- 5) zip ----
echo [5/5] Creating ZIP files ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Compress-Archive -Path 'dist\Cashier\*' -DestinationPath 'dist\IstakozaPOS_Cashier_v12.zip' -Force; Compress-Archive -Path 'dist\Backoffice\*' -DestinationPath 'dist\IstakozaPOS_Backoffice_v12.zip' -Force"
if errorlevel 1 echo [!] ZIP step failed - the folders in dist are still ready to use

REM ---- 6) optional: NSIS installer if makensis is installed ----
set "NSIS="
where makensis >nul 2>&1 && set "NSIS=makensis"
if not defined NSIS if exist "%ProgramFiles(x86)%\NSIS\makensis.exe" set "NSIS=%ProgramFiles(x86)%\NSIS\makensis.exe"
if not defined NSIS if exist "%ProgramFiles%\NSIS\makensis.exe" set "NSIS=%ProgramFiles%\NSIS\makensis.exe"
if defined NSIS (
  echo [+] NSIS found - building installer ...
  "%NSIS%" /V1 IstakozaPOS.nsi
  if exist IstakozaPOS_Setup_64bit.exe move /Y IstakozaPOS_Setup_64bit.exe dist\ >nul
)

echo.
echo ========================================
echo   DONE
echo   dist\Cashier\IstakozaPOS_Cashier.bat
echo   dist\Backoffice\IstakozaPOS_Backoffice.bat
echo   dist\*.zip  (ready to copy to other PCs)
if defined NSIS echo   dist\IstakozaPOS_Setup_64bit.exe
echo ========================================
echo.
pause
exit /b 0

:make_launcher
REM %1=folder  %2=mode  %3=port
> "dist\%1\IstakozaPOS_%1.bat" (
  echo @echo off
  echo chcp 65001 ^>nul
  echo set ISTAKOZA_MODE=%2
  echo set ISTAKOZA_PORT=%3
  echo title Istakoza POS - %1
  echo cd /d "%%~dp0"
  echo py\python.exe server.py
  echo pause
)
exit /b 0

:dl_fail
echo.
echo [X] Could not get the Python runtime.
echo     Option A: connect to the internet and run build.bat again.
echo     Option B: download "Windows embeddable package (64-bit)" from python.org,
echo               extract it into a folder named "py" next to build.bat, then run again.
goto :fail

:fail
echo.
pause
exit /b 1
