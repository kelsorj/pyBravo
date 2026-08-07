@echo off
setlocal
cd /d "%~dp0.."
set "PYBRAVO_PYTHON_EXE=%PYBRAVO_PYTHON%"
if not defined PYBRAVO_PYTHON_EXE set "PYBRAVO_PYTHON_EXE=python"
echo Starting PyBravo with "%PYBRAVO_PYTHON_EXE%"
"%PYBRAVO_PYTHON_EXE%" -m pybravo.web.server
