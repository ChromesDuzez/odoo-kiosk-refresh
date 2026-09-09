<#
.SYNOPSIS
    Reports the state of the display agent on this machine. Changes nothing.

.DESCRIPTION
    Run this on the display laptop when the agent will not start, or nothing
    appears at logon. It answers, in order, the questions worth asking:

      is Python findable  ->  is the config sane  ->  is the startup entry
      there and pointing at the right thing  ->  is the agent running  ->  is
      anything holding the port  ->  is the firewall open  ->  what does the
      log say

    Read-only: it starts nothing, stops nothing, and needs no Administrator
    rights (without them it cannot see the firewall rule and says so).

.EXAMPLE
    .\check-setup.ps1
#>

[CmdletBinding()]
param()

# Deliberately NOT 'Stop': a failing probe should print and move on, so one
# missing piece cannot hide the state of everything after it.
$ErrorActionPreference = 'Continue'

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $Root 'lib-elevate.ps1')

$Agent = Join-Path $Root 'display_agent.py'
$Config = Join-Path $Root 'agent.config.json'
$LogPath = Join-Path $Root 'agent.log'
$StartupDir = [Environment]::GetFolderPath('Startup')
$ShortcutPath = Join-Path $StartupDir 'Odoo Customer Display Agent.lnk'
$FirewallRuleName = 'Odoo Customer Display Agent'

$problems = [System.Collections.Generic.List[string]]::new()

function Say-Ok($t) { Write-Host "  [ ok ] $t" -ForegroundColor Green }
function Say-No($t, $fix) {
    Write-Host "  [ !! ] $t" -ForegroundColor Red
    $script:problems.Add("$t`n         -> $fix")
}
function Say-Hm($t) { Write-Host "  [ ?? ] $t" -ForegroundColor Yellow }
function Say-Info($t) { Write-Host "         $t" -ForegroundColor DarkGray }

Write-Host "`nChecking: $Root" -ForegroundColor Cyan
Write-Host ("=" * 62)

# ------------------------------------------------------------------- python
Write-Host "`nPython" -ForegroundColor Cyan
$PythonW = $null
$PythonExe = $null
if (Get-Command py.exe -ErrorAction SilentlyContinue) {
    $PythonExe = (& py.exe -3 -c "import sys; print(sys.executable)" 2>$null)
    if ($PythonExe) {
        $candidate = Join-Path (Split-Path -Parent $PythonExe) 'pythonw.exe'
        if (Test-Path $candidate) { $PythonW = $candidate }
    }
}
if (-not $PythonExe) {
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd) {
        $PythonExe = $cmd.Source
        $candidate = Join-Path (Split-Path -Parent $PythonExe) 'pythonw.exe'
        if (Test-Path $candidate) { $PythonW = $candidate }
    }
}
if ($PythonExe) { Say-Ok "python: $PythonExe" } else { Say-No 'Python not found on PATH' 'Install Python, or add it to PATH for this account.' }
if ($PythonW) { Say-Ok "pythonw: $PythonW" } else { Say-No 'pythonw.exe not found' 'The startup entry needs it; reinstall Python with the launcher.' }

# ------------------------------------------------------------------- config
Write-Host "`nConfig" -ForegroundColor Cyan
$parsed = $null
$Port = 8765
if (-not (Test-Path $Agent)) {
    Say-No 'display_agent.py is missing' "Copy the whole folder over again."
}
if (-not (Test-Path $Config)) {
    Say-No 'agent.config.json is missing' 'Run "Install Agent.cmd" -- it writes one now.'
}
else {
    try {
        $parsed = Get-Content $Config -Raw | ConvertFrom-Json
        Say-Ok 'agent.config.json parses'
        if ($parsed.listen_port) { $Port = [int]$parsed.listen_port }

        if ($parsed.shared_secret) { Say-Ok "pairing code present ($($parsed.shared_secret.Length) chars)" }
        else { Say-No 'no pairing code' 'Delete agent.config.json and re-run the installer.' }

        if ($parsed.paired) { Say-Ok 'paired with a POS computer' }
        else { Say-Hm 'not paired yet -- the display will show the pairing code' }

        if ($parsed.url) { Say-Ok "url: $($parsed.url)" }
        else { Say-Hm 'no url set -- the display will show the "nothing to show yet" screen' }

        $hosts = @($parsed.allowed_hosts)
        if ($hosts.Count -eq 0 -or -not $hosts[0]) {
            Say-No 'allowed_hosts is empty' 'Every URL will be refused. Set it to your Odoo host, or ["*"] for any.'
        }
        elseif ($hosts -contains '*') { Say-Hm 'allowed_hosts is ["*"] -- any host is accepted' }
        else { Say-Ok "allowed_hosts: $($hosts -join ', ')" }
    }
    catch {
        Say-No "agent.config.json is not valid JSON: $($_.Exception.Message)" 'Fix or delete it, then re-run the installer.'
    }
}

