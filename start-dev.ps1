param(
    [switch]$NoBrowser,
    [switch]$CheckOnly,
    [switch]$SkipFrontendBuild
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host "[INFO] $Message"
}

function Write-Ok {
    param([string]$Message)
    Write-Host "[OK] $Message"
}

function Fail {
    param([string]$Message)
    Write-Host "[ERROR] $Message" -ForegroundColor Red
    exit 1
}

function Get-SingleQuotedLiteral {
    param([string]$Value)
    return "'" + ($Value -replace "'", "''") + "'"
}

function Resolve-RequiredCommand {
    param(
        [string]$Name,
        [string]$InstallHint
    )

    $command = Get-Command -Name $Name -ErrorAction SilentlyContinue
    if (-not $command) {
        Fail "$Name was not found. $InstallHint"
    }

    return $command.Source
}

function Test-BackendDependencies {
    param([string]$PythonExe)

    $importCheck = "import fastapi, uvicorn, sqlalchemy, pydantic, dotenv, openai"
    $output = & $PythonExe -c $importCheck 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host $output
        Fail "Backend dependencies are missing. Run: cd backend; python -m pip install -r requirements.txt"
    }
}

function Invoke-FrontendBuild {
    param(
        [string]$FrontendDir,
        [string]$NpmCommand
    )

    Write-Step "Building frontend dist for backend static serving..."
    Push-Location -LiteralPath $FrontendDir
    $stdoutFile = [System.IO.Path]::GetTempFileName()
    $stderrFile = [System.IO.Path]::GetTempFileName()
    try {
        $escapedNpmCommand = $NpmCommand -replace '"', '\"'
        $escapedStdoutFile = $stdoutFile -replace '"', '\"'
        $escapedStderrFile = $stderrFile -replace '"', '\"'
        $buildCommand = "`"$escapedNpmCommand`" run build > `"$escapedStdoutFile`" 2> `"$escapedStderrFile`""
        & $env:ComSpec /d /c $buildCommand
        $exitCode = $LASTEXITCODE
        $stdout = Get-Content -LiteralPath $stdoutFile -Raw -ErrorAction SilentlyContinue
        $stderr = Get-Content -LiteralPath $stderrFile -Raw -ErrorAction SilentlyContinue
    }
    finally {
        Remove-Item -LiteralPath $stdoutFile, $stderrFile -Force -ErrorAction SilentlyContinue
        Pop-Location
    }

    if ($stdout) {
        Write-Host $stdout.TrimEnd()
    }
    if ($stderr) {
        Write-Host $stderr.TrimEnd()
    }
    if ($exitCode -ne 0) {
        Fail "Frontend build failed. Fix the build error above before starting static serving."
    }

    Write-Ok "Frontend dist was rebuilt."
}

function Get-ListeningProcesses {
    param([int]$Port)

    try {
        $connections = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop
    }
    catch {
        $connections = @()
    }

    $items = @()
    foreach ($connection in $connections) {
        $processName = "unknown"
        try {
            $process = Get-Process -Id $connection.OwningProcess -ErrorAction Stop
            $processName = $process.ProcessName
        }
        catch {
            $processName = "unknown"
        }

        $items += [pscustomobject]@{
            Port = $Port
            ProcessId = $connection.OwningProcess
            ProcessName = $processName
        }
    }

    return $items
}

function Assert-PortFree {
    param([int]$Port)

    $listeners = Get-ListeningProcesses -Port $Port
    if ($listeners.Count -eq 0) {
        Write-Ok "Port $Port is free."
        return
    }

    Write-Host "[ERROR] Port $Port is already in use:" -ForegroundColor Red
    $listeners | Format-Table -AutoSize | Out-String | Write-Host
    exit 1
}

function Wait-HttpReady {
    param(
        [string]$Name,
        [string]$Url,
        [int]$TimeoutSeconds
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) {
                Write-Ok "$Name is reachable at $Url."
                return $true
            }
        }
        catch {
            Start-Sleep -Seconds 1
        }
    }

    Write-Host "[ERROR] $Name did not become reachable at $Url within $TimeoutSeconds seconds." -ForegroundColor Red
    return $false
}

$ScriptPath = $MyInvocation.MyCommand.Path
if (-not $ScriptPath) {
    Fail "Unable to resolve script path."
}

$RepoRoot = Split-Path -Parent $ScriptPath
$BackendDir = Join-Path $RepoRoot "backend"
$FrontendDir = Join-Path $RepoRoot "frontend"
$BackendEnv = Join-Path $BackendDir ".env"
$BackendEnvExample = Join-Path $BackendDir ".env.example"
$FrontendNodeModules = Join-Path $FrontendDir "node_modules"
$FrontendPackageJson = Join-Path $FrontendDir "package.json"
$FrontendDist = Join-Path $FrontendDir "dist"
$FrontendIndex = Join-Path $FrontendDist "index.html"

