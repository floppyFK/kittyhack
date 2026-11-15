# Install the Kittyhack model server on Windows.
#
# The server lives in C:\ProgramData\kittyhack-model-server and starts when the
# installing user logs on. AMD graphics cards are supported on Linux only.
#
#   powershell -ExecutionPolicy Bypass -File install_model_server.ps1 -Variant cpu -Lang de
#   powershell -ExecutionPolicy Bypass -File install_model_server.ps1 -Variant cuda -Lang en
#   powershell -ExecutionPolicy Bypass -File install_model_server.ps1 -Uninstall

[CmdletBinding()]
param(
    [string]$Variant = "",
    [string]$Lang = "en",
    [string]$Sources = "",
    [int]$Port = 0,
    [switch]$NonInteractive,
    [switch]$Uninstall,
    [switch]$Yes
)

$ErrorActionPreference = "Stop"
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}
$TaskName = "KittyhackModelServer"
$InstallDir = Join-Path $env:ProgramData "kittyhack-model-server"

function Say([string]$English, [string]$German) {
    if ($Lang -eq "de") {
        Write-Host $German
    } else {
        Write-Host $English
    }
}

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Resolve-Python {
    $candidates = @(
        @{ Exe = "py"; Prefix = @("-3") },
        @{ Exe = "python"; Prefix = @() }
    )
    foreach ($candidate in $candidates) {
        $command = Get-Command $candidate.Exe -ErrorAction SilentlyContinue
        if (-not $command) {
            continue
        }
        $code = "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
        & $candidate.Exe @($candidate.Prefix + @("-c", $code))
        if ($LASTEXITCODE -eq 0) {
            return $candidate
        }
    }
    return $null
}

