# Invoked by the "IccuthologyDailyPredictions" Windows Scheduled Task at 6am local time.
# Runs the daily orchestrator prompt headlessly via `claude -p` in Auto Mode.

$ErrorActionPreference = "Stop"
$repoRoot = "D:\dev\iccuthology"
Set-Location $repoRoot

$logDir = Join-Path $repoRoot "tmp\daily-predictions-logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir ("run-{0}.log" -f (Get-Date -Format "yyyy-MM-dd_HHmmss"))

# Health markers. A failed run is otherwise invisible until someone notices
# nothing new landed in R2 -- the 2026-07-27 and 07-28 runs both died on an
# expired OAuth session and went unnoticed for two days.
#   $statusFile: always rewritten, machine-greppable, records the last outcome.
#   $alertFile:  only exists while the last run is BROKEN (deleted on success),
#                dropped on the Desktop so a failure is impossible to miss.
$statusFile = Join-Path $logDir "LAST-RUN-STATUS.txt"
$alertFile = Join-Path ([Environment]::GetFolderPath('Desktop')) "ICCUTHOLOGY-PREDICTIONS-FAILED.txt"

function Write-RunStatus {
    param([string]$State, [string[]]$Reasons, [string[]]$Warnings)

    $lines = @(
        "state     : $State",
        "run       : $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')",
        "log       : $logFile"
    )
    foreach ($r in $Reasons) { $lines += "reason    : $r" }
    foreach ($w in $Warnings) { $lines += "warning   : $w" }
    $lines | Out-File -FilePath $statusFile -Encoding utf8

    if ($State -eq 'OK') {
        # Self-clearing: a recovered run removes yesterday's Desktop alert.
        if (Test-Path $alertFile) { Remove-Item $alertFile -Force }
    }
    else {
        $lines | Out-File -FilePath $alertFile -Encoding utf8
    }
}

# Pre-flight: the access token renews every ~8h against the refresh token, so
# the run only breaks when the *refresh* token lapses (~monthly). Check it
# locally -- free, and it warns before the failure rather than after.
$warnings = @()
try {
    $credPath = Join-Path $env:USERPROFILE ".claude\.credentials.json"
    if (-not (Test-Path $credPath)) {
        $warnings += "no .credentials.json found -- ``claude`` is probably not logged in"
    }
    else {
        $oauth = (Get-Content $credPath -Raw | ConvertFrom-Json).claudeAiOauth
        if (-not $oauth.refreshToken) {
            $warnings += "credentials have no refreshToken -- re-run ``claude`` then /login"
        }
        elseif ($oauth.refreshTokenExpiresAt) {
            $refreshExpiry = [DateTimeOffset]::FromUnixTimeMilliseconds([int64]$oauth.refreshTokenExpiresAt).LocalDateTime
            $daysLeft = [math]::Round(($refreshExpiry - (Get-Date)).TotalDays, 1)
            if ($daysLeft -lt 0) {
                $warnings += "refresh token EXPIRED $refreshExpiry -- re-run ``claude`` then /login"
            }
            elseif ($daysLeft -lt 3) {
                $warnings += "refresh token expires $refreshExpiry ($daysLeft days) -- re-login soon"
            }
        }
    }
}
catch {
    $warnings += "could not read credentials: $($_.Exception.Message)"
}
foreach ($w in $warnings) { Write-Warning $w }

# The prompt goes in via stdin: PS 5.1 does not escape embedded double quotes
# when passing a string argument to a native exe, so `& claude -p $prompt`
# delivered the prompt truncated at the first `"` in the file (bit the
# 2026-07-21 and 2026-07-22 runs, which received only part of STEP 1).
$OutputEncoding = [Text.Encoding]::UTF8
$exitCode = 1
try {
    Get-Content -Raw -Path (Join-Path $repoRoot "scripts\daily-predictions-prompt.md") |
        & claude -p `
        --model claude-sonnet-5 `
        --permission-mode auto `
        --allowedTools "Bash,Read,Write,Edit,Glob,Grep,Agent" `
        --output-format text |
        Tee-Object -FilePath $logFile
    $exitCode = $LASTEXITCODE
}
catch {
    Write-RunStatus -State 'FAILED' -Reasons @("run threw: $($_.Exception.Message)") -Warnings $warnings
    exit 1
}

# Exit 0 is not proof the orchestration happened -- on 2026-07-22 the task
# exited clean having delivered a truncated prompt. Check the output too.
$output = if (Test-Path $logFile) { (Get-Content -Raw -Path $logFile) } else { "" }
if ($null -eq $output) { $output = "" }
$output = $output.Trim()

$reasons = @()
if ($exitCode -ne 0) {
    $reasons += "claude exited with code $exitCode"
}
if ($output -match 'Failed to authenticate|OAuth session expired|Invalid API key|Please run /login|Credit balance is too low') {
    $reasons += "authentication/billing failure -- re-run ``claude`` then /login"
}
# A real run reports back at length; the auth-failure runs logged 72 chars.
# Kept well under the shortest genuine run seen (527 chars, a no-op day) so a
# terse "nothing to do" report cannot trip a false alarm.
if ($output.Length -lt 300) {
    $reasons += "output only $($output.Length) chars -- orchestration almost certainly did not run"
}

if ($reasons.Count -gt 0) {
    Write-RunStatus -State 'FAILED' -Reasons $reasons -Warnings $warnings
    foreach ($r in $reasons) { Write-Warning $r }
    if ($exitCode -eq 0) { $exitCode = 1 }  # never report a silent failure as success
}
else {
    Write-RunStatus -State 'OK' -Reasons @() -Warnings $warnings
}

exit $exitCode
