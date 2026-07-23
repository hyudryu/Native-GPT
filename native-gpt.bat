@echo off
setlocal EnableExtensions
rem ============================================================
rem  Native GPT - one-click launcher (Windows)
rem
rem  Usage:
rem    native-gpt.bat                start the desktop app window
rem    native-gpt.bat --headless     server only (phone / browser)
rem    native-gpt.bat --port 8790    custom port (default 8787)
rem
rem  Environment:
rem    AGENTGPT_TOKEN   bearer token for LAN/Tailscale clients
rem                     (defaults to agentgpt-local-dev)
rem ============================================================
cd /d "%~dp0"

set "MODE="
set "PORT=8787"

:parse
if "%~1"=="" goto done_parse
if "%~1"=="--headless" (set "MODE=--headless" & shift & goto parse)
if "%~1"=="--port" (set "PORT=%~2" & shift & shift & goto parse)
echo Unknown argument: %~1
goto usage

:done_parse
if "%AGENTGPT_TOKEN%"=="" set "AGENTGPT_TOKEN=agentgpt-local-dev"

rem ---- prerequisite checks ------------------------------------
where cargo >nul 2>nul || goto missing_cargo
where pnpm >nul 2>nul || goto missing_pnpm
where uv >nul 2>nul || goto missing_uv

rem ---- first-run setup ----------------------------------------
if not exist "node_modules" (
    echo Installing JS dependencies...
    call pnpm install || goto fail
)
if not exist "apps\agent-runtime\.venv" (
    echo Installing the Python agent runtime...
    uv sync --directory apps/agent-runtime || goto fail
)

rem ---- build the UI every launch ------------------------------
rem The Tauri host serves apps/ui/dist statically, so source changes
rem don't appear until the bundle is rebuilt. Rebuild on every run to
rem avoid serving a stale UI.
echo Building the UI...
call pnpm --filter @agentgpt/ui build || goto fail

rem ---- launch --------------------------------------------------
echo.
echo   Native GPT
echo   Local:      http://127.0.0.1:%PORT%
echo   Phone pair: http://127.0.0.1:%PORT%/api/pair
echo.
cargo run -p agentgpt-host -- %MODE% --port %PORT%
exit /b %ERRORLEVEL%

:usage
echo.
echo   native-gpt.bat [--headless] [--port N]
echo.
pause
exit /b 2

:missing_cargo
echo ERROR: Rust cargo was not found in PATH.
echo Install Rust via https://rustup.rs/ and try again.
goto fail

:missing_pnpm
echo ERROR: pnpm was not found in PATH.
echo Run: npm install -g pnpm
goto fail

:missing_uv
echo ERROR: uv was not found in PATH.
echo See https://docs.astral.sh/uv/getting-started/installation/
goto fail

:fail
echo.
echo Launch failed - see the messages above.
pause
exit /b 1
