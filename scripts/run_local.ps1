<#
.SYNOPSIS
    Windows PowerShell 로컬 실행기 — 로그인 없이 member 권한으로 Dashboard를 띄운다.

.DESCRIPTION
    bash 전용 `set -a; source .env.local; set +a` 흐름을 PowerShell로 대체한다.
    앱은 .env 파일을 자동 로드하지 않으므로(load_settings는 os.environ만 읽음), 이 스크립트가
    .env.local의 환경변수를 현재 프로세스에 주입한 뒤 uvicorn을 기동한다. 그래야
    AUTH_MODE=local_auto 가 앱에 전달되어 loopback 최초 접속 때 일반 회원(member) 세션이
    자동 발급된다.

    macOS/Linux(bash)에서는 이 스크립트 대신 docs/local-analysis-human-guide.md 의
    bash 절차를 사용한다.

    Windows PowerShell(기본 5.1)은 .ps1 실행을 정책으로 막을 수 있으므로 -ExecutionPolicy
    Bypass로 실행한다. pwsh(PowerShell 7)가 설치돼 있으면 `pwsh -File ...`도 가능하다.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\run_local.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\run_local.ps1 -Port 8890
#>
[CmdletBinding()]
param(
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 8765,
    [switch]$NoReload,
    [switch]$SkipDbInit
)

$ErrorActionPreference = "Stop"

# 안정 버전 Python 런처 플래그를 고른다. 매우 최신 버전(예: 3.14)은 pydantic_core·psycopg
# 등의 네이티브 휠이 아직 없을 수 있어, 휠이 준비된 3.13 → 3.12 → 3.11 순으로 선택한다.
function Resolve-StablePython {
    foreach ($v in '3.13', '3.12', '3.11', '3') {
        try { & py "-$v" --version *> $null; if ($LASTEXITCODE -eq 0) { return "-$v" } } catch { break }
    }
    return $null
}

# 스크립트 위치 기준으로 dashboard 루트를 계산한다(어느 위치에서 실행해도 동작).
$DashboardRoot = Split-Path -Parent $PSScriptRoot
Set-Location $DashboardRoot

$EnvExample = Join-Path $DashboardRoot ".env.local.example"
$EnvLocal   = Join-Path $DashboardRoot ".env.local"

# 1) .env.local 준비 — 없으면 견본에서 복사(기존 파일은 덮어쓰지 않는다).
if (-not (Test-Path $EnvLocal)) {
    if (-not (Test-Path $EnvExample)) {
        throw ".env.local.example 이 없습니다. dashboard 저장소가 최신 dev 인지 확인하세요."
    }
    Copy-Item $EnvExample $EnvLocal
    Write-Host "[run_local] .env.local 생성 (원본: .env.local.example)" -ForegroundColor Cyan
}

# 2) .env.local 을 현재 프로세스 환경에 주입 — bash `source` 대체.
$loaded = 0
foreach ($line in Get-Content -LiteralPath $EnvLocal) {
    $trimmed = $line.Trim()
    if ($trimmed.Length -eq 0 -or $trimmed.StartsWith("#")) { continue }
    $idx = $trimmed.IndexOf("=")
    if ($idx -lt 1) { continue }
    $name = $trimmed.Substring(0, $idx).Trim()
    $value = $trimmed.Substring($idx + 1).Trim()
    # 견본은 따옴표를 쓰지 않지만, 감싼 따옴표가 있으면 제거한다.
    if ($value.Length -ge 2 -and
        (($value.StartsWith('"') -and $value.EndsWith('"')) -or
         ($value.StartsWith("'") -and $value.EndsWith("'")))) {
        $value = $value.Substring(1, $value.Length - 2)
    }
    Set-Item -LiteralPath "env:$name" -Value $value
    $loaded++
}
Write-Host "[run_local] .env.local 환경변수 $loaded개 로드 (AUTH_MODE=$($env:AUTH_MODE))" -ForegroundColor Cyan

if ($env:AUTH_MODE -ne "local_auto") {
    Write-Warning "AUTH_MODE 가 local_auto 가 아닙니다. 로그인 없이 접근하려면 .env.local 의 AUTH_MODE=local_auto 를 확인하세요."
}

# 3) venv 확보 — 없으면 안정 버전 Python으로 생성하고 의존성을 설치한다.
$VenvPython = Join-Path $DashboardRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    $ver = Resolve-StablePython
    if ($ver) { Write-Host "[run_local] .venv 없음 — py $ver 로 생성" -ForegroundColor Cyan; & py $ver -m venv .venv }
    else      { Write-Host "[run_local] .venv 없음 — python 으로 생성" -ForegroundColor Cyan; & python -m venv .venv }
    & $VenvPython -m pip install --upgrade pip -q
    & $VenvPython -m pip install -r requirements.txt
}

# 3-1) 네이티브 확장 사전점검 — venv Python과 설치된 바이너리(pydantic_core 등)의 정합 확인.
# 예: venv를 3.14로 다시 만들었는데 site-packages에 3.13용 .pyd가 남으면 여기서 걸린다.
& $VenvPython -c "import fastapi" *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Warning "venv의 Python과 설치된 네이티브 패키지(pydantic_core 등)가 맞지 않습니다 (ModuleNotFoundError: pydantic_core._pydantic_core 등)."
    Write-Host "  .venv를 안정 버전 Python으로 새로 만드세요:" -ForegroundColor Yellow
    Write-Host "    Remove-Item .venv -Recurse -Force" -ForegroundColor Yellow
    Write-Host "    py -3.13 -m venv .venv" -ForegroundColor Yellow
    Write-Host "    .venv\Scripts\python -m pip install -r requirements.txt" -ForegroundColor Yellow
    exit 1
}

# 4) 인증·권한 스키마 초기화(비-production 기동 시 앱도 자동 수행하지만 명시적으로 한 번 더).
if (-not $SkipDbInit) {
    Write-Host "[run_local] 인증 DB 초기화 (scripts/init_auth_db.py)" -ForegroundColor Cyan
    & $VenvPython (Join-Path $DashboardRoot "scripts\init_auth_db.py")
}

# 5) uvicorn 기동 — reload 자식 프로세스도 위에서 주입한 환경변수를 상속한다.
$uvArgs = @("-m", "uvicorn", "app.main:app", "--host", $BindHost, "--port", "$Port")
if (-not $NoReload) { $uvArgs += "--reload" }

Write-Host "[run_local] uvicorn 기동 → http://$BindHost`:$Port/charts (로그인 없이 접근)" -ForegroundColor Green
& $VenvPython @uvArgs
