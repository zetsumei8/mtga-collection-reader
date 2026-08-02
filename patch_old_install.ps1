[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$TargetPath,
    [switch]$NoPause,
    [switch]$ForcePatch
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$SourcePath = Split-Path -Parent $MyInvocation.MyCommand.Path
$ManifestPath = Join-Path $SourcePath 'app_manifest.json'
if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
    throw 'app_manifest.json is missing from the update package.'
}

$Manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
$NewVersion = [string]$Manifest.version
$ManagedFiles = @($Manifest.managed_files | ForEach-Object { [string]$_ })
$Timestamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$LogLines = New-Object System.Collections.Generic.List[string]
$Target = $null
$BackupRoot = $null
$PatchSucceeded = $false
$PreExisting = @{}
$OldVersion = 'unknown'
$InstalledVersionExisted = $false

function Write-PatchLog {
    param([string]$Message)
    $line = ('[{0}] {1}' -f (Get-Date -Format 'HH:mm:ss'), $Message)
    $LogLines.Add($line)
    Write-Host $line
}

function Pause-IfNeeded {
    if (-not $NoPause) {
        Write-Host ''
        Read-Host 'Press Enter to close' | Out-Null
    }
}

function Select-InstallFolder {
    Add-Type -AssemblyName System.Windows.Forms
    $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
    $dialog.Description = 'Select the older MTGA Collection Reader or Exporter folder that contains mtg.py'
    $dialog.ShowNewFolderButton = $false
    if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
        return $dialog.SelectedPath
    }
    return $null
}

function Get-ExporterVersion {
    param([string]$Folder)

    $manifest = Join-Path $Folder 'app_manifest.json'
    if (Test-Path -LiteralPath $manifest -PathType Leaf) {
        try {
            $data = Get-Content -LiteralPath $manifest -Raw | ConvertFrom-Json
            if ($data.version) { return [string]$data.version }
        } catch { }
    }

    $installed = Join-Path $Folder 'installed_version.txt'
    if (Test-Path -LiteralPath $installed -PathType Leaf) {
        $value = (Get-Content -LiteralPath $installed -Raw).Trim()
        if ($value) { return $value }
    }

    $python = Join-Path $Folder 'mtg.py'
    if (Test-Path -LiteralPath $python -PathType Leaf) {
        $match = Select-String -LiteralPath $python -Pattern '^APP_VERSION\s*=\s*["'']([^"'']+)["'']' | Select-Object -First 1
        if ($match -and $match.Matches.Count -gt 0) {
            return $match.Matches[0].Groups[1].Value
        }
    }

    return 'unknown'
}

function Test-VersionAtLeast {
    param([string]$Left, [string]$Right)
    try {
        return ([version]$Left -ge [version]$Right)
    } catch {
        return $false
    }
}

function Test-ExporterRunning {
    param([string]$Folder)
    try {
        $escaped = [regex]::Escape((Resolve-Path -LiteralPath $Folder).Path)
        $running = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
            ($_.Name -ieq 'MTGA_Collection_Reader.exe') -or
            ($_.Name -ieq 'MTGA_Collection_Exporter.exe') -or
            ($_.CommandLine -and $_.CommandLine -match $escaped -and $_.CommandLine -match 'mtg\.py')
        } | Select-Object -First 1
        return [bool]$running
    } catch {
        return $false
    }
}

