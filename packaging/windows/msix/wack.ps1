# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
# On Windows: pack an MSIX layout (make_layout.sh), sign it with a throwaway
# test certificate and run the Windows App Certification Kit on it, as the
# Microsoft Store's certification would.  Installs the Windows SDK (App
# Certification Kit, signing and packaging tools) first if needed.  Needs an
# administrator.
#
#   powershell -ExecutionPolicy Bypass -File wack.ps1 -Layout <folder> [-Work <folder>]
#
# The test certificate's subject must be the manifest's Publisher.  It is
# trusted only for the test and removed again afterwards.
param(
    [Parameter(Mandatory = $true)][string]$Layout,
    [string]$Work = (Split-Path -Parent $Layout),
    [switch]$InstallOnly
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

# Keep the machine awake while this runs (installing and testing take a while)
Add-Type -Namespace Win32 -Name Power -MemberDefinition @'
[DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint flags);
'@
[void][Win32.Power]::SetThreadExecutionState([uint32]2147483651)   # 0x80000003: CONTINUOUS | SYSTEM | DISPLAY

$kits = Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10"
$appcert = Join-Path $kits "App Certification Kit\appcert.exe"

function Find-SdkTool([string]$name) {
    $arch = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "arm64" } else { "x64" }
    foreach ($dir in Get-ChildItem (Join-Path $kits "bin") -Directory -ErrorAction SilentlyContinue |
                     Where-Object Name -like "10.*" | Sort-Object { [version]$_.Name } -Descending) {
        foreach ($a in @($arch, "x64")) {
            $tool = Join-Path $dir.FullName "$a\$name"
            if (Test-Path $tool) { return $tool }
        }
    }
    return $null
}

# ------------------------------------------------------------------ the SDK
if (-not (Test-Path $appcert) -or -not (Find-SdkTool "makeappx.exe") -or -not (Find-SdkTool "signtool.exe")) {
    $setup = Join-Path $Work "winsdksetup.exe"
    if (-not (Test-Path $setup)) {
        Write-Output "Downloading the Windows SDK installer"
        Invoke-WebRequest "https://go.microsoft.com/fwlink/?linkid=2347650" -OutFile $setup
    }
    Write-Output "Installing the App Certification Kit and the packaging and signing tools"
    $features = "OptionId.WindowsSoftwareLogoToolkit OptionId.SigningTools OptionId.UWPManaged"
    $p = Start-Process $setup -ArgumentList "/features $features /quiet /norestart /log `"$Work\sdk-install.log`"" -Wait -PassThru
    Write-Output "  installer exit code $($p.ExitCode)"
}
$makeappx = Find-SdkTool "makeappx.exe"
$signtool = Find-SdkTool "signtool.exe"
Write-Output "appcert:  $(if (Test-Path $appcert) { $appcert } else { 'MISSING' })"
Write-Output "makeappx: $makeappx"
Write-Output "signtool: $signtool"
if ($InstallOnly) { exit 0 }
if (-not (Test-Path $appcert) -or -not $makeappx -or -not $signtool) { throw "the SDK tools are missing" }

# ------------------------------------------------------------------ pack and sign
[xml]$manifest = Get-Content (Join-Path $Layout "AppxManifest.xml")
$identity = $manifest.Package.Identity
$msix = Join-Path $Work "$($identity.Name)_$($identity.Version)_$($identity.ProcessorArchitecture).msix"
Write-Output "Packing $msix"
& $makeappx pack /o /d $Layout /p $msix | Select-Object -Last 3
if ($LASTEXITCODE) { throw "makeappx failed ($LASTEXITCODE)" }

$cert = New-SelfSignedCertificate -Type Custom -Subject $identity.Publisher -KeyUsage DigitalSignature `
    -FriendlyName "anySCP MSIX test (throwaway)" -CertStoreLocation Cert:\CurrentUser\My `
    -NotAfter (Get-Date).AddDays(2) -TextExtension @("2.5.29.37={text}1.3.6.1.5.5.7.3.3", "2.5.29.19={text}")
$pfx = Join-Path $Work "test-signing.pfx"
$cer = Join-Path $Work "test-signing.cer"
$password = ConvertTo-SecureString "anyscp-test" -AsPlainText -Force
try {
    Export-PfxCertificate -Cert $cert -FilePath $pfx -Password $password | Out-Null
    Export-Certificate -Cert $cert -FilePath $cer | Out-Null
    & $signtool sign /q /fd SHA256 /f $pfx /p "anyscp-test" $msix
    if ($LASTEXITCODE) { throw "signtool failed ($LASTEXITCODE)" }
    $trusted = Import-Certificate -FilePath $cer -CertStoreLocation Cert:\LocalMachine\TrustedPeople

    # -------------------------------------------------------------- certify
    $report = Join-Path $Work "wack-report.xml"
    Remove-Item $report -ErrorAction SilentlyContinue
    Write-Output "Running the App Certification Kit (this takes a few minutes)"
    & $appcert reset | Out-Null
    & $appcert test -appxpackagepath $msix -reportoutputpath $report | Select-Object -Last 5
    if (-not (Test-Path $report)) { throw "no report from the App Certification Kit" }
    [xml]$xml = Get-Content $report
    Write-Output ""
    Write-Output "OVERALL RESULT: $($xml.REPORT.OVERALL_RESULT)"
    foreach ($test in $xml.SelectNodes("//TEST")) {
        $result = $test.RESULT.InnerText
        if (-not $result) { $result = $test.RESULT }
        $messages = ($test.SelectNodes(".//MESSAGE") | ForEach-Object { $_.TEXT; $_.InnerText } |
                     Where-Object { $_ } | Select-Object -Unique) -join " | "
        Write-Output ("  {0,-8} {1}{2}" -f $result, $test.NAME, $(if ($messages -and $result -ne "PASS") { " -- $messages" } else { "" }))
    }
} finally {
    # nothing of the test certificate stays trusted or installed
    if ($trusted) { Remove-Item "Cert:\LocalMachine\TrustedPeople\$($trusted.Thumbprint)" -ErrorAction SilentlyContinue }
    Remove-Item "Cert:\CurrentUser\My\$($cert.Thumbprint)" -ErrorAction SilentlyContinue
    Remove-Item $pfx, $cer -ErrorAction SilentlyContinue
    Get-AppxPackage -Name $identity.Name -ErrorAction SilentlyContinue | Remove-AppxPackage -ErrorAction SilentlyContinue
}
