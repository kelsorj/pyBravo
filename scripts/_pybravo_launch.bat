@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem Shared Windows launcher used by start_pybravo.bat and
rem start_vision_service.bat. The counterpart of _pybravo_launch.sh.
rem Usage: _pybravo_launch.bat <module> [args...]
rem
rem Interpreter selection, in order:
rem   1. %PYBRAVO_PYTHON%          - explicit override, e.g. a conda python
rem   2. uv                        - resolves Python 3.11+ and installs from uv.lock
rem   3. .venv\Scripts\python.exe  - a virtualenv in the repo
rem   4. py -3.13 / -3.12 / -3.11  - the Windows Python launcher
rem   5. python on PATH            - only if it is actually 3.11 or newer
rem
rem The uv path re-syncs .venv against uv.lock on every launch, so a git pull
rem that adds a dependency cannot leave you with a stale environment. It also
rem means optional extras get pruned unless you ask for them:
rem   set PYBRAVO_EXTRAS=llm && scripts\start_pybravo.bat
rem
rem -B keeps Python from writing .pyc files, so stale bytecode cannot survive a
rem code change.

rem Capture the script's own directory BEFORE any shift. `shift` renumbers %0
rem as well as %1..%9, so after shifting, %~dp0 no longer refers to this file -
rem it refers to the consumed argument, and "cd /d %~dp0.." silently lands
rem somewhere else. That put uv outside the project, which surfaced only as
rem "No module named 'pybravo'" from an unrelated interpreter.
set "SCRIPT_DIR=%~dp0"

set "MODULE=%~1"
if not defined MODULE goto usage
shift

rem %* ignores shift, so collect the remaining arguments by hand. %1 rather
rem than %~1 keeps the caller's quoting intact.
set "ARGS="
:collect_args
if "%~1"=="" goto args_done
set "ARGS=!ARGS! %1"
shift
goto collect_args
:args_done

cd /d "%SCRIPT_DIR%.." || (
    echo Could not change to the repository root from "%SCRIPT_DIR%" 1>&2
    exit /b 1
)

if defined PYBRAVO_PYTHON goto use_override

where uv >nul 2>&1
if not errorlevel 1 goto run_uv

if exist ".venv\Scripts\python.exe" goto use_venv

where py >nul 2>&1
if errorlevel 1 goto try_bare_python
for %%v in (3.13 3.12 3.11) do (
    py -%%v -c "import sys" >nul 2>&1
    if not errorlevel 1 (
        set "PYLAUNCH=-%%v"
        goto run_pylauncher
    )
)

:try_bare_python
where python >nul 2>&1
if errorlevel 1 goto no_python
rem Do not trust a bare "python" to be new enough. On a stock Windows box it is
rem often a 3.9 from the Store, and pybravo needs 3.11 (enum.StrEnum, PEP 604
rem unions in signatures). Failing here with a clear message beats a TypeError
rem thrown from inside a class body.
python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if errorlevel 1 goto old_python
set "PY=python"
goto run_direct

:use_override
set "PY=%PYBRAVO_PYTHON%"
goto run_direct

:use_venv
set "PY=.venv\Scripts\python.exe"
goto run_direct

:run_uv
set "EXTRAS="
for %%e in (%PYBRAVO_EXTRAS%) do set "EXTRAS=!EXTRAS! --extra %%e"
rem Sync explicitly, then invoke the environment's interpreter by path. This is
rem more verbose than `uv run python -m ...` but it fails loudly: if the project
rem cannot be found, `uv sync` says so, whereas `uv run` falls back to an
rem ambient interpreter and the only symptom is a puzzling ModuleNotFoundError.
echo Syncing dependencies with uv...
uv sync --frozen!EXTRAS!
if errorlevel 1 (
    echo. 1>&2
    echo uv sync failed. If the lockfile is out of date, run: uv lock 1>&2
    exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
    echo. 1>&2
    echo uv sync did not produce .venv\Scripts\python.exe 1>&2
    echo Set UV_PROJECT_ENVIRONMENT if your environment lives elsewhere. 1>&2
    exit /b 1
)
echo Starting %MODULE% with: .venv\Scripts\python.exe -B -m %MODULE%
".venv\Scripts\python.exe" -B -m %MODULE%!ARGS!
exit /b !errorlevel!

:run_pylauncher
echo Starting %MODULE% with: py !PYLAUNCH! -B -m %MODULE%
py !PYLAUNCH! -B -m %MODULE%!ARGS!
exit /b !errorlevel!

:run_direct
echo Starting %MODULE% with: "!PY!" -B -m %MODULE%
"!PY!" -B -m %MODULE%!ARGS!
exit /b !errorlevel!

:usage
echo Usage: %~nx0 ^<module^> [args...] 1>&2
exit /b 2

:old_python
echo. 1>&2
echo The "python" on your PATH is older than 3.11. pyBravo needs 3.11 or newer. 1>&2
goto install_help

:no_python
echo. 1>&2
echo No suitable Python found. pyBravo needs Python 3.11 or newer. 1>&2

:install_help
echo. 1>&2
echo Easiest fix - install uv, which handles the interpreter and dependencies: 1>&2
echo   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 ^| iex" 1>&2
echo. 1>&2
echo Or install Python 3.12 and create a virtualenv in the repo: 1>&2
echo   winget install Python.Python.3.12 1>&2
echo   py -3.12 -m venv .venv 1>&2
echo   .venv\Scripts\python.exe -m pip install -e . 1>&2
echo. 1>&2
echo Or point the launcher at an interpreter you already have: 1>&2
echo   set PYBRAVO_PYTHON=C:\Path\To\python.exe ^&^& scripts\start_pybravo.bat 1>&2
exit /b 1
