# Invoked by the "IccuthologyDailyPredictions" Windows Scheduled Task at 6am local time.
# Runs the daily orchestrator prompt headlessly via `claude -p` in Auto Mode.

$ErrorActionPreference = "Stop"
$repoRoot = "D:\dev\iccuthology"
Set-Location $repoRoot

$logDir = Join-Path $repoRoot "tmp\daily-predictions-logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir ("run-{0}.log" -f (Get-Date -Format "yyyy-MM-dd_HHmmss"))

$prompt = Get-Content -Raw -Path (Join-Path $repoRoot "scripts\daily-predictions-prompt.md")

& claude -p $prompt `
    --model claude-sonnet-5 `
    --permission-mode auto `
    --allowedTools "Bash,Read,Write,Edit,Glob,Grep,Agent" `
    --output-format text |
    Tee-Object -FilePath $logFile

exit $LASTEXITCODE
