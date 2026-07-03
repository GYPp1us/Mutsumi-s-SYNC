$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$rendererDir = Join-Path $repoRoot "tools\markdown-renderer"

if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    throw "Node.js was not found in PATH. Install Node.js 20+ first."
}
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    throw "npm was not found in PATH. Install Node.js with npm first."
}

Push-Location $rendererDir
try {
    npm install
    npx playwright install chromium
    npm run check
}
finally {
    Pop-Location
}

Write-Host ""
Write-Host "Markdown renderer installed."
Write-Host "Enable it in config.yaml:"
Write-Host "render:"
Write-Host "  markdown_image:"
Write-Host "    enabled: true"