function Copy-ManagedFilesToBackup {
    param([string]$From, [string]$Backup)
    foreach ($relative in $ManagedFiles) {
        $sourceFile = Join-Path $From $relative
        $PreExisting[$relative] = Test-Path -LiteralPath $sourceFile -PathType Leaf
        if ($PreExisting[$relative]) {
            $backupFile = Join-Path $Backup $relative
            $backupParent = Split-Path -Parent $backupFile
            if (-not (Test-Path -LiteralPath $backupParent)) {
                New-Item -ItemType Directory -Path $backupParent -Force | Out-Null
            }
            Copy-Item -LiteralPath $sourceFile -Destination $backupFile -Force
        }
    }

    $runtimePath = Join-Path $From 'runtime_path.txt'
    if (Test-Path -LiteralPath $runtimePath -PathType Leaf) {
        Copy-Item -LiteralPath $runtimePath -Destination (Join-Path $Backup 'runtime_path.txt') -Force
    }

    $script:InstalledVersionExisted = Test-Path -LiteralPath (Join-Path $From 'installed_version.txt') -PathType Leaf
    if ($script:InstalledVersionExisted) {
        Copy-Item -LiteralPath (Join-Path $From 'installed_version.txt') -Destination (Join-Path $Backup 'installed_version.txt') -Force
    }
    if (Test-Path -LiteralPath (Join-Path $From 'install_state.json') -PathType Leaf) {
        Copy-Item -LiteralPath (Join-Path $From 'install_state.json') -Destination (Join-Path $Backup 'install_state.json') -Force
    }
}

function Install-NewManagedFiles {
    param([string]$From, [string]$To)
    foreach ($relative in $ManagedFiles) {
        $sourceFile = Join-Path $From $relative
        if (-not (Test-Path -LiteralPath $sourceFile -PathType Leaf)) {
            throw "Update package is incomplete; missing $relative"
        }
        $destinationFile = Join-Path $To $relative
        $destinationParent = Split-Path -Parent $destinationFile
        if (-not (Test-Path -LiteralPath $destinationParent)) {
            New-Item -ItemType Directory -Path $destinationParent -Force | Out-Null
        }
        $temporaryFile = "$destinationFile.patch_new"
        Copy-Item -LiteralPath $sourceFile -Destination $temporaryFile -Force
        Move-Item -LiteralPath $temporaryFile -Destination $destinationFile -Force
    }
}

