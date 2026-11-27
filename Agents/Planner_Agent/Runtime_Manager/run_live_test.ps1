# Live test launcher: start GoModel from C:\Projects\NTG (.env + config.yaml) and run live_test.py.
# Everything is logged to Runtime_Manager\live_test_logs\ (no secrets are printed).

$ErrorActionPreference = 'Continue'
$Here    = Split-Path -Parent $MyInvocation.MyCommand.Path            # ...\Runtime_Manager
$NtgRoot = (Resolve-Path (Join-Path $Here '..\..\..')).Path            # C:\Projects\NTG
$Logs    = Join-Path $Here 'live_test_logs'
New-Item -ItemType Directory -Force -Path $Logs | Out-Null
Start-Transcript -Path (Join-Path $Logs 'run.log') -Force | Out-Null

function Log($m) { Write-Host "[live-test] $m" }
Log "NTG root: $NtgRoot"
$free = (Get-PSDrive -Name ($NtgRoot.Substring(0,1))).Free
Log ("Free disk on {0}: {1:N1} GB" -f $NtgRoot.Substring(0,1), ($free / 1GB))

# ---------------------------------------------------------------- 1. find GoModel
$gm = $null
$cmd = Get-Command gomodel -ErrorAction SilentlyContinue
if ($cmd) { $gm = $cmd.Source }
foreach ($c in @("$env:LOCALAPPDATA\Programs\gomodel\gomodel.exe", "$NtgRoot\gomodel.exe", "$NtgRoot\bin\gomodel.exe")) {
    if (-not $gm -and (Test-Path $c)) { $gm = $c }
}

function Test-Health {
    try { (Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 -Uri 'http://localhost:8080/health').StatusCode -eq 200 }
    catch { $false }
}

$alreadyUp = Test-Health
if ($alreadyUp) {
    Log "GoModel is already running on :8080 - reusing it (restart it yourself if you changed .env)."
} else {
    if (-not $gm) {
        Log "gomodel.exe not found (PATH, %LOCALAPPDATA%\Programs\gomodel, $NtgRoot)."
        $ans = Read-Host "Download the official GoModel Windows release from github.com/ENTERPILOT/GoModel (~40 MB, checksum-verified) into %LOCALAPPDATA%\Programs\gomodel? [y/N]"
        if ($ans -notmatch '^(y|yes)$') { Log "Not installing. Install GoModel or put gomodel.exe in $NtgRoot and re-run."; Stop-Transcript | Out-Null; Read-Host 'Press Enter to close'; exit 1 }
        $req = [System.Net.WebRequest]::Create('https://github.com/ENTERPILOT/GoModel/releases/latest')
        $req.AllowAutoRedirect = $false
        $res = $req.GetResponse(); $tag = ($res.Headers['Location'] -split '/')[-1]; $res.Close()
        $ver = $tag.TrimStart('v')
        $zip = "gomodel_${ver}_windows_amd64.zip"
        $tmp = Join-Path $env:TEMP "gomodel-$ver"; New-Item -ItemType Directory -Force -Path $tmp | Out-Null
        Log "Downloading GoModel $tag ..."
        Invoke-WebRequest -UseBasicParsing "https://github.com/ENTERPILOT/GoModel/releases/download/$tag/$zip" -OutFile "$tmp\$zip"
        Invoke-WebRequest -UseBasicParsing "https://github.com/ENTERPILOT/GoModel/releases/download/$tag/checksums.txt" -OutFile "$tmp\checksums.txt"
        $expected = (Get-Content "$tmp\checksums.txt" | Where-Object { $_ -match [regex]::Escape($zip) } | ForEach-Object { ($_ -split '\s+')[0] })
        $actual = (Get-FileHash "$tmp\$zip" -Algorithm SHA256).Hash.ToLower()
        if (-not $expected -or $actual -ne $expected.ToLower()) { Log "CHECKSUM MISMATCH - aborting."; Stop-Transcript | Out-Null; Read-Host 'Press Enter'; exit 1 }
        Log "Checksum verified."
        Expand-Archive "$tmp\$zip" -DestinationPath $tmp -Force
        $dest = "$env:LOCALAPPDATA\Programs\gomodel"; New-Item -ItemType Directory -Force -Path $dest | Out-Null
        Copy-Item "$tmp\gomodel.exe" "$dest\gomodel.exe" -Force
        $gm = "$dest\gomodel.exe"
    }
    Log "Using $gm"
    & $gm --version 2>&1 | ForEach-Object { Log "version: $_" }

    # ------------------------------------------------------------ 2. start GoModel
    $out = Join-Path $Logs 'gomodel.out.log'; $err = Join-Path $Logs 'gomodel.err.log'
    $p = Start-Process -FilePath $gm -WorkingDirectory $NtgRoot -RedirectStandardOutput $out -RedirectStandardError $err -WindowStyle Minimized -PassThru
    Set-Content -Path (Join-Path $Logs 'gomodel.pid') -Value $p.Id
    Log "Started GoModel (PID $($p.Id)) in $NtgRoot - waiting for /health (MCP stdio servers can take a while on first start)..."
    $ok = $false
    for ($i = 0; $i -lt 90; $i++) {
        Start-Sleep -Seconds 2
        if ($p.HasExited) { Log "GoModel exited with code $($p.ExitCode) - see gomodel.err.log"; break }
        if (Test-Health) { $ok = $true; break }
    }
    if (-not $ok) { Log "GoModel did not become healthy. Last log lines:"; Get-Content $err -Tail 40 -ErrorAction SilentlyContinue; Get-Content $out -Tail 20 -ErrorAction SilentlyContinue }
    else { Log "GoModel healthy after $([int](($i+1)*2))s"; Start-Sleep -Seconds 8 }   # let MCP servers finish connecting
}

# ---------------------------------------------------------------- 3. python live test
$py = $null; $pyArgs = @()
foreach ($cand in @('py -3.13', 'py -3', 'python', 'python3')) {
    $parts = $cand -split ' '
    $exe = $parts[0]; $extra = @($parts | Select-Object -Skip 1)
    try {
        $v = & $exe @extra -c "import sys;print(sys.version_info>=(3,11))" 2>$null
        if ($v -eq 'True') { $py = $exe; $pyArgs = $extra; break }
    } catch {}
}
if (-not $py) { Log "Python 3.11+ not found."; Stop-Transcript | Out-Null; Read-Host 'Press Enter'; exit 1 }
Log "Python: $py $($pyArgs -join ' ')"
$env:PYTHONUTF8 = '1'; $env:PYTHONIOENCODING = 'utf-8'
Push-Location $Here
& $py @pyArgs (Join-Path $Here 'live_test.py') 2>&1 | Tee-Object -FilePath (Join-Path $Logs 'live_test.log')
Pop-Location

Log "Done. GoModel is left running for the Planner Agent (dashboard: http://localhost:8080/admin/dashboard)."
Log "Stop it with: Stop-Process -Id (Get-Content '$Logs\gomodel.pid')"
Stop-Transcript | Out-Null
Read-Host 'Press Enter to close'