# ------------------------------------------------------------------- chrome
Write-Host "`nChrome" -ForegroundColor Cyan
$chromeCandidates = @(
    'C:\Program Files\Google\Chrome\Application\chrome.exe'
    'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe'
    (Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe')
)
if ($parsed -and $parsed.chrome_path -and $parsed.chrome_path -ne 'auto') {
    $chromeCandidates = @($parsed.chrome_path) + $chromeCandidates
}
$foundChrome = $chromeCandidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if ($foundChrome) {
    Say-Ok "chrome.exe: $foundChrome"
}
else {
    # The agent looks Chrome up before it binds the port, so a Chrome it cannot
    # find stops it dead before anything else -- silently, under pythonw.exe.
    Say-No 'chrome.exe not found in any standard location' `
        'Set "chrome_path" in agent.config.json to its full path. The agent will not start without it.'
}

# ------------------------------------------------------------- startup entry
Write-Host "`nStartup entry" -ForegroundColor Cyan
Say-Info "for account: $env:USERNAME"
if (Test-Path $ShortcutPath) {
    Say-Ok 'shortcut exists'
    try {
        $sc = (New-Object -ComObject WScript.Shell).CreateShortcut($ShortcutPath)
        Say-Info "target: $($sc.TargetPath) $($sc.Arguments)"
        if (-not (Test-Path $sc.TargetPath)) {
            Say-No 'shortcut points at a missing program' 'Re-run the installer.'
        }
        # A shortcut left behind by an install from a different folder would
        # silently start the wrong copy.
        if ($sc.Arguments -notlike "*$Root*") {
            Say-No 'shortcut points at a different folder' "It runs: $($sc.Arguments)"
        }
    }
    catch { Say-Hm "could not read the shortcut: $($_.Exception.Message)" }
}
else {
    Say-No 'no startup shortcut' 'Run "Install Agent.cmd" -- nothing will start at logon without it.'
}

# ------------------------------------------------------------------ running
Write-Host "`nAgent process" -ForegroundColor Cyan
$procs = @(
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -like '*display_agent.py*' }
)
if ($procs.Count -eq 0) {
    Say-Hm 'not running'
}
else {
    foreach ($p in $procs) {
        Say-Ok "running: pid $($p.ProcessId)"
        Say-Info $p.CommandLine
    }
    if ($procs.Count -gt 1) {
        Say-No "$($procs.Count) copies are running" 'Only one can hold the port. Run "Stop Agent.cmd", then start once.'
    }
}

# --------------------------------------------------------------------- port
Write-Host "`nPort $Port" -ForegroundColor Cyan
$listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
if ($listeners.Count -eq 0) {
    Say-Hm 'nothing listening'
}
else {
    foreach ($l in $listeners) {
        $owner = Get-Process -Id $l.OwningProcess -ErrorAction SilentlyContinue
        Say-Ok "listening: pid $($l.OwningProcess) ($($owner.ProcessName))"
        if ($owner -and $owner.ProcessName -notin @('python', 'pythonw')) {
            Say-No 'the port is held by something that is not the agent' "Change listen_port in agent.config.json, or stop $($owner.ProcessName)."
        }
    }
}

# ----------------------------------------------------------------- firewall
Write-Host "`nFirewall" -ForegroundColor Cyan
if (Test-Administrator) {
    $rule = Get-NetFirewallRule -DisplayName $FirewallRuleName -ErrorAction SilentlyContinue
    if ($rule) { Say-Ok 'inbound rule present' }
    else { Say-No 'no inbound rule' 'The POS computer will time out. Re-run "Install Agent.cmd" and accept the UAC prompt.' }
}
else {
    Say-Hm 'not elevated -- cannot check the rule (run this from an admin PowerShell to see it)'
}

# ---------------------------------------------------------------------- log
Write-Host "`nLast log lines" -ForegroundColor Cyan
if (Test-Path $LogPath) {
    Get-Content $LogPath -Tail 12 | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
}
else {
    Say-Hm "no agent.log yet -- the agent has never started from this folder"
}

# ------------------------------------------------------------------ summary
Write-Host "`n$('=' * 62)"
if ($problems.Count -eq 0) {
    Write-Host "No problems found." -ForegroundColor Green
    if ($procs.Count -eq 0) {
        Write-Host "`nThe agent is not running. Start it in a visible window to watch it:" -ForegroundColor Cyan
        Write-Host "  python display_agent.py run" -ForegroundColor Cyan
    }
}
else {
    Write-Host "$($problems.Count) problem(s):" -ForegroundColor Red
    foreach ($p in $problems) { Write-Host "  - $p" -ForegroundColor Red }
}

Write-Host "`nTo watch it start, with errors visible instead of hidden:" -ForegroundColor DarkGray
Write-Host "  python display_agent.py run`n" -ForegroundColor DarkGray
