@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PY=
py -3 --version >nul 2>&1 && set PY=py -3
if not defined PY python --version >nul 2>&1 && set PY=python
if not defined PY (
  echo [오류] 파이썬이 설치되어 있지 않습니다.
  echo README.md의 "1. 처음 한 번만" 부분을 따라 설치해 주세요.
  pause
  exit /b 1
)
echo 로블록스 수치 추적을 시작합니다. 이 창을 닫으면 멈춥니다.
%PY% tracker.py %*
pause
