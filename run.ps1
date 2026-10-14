# 로컬 서버 실행: .\run.ps1 [-Port 8282] [-BindHost 0.0.0.0]
param([int]$Port = 8282, [string]$BindHost = "0.0.0.0")
Set-Location $PSScriptRoot
& .\.venv\Scripts\python.exe -m uvicorn app.main:app --host $BindHost --port $Port --env-file .env
