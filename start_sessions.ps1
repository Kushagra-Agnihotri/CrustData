# Payables Desk: set up 3 parallel Claude Code sessions (one per branch) and open them.
# Run from the repo root:  powershell -ExecutionPolicy Bypass -File .\start_sessions.ps1
# Session folders are created next to the repo: ..\cd-live, ..\cd-ui, ..\cd-bank

$ErrorActionPreference = "Continue"
$Repo    = $PSScriptRoot                       # this repo
$Root    = Split-Path $Repo -Parent             # PROJECTS folder; worktrees go here
$Prompts = Join-Path $Repo "prompts"

function Need($cmd, $hint) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) { Write-Host "Missing '$cmd'. $hint" -ForegroundColor Red; exit 1 }
}
Need git    "Install Git for Windows."
Need npx    "Install Node.js (needed for the Impeccable skill)."
Need claude "Install Claude Code: npm install -g @anthropic-ai/claude-code"
Need python "Install Python 3.11+."

# 1. Update main (stop if there are uncommitted changes to tracked files)
git -C $Repo checkout main
$dirty = git -C $Repo status --porcelain --untracked-files=no | Where-Object { $_ -notmatch "(CHANGES_TODO|PRODUCT)\.md$" }
if ($dirty) { Write-Host "Uncommitted changes on main:`n$dirty`nCommit or stash them, then re-run." -ForegroundColor Red; exit 1 }
git -C $Repo pull

# Keep the launcher files out of git
$exclude = Join-Path $Repo ".git\info\exclude"
foreach ($pat in @("prompts/", "start_sessions.ps1")) {
    if (-not (Select-String -Path $exclude -SimpleMatch $pat -Quiet -ErrorAction SilentlyContinue)) { Add-Content $exclude $pat }
}

# 2. Commit the updated spec + product brief on main
git -C $Repo add CHANGES_TODO.md PRODUCT.md
git -C $Repo diff --cached --quiet
if ($LASTEXITCODE -ne 0) {
    git -C $Repo commit -m "Add change spec and product brief"
    git -C $Repo push
    if ($LASTEXITCODE -ne 0) { Write-Host "Push failed (no write access?). Continuing; ask Kushagra to add you as a collaborator." -ForegroundColor Yellow }
}

# 3. .env with keys (never committed; .env is git-ignored)
$EnvFile = Join-Path $Repo ".env"
if (-not (Test-Path $EnvFile)) {
    $crust  = Read-Host "Paste CRUSTDATA_API_KEY"
    $gemini = Read-Host "Paste GEMINI_API_KEY (Enter to skip)"
    $example = Join-Path $Repo ".env.example"
    $lines = @()
    if (Test-Path $example) { $lines = Get-Content $example | Where-Object { $_ -notmatch '^(CRUSTDATA_API_KEY|GEMINI_API_KEY)=' } }
    $lines += "CRUSTDATA_API_KEY=$crust"
    if ($gemini) { $lines += "GEMINI_API_KEY=$gemini" }
    Set-Content -Path $EnvFile -Value $lines -Encoding UTF8
}
$ignored = git -C $Repo check-ignore .env
if (-not $ignored) { Write-Host "WARNING: .env is NOT git-ignored. Adding it to .gitignore." -ForegroundColor Yellow; Add-Content (Join-Path $Repo ".gitignore") "`n.env" }

# 4. Credit balance before the run (write it down for the README)
$key = (Get-Content $EnvFile | Where-Object { $_ -match '^CRUSTDATA_API_KEY=' }) -replace '^CRUSTDATA_API_KEY=',''
try {
    $r = Invoke-RestMethod -Uri "https://api.crustdata.com/account/credits" -Headers @{ authorization = "Bearer $key"; "x-api-version" = "2025-11-01" }
    Write-Host "Crustdata credits now: $($r | ConvertTo-Json -Compress)" -ForegroundColor Green
} catch { Write-Host "Credits check failed: $($_.Exception.Message)" -ForegroundColor Yellow }

# 5. Worktrees: one folder + branch per session
$sessions = @(
    @{ Dir = "cd-live"; Branch = "live-data";   Prompt = "session1-live-data.md";   Title = "1 Live data" },
    @{ Dir = "cd-ui";   Branch = "new-ui";      Prompt = "session2-new-ui.md";      Title = "2 New UI" },
    @{ Dir = "cd-bank"; Branch = "bank-change"; Prompt = "session3-bank-change.md"; Title = "3 Bank change" }
)
foreach ($s in $sessions) {
    $wt = Join-Path $Root $s.Dir
    if (-not (Test-Path $wt)) {
        git -C $Repo show-ref --verify --quiet "refs/heads/$($s.Branch)"
        if ($LASTEXITCODE -eq 0) { git -C $Repo worktree add $wt $s.Branch } else { git -C $Repo worktree add $wt -b $s.Branch }
    }
    Copy-Item $EnvFile (Join-Path $wt ".env") -Force
}

# 6. Impeccable skill in the UI worktree
Push-Location (Join-Path $Root "cd-ui")
try { npx -y skills add pbakaus/impeccable --skill impeccable --agent claude-code } catch { Write-Host "Impeccable install failed; install it inside session 2." -ForegroundColor Yellow }
Pop-Location

# 7. Open one window per session, Claude Code started with its prompt
foreach ($s in $sessions) {
    $wt = Join-Path $Root $s.Dir
    $p  = Join-Path $Prompts $s.Prompt
    $cmd = "`$host.UI.RawUI.WindowTitle = '$($s.Title)'; `$env:PYTHONUTF8 = '1'; Set-Location '$wt'; claude 'Read the file $p and follow it exactly. Start now.'"
    Start-Process powershell -ArgumentList "-NoExit", "-Command", $cmd
}

Write-Host ""
Write-Host "Three Claude Code windows are open:" -ForegroundColor Green
Write-Host "  1 Live data    -> $Root\cd-live  (branch live-data, merge by 5:15)"
Write-Host "  2 New UI       -> $Root\cd-ui    (branch new-ui, decide at 5:30)"
Write-Host "  3 Bank change  -> $Root\cd-bank  (branch bank-change)"
Write-Host "Merge from $Repo :  git merge live-data   (then bank-change, then new-ui if it works)"