function Restore-ManagedFiles {
    param([string]$To, [string]$Backup)
    Write-PatchLog 'Restoring the previous application files...'
    foreach ($relative in $ManagedFiles) {
        $destinationFile = Join-Path $To $relative
        $backupFile = Join-Path $Backup $relative
        if (Test-Path -LiteralPath $backupFile -PathType Leaf) {
            Copy-Item -LiteralPath $backupFile -Destination $destinationFile -Force
        } elseif ($PreExisting.ContainsKey($relative) -and -not $PreExisting[$relative]) {
            Remove-Item -LiteralPath $destinationFile -Force -ErrorAction SilentlyContinue
        }
        Remove-Item -LiteralPath "$destinationFile.patch_new" -Force -ErrorAction SilentlyContinue
    }

    $backupRuntimePath = Join-Path $Backup 'runtime_path.txt'
    if (Test-Path -LiteralPath $backupRuntimePath -PathType Leaf) {
        Copy-Item -LiteralPath $backupRuntimePath -Destination (Join-Path $To 'runtime_path.txt') -Force
    }

    $backupInstalledVersion = Join-Path $Backup 'installed_version.txt'
    if (Test-Path -LiteralPath $backupInstalledVersion -PathType Leaf) {
        Copy-Item -LiteralPath $backupInstalledVersion -Destination (Join-Path $To 'installed_version.txt') -Force
    } elseif (-not $script:InstalledVersionExisted) {
        Remove-Item -LiteralPath (Join-Path $To 'installed_version.txt') -Force -ErrorAction SilentlyContinue
    }
    $backupInstallState = Join-Path $Backup 'install_state.json'
    if (Test-Path -LiteralPath $backupInstallState -PathType Leaf) {
        Copy-Item -LiteralPath $backupInstallState -Destination (Join-Path $To 'install_state.json') -Force
    } else {
        Remove-Item -LiteralPath (Join-Path $To 'install_state.json') -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-RuntimeUpdate {
    param([string]$Folder)
    $installer = Join-Path $Folder 'install.bat'
    if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {
        throw 'The patched install.bat is missing.'
    }

    $arguments = "/d /c `"`"$installer`" --no-pause --skip-action-menu --patch-runtime`""
    $process = Start-Process -FilePath $env:ComSpec -ArgumentList $arguments -Wait -PassThru -NoNewWindow
    if ($process.ExitCode -ne 0) {
        throw "Runtime update failed with exit code $($process.ExitCode)."
    }
}

function Copy-LegacyData {
    param([string]$OldFolder, [string]$OldVersion)
    $runtimeFile = Join-Path $OldFolder 'runtime_path.txt'
    if (-not (Test-Path -LiteralPath $runtimeFile -PathType Leaf)) { return }

    $runtimeRoot = (Get-Content -LiteralPath $runtimeFile -Raw).Trim()
    if (-not $runtimeRoot) { return }

    $dataDir = Join-Path $runtimeRoot 'Data'
    $exportsDir = Join-Path $runtimeRoot 'Exports'
    New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
    New-Item -ItemType Directory -Path $exportsDir -Force | Out-Null

    $dataCandidates = @(
        (Join-Path $OldFolder 'settings.json'),
        (Join-Path $OldFolder 'arena_id_lookup.json'),
        (Join-Path $OldFolder 'last_anchors.json'),
        (Join-Path $OldFolder 'MTGA Collection Reader\settings.json'),
        (Join-Path $OldFolder 'MTGA Collection Reader\arena_id_lookup.json'),
        (Join-Path $OldFolder 'MTGA Collection Reader\last_anchors.json'),
        (Join-Path $OldFolder 'MTGA Collection Exporter\settings.json'),
        (Join-Path $OldFolder 'MTGA Collection Exporter\arena_id_lookup.json'),
        (Join-Path $OldFolder 'MTGA Collection Exporter\last_anchors.json'),
        (Join-Path $env:LOCALAPPDATA 'MTGA Collection Reader\settings.json'),
        (Join-Path $env:LOCALAPPDATA 'MTGA Collection Exporter\settings.json'),
        (Join-Path $env:LOCALAPPDATA 'MTGA Collection Reader\arena_id_lookup.json'),
        (Join-Path $env:LOCALAPPDATA 'MTGA Collection Exporter\arena_id_lookup.json'),
        (Join-Path $env:LOCALAPPDATA 'MTGA Collection Reader\last_anchors.json'),
        (Join-Path $env:LOCALAPPDATA 'MTGA Collection Exporter\last_anchors.json')
    )
    foreach ($candidate in $dataCandidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $destination = Join-Path $dataDir (Split-Path -Leaf $candidate)
            if (-not (Test-Path -LiteralPath $destination)) {
                Copy-Item -LiteralPath $candidate -Destination $destination -Force
                Write-PatchLog "Preserved legacy data: $(Split-Path -Leaf $candidate)"
            }
        }
    }

    $legacyFiles = @(
        'mtga_collection.txt',
        'mtga_collection.json',
        'mtga_collection.csv',
        'mtga_export_report.json'
    )
    $legacyFiles += @(Get-ChildItem -LiteralPath $OldFolder -File -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -like 'mtga_export_*.log' -or $_.Name -like 'mtga_visual_*.log'
    } | ForEach-Object { $_.Name })

    $legacyFiles = @($legacyFiles | Select-Object -Unique)
    $foundLegacy = $false
    foreach ($name in $legacyFiles) {
        if (Test-Path -LiteralPath (Join-Path $OldFolder $name) -PathType Leaf) {
            $foundLegacy = $true
            break
        }
    }
    if (Test-Path -LiteralPath (Join-Path $OldFolder 'visual_review') -PathType Container) {
        $foundLegacy = $true
    }

    if ($foundLegacy) {
        $importFolder = Join-Path $exportsDir ("Imported from v{0} {1}" -f $OldVersion, $Timestamp)
        New-Item -ItemType Directory -Path $importFolder -Force | Out-Null
        foreach ($name in $legacyFiles) {
            $source = Join-Path $OldFolder $name
            if (Test-Path -LiteralPath $source -PathType Leaf) {
                Copy-Item -LiteralPath $source -Destination (Join-Path $importFolder $name) -Force
            }
        }
        $reviewSource = Join-Path $OldFolder 'visual_review'
        if (Test-Path -LiteralPath $reviewSource -PathType Container) {
            Copy-Item -LiteralPath $reviewSource -Destination (Join-Path $importFolder 'visual_review') -Recurse -Force
        }
        Write-PatchLog "Copied legacy exports into $importFolder"
    }
}