function Get-SourceRoot([string]$Requested) {
    if ($Requested -and (Test-Path (Join-Path $Requested "model-server\server.py"))) {
        return (Resolve-Path $Requested).Path
    }
    $beside = Join-Path (Split-Path -Parent $PSCommandPath) "..\model-server\server.py"
    if (Test-Path $beside) {
        return (Resolve-Path (Join-Path (Split-Path -Parent $PSCommandPath) "..")).Path
    }
    $tmp = Join-Path $env:TEMP ("kittyhack-src-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $tmp | Out-Null
    $zip = Join-Path $tmp "kittyhack.zip"
    Say "Downloading Kittyhack sources…" "Lade die Kittyhack-Quellen herunter…"
    Invoke-WebRequest -Uri "https://github.com/floppyFK/kittyhack/archive/refs/heads/main.zip" -OutFile $zip
    Expand-Archive -Path $zip -DestinationPath $tmp -Force
    $extracted = Get-ChildItem -Path $tmp -Directory | Where-Object { $_.Name -like "kittyhack-*" } | Select-Object -First 1
    if (-not $extracted -or -not (Test-Path (Join-Path $extracted.FullName "model-server\server.py"))) {
        throw "Kittyhack sources did not contain model-server/server.py"
    }
    return $extracted.FullName
}

function Copy-ServerSources([string]$SourceRoot, [string]$Dest) {
    $from = Join-Path $SourceRoot "model-server"
    $names = @(
        "server.py", "train.py", "config.py",
        "requirements.txt", "requirements-torch-cpu.txt", "requirements-torch-cuda-windows.txt",
        "config.ini.example"
    )
    New-Item -ItemType Directory -Force -Path $Dest | Out-Null
    foreach ($name in $names) {
        $path = Join-Path $from $name
        if (-not (Test-Path $path)) {
            throw "Missing $path"
        }
        Copy-Item -Path $path -Destination (Join-Path $Dest $name) -Force
    }
}

function Get-RequirementsFingerprint([string]$Dest, [string]$Chosen) {
    $torchFile = if ($Chosen -eq "cuda") { "requirements-torch-cuda-windows.txt" } else { "requirements-torch-cpu.txt" }
    $hashes = @(
        (Get-FileHash -Algorithm SHA256 (Join-Path $Dest "requirements.txt")).Hash
        (Get-FileHash -Algorithm SHA256 (Join-Path $Dest $torchFile)).Hash
        $Chosen
    )
    return ($hashes -join " ")
}

function Read-ListenPort([string]$ConfigPath) {
    $text = Get-Content -Raw -Path $ConfigPath
    if ($text -match '(?m)^\s*port\s*=\s*(\d+)\s*$') {
        return [int]$Matches[1]
    }
    return 8000
}

function Write-Config([string]$Dest) {
    $config = Join-Path $Dest "config.ini"
    if (Test-Path $config) {
        return
    }
    Copy-Item (Join-Path $Dest "config.ini.example") $config
    $chosen = $Port
    if ($chosen -le 0 -and -not $NonInteractive) {
        $entered = Read-Host "Port for the model server [8000]"
        if ([string]::IsNullOrWhiteSpace($entered)) {
            $chosen = 8000
        } else {
            $chosen = [int]$entered
        }
    }
    if ($chosen -le 0) {
        $chosen = 8000
    }
    if ($chosen -lt 1 -or $chosen -gt 65535) {
        throw "Invalid port: $chosen"
    }
    $text = Get-Content -Raw -Path $config
    $text = [regex]::Replace($text, '(?m)^port\s*=\s*.*$', "port = $chosen")
    # Windows starts DataLoader workers with spawn. Zero avoids that extra process.
    $text = [regex]::Replace($text, '(?m)^workers\s*=\s*.*$', "workers = 0")
    $utf8 = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($config, $text, $utf8)
}

function Install-PythonEnvironment([string]$Dest, [string]$Chosen, $Python) {
    $stamp = Join-Path $Dest ".requirements.sha256"
    $fingerprint = Get-RequirementsFingerprint $Dest $Chosen
    $pythonExe = Join-Path $Dest ".venv\Scripts\python.exe"
    if ((Test-Path $pythonExe) -and (Test-Path $stamp) -and ((Get-Content -Raw $stamp).Trim() -eq $fingerprint)) {
        Say "Python environment is already up to date." "Die Python-Umgebung ist bereits aktuell."
        return
    }
    if (-not (Test-Path $pythonExe)) {
        Say "Creating the model server virtualenv…" "Erstelle die virtuelle Umgebung des Modellservers…"
        & $Python.Exe @($Python.Prefix + @("-m", "venv", (Join-Path $Dest ".venv")))
        if ($LASTEXITCODE -ne 0) {
            throw "python -m venv failed"
        }
    }
    $torchFile = if ($Chosen -eq "cuda") { "requirements-torch-cuda-windows.txt" } else { "requirements-torch-cpu.txt" }
    Say "Installing model server dependencies ($Chosen). This can take several minutes." `
        "Installiere die Abhängigkeiten des Modellservers ($Chosen). Das kann einige Minuten dauern."
    & $pythonExe -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed" }
    & $pythonExe -m pip install -r (Join-Path $Dest $torchFile)
    if ($LASTEXITCODE -ne 0) { throw "torch install failed" }
    & $pythonExe -m pip install -r (Join-Path $Dest "requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "requirements install failed" }
    Set-Content -Path $stamp -Value $fingerprint -Encoding ascii
    Set-Content -Path (Join-Path $Dest ".torch-variant") -Value $Chosen -Encoding ascii
}

function Register-ServerTask([string]$Dest) {
    # A SYSTEM task has no console. The venv launcher then exits with code 101
    # before server.py starts. The logged-on user also has the graphics card.
    $pythonExe = Join-Path $Dest ".venv\Scripts\python.exe"
    $serverPy = Join-Path $Dest "server.py"
    $logFile = Join-Path $Dest "server.log"
    $runner = Join-Path $Dest "run-server.cmd"
    $user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $cmd = "@echo off`r`ncd /d `"$Dest`"`r`n`"$pythonExe`" -u `"$serverPy`" >> `"$logFile`" 2>&1`r`n"
    $utf8 = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($runner, $cmd, $utf8)
    & icacls $Dest /grant "${user}:(OI)(CI)M" /T | Out-Null
    $action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$runner`"" -WorkingDirectory $Dest
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -RestartCount 999 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -StartWhenAvailable
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Start-ScheduledTask -TaskName $TaskName
}

function Test-ListenPort([int]$ListenPort) {
    $deadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $deadline) {
        $client = New-Object System.Net.Sockets.TcpClient
        try {
            $client.Connect("127.0.0.1", $ListenPort)
            return $true
        } catch {
            Start-Sleep -Milliseconds 500
        } finally {
            $client.Dispose()
        }
    }
    return $false
}

function Test-FirewallActive {
    try {
        $profiles = @(Get-NetFirewallProfile -ErrorAction Stop | Where-Object { $_.Enabled -eq $true -or $_.Enabled -eq "True" })
        return $profiles.Count -gt 0
    } catch {
        return $true
    }
}

function Enable-PrivatePort([int]$ListenPort) {
    if (-not (Test-FirewallActive)) {
        return
    }
    if ($NonInteractive) {
        Say "The firewall is active. Port $ListenPort stays closed." "Die Firewall ist aktiv. Port $ListenPort bleibt geschlossen."
        return
    }
    Say "Open port $ListenPort in the firewall for private networks? (y/N): " `
        "Port $ListenPort in der Firewall für private Netze öffnen? (y/N): "
    $answer = Read-Host
    if ($answer -notmatch '^(y|yes|j|ja)$') {
        Say "Leaving port $ListenPort closed." "Port $ListenPort bleibt geschlossen."
        return
    }
    try {
        Get-NetFirewallRule -DisplayName "Kittyhack model server" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
        New-NetFirewallRule `
            -DisplayName "Kittyhack model server" `
            -Direction Inbound `
            -Action Allow `
            -Protocol TCP `
            -LocalPort $ListenPort `
            -Profile Private | Out-Null
        Say "Allowed port $ListenPort from private networks." "Port $ListenPort ist für private Netze erlaubt."
    } catch {
        Say "Could not open port $ListenPort in the Windows firewall." "Port $ListenPort konnte in der Windows-Firewall nicht geöffnet werden."
    }
}

function Get-PrimaryIPv4 {
    $config = Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway -and $_.IPv4Address } | Select-Object -First 1
    if ($config) {
        $address = @($config.IPv4Address) | Select-Object -First 1
        if ($address.IPAddress) {
            return $address.IPAddress
        }
    }
    return "127.0.0.1"
}

