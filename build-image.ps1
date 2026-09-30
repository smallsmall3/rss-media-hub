# ============================================================================
#  Build the Docker image and export it as a tar, for importing into
#  UGREEN NAS (Docker -> Images -> Import).
#
#  REQUIREMENTS (all must be true before this script can work):
#    * Docker Desktop (or Docker Engine) installed on THIS computer
#    * Docker daemon RUNNING (Docker Desktop started, whale icon steady)
#    * Run from the project root, where Dockerfile + requirements.txt + app\ live
#
#  Run this first to see whether your machine is ready:
#      powershell -ExecutionPolicy Bypass -File .\build-image.ps1 -CheckOnly
#
#  Then actually build:
#      powershell -ExecutionPolicy Bypass -File .\build-image.ps1
#
#  This script is intentionally ASCII-only: Windows PowerShell 5.1 mis-decodes
#  UTF-8 files without a BOM, which breaks non-English text.
#
#  Options:
#      -CheckOnly                   only report environment status, build nothing
#      -Tag rss-media-hub:1.0.0     image name:tag
#      -Out D:\rss-media-hub.tar    export path (default: .\rss-media-hub-<tag>.tar)
#      -Platform linux/amd64        force target arch (when PC and NAS differ)
#      -NoCache                     rebuild without cache
# ============================================================================

[CmdletBinding()]
param(
    [string]$Tag = "rss-media-hub:1.0.0",
    [string]$Out = "",
    [string]$Platform = "",
    [switch]$NoCache,
    [switch]$CheckOnly
)

function Write-Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }
function Write-Ok($text)   { Write-Host "    [OK]   $text" -ForegroundColor Green }
function Write-Warn($text) { Write-Host "    [WARN] $text" -ForegroundColor Yellow }
function Write-Bad($text)  { Write-Host "    [FAIL] $text" -ForegroundColor Red }

Write-Host ""
Write-Host "rss-media-hub image builder" -ForegroundColor White
Write-Host "------------------------------------------------------------"

$ready = $true

# ---------------------------------------------------------------- 1/4 files
Write-Step "1/4 Project files"
$root = $PSScriptRoot
if (-not $root) { $root = (Get-Location).Path }
$required = @("Dockerfile", "requirements.txt", "docker\entrypoint.sh", "app\main.py")
foreach ($rel in $required) {
    if (Test-Path (Join-Path $root $rel)) {
        Write-Ok $rel
    } else {
        Write-Bad "$rel  (missing)"
        $ready = $false
    }
}

# ---------------------------------------------------------------- 2/4 docker
Write-Step "2/4 Docker command"
$dockerCmd = Get-Command docker -ErrorAction SilentlyContinue
if ($dockerCmd) {
    Write-Ok "found: $($dockerCmd.Source)"
} else {
    Write-Bad "docker command not found"
    Write-Host ""
    Write-Host "    This computer has no Docker. Pick one of these:" -ForegroundColor Yellow
    Write-Host "      A) Install Docker Desktop, start it, then re-run this script:"
    Write-Host "         https://www.docker.com/products/docker-desktop/"
    Write-Host "      B) Build on the UGREEN NAS itself (no PC Docker needed):"
    Write-Host "         copy the project folder to the NAS, then in its terminal run:"
    Write-Host "           cd <project dir> && docker build -t rss-media-hub:1.0.0 ."
    Write-Host "      C) Build on any other machine that has Docker, then copy the tar."
    Write-Host ""
    $ready = $false
}

# ---------------------------------------------------------------- 3/4 daemon
Write-Step "3/4 Docker daemon"
if ($dockerCmd) {
    $serverVersion = ""
    try { $serverVersion = (docker version --format "{{.Server.Version}}" 2>&1 | Select-Object -First 1) } catch {}
    if ($serverVersion -and $LASTEXITCODE -eq 0) {
        Write-Ok "daemon running, server version $serverVersion"
        $hostArch = ""
        try { $hostArch = (docker version --format "{{.Server.Arch}}" 2>$null | Select-Object -First 1) } catch {}
        Write-Ok "docker server arch: $hostArch"
        if (-not $Platform -and $hostArch -ne "amd64") {
            Write-Warn "UGREEN NAS is usually x86_64 (amd64); consider -Platform linux/amd64"
        }
    } else {
        Write-Bad "docker is installed but the daemon is not running"
        Write-Host "    Start Docker Desktop and wait for the whale icon to stop animating." -ForegroundColor Yellow
        $ready = $false
    }
} else {
    Write-Warn "skipped (no docker command)"
}