Write-Step "Checking project layout..."
if (-not (Test-Path -LiteralPath $BackendDir -PathType Container)) {
    Fail "backend directory was not found. Run this script from the repository root."
}
if (-not (Test-Path -LiteralPath $FrontendDir -PathType Container)) {
    Fail "frontend directory was not found. Run this script from the repository root."
}
Write-Ok "Project layout looks valid."

Write-Step "Resolving runtimes..."
$VenvPython = Join-Path $BackendDir ".venv\Scripts\python.exe"
if (Test-Path -LiteralPath $VenvPython -PathType Leaf) {
    $PythonExe = $VenvPython
    Write-Ok "Using backend virtualenv Python."
}
else {
    $PythonExe = Resolve-RequiredCommand -Name "python" -InstallHint "Install Python 3.9+ or create backend/.venv."
    Write-Ok "Using Python from PATH."
}

$NodeExe = Resolve-RequiredCommand -Name "node" -InstallHint "Install Node.js 18+."
$NpmExe = Get-Command -Name "npm.cmd" -ErrorAction SilentlyContinue
if ($NpmExe) {
    $NpmCommand = $NpmExe.Source
}
else {
    $NpmCommand = Resolve-RequiredCommand -Name "npm" -InstallHint "Install npm with Node.js."
}
Write-Ok "Node and npm are available."

Write-Step "Checking configuration and dependencies..."
if (-not (Test-Path -LiteralPath $BackendEnv -PathType Leaf)) {
    Fail "backend/.env was not found. Create it from backend/.env.example and fill in required values."
}
if (-not (Test-Path -LiteralPath $BackendEnvExample -PathType Leaf)) {
    Fail "backend/.env.example was not found."
}
Write-Ok "backend/.env exists."

Test-BackendDependencies -PythonExe $PythonExe
Write-Ok "Backend dependencies are importable."

if (-not (Test-Path -LiteralPath $FrontendPackageJson -PathType Leaf)) {
    Fail "frontend/package.json was not found."
}
if (-not (Test-Path -LiteralPath $FrontendNodeModules -PathType Container)) {
    Fail "frontend/node_modules was not found. Run: cd frontend; npm install"
}
Write-Ok "Frontend dependencies directory exists."

if (-not $SkipFrontendBuild) {
    Invoke-FrontendBuild -FrontendDir $FrontendDir -NpmCommand $NpmCommand
}
else {
    Write-Step "Skipping frontend build because -SkipFrontendBuild was provided."
}

if (-not (Test-Path -LiteralPath $FrontendIndex -PathType Leaf)) {
    Fail "frontend/dist/index.html was not found. Run: cd frontend; npm run build"
}
Write-Ok "Frontend dist is available for backend static serving."

Write-Step "Checking ports..."
Assert-PortFree -Port 9800
Assert-PortFree -Port 5174

if ($CheckOnly) {
    Write-Ok "CheckOnly completed. No services were started."
    exit 0
}

Write-Step "Starting backend and frontend in separate PowerShell windows..."
$BackendCommand = "Set-Location -LiteralPath $(Get-SingleQuotedLiteral $BackendDir); & $(Get-SingleQuotedLiteral $PythonExe) -m app.main --serve-static --static-dir $(Get-SingleQuotedLiteral $FrontendDist)"
$FrontendCommand = "Set-Location -LiteralPath $(Get-SingleQuotedLiteral $FrontendDir); & $(Get-SingleQuotedLiteral $NpmCommand) run dev"

$BackendProcess = Start-Process -FilePath "powershell.exe" -ArgumentList @("-NoExit", "-ExecutionPolicy", "Bypass", "-Command", $BackendCommand) -PassThru
Write-Ok "Backend window started. PID: $($BackendProcess.Id)"

Start-Sleep -Seconds 2

$FrontendProcess = Start-Process -FilePath "powershell.exe" -ArgumentList @("-NoExit", "-ExecutionPolicy", "Bypass", "-Command", $FrontendCommand) -PassThru
Write-Ok "Frontend window started. PID: $($FrontendProcess.Id)"

Write-Step "Waiting for services..."
$BackendReady = Wait-HttpReady -Name "Backend" -Url "http://localhost:9800/health" -TimeoutSeconds 45
$FrontendReady = Wait-HttpReady -Name "Frontend" -Url "http://localhost:5174" -TimeoutSeconds 60

if (-not $BackendReady -or -not $FrontendReady) {
    Fail "One or more services failed readiness checks. Inspect the service windows for logs."
}

if (-not $NoBrowser) {
    Write-Step "Opening browser..."
    Start-Process "http://localhost:5174"
}

Write-Ok "Development environment is running."
Write-Host "Frontend: http://localhost:5174"
Write-Host "Backend:  http://localhost:9800"
Write-Host "Admin:    http://localhost:9800/admin"
Write-Host "API docs: http://localhost:9800/docs"
