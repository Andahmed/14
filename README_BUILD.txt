Istakoza POS v12 - Build
========================
One click:  double-click  build.bat

- If the "py" folder does not exist, build.bat downloads portable Python 3.11.9
  automatically (needs internet only the first time).
- Output in dist\ :
    dist\Cashier\IstakozaPOS_Cashier.bat        (port 8080)
    dist\Backoffice\IstakozaPOS_Backoffice.bat  (port 8081)
    dist\IstakozaPOS_Cashier_v12.zip / IstakozaPOS_Backoffice_v12.zip
- If NSIS is installed (https://nsis.sourceforge.io) an installer is also built:
    dist\IstakozaPOS_Setup_64bit.exe

Data: C:\ProgramData\Istakoza\pos_data.db   (first login: admin / admin)
