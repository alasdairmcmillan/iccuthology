# Invoked by the "IccuthologyDailyPredictions" Windows Scheduled Task at 6am local time.
# Runs the daily orchestrator prompt headlessly via `claude -p` in Auto Mode.

$ErrorActionPreference = "Stop"
$repoRoot = "D:\dev\iccuthology"
Set-Location $repoRoot

$logDir = Join-Path $repoRoot "tmp\daily-predictions-logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir ("run-{0}.log" -f (Get-Date -Format "yyyy-MM-dd_HHmmss"))

# The prompt goes in via stdin: PS 5.1 does not escape embedded double quotes
# when passing a string argument to a native exe, so `& claude -p $prompt`
# delivered the prompt truncated at the first `"` in the file (bit the
# 2026-07-21 and 2026-07-22 runs, which received only part of STEP 1).
$OutputEncoding = [Text.Encoding]::UTF8
Get-Content -Raw -Path (Join-Path $repoRoot "scripts\daily-predictions-prompt.md") |
    & claude -p `
    --model claude-sonnet-5 `
    --permission-mode auto `
    --allowedTools "Bash,Read,Write,Edit,Glob,Grep,Agent" `
    --output-format text |
    Tee-Object -FilePath $logFile

exit $LASTEXITCODE
