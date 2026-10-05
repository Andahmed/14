; Istakoza POS v12 - NSIS Installer
; Normally compiled automatically by build.bat (needs NSIS 3.x + the py\ folder next to this file)

Unicode true
!include "MUI2.nsh"
!include "x64.nsh"

Name "Istakoza POS v12"
OutFile "IstakozaPOS_Setup_64bit.exe"
InstallDir "$PROGRAMFILES64\Istakoza"
RequestExecutionLevel admin

!define MUI_ABORTWARNING
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"
!insertmacro MUI_LANGUAGE "Arabic"

Function .onInit
  SetRegView 64
  SetShellVarContext all
FunctionEnd

Function un.onInit
  SetRegView 64
  SetShellVarContext all
FunctionEnd

Section "Cashier" SecCashier
  SetOutPath "$INSTDIR\Cashier"
  File "server.py"
  File "pos_istakoza.html"
  File /r "py"
  FileOpen $0 "$INSTDIR\Cashier\IstakozaPOS_Cashier.bat" w
  FileWrite $0 "@echo off$\r$\n"
  FileWrite $0 "chcp 65001 >nul$\r$\n"
  FileWrite $0 "set ISTAKOZA_MODE=cashier$\r$\n"
  FileWrite $0 "set ISTAKOZA_PORT=8080$\r$\n"
  FileWrite $0 "title Istakoza POS - Cashier$\r$\n"
  FileWrite $0 'cd /d "%~dp0"$\r$\n'
  FileWrite $0 "py\python.exe server.py$\r$\n"
  FileWrite $0 "pause$\r$\n"
  FileClose $0
  CreateDirectory "$SMPROGRAMS\Istakoza"
  CreateShortcut "$DESKTOP\Istakoza Cashier.lnk" "$INSTDIR\Cashier\IstakozaPOS_Cashier.bat" "" "" 0 SW_SHOWMINIMIZED
  CreateShortcut "$SMPROGRAMS\Istakoza\Cashier.lnk" "$INSTDIR\Cashier\IstakozaPOS_Cashier.bat"
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  CreateShortcut "$SMPROGRAMS\Istakoza\Uninstall.lnk" "$INSTDIR\Uninstall.exe"
SectionEnd

Section "Backoffice" SecBackoffice
  SetOutPath "$INSTDIR\Backoffice"
  File "server.py"
  File "pos_istakoza.html"
  File /r "py"
  FileOpen $0 "$INSTDIR\Backoffice\IstakozaPOS_Backoffice.bat" w
  FileWrite $0 "@echo off$\r$\n"
  FileWrite $0 "chcp 65001 >nul$\r$\n"
  FileWrite $0 "set ISTAKOZA_MODE=backoffice$\r$\n"
  FileWrite $0 "set ISTAKOZA_PORT=8081$\r$\n"
  FileWrite $0 "title Istakoza POS - Backoffice$\r$\n"
  FileWrite $0 'cd /d "%~dp0"$\r$\n'
  FileWrite $0 "py\python.exe server.py$\r$\n"
  FileWrite $0 "pause$\r$\n"
  FileClose $0
  CreateDirectory "$SMPROGRAMS\Istakoza"
  CreateShortcut "$DESKTOP\Istakoza Backoffice.lnk" "$INSTDIR\Backoffice\IstakozaPOS_Backoffice.bat" "" "" 0 SW_SHOWMINIMIZED
  CreateShortcut "$SMPROGRAMS\Istakoza\Backoffice.lnk" "$INSTDIR\Backoffice\IstakozaPOS_Backoffice.bat"
  WriteUninstaller "$INSTDIR\Uninstall.exe"
SectionEnd

; Uninstall removes program files only - the database in C:\ProgramData\Istakoza is kept on purpose
Section "Uninstall"
  Delete "$DESKTOP\Istakoza Cashier.lnk"
  Delete "$DESKTOP\Istakoza Backoffice.lnk"
  RMDir /r "$SMPROGRAMS\Istakoza"
  RMDir /r "$INSTDIR"
SectionEnd
