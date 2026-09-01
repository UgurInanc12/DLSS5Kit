@echo off
rem Build dlss5kit.addon64 with the VS2019 Build Tools compiler.
setlocal
call "C:\Program Files (x86)\Microsoft Visual Studio\2019\BuildTools\VC\Auxiliary\Build\vcvars64.bat" >nul || exit /b 1
cd /d "%~dp0"
cl /nologo /std:c++17 /O2 /W4 /EHsc /LD /DNDEBUG ^
   dlss5kit_addon.cpp ^
   /link /OUT:dlss5kit.addon64 user32.lib
if errorlevel 1 exit /b 1
echo built dlss5kit.addon64
