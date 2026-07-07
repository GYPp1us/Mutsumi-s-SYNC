<#
.SYNOPSIS
Run the Mutsumi's SYNC release workflow from a local Windows checkout.

.DESCRIPTION
This script automates the current "upstream and production" path:
local tests, optional commit, push, optional GitHub PR creation/merge, tarball
packaging, SCP upload, server-side tests, service restart, and log verification.

Default production paths:

  /opt/mutsumi-sync-v3/releases
  /opt/mutsumi-sync-v3/shared/config.yaml
  /opt/mutsumi-sync-v3/shared/data
  journalctl -u mutsumi-sync-v3.service --since -2min --no-pager -l

Start with:

  .\scripts\release_to_production.ps1 -DryRun

Typical feature branch release:

  .\scripts\release_to_production.ps1 -CommitMessage "feat: add release helper"

Push only, no production deploy:

  .\scripts\release_to_production.ps1 -SkipDeploy
#>

[CmdletBinding()]
param(
    [string]$RemoteName = "origin",
    [string]$BaseBranch = "main",
    [string]$Remote = "root@arcol.site",
    [string]$DeployRoot = "/opt/mutsumi-sync-v3",
    [string]$ServiceName = "mutsumi-sync-v3.service",
    [string]$CommitMessage = "",
    [string]$PrTitle = "",
    [string]$PrBody = "",
    [switch]$DryRun,
    [switch]$AllowDirty,
    [switch]$SkipLocalTests,
    [switch]$SkipRendererCheck,
    [switch]$SkipPr,
    [switch]$NoMerge,
    [switch]$SkipDeploy,
    [switch]$SkipServerTests,
    [switch]$SkipServerRendererCheck
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Invoke-Checked {
    param(
        [string]$Display,
        [scriptblock]$Command
    )

    Write-Step $Display
    if ($DryRun) {
        Write-Host "[DryRun] $Display" -ForegroundColor Yellow
        return
    }

    $oldErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = & $Command 2>&1
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $oldErrorActionPreference
    }

    foreach ($line in @($output)) {
        Write-Host $line
    }

    if ($exitCode -ne 0) {
        throw "Command failed with exit code ${exitCode}: $Display"
    }
}

function Require-Command {
    param([string]$Name)

    if ($DryRun -and $Name -ne "git") {
        Write-Host "[DryRun] Skipping command availability check: $Name" -ForegroundColor Yellow
        return
    }

    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command not found on PATH: $Name"
    }
}

function Get-CurrentBranch {
    $branch = (& git branch --show-current).Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($branch)) {
        throw "Unable to determine current git branch."
    }
    return $branch
}

function Get-GitStatus {
    # Keep this direct command visible: git status --porcelain
    $status = & git status --porcelain
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to read git status."
    }
    return @($status)
}

function Get-LineCount {
    param([AllowNull()]$Lines)

    if ($null -eq $Lines) {
        return 0
    }
    return @($Lines).Length
}

function Assert-CleanOrCommittable {
    param([string[]]$StatusLines)

    if ((Get-LineCount $StatusLines) -eq 0) {
        return
    }

    if (-not [string]::IsNullOrWhiteSpace($CommitMessage)) {
        Write-Host "Working tree has changes; -CommitMessage is set, so the script will commit them after local checks." -ForegroundColor Yellow
        return
    }

    if ($AllowDirty) {
        Write-Host "Working tree is dirty and -AllowDirty is set. The deploy archive still uses committed HEAD only." -ForegroundColor Yellow
        return
    }

    $joined = $StatusLines -join [Environment]::NewLine
    throw "Working tree is dirty. Commit first, pass -CommitMessage, or pass -AllowDirty for push-only checks.`n$joined"
}

function Invoke-LocalChecks {
    if (-not $SkipLocalTests) {
        Invoke-Checked "Run local pytest: python -m pytest tests/ -q" {
            $env:PYTHONPATH = "."
            python -m pytest tests/ -q
        }
    }

    if (-not $SkipRendererCheck) {
        Invoke-Checked "Run Markdown renderer check: npm run check" {
            Push-Location "tools/markdown-renderer"
            try {
                npm run check
            }
            finally {
                Pop-Location
            }
        }
    }
}

