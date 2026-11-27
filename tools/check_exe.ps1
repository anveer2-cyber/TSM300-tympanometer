# Tries dist\TSM300.exe the way a PC without Python runs it: its version, a real result
# decoded and drawn, the audiometer's sheet and simulated reading, the window opening, and the program replacing itself with the newest
# published release. Run from the repository folder after tools/build_exe.py; the last part
# needs the internet. Stops with an error at the first thing that does not work.
$ErrorActionPreference = "Stop"

$exe = (Resolve-Path "dist\TSM300.exe").Path
$version = (Select-String -Path "src\tsm300\__init__.py" -Pattern '__version__ = "(.+)"').Matches[0].Groups[1].Value
$work = Join-Path $PWD "build\check"
Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $work | Out-Null
$env:TSM300_DIR = Join-Path $work "settings"                    # keep this PC's own settings and results out of it
Remove-Item Env:QT_QPA_PLATFORM -ErrorAction SilentlyContinue   # the real Windows display plug-in has to load

function Run-Program([string] $program, [string[]] $arguments, [string] $name) {
    # Waits for the program itself only: one of the runs leaves a window open behind it.
    $out = Join-Path $work "$name.out"
    $err = Join-Path $work "$name.err"
    $process = Start-Process $program -ArgumentList $arguments -PassThru -NoNewWindow `
        -RedirectStandardOutput $out -RedirectStandardError $err
    $null = $process.Handle   # without this the exit code is lost once the process has ended
    $process.WaitForExit()
    if ($process.ExitCode -ne 0) {
        Get-Content $out, $err -Encoding utf8
        throw "'$name' ended with exit code $($process.ExitCode)"
    }
    return [string](Get-Content $out -Raw -Encoding utf8)
}

function Wait-ForWindow([string] $wanted) {
    # Waits for the program's window by its title, closes the program, and fails if it wrote an error log.
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
    if ($title -notlike $wanted) { throw "no window titled '$wanted' within 90 seconds (title seen: '$title')" }
}

# --- its version, through a redirected stream
$said = (Run-Program $exe @("--version") "version").Trim()
if ($said -ne $version) { throw "the program says version '$said', the source says '$version'" }

# --- a transmission captured from the real device, decoded and drawn
$results = Join-Path $work "results"
Run-Program $exe @("decode", "tests\data\both_ears.bin", "--out", $results, "--chart", "00512") "decode" | Out-Null
$sheet = Get-ChildItem $results -Filter *.jpg | Select-Object -First 1
if (-not $sheet -or $sheet.Length -lt 100000) { throw "no result sheet was drawn" }

# --- the audiometer: a sample sheet, the whole reading of the simulated device with its byte log,
# and Windows' USB device list read through winusb (no audiometer on this PC, so it ends with 1)
$demo = Join-Path $work "hearing.jpg"
Run-Program $exe @("hearing-demo", $demo) "hearing-demo" | Out-Null
if (-not (Test-Path $demo) -or (Get-Item $demo).Length -lt 100000) { throw "no hearing sheet was drawn" }
$hearingLog = Join-Path $work "hearing-log"
$said = Run-Program $exe @("hearing-check", "--simulator", "--out", $hearingLog) "hearing-check"
if ($said -notmatch "R37A21C000777") { throw "the simulated audiometer was not read: $said" }
if (-not (Get-ChildItem $hearingLog -Filter *.txt)) { throw "no byte log of the audiometer was written" }
$usbOut = Join-Path $work "hearing-usb.out"
$process = Start-Process $exe -ArgumentList @("hearing-check") -PassThru -NoNewWindow `
    -RedirectStandardOutput $usbOut -RedirectStandardError (Join-Path $work "hearing-usb.err")
$null = $process.Handle
$process.WaitForExit()
$said = [string](Get-Content $usbOut -Raw -Encoding utf8)
if ($said -notmatch "CF6C4074") { throw "the USB device list could not be read: $said" }

# --- the window
Start-Process $exe | Out-Null
Wait-ForWindow "TSM-300*v$version"

# --- updating: a copy replaces itself with the newest published release and starts it
$latest = Invoke-WebRequest "https://github.com/anveer2-cyber/TSM300-tympanometer/releases/latest" -Method Head
$published = $latest.BaseResponse.RequestMessage.RequestUri.AbsolutePath -replace '^.*/tag/v', ''
if ($published -notmatch '^\d+(\.\d+)+$') { throw "could not tell the newest published version ('$published')" }
$app = Join-Path $work "app"
New-Item -ItemType Directory -Force $app | Out-Null
$copy = Join-Path $app "TSM300.exe"
Copy-Item $exe $copy
Run-Program $copy @("update", "--again", "--restart") "update" | Out-Null
Wait-ForWindow "TSM-300*v$published"
$said = (Run-Program $copy @("--version") "updated").Trim()
if ($said -ne $published) { throw "after updating the program says '$said', the published version is '$published'" }

$size = [math]::Round((Get-Item $exe).Length / 1MB)
Write-Output "TSM300.exe v$version ($size MB): version, result sheet, audiometer, window and self-update (to v$published) all work"
