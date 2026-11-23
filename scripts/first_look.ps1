# Day-one report for the game executable: PE inspection, encryption check, import surface and a loader dry run.
# Reads the exe only; no game code runs.
param(
    [Parameter(Mandatory)][string]$Exe,
    [string]$GameDir = (Split-Path $Exe -Parent)
)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$name = [IO.Path]::GetFileNameWithoutExtension($Exe)
$out = Join-Path $root "reports\exe\$name"
New-Item -ItemType Directory -Force $out | Out-Null

if (-not (Test-Path $Exe)) { throw "$Exe not found" }
$fs = [IO.File]::OpenRead($Exe); $mz = New-Object byte[] 2; [void]$fs.Read($mz, 0, 2); $fs.Close()
if ([Text.Encoding]::ASCII.GetString($mz) -ne "MZ") {
    throw "$Exe doesn't start with an MZ header. It's still packaged or encrypted, so there's nothing to inspect."
}

$size = (Get-Item $Exe).Length
$hash = (Get-FileHash $Exe -Algorithm SHA256).Hash.ToLower()
"sha256 $hash  size $size" | Tee-Object (Join-Path $out "hash.txt")

Write-Host "`n== PE inspection"
python -I (Join-Path $root "tools\pe_inspect.py") $Exe --game-dir $GameDir --out $out
if ($LASTEXITCODE -ne 0) { throw "pe_inspect failed" }

Write-Host "`n== GDK runtime classes and slots called through xgameruntime.lib (read from the file)"
python -I (Join-Path $root "tools\gdk_runtime_calls.py") $Exe --out $out
if ($LASTEXITCODE -ne 0) { Write-Warning "gdk_runtime_calls could not read all of $Exe; its report may be incomplete" }

Write-Host "`n== Archetype lookup candidates for mods (docs/modding.md; pass the top RVA to gta6rt --archetype-lookup)"
python -I (Join-Path $root "tools\find_hash_lookup.py") $Exe --json (Join-Path $out "hash_lookup.json")
if ($LASTEXITCODE -ne 0) { Write-Warning "find_hash_lookup could not scan $Exe" }

Write-Host "`n== What to confirm against this executable (every ASSUMPTION / UNVERIFIED / VERIFY note; called ones first)"
$calls = Join-Path $out ((Split-Path $Exe -Leaf) + ".gdk_calls.json")
$checklistArgs = @("--out", (Join-Path $out "checklist.md"))
if (Test-Path $calls) { $checklistArgs += @("--gdk-calls", $calls) }
python -I (Join-Path $root "tools\exe_checklist.py") @checklistArgs
if ($LASTEXITCODE -ne 0) { Write-Warning "exe_checklist could not write the checklist" }

$rt = Join-Path $root "runtime\build\gta6rt.exe"
$tlsHost = Join-Path $root "runtime\build\gta6rt_tls_host.dll"
$runtimeFiles = @($rt, $tlsHost)
$missing = @($runtimeFiles | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Leaf) })
# A gta6rt.exe older than the runtime sources (a pull since the last build) would report the old shim table, so it
# is rebuilt then too, not only when missing.
$sources = @("runtime\src", "runtime\shims", "runtime\CMakeLists.txt") | ForEach-Object { Join-Path $root $_ } |
    Where-Object { Test-Path $_ }
$newest = $sources | Get-ChildItem -Recurse -File -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
$stale = ($missing.Count -eq 0) -and $newest -and ($newest.LastWriteTimeUtc -gt (Get-Item -LiteralPath $rt).LastWriteTimeUtc)
if ($stale) { Write-Host "runtime\build\gta6rt.exe is older than $($newest.Name): rebuilding it first" }
if ($missing.Count -gt 0 -or $stale) {
    if ($missing.Count -gt 0) { Write-Host "Missing runtime files: $($missing -join ', '); rebuilding the runtime" }
    & (Join-Path $PSScriptRoot "build_runtime.ps1")
    foreach ($file in $runtimeFiles) {
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "runtime build did not produce $file" }
    }
}
Write-Host "`n== Loader dry run (map, relocate, bind imports; nothing executes)"
& {
    # Keep native stderr in the report without letting Windows PowerShell stop the dry run on it.
    $ErrorActionPreference = "Continue"
    $PSNativeCommandUseErrorActionPreference = $false
    & $rt --dry-run $Exe --game-dir $GameDir 2>&1
} | Tee-Object (Join-Path $out "dryrun.txt") | Select-Object -Last 40
$dryRunExitCode = $LASTEXITCODE

# Shim to-do list, grouped by DLL, most functions first.
$report = Get-Content (Join-Path $out "$([IO.Path]::GetFileName($Exe)).json") -Raw | ConvertFrom-Json
$todo = $report.imports | Where-Object { $_.where -eq "shim" } | Sort-Object { -$_.functions.Count }
$lines = foreach ($t in $todo) { "{0,-40} {1,5}" -f $t.dll, $t.functions.Count }
Set-Content -LiteralPath (Join-Path $out "shim-todo.txt") -Value $lines
Write-Host "`n== Shim to-do (DLL, function count)"
$lines
$enc = $report.sections | Where-Object { $_.looks_encrypted } | ForEach-Object { $_.name }
if ($enc) { Write-Warning "High-entropy sections (still encrypted or packed): $($enc -join ', ')" }
Write-Host "`nReports: $out"
exit $dryRunExitCode
