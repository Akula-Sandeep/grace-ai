<#
Rebuilds Grace from this folder, runs the live evaluation and copies the report to
.\eval-reports\<date-time>\. Earlier reports are never overwritten.

Run it from PowerShell (any folder):
    powershell -ExecutionPolicy Bypass -File "C:\Users\ASUS TUF\Downloads\AI-Movie-Agent\scripts\run-live-eval.ps1"

Optional: -Only G01,R01 runs just those scenarios.

Turns are paced like a person (5 s apart, and waiting while every AI model rests
after a rate limit); both are recorded in the report. A full run takes about
10-20 minutes on Groq's free tier.

Exit status of each evaluation: 0 everything passed, 1 a scenario failed,
2 a required provider check failed (results are blocked or unverified),
3 the run stopped early. The reports are saved in every case.
#>
param([string]$Only = "")

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$inContainer = "/tmp/grace-eval/$stamp"
$reportsRoot = Join-Path $root "eval-reports"
$onHost = Join-Path $reportsRoot $stamp

Write-Host "== Rebuilding and starting the stack" -ForegroundColor Cyan
docker compose up --build -d
if ($LASTEXITCODE -ne 0) { Write-Host "docker compose up failed (exit $LASTEXITCODE)." -ForegroundColor Red; exit 1 }

Write-Host "== Waiting for the backend to report healthy" -ForegroundColor Cyan
$id = docker compose ps -q backend | Select-Object -First 1
if (-not $id) { Write-Host "No backend container is running. See: docker compose logs backend" -ForegroundColor Red; exit 1 }
$deadline = (Get-Date).AddMinutes(4)
do {
    $health = (docker inspect --format "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}" $id | Out-String).Trim()
    if ($health -eq "healthy") { break }
    Start-Sleep -Seconds 3
} while ((Get-Date) -lt $deadline)
if ($health -ne "healthy") { Write-Host "The backend is '$health'. See: docker compose logs backend" -ForegroundColor Red; exit 1 }

$extra = @()
if ($Only) { $extra = @("--only", $Only) }

Write-Host "== Live evaluation" -ForegroundColor Cyan
docker compose exec -T backend python -m app.evals.live_agent --out "$inContainer/model" @extra
$modelExit = $LASTEXITCODE

Write-Host "== Report inside the container" -ForegroundColor Cyan
docker compose exec -T backend ls -la "$inContainer/model"
if ($LASTEXITCODE -ne 0) { Write-Host "A report folder is missing in the container; see the output above." -ForegroundColor Red; exit 1 }

New-Item -ItemType Directory -Force $reportsRoot | Out-Null
# Relative destination: avoids any ambiguity between a drive letter and "container:path".
docker compose cp "backend:$inContainer" "eval-reports/$stamp"
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $onHost)) { Write-Host "Copying the reports out of the container failed." -ForegroundColor Red; exit 1 }

Write-Host "== Reports on this computer" -ForegroundColor Cyan
Get-ChildItem -Recurse -File $onHost | ForEach-Object { Write-Host $_.FullName }
Write-Host ""
Write-Host "Exit status: $modelExit"
Write-Host "(0 all passed, 1 a scenario failed or was not graded, 2 a required provider check failed, 3 the run stopped early)"
exit $modelExit
