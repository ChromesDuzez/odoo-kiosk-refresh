<#
.SYNOPSIS
    Sets the display agent to start automatically on the kiosk laptop.

.DESCRIPTION
    Run this ON THE DISPLAY LAPTOP, once, after you have filled in
    agent.config.json.

    It does two things:

      1. Puts a shortcut in the current user's Startup folder that launches
         the agent with pythonw.exe (no console window on the customer's
         screen). The Startup folder specifically -- not a SYSTEM scheduled
         task -- because Chrome has to appear on the logged-in user's desktop,
         and a SYSTEM task runs in a session with no visible desktop at all.

      2. Opens the agent's port to the local network, if you run this as
         Administrator. Without that, Windows Firewall silently drops the
         refresh requests and the POS computer just sees a timeout.

.PARAMETER Port
    Must match "listen_port" in agent.config.json. Default 8765.

.PARAMETER RemoteAddress
    Which addresses may reach the port. Defaults to LocalSubnet, which is the
    right answer unless the POS computer is on a different subnet.

.NOTES
    To undo all of this, use uninstall.ps1 -- it also stops the running agent
    and closes the kiosk Chrome window, which this script does not do.

.EXAMPLE
    .\install-agent.ps1
    .\install-agent.ps1 -Port 9000 -RemoteAddress 192.168.1.50
#>

[CmdletBinding()]
param(
    [int]$Port = 8765,
    [string]$RemoteAddress = 'LocalSubnet'
)

$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Agent = Join-Path $Root 'display_agent.py'
$Config = Join-Path $Root 'agent.config.json'
$StartupDir = [Environment]::GetFolderPath('Startup')
$ShortcutPath = Join-Path $StartupDir 'Odoo Customer Display Agent.lnk'
$FirewallRuleName = 'Odoo Customer Display Agent'

$IsAdmin = ([Security.Principal.WindowsPrincipal] `
        [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

# ------------------------------------------------------------ sanity checks

if (-not (Test-Path $Agent)) {
    Write-Host "display_agent.py not found next to this script." -ForegroundColor Red
    Write-Host "  expected: $Agent"
    exit 1
}

if (-not (Test-Path $Config)) {
    Write-Host "agent.config.json does not exist yet." -ForegroundColor Red
    Write-Host "Run this first, then edit the file it writes:" -ForegroundColor Yellow
    Write-Host "  python display_agent.py init"
    exit 1
}

# Catch the two blanks that produce a confusing failure much later: an agent
# that rejects every request, or one with nothing to display.
try {
    $parsed = Get-Content $Config -Raw | ConvertFrom-Json
}
catch {
    Write-Host "agent.config.json is not valid JSON: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

if (-not $parsed.shared_secret) {
    Write-Host "No pairing code in agent.config.json." -ForegroundColor Red
    Write-Host "Run:  python display_agent.py init" -ForegroundColor Yellow
    exit 1
}
if (-not $parsed.url) {
    Write-Host "No url set yet -- that is fine before pairing, the display will show" -ForegroundColor Yellow
    Write-Host "the pairing code. Send a URL afterwards from the POS computer." -ForegroundColor Yellow
}
if ($parsed.listen_port -and [int]$parsed.listen_port -ne $Port) {
    Write-Host "Config says port $($parsed.listen_port) but -Port is $Port." -ForegroundColor Yellow
    Write-Host "Using $($parsed.listen_port) for the firewall rule." -ForegroundColor Yellow
    $Port = [int]$parsed.listen_port
}

# ------------------------------------------------------------------- python

# pythonw.exe runs without a console window, which is what we want on a
# customer-facing screen. Prefer the one the py launcher points at.
$PythonW = $null
$pyLauncher = Get-Command 'py.exe' -ErrorAction SilentlyContinue
if ($pyLauncher) {
    $candidate = & py.exe -3 -c "import sys, os; print(os.path.join(os.path.dirname(sys.executable), 'pythonw.exe'))" 2>$null
    if ($candidate -and (Test-Path $candidate)) { $PythonW = $candidate }
}
if (-not $PythonW) {
    $pythonCmd = Get-Command 'python.exe' -ErrorAction SilentlyContinue
    if ($pythonCmd) {
        $candidate = Join-Path (Split-Path -Parent $pythonCmd.Source) 'pythonw.exe'
        if (Test-Path $candidate) { $PythonW = $candidate }
    }
}
if (-not $PythonW) {
    Write-Host "Could not find pythonw.exe." -ForegroundColor Red
    Write-Host "Make sure Python is installed and on PATH." -ForegroundColor Yellow
    exit 1
}

Write-Host "Using: $PythonW" -ForegroundColor DarkGray

# ----------------------------------------------------------------- shortcut

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($ShortcutPath)
$shortcut.TargetPath = $PythonW
$shortcut.Arguments = "`"$Agent`" run"
$shortcut.WorkingDirectory = $Root
$shortcut.Description = 'Keeps the Odoo customer display running and accepts refresh commands'
$shortcut.WindowStyle = 7          # minimised, in case pythonw ever falls back
$shortcut.Save()

Write-Host "Startup shortcut created:" -ForegroundColor Green
Write-Host "  $ShortcutPath"

# ----------------------------------------------------------------- firewall

if ($IsAdmin) {
    Get-NetFirewallRule -DisplayName $FirewallRuleName -ErrorAction SilentlyContinue |
        Remove-NetFirewallRule -ErrorAction SilentlyContinue

    New-NetFirewallRule `
        -DisplayName $FirewallRuleName `
        -Direction Inbound `
        -Action Allow `
        -Protocol TCP `
        -LocalPort $Port `
        -RemoteAddress $RemoteAddress `
        -Profile Private, Domain | Out-Null

    Write-Host "Firewall opened on TCP $Port from $RemoteAddress (private/domain networks)." -ForegroundColor Green
}
else {
    Write-Host "`nNot running as Administrator -- firewall rule NOT created." -ForegroundColor Yellow
    Write-Host "The POS computer will time out until you run this in an elevated PowerShell:" -ForegroundColor Yellow
    Write-Host "  New-NetFirewallRule -DisplayName '$FirewallRuleName' -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -RemoteAddress $RemoteAddress -Profile Private,Domain"
}

# -------------------------------------------------------------------- finish

Write-Host "`nThis laptop's addresses:" -ForegroundColor Cyan
Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
    ForEach-Object { Write-Host "  $($_.IPAddress)  ($($_.InterfaceAlias))" }

Write-Host "`nStart it now without rebooting:" -ForegroundColor Cyan
Write-Host "  Start-Process '$PythonW' -ArgumentList '`"$Agent`" run' -WorkingDirectory '$Root'"

if (-not $parsed.paired) {
    Write-Host "`nOnce it starts, this screen will show a pairing code and its own" -ForegroundColor Cyan
    Write-Host "address. Go to the POS computer, run 'Pair With Display', and type" -ForegroundColor Cyan
    Write-Host "in what you see. You are done on this machine after that." -ForegroundColor Cyan
}
