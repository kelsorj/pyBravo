@echo off
rem Windows launcher for the pyBravo web server - the equivalent of
rem start_pybravo.sh. Serves the UI on http://localhost:8000.
call "%~dp0_pybravo_launch.bat" pybravo.web.server %*
exit /b %errorlevel%
