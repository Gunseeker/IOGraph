param(
    [string]$Python = ".venv\Scripts\python.exe"
)
$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
try {
    # Same onedir/runtime layout and Qt collection as Release Windows.
    $buildVersion = (Get-Content VERSION -Raw).Trim() + "-raw-data"
    $workPath = Join-Path $repoRoot "build\raw-data"
    New-Item -ItemType Directory -Path $workPath -Force | Out-Null
    [IO.File]::WriteAllText((Join-Path $workPath "VERSION"), $buildVersion)
    $match = [regex]::Match($buildVersion, '^(\d+)\.(\d+)\.(\d+)')
    if (-not $match.Success) { throw "VERSION must begin with major.minor.patch" }
    $tuple = ($match.Groups[1..3].Value -join ', ') + ', 0'
    $resource = @"
VSVersionInfo(
  ffi=FixedFileInfo(filevers=($tuple), prodvers=($tuple), mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable(u'040904B0', [
    StringStruct(u'CompanyName', u'Gunseeker'),
    StringStruct(u'FileDescription', u'IOGraph Raw Data Fork'),
    StringStruct(u'FileVersion', u'$buildVersion'),
    StringStruct(u'ProductName', u'IOGraph Raw Data Fork'),
    StringStruct(u'ProductVersion', u'$buildVersion')])]),
    VarFileInfo([VarStruct(u'Translation', [1033, 1200])])])
"@
    [IO.File]::WriteAllText((Join-Path $workPath "version_info.txt"), $resource)
    & $Python -m PyInstaller --noconfirm --windowed --name IOGraph --contents-directory runtime `
        --distpath dist/raw-data --workpath build/raw-data/work --specpath build/raw-data `
        --icon "$repoRoot/iograph/resources/icon256.png" --version-file "$workPath/version_info.txt" `
        --add-data "$repoRoot/iograph/resources;iograph/resources" --add-data "$workPath/VERSION;." `
        --collect-all PyQt6 "$repoRoot/run_iograph.py"
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
    # Qt uses the Windows ICU API. A bundled Python development runtime may
    # contribute a different icuuc.dll with version-suffixed exports; let Qt
    # load the OS implementation rather than that incompatible DLL.
    $bundledIcu = Join-Path $repoRoot "dist\raw-data\IOGraph\runtime\icuuc.dll"
    if (Test-Path -LiteralPath $bundledIcu) {
        Remove-Item -LiteralPath $bundledIcu
    }
    $zipPath = Join-Path $repoRoot "dist\IOGraph-windows-$buildVersion.zip"
    Compress-Archive -Path dist/raw-data/IOGraph/* -DestinationPath $zipPath -Force
    Write-Output "Executable: $repoRoot\dist\raw-data\IOGraph\IOGraph.exe"
    Write-Output "ZIP: $zipPath"
} finally {
    Pop-Location
}