function Invoke-OptionalCommit {
    param([string[]]$InitialStatus)

    if ([string]::IsNullOrWhiteSpace($CommitMessage)) {
        return
    }

    if ((Get-LineCount $InitialStatus) -eq 0) {
        Write-Host "No working tree changes to commit." -ForegroundColor Yellow
        return
    }

    Invoke-Checked "Stage changes for release commit: git add -A" {
        git add -A
    }
    Invoke-Checked "Create release commit: git commit -m `"$CommitMessage`"" {
        git commit -m $CommitMessage
    }
}

function Invoke-GitHubFlow {
    param([string]$CurrentBranch)

    Invoke-Checked "Fetch remote refs: git fetch $RemoteName" {
        git fetch $RemoteName
    }

    Invoke-Checked "Push branch: git push -u $RemoteName $CurrentBranch" {
        git push -u $RemoteName $CurrentBranch
    }

    if ($CurrentBranch -eq $BaseBranch) {
        Invoke-Checked "Fast-forward local $BaseBranch from remote: git pull --ff-only $RemoteName $BaseBranch" {
            git pull --ff-only $RemoteName $BaseBranch
        }
        return $BaseBranch
    }

    if ($SkipPr) {
        if (-not $SkipDeploy) {
            throw "Refusing to deploy a non-$BaseBranch branch when -SkipPr is set. Use -SkipDeploy or let the script create/merge the PR."
        }
        return $CurrentBranch
    }

    if ([string]::IsNullOrWhiteSpace($PrTitle)) {
        $PrTitle = "Release $CurrentBranch"
    }
    if ([string]::IsNullOrWhiteSpace($PrBody)) {
        $PrBody = "Automated release PR created by scripts/release_to_production.ps1."
    }

    Write-Step "Create or reuse GitHub PR"
    if ($DryRun) {
        Write-Host "[DryRun] gh pr list --head $CurrentBranch --state open --json url --jq .[0].url" -ForegroundColor Yellow
        Write-Host "[DryRun] gh pr create --base $BaseBranch --head $CurrentBranch --title `"$PrTitle`" --body `"$PrBody`"" -ForegroundColor Yellow
    }
    else {
        $prUrl = (& gh pr list --head $CurrentBranch --state open --json url --jq ".[0].url")
        if ($LASTEXITCODE -ne 0) {
            throw "gh pr list failed."
        }
        $prUrl = (@($prUrl) | Select-Object -First 1)
        if (-not [string]::IsNullOrWhiteSpace($prUrl) -and $prUrl -ne "null") {
            Write-Host "Using existing PR: $prUrl"
        }
        else {
            $oldErrorActionPreference = $ErrorActionPreference
            try {
                $ErrorActionPreference = "Continue"
                $createdPrOutput = (& gh pr create --base $BaseBranch --head $CurrentBranch --title $PrTitle --body $PrBody 2>&1)
                $createExitCode = $LASTEXITCODE
            }
            finally {
                $ErrorActionPreference = $oldErrorActionPreference
            }
            foreach ($line in @($createdPrOutput)) {
                Write-Host $line
            }
            if ($createExitCode -ne 0) {
                throw "gh pr create failed."
            }
        }
    }

    if ($NoMerge) {
        if (-not $SkipDeploy) {
            throw "Refusing to deploy before merging the PR. Use -SkipDeploy with -NoMerge."
        }
        return $CurrentBranch
    }

    Invoke-Checked "Merge PR: gh pr merge $CurrentBranch --merge --delete-branch" {
        gh pr merge $CurrentBranch --merge --delete-branch
    }
    Invoke-Checked "Switch to base branch: git switch $BaseBranch" {
        git switch $BaseBranch
    }
    Invoke-Checked "Update base branch: git pull --ff-only $RemoteName $BaseBranch" {
        git pull --ff-only $RemoteName $BaseBranch
    }

    return $BaseBranch
}