function Remove-Server {
    if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
        Say "No model server task is installed." "Es ist kein Modellserver-Dienst installiert."
        return
    }
    if (-not $Yes) {
        $answer = Read-Host "Remove the model server, its task, and $InstallDir? (y/N)"
        if ($answer -notmatch '^(y|yes|j|ja)$') {
            Say "Keeping the model server." "Modellserver bleibt installiert."
            return
        }
    }
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Get-NetFirewallRule -DisplayName "Kittyhack model server" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    if (Test-Path $InstallDir) {
        Remove-Item -Recurse -Force $InstallDir
    }
    Say "Model server removed." "Modellserver entfernt."
}

if ($Lang -ne "de") {
    $Lang = "en"
}

if (-not (Test-Administrator)) {
    Say "Run this script as administrator." "Dieses Skript muss als Administrator laufen."
    exit 1
}

if ($Uninstall) {
    Remove-Server
    exit 0
}

$Variant = $Variant.ToLowerInvariant()
if (-not $Variant -and -not $NonInteractive) {
    Say "Install for the processor (cpu) or an NVIDIA graphics card (cuda)? [cpu]" `
        "Für den Prozessor (cpu) oder eine NVIDIA-Grafikkarte (cuda) installieren? [cpu]"
    $entered = Read-Host
    if ([string]::IsNullOrWhiteSpace($entered)) {
        $Variant = "cpu"
    } else {
        $Variant = $entered.ToLowerInvariant()
    }
}
if (-not $Variant) {
    $Variant = "cpu"
}
if ($Variant -eq "rocm") {
    Say "AMD graphics cards are supported on Linux, not on Windows. On Windows, choose cpu or cuda." `
        "AMD-Grafikkarten werden unter Linux unterstützt, nicht unter Windows. Unter Windows wählst du cpu oder cuda."
    exit 1
}
if ($Variant -ne "cpu" -and $Variant -ne "cuda") {
    Say "Unknown variant: $Variant" "Unbekannte Variante: $Variant"
    exit 1
}

$python = Resolve-Python
if (-not $python) {
    Say "Python 3.10 or newer is required. Install it from python.org and enable the Python launcher." `
        "Python 3.10 oder neuer wird benötigt. Installiere es von python.org und aktiviere den Python-Launcher."
    exit 1
}

Say "Installing the model server to $InstallDir" "Installiere den Modellserver nach $InstallDir"
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$sourceRoot = Get-SourceRoot $Sources
Copy-ServerSources $sourceRoot $InstallDir
Write-Config $InstallDir
Install-PythonEnvironment $InstallDir $Variant $python
Register-ServerTask $InstallDir
$listenPort = Read-ListenPort (Join-Path $InstallDir "config.ini")
Enable-PrivatePort $listenPort
if (-not (Test-ListenPort $listenPort)) {
    Say "The model server did not start. See $(Join-Path $InstallDir 'server.log')" `
        "Der Modellserver ist nicht gestartet. Siehe $(Join-Path $InstallDir 'server.log')"
    exit 1
}
$hostAddress = Get-PrimaryIPv4
Say "Model server is running at http://${hostAddress}:$listenPort" "Modellserver läuft unter http://${hostAddress}:$listenPort"
Say "It starts again when you log on to Windows." "Er startet wieder, wenn du dich unter Windows anmeldest."
Say "Training settings: $(Join-Path $InstallDir 'config.ini')" "Trainingseinstellungen: $(Join-Path $InstallDir 'config.ini')"
