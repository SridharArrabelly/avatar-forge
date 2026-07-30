<#
.SYNOPSIS
  Prepares the Windows host for the Avatar-Forge Teams meeting media bot.

.DESCRIPTION
  Run this ON the Windows VM (RDP in, or via "az vm run-command"). It is
  idempotent and split into stages so prep can run automatically and the
  cert/build/run steps can finish once they have what they need.

  Stages:
    -Stage Prep   : open the Windows firewall for signaling+media ports and
                    install the .NET 8 SDK + ASP.NET Core Runtime. (Automatable;
                    this is what "az vm run-command" runs.)
    -Stage Cert   : obtain a publicly-trusted TLS cert for the host FQDN using
                    win-acme (Let's Encrypt, HTTP-01). Requires inbound TCP 80.
    -Stage Build  : clone the repo and "dotnet publish -r win-x64" the bot.
    -Stage Run    : install + start the bot as a Windows service.

  Parameters are only needed for the stage(s) you run. ASCII-only on purpose so
  it survives "az vm run-command" transit.

.EXAMPLE
  # Automated prep (firewall + .NET) - what the deployment runs for you:
  powershell -ExecutionPolicy Bypass -File setup-host.ps1 -Stage Prep

.EXAMPLE
  # On the box, after prep, get a cert then build+run:
  .\setup-host.ps1 -Stage Cert  -Fqdn <host>.<region>.cloudapp.azure.com -CertEmail you@example.com
  .\setup-host.ps1 -Stage Build -RepoUrl https://github.com/SridharArrabelly/avatar-forge.git
  .\setup-host.ps1 -Stage Run   -Fqdn ... -Thumbprint <cert-thumb> -BridgeUrl wss://<app>/ws/acs/audio `
                                -BotAppId <app-id> -BotTenantId <tenant-id> -BotSecret <secret>
#>
[CmdletBinding()]
param(
    [ValidateSet('Prep', 'Cert', 'Build', 'Run')]
    [string]$Stage = 'Prep',

    [int]$SignalingPort = 9441,
    [int]$MediaPort = 8445,

    [string]$Fqdn,
    [string]$CertEmail,
    [string]$Thumbprint,
    [string]$RepoUrl = 'https://github.com/SridharArrabelly/avatar-forge.git',
    [string]$Branch = 'main',
    [string]$WorkDir = 'C:\avatar-meetingbot',
    [string]$BridgeUrl,
    # Deployment-specific: the calling bot's Entra app registration. No defaults
    # on purpose - a wrong-but-plausible default is worse than a missing one.
    [string]$BotAppId,
    [string]$BotTenantId,
    [string]$BotSecret
)

$ErrorActionPreference = 'Stop'

function Write-Step($m) { Write-Host "`n=== $m ===" -ForegroundColor Cyan }

switch ($Stage) {

    'Prep' {
        Write-Step "Opening Windows firewall (signaling $SignalingPort, media $MediaPort, ACME 80)"
        New-NetFirewallRule -DisplayName 'AvatarBot-Signaling' -Direction Inbound -Action Allow -Protocol TCP -LocalPort $SignalingPort -ErrorAction SilentlyContinue | Out-Null
        New-NetFirewallRule -DisplayName 'AvatarBot-Media'     -Direction Inbound -Action Allow -Protocol TCP -LocalPort $MediaPort     -ErrorAction SilentlyContinue | Out-Null
        New-NetFirewallRule -DisplayName 'AvatarBot-ACME-HTTP' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 80            -ErrorAction SilentlyContinue | Out-Null

        Write-Step 'Installing .NET 8 SDK + ASP.NET Core Runtime (dotnet-install.ps1)'
        $dot = Join-Path $env:TEMP 'dotnet-install.ps1'
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest 'https://dot.net/v1/dotnet-install.ps1' -OutFile $dot -UseBasicParsing
        & $dot -Channel 8.0 -InstallDir 'C:\Program Files\dotnet'
        & $dot -Channel 8.0 -Runtime aspnetcore -InstallDir 'C:\Program Files\dotnet'
        [Environment]::SetEnvironmentVariable('PATH', $env:PATH + ';C:\Program Files\dotnet', 'Machine')

        # The Graph Real-Time Media native stack (NativeMedia.dll) links against the
        # Visual C++ runtime; without it MediaPlatform.Initialize throws
        # DllNotFoundException. Install the x64 redistributable.
        Write-Step 'Installing Visual C++ x64 Redistributable (required by the media stack)'
        if (-not (Test-Path "$env:SystemRoot\System32\vcruntime140.dll")) {
            $vc = Join-Path $env:TEMP 'vc_redist.x64.exe'
            Invoke-WebRequest 'https://aka.ms/vs/17/release/vc_redist.x64.exe' -OutFile $vc -UseBasicParsing
            Start-Process -FilePath $vc -ArgumentList '/install','/quiet','/norestart' -Wait
        }

        # The media platform requires at least 2 usable cores (it throws
        # "MediaPlatform needs a system with at least 2 cores" otherwise). Use a
        # 4-vCPU VM (e.g. Standard_D4s_v5) for headroom on real-time transcode.
        Write-Step "Host has $([Environment]::ProcessorCount) logical processors (media platform needs >= 2; 4+ recommended)"

        Write-Step 'Prep complete'
        & 'C:\Program Files\dotnet\dotnet.exe' --info | Select-Object -First 5
    }

    'Cert' {
        if (-not $Fqdn -or -not $CertEmail) { throw 'Cert stage requires -Fqdn and -CertEmail.' }
        Write-Step "Obtaining a Lets Encrypt cert for $Fqdn via win-acme (HTTP-01, needs inbound TCP 80)"
        $wacsDir = 'C:\win-acme'
        if (-not (Test-Path $wacsDir)) {
            New-Item -ItemType Directory $wacsDir | Out-Null
            $zip = Join-Path $env:TEMP 'wacs.zip'
            Invoke-WebRequest 'https://github.com/win-acme/win-acme/releases/download/v2.2.9.1701/win-acme.v2.2.9.1701.x64.pluggable.zip' -OutFile $zip -UseBasicParsing
            Expand-Archive $zip -DestinationPath $wacsDir -Force
        }
        & "$wacsDir\wacs.exe" --target manual --host $Fqdn --validation selfhosting --emailaddress $CertEmail --accepttos --store certificatestore --certificatestore My
        Write-Host "`nInstalled cert thumbprints for ${Fqdn}:" -ForegroundColor Green
        Get-ChildItem Cert:\LocalMachine\My | Where-Object { $_.Subject -like "*$Fqdn*" } | Format-List Subject, Thumbprint, NotAfter
    }

    'Build' {
        Write-Step "Cloning $RepoUrl ($Branch) and publishing the bot"
        if (-not (Test-Path $WorkDir)) { New-Item -ItemType Directory $WorkDir | Out-Null }
        if (-not (Test-Path "$WorkDir\repo")) {
            git clone --branch $Branch --depth 1 $RepoUrl "$WorkDir\repo"
        }
        else { Set-Location "$WorkDir\repo"; git pull }
        Set-Location "$WorkDir\repo"
        & 'C:\Program Files\dotnet\dotnet.exe' publish meeting-bot\MeetingBot.csproj -c Release -r win-x64 --self-contained -o "$WorkDir\publish"
        Write-Step "Published to $WorkDir\publish"
    }

    'Run' {
        foreach ($p in 'Fqdn', 'Thumbprint', 'BridgeUrl', 'BotAppId', 'BotTenantId', 'BotSecret') {
            if (-not (Get-Variable $p).Value) { throw "Run stage requires -$p." }
        }
        Write-Step 'Writing environment and starting the bot service'
        $exe = "$WorkDir\publish\AvatarForge.MeetingBot.exe"
        if (-not (Test-Path $exe)) { throw "Publish output not found at $exe; run -Stage Build first." }

        [Environment]::SetEnvironmentVariable('Bot__AppId', $BotAppId, 'Machine')
        [Environment]::SetEnvironmentVariable('Bot__TenantId', $BotTenantId, 'Machine')
        [Environment]::SetEnvironmentVariable('Bot__ServiceFqdn', $Fqdn, 'Machine')
        [Environment]::SetEnvironmentVariable('Bot__CertificateThumbprint', $Thumbprint, 'Machine')
        [Environment]::SetEnvironmentVariable('Bot__BridgeWebSocketUrl', $BridgeUrl, 'Machine')
        [Environment]::SetEnvironmentVariable('Bot__BridgeSampleRate', '16000', 'Machine')
        [Environment]::SetEnvironmentVariable('BOT_CLIENT_SECRET', $BotSecret, 'Machine')

        $svc = 'AvatarForgeMeetingBot'
        if (Get-Service $svc -ErrorAction SilentlyContinue) { Stop-Service $svc -Force; sc.exe delete $svc | Out-Null; Start-Sleep 2 }
        New-Service -Name $svc -BinaryPathName $exe -DisplayName 'Avatar Forge Meeting Bot' -StartupType Automatic | Out-Null
        Start-Service $svc
        $api = "https://${Fqdn}:${SignalingPort}/api/join"
        Write-Step "Service '$svc' started. Operator API: $api"
    }
}
