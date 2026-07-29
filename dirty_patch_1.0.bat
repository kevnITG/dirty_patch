@echo off
setlocal
set "SCRIPT=%~dp0dirty_patch_1.0.py"
set "PYEXE="
set "PYW="

if not exist "%SCRIPT%" goto :noscript
if not exist "%~dp0arc_toolkit.py" goto :notoolkit

py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 (
    set "PYEXE=py -3"
    set "PYW=pyw -3"
    goto :found
)

python -c "import sys" >nul 2>nul
if not errorlevel 1 (
    set "PYEXE=python"
    set "PYW=pythonw"
    goto :found
)

for %%P in (
    "%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
    "%ProgramFiles%\Python314\python.exe"
    "%ProgramFiles%\Python313\python.exe"
    "%ProgramFiles%\Python312\python.exe"
    "%ProgramFiles%\Python311\python.exe"
    "%ProgramFiles%\Python310\python.exe"
    "%ProgramFiles(x86)%\Python312\python.exe"
    "%ProgramFiles(x86)%\Python311\python.exe"
    "%SystemDrive%\Python313\python.exe"
    "%SystemDrive%\Python312\python.exe"
    "%SystemDrive%\Python311\python.exe"
) do (
    if exist "%%~P" (
        set PYEXE="%%~P"
        set PYW="%%~dpPpythonw.exe"
        goto :found
    )
)

goto :nopython

:found
%PYEXE% -c "import numpy, PIL" >nul 2>nul
if errorlevel 1 goto :nodeps

%PYW% -c "import sys" >nul 2>nul
if errorlevel 1 set "PYW=%PYEXE%"

start "" %PYW% "%SCRIPT%"
goto :end

:nodeps
echo.
echo Found Python, but it's missing the libraries this tool needs.
echo.
echo Install them with:
echo     %PYEXE% -m pip install numpy Pillow
echo.
echo ^(add py7zr as well if you want to import straight from a .7z archive^)
echo.
pause
goto :end

:noscript
echo Could not find "%SCRIPT%".
echo Make sure dirty_patch_1.0.py is in the same folder as this .bat file.
pause
goto :end

:notoolkit
echo Could not find "%~dp0arc_toolkit.py".
echo All three files ^(the .bat, dirty_patch_1.0.py and arc_toolkit.py^)
echo need to sit together in the same folder.
pause
goto :end

:nopython
echo Could not find Python.
echo.
echo This tool needs Python 3 with numpy and Pillow ^(plus py7zr for .7z sources^).
echo Install Python from https://www.python.org/downloads/ - tick
echo "Add python.exe to PATH" in the installer - then run:
echo     py -3 -m pip install numpy Pillow py7zr
echo.
pause

:end
endlocal