# ---------------------------------------------------------------- 4/4 disk
Write-Step "4/4 Disk space"
$freeGb = $null
try {
    $driveName = (Get-Item $root).PSDrive.Name
    $psDrive = Get-PSDrive -Name $driveName -ErrorAction Stop
    if ($null -ne $psDrive.Free) {
        $freeGb = [math]::Round($psDrive.Free / 1GB, 1)
    }
} catch {
    $freeGb = $null
}
if ($null -ne $freeGb) {
    Write-Ok "free space on ${driveName}: ${freeGb} GB (about 2 GB needed while building)"
    if ($freeGb -lt 2) {
        Write-Warn "low disk space; the build may fail"
    }
} else {
    Write-Warn "could not read free disk space (not a blocker; just make sure you have ~2 GB)"
}

Write-Host ""
Write-Host "------------------------------------------------------------"
if ($CheckOnly) {
    if ($ready) {
        Write-Host "READY. Now run without -CheckOnly:" -ForegroundColor Green
        Write-Host "  powershell -ExecutionPolicy Bypass -File .\build-image.ps1"
    } else {
        Write-Host "NOT READY. Fix the [FAIL] items above first." -ForegroundColor Red
    }
    Write-Host ""
    if ($ready) { exit 0 } else { exit 1 }
}
if (-not $ready) {
    Write-Host "NOT READY. Fix the [FAIL] items above, or run with -CheckOnly for details." -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------- out path
if (-not $Out) {
    $safeName = ($Tag -replace "[:/\\]", "-")
    $Out = Join-Path $root "$safeName.tar"
}
$Out = [System.IO.Path]::GetFullPath($Out)

# ---------------------------------------------------------------- build
Write-Host ""
Write-Step "Building image $Tag (first build takes 2-5 minutes)"
$buildArgs = @("build", "-t", $Tag)
if ($Platform) { $buildArgs += @("--platform", $Platform) }
if ($NoCache)  { $buildArgs += "--no-cache" }
$buildArgs += $root

& docker @buildArgs
if ($LASTEXITCODE -ne 0) {
    Write-Bad "build failed - please share the error output above"
    exit 1
}
Write-Ok "build done"

# ---------------------------------------------------------------- save
Write-Step "Exporting tar: $Out"
& docker save $Tag -o $Out
if ($LASTEXITCODE -ne 0) {
    Write-Bad "export failed - check free disk space (about 200-300 MB needed)"
    exit 1
}
$sizeMb = [math]::Round((Get-Item $Out).Length / 1MB, 1)
Write-Ok "export done, file size: $sizeMb MB"

# ---------------------------------------------------------------- next steps
Write-Host ""
Write-Host "================================================================================" -ForegroundColor Cyan
Write-Host " Next: import into UGREEN NAS" -ForegroundColor Cyan
Write-Host "================================================================================" -ForegroundColor Cyan
Write-Host " 1. Copy this file to the NAS (UGREEN Files upload, or SMB share):"
Write-Host "      $Out"
Write-Host ""
Write-Host " 2. UGREEN Docker -> Images -> Local images -> Import -> pick the tar file"
Write-Host "    Wait 1-3 minutes for the import to finish."
Write-Host ""
Write-Host " 3. UGREEN Docker -> Project -> Create -> paste compose.yaml from the repo,"
Write-Host "    then set the image line to:"
Write-Host "      image: $Tag"
Write-Host "    Fill in Telegram / TMDB / Emby values and your storage folders, then deploy."
Write-Host ""
Write-Host " 4. Check the container log: 'Telegram bot: @xxx' means it works."
Write-Host "================================================================================" -ForegroundColor Cyan
Write-Host ""
