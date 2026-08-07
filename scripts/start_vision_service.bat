@echo off
rem Windows launcher for the vision service - the equivalent of
rem start_vision_service.sh.
rem
rem The camera stack lives in the optional `vision` extra. Under uv, ask for it:
rem   set PYBRAVO_EXTRAS=vision && scripts\start_vision_service.bat
call "%~dp0_pybravo_launch.bat" pybravo.vision_service %*
exit /b %errorlevel%