try {
    Write-Host ''
    Write-Host ' MTGA Collection Reader - Patch Older Install'
    Write-Host ' ================================================='
    Write-Host ''

    if (-not $TargetPath) {
        $TargetPath = Select-InstallFolder
    }
    if (-not $TargetPath) {
        throw 'No older installation folder was selected.'
    }

    $Target = [System.IO.Path]::GetFullPath($TargetPath.Trim('"'))
    $SourceResolved = [System.IO.Path]::GetFullPath($SourcePath)
    if ($Target.TrimEnd('\') -ieq $SourceResolved.TrimEnd('\')) {
        throw 'Choose the separate older installation folder, not the v5.0 update-package folder.'
    }
    if (-not (Test-Path -LiteralPath $Target -PathType Container)) {
        throw "The selected folder does not exist: $Target"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $Target 'mtg.py') -PathType Leaf)) {
        throw 'That folder does not appear to be an MTGA Collection Reader or Exporter installation; mtg.py was not found.'
    }
    if (Test-ExporterRunning -Folder $Target) {
        throw 'The exporter appears to be running from the selected folder. Close it before patching.'
    }

    $OldVersion = Get-ExporterVersion -Folder $Target
    Write-PatchLog "Selected installation: $Target"
    Write-PatchLog "Detected version: $OldVersion"
    Write-PatchLog "Patch package version: $NewVersion"

    if ((Test-VersionAtLeast -Left $OldVersion -Right $NewVersion) -and -not $ForcePatch) {
        throw "The selected installation is already version $OldVersion or newer. Use --force only for a repair patch."
    }

    $BackupRoot = Join-Path $Target ("Patch Backups\v{0}_to_v{1}_{2}" -f $OldVersion, $NewVersion, $Timestamp)
    New-Item -ItemType Directory -Path $BackupRoot -Force | Out-Null
    Write-PatchLog "Backup: $BackupRoot"

    Copy-ManagedFilesToBackup -From $Target -Backup $BackupRoot
    Install-NewManagedFiles -From $SourcePath -To $Target
    Write-PatchLog 'Application files copied. Updating the private runtime...'

    Invoke-RuntimeUpdate -Folder $Target
    Copy-LegacyData -OldFolder $Target -OldVersion $OldVersion

    Set-Content -LiteralPath (Join-Path $Target 'installed_version.txt') -Value $NewVersion -Encoding ASCII
    $state = [ordered]@{
        app_name = [string]$Manifest.app_name
        version = $NewVersion
        patched_from = $OldVersion
        patched_at = (Get-Date).ToString('o')
        backup_path = $BackupRoot
        runtime_path_file = (Join-Path $Target 'runtime_path.txt')
    }
    $state | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $Target 'install_state.json') -Encoding UTF8

    $PatchSucceeded = $true
    Write-PatchLog "Patch complete: v$OldVersion to v$NewVersion"
    Write-PatchLog 'Settings, runtime location, exports, and review files were preserved.'
    Write-PatchLog 'Start the patched copy with run_reader.bat in the older installation folder.'
} catch {
    Write-Host ''
    Write-PatchLog ("PATCH FAILED: " + $_.Exception.Message)
    if ($Target -and $BackupRoot -and (Test-Path -LiteralPath $BackupRoot -PathType Container)) {
        try {
            Restore-ManagedFiles -To $Target -Backup $BackupRoot
            Write-PatchLog 'Application-file rollback completed. Runtime data and exports were not deleted.'
        } catch {
            Write-PatchLog ("Rollback also encountered an error: " + $_.Exception.Message)
        }
    }
} finally {
    if ($Target -and (Test-Path -LiteralPath $Target -PathType Container)) {
        $logPath = Join-Path $Target ("patch_{0}_to_{1}_{2}.log" -f $(if ($OldVersion) { $OldVersion } else { 'unknown' }), $NewVersion, $Timestamp)
        try { $LogLines | Set-Content -LiteralPath $logPath -Encoding UTF8 } catch { }
    }
    Write-Host ''
    Pause-IfNeeded
}

if ($PatchSucceeded) { exit 0 }
exit 1