function New-RemoteDeployScript {
    param(
        [string]$Path,
        [bool]$RunServerTests,
        [bool]$RunServerRendererCheck
    )

    $serverTests = if ($RunServerTests) {
        @'
cd "$release_dir"
PYTHONPATH=. "$deploy_root/venv/bin/python" -m pytest tests/ -q
'@
    }
    else {
        'echo "[skip] server pytest disabled by caller"'
    }

    $serverRendererCheck = if ($RunServerRendererCheck) {
        @'
if [ -d "$release_dir/tools/markdown-renderer" ] && [ -f "$release_dir/tools/markdown-renderer/package.json" ]; then
  cd "$release_dir/tools/markdown-renderer"
  if [ -d node_modules ]; then
    npm run check
  else
    echo "[warn] renderer node_modules missing; skip npm run check"
  fi
fi
'@
    }
    else {
        'echo "[skip] server renderer check disabled by caller"'
    }

    $body = @"
#!/usr/bin/env bash
set -euo pipefail

release_name="`$1"
deploy_root="`$2"
service_name="`$3"
archive="/tmp/mutsumi-release.tar"
release_dir="`$deploy_root/releases/`$release_name"

mkdir -p "`$deploy_root/releases"
rm -rf "`$release_dir"
mkdir -p "`$release_dir"
tar -xf "`$archive" -C "`$release_dir"

ln -sfn "`$deploy_root/shared/config.yaml" "`$release_dir/config.yaml"
rm -rf "`$release_dir/data"
ln -sfn "`$deploy_root/shared/data" "`$release_dir/data"

if [ -d "`$deploy_root/current/tools/markdown-renderer/node_modules" ] && [ -d "`$release_dir/tools/markdown-renderer" ]; then
  cp -a "`$deploy_root/current/tools/markdown-renderer/node_modules" "`$release_dir/tools/markdown-renderer/node_modules"
fi

chown -R root:root "`$release_dir"

$serverTests

$serverRendererCheck

cd "`$release_dir"
ln -sfn "`$release_dir" "`$deploy_root/current"
systemctl restart "`$service_name"
systemctl status "`$service_name" --no-pager -l
journalctl -u "`$service_name" --since -2min --no-pager -l
readlink -f "`$deploy_root/current"

"`$deploy_root/venv/bin/python" - <<'PY'
from pathlib import Path
import yaml

cfg = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8")) or {}
stream_path = (((cfg.get("logging") or {}).get("stream_store") or {}).get("path"))
print(f"logging.stream_store.path={stream_path}")
if stream_path:
    path = Path(stream_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    print(f"stream_log_exists={path.exists()} path={path}")
PY
"@

    $lfBody = $body -replace "`r`n", "`n"
    [System.IO.File]::WriteAllText($Path, $lfBody, [System.Text.UTF8Encoding]::new($false))
}

function Invoke-ProductionDeploy {
    $shortSha = (& git rev-parse --short HEAD).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to read current git SHA."
    }
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $releaseName = "$stamp-$shortSha"
    $archivePath = Join-Path ([System.IO.Path]::GetTempPath()) "mutsumi-release-$releaseName.tar"
    $remoteScriptPath = "/tmp/mutsumi-deploy-$releaseName.sh"
    $localScriptPath = Join-Path ([System.IO.Path]::GetTempPath()) "mutsumi-deploy-$releaseName.sh"

    Invoke-Checked "Create deploy archive: git archive --format=tar -o $archivePath HEAD" {
        git archive --format=tar -o $archivePath HEAD
    }
    Invoke-Checked "Upload archive with scp to $Remote" {
        scp $archivePath "${Remote}:/tmp/mutsumi-release.tar"
    }

    if (-not $DryRun) {
        New-RemoteDeployScript `
            -Path $localScriptPath `
            -RunServerTests:(-not $SkipServerTests) `
            -RunServerRendererCheck:(-not $SkipServerRendererCheck)
    }

    Invoke-Checked "Upload remote deploy script with scp" {
        scp $localScriptPath "${Remote}:$remoteScriptPath"
    }
    Invoke-Checked "Execute remote deploy on $Remote for $ServiceName" {
        ssh $Remote "bash '$remoteScriptPath' '$releaseName' '$DeployRoot' '$ServiceName'"
    }

    Write-Host "Release name: $releaseName"
}

Write-Step "Resolve repository root"
Require-Command "git"
Require-Command "python"
if (-not $SkipRendererCheck) { Require-Command "npm" }
if (-not $SkipPr) { Require-Command "gh" }
if (-not $SkipDeploy) {
    Require-Command "scp"
    Require-Command "ssh"
}

$repoRoot = (& git rev-parse --show-toplevel).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($repoRoot)) {
    throw "This script must be run inside a git checkout."
}
Set-Location $repoRoot
Write-Host "Repository: $repoRoot"

$initialStatus = Get-GitStatus
Assert-CleanOrCommittable -StatusLines $initialStatus

Invoke-LocalChecks
Invoke-OptionalCommit -InitialStatus $initialStatus

$postCommitStatus = Get-GitStatus
if ((Get-LineCount $postCommitStatus) -gt 0 -and -not $AllowDirty) {
    if ($DryRun -and -not [string]::IsNullOrWhiteSpace($CommitMessage)) {
        Write-Host "Working tree remains dirty because dry-run did not create the requested commit." -ForegroundColor Yellow
    }
    else {
        $joined = $postCommitStatus -join [Environment]::NewLine
        throw "Working tree is still dirty after optional commit. Use -AllowDirty only for non-deploy checks.`n$joined"
    }
}

$currentBranch = Get-CurrentBranch
$releaseBranch = Invoke-GitHubFlow -CurrentBranch $currentBranch

if ($SkipDeploy) {
    Write-Step "Skip production deploy"
    Write-Host "Release branch pushed: $releaseBranch"
    exit 0
}

if ($releaseBranch -ne $BaseBranch) {
    throw "Production deploy requires $BaseBranch, got $releaseBranch."
}

Invoke-ProductionDeploy
