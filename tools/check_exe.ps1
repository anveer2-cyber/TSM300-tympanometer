# Tries dist\TSM300.exe the way a PC without Python runs it: its version, a real result
# decoded and drawn, and the window opening. Run from the repository folder after
# tools/build_exe.py. Stops with an error at the first thing that does not work.
$ErrorActionPreference = "Stop"

$exe = (Resolve-Path "dist\TSM300.exe").Path
$version = (Select-String -Path "src\tsm300\__init__.py" -Pattern '__version__ = "(.+)"').Matches[0].Groups[1].Value
$work = Join-Path $PWD "build\check"
Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $work | Out-Null
$env:TSM300_DIR = Join-Path $work "settings"                    # keep this PC's own settings and results out of it
Remove-Item Env:QT_QPA_PLATFORM -ErrorAction SilentlyContinue   # the real Windows display plug-in has to load

function Run-Exe([string[]] $arguments, [string] $name) {
    $out = Join-Path $work "$name.out"
    $err = Join-Path $work "$name.err"
    $process = Start-Process $exe -ArgumentList $arguments -Wait -PassThru -NoNewWindow `
        -RedirectStandardOutput $out -RedirectStandardError $err
    if ($process.ExitCode -ne 0) {
        Get-Content $err
        throw "'$name' ended with exit code $($process.ExitCode)"
    }
    return [string](Get-Content $out -Raw -Encoding utf8)
}

$said = (Run-Exe @("--version") "version").Trim()
if ($said -ne $version) { throw "the program says version '$said', the source says '$version'" }

$results = Join-Path $work "results"
Run-Exe @("decode", "tests\data\both_ears.bin", "--out", $results, "--chart", "00512") "decode" | Out-Null
$sheet = Get-ChildItem $results -Filter *.jpg | Select-Object -First 1
if (-not $sheet -or $sheet.Length -lt 100000) { throw "no result sheet was drawn" }

Start-Process $exe | Out-Null
$wanted = "TSM-300*v$version"
$title = ""
foreach ($second in 1..90) {
    Start-Sleep -Seconds 1
    $title = (Get-Process TSM300 -ErrorAction SilentlyContinue | Where-Object MainWindowTitle |
        Select-Object -First 1).MainWindowTitle
    if ($title -like $wanted) { break }
}
Get-Process TSM300 -ErrorAction SilentlyContinue | Stop-Process -Force
$log = Join-Path $env:TSM300_DIR "error.log"
if (Test-Path $log) {
    Get-Content $log -Encoding utf8
    throw "the program reported an error while starting"
}
if ($title -notlike $wanted) { throw "the window did not open within 90 seconds (window title seen: '$title')" }

$size = [math]::Round((Get-Item $exe).Length / 1MB)
Write-Output "TSM300.exe v$version ($size MB): the version, a decoded result sheet and the window all work"
