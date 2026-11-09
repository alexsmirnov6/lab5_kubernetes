param([string]$KindVersion = 'v0.33.0')
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program failed: $LASTEXITCODE" }
}

New-Item -ItemType Directory -Force tools, logs | Out-Null
Invoke-Checked docker @('info')
if (-not (Test-Path tools/kind.exe)) {
    $release = "https://github.com/kubernetes-sigs/kind/releases/download/$KindVersion"
    Invoke-WebRequest "$release/kind-windows-amd64" -OutFile tools/kind.exe
    Invoke-WebRequest "$release/kind-windows-amd64.sha256sum" -OutFile tools/kind.sha256
}
$expected = ((Get-Content tools/kind.sha256) -split '\s+')[0]
if ((Get-FileHash tools/kind.exe).Hash.ToLower() -ne $expected) { throw 'kind checksum mismatch' }

$kubectlRelease = 'https://dl.k8s.io/release/v1.37.0/bin/windows/amd64'
if (-not (Test-Path tools/kubectl.exe)) {
    Invoke-WebRequest "$kubectlRelease/kubectl.exe" -OutFile tools/kubectl.exe
    Invoke-WebRequest "$kubectlRelease/kubectl.exe.sha256" -OutFile tools/kubectl.sha256
}
if ((Get-FileHash tools/kubectl.exe).Hash.ToLower() -ne (Get-Content tools/kubectl.sha256).Trim()) {
    throw 'kubectl checksum mismatch'
}

# Reserve all but two CPUs for a repeatable scheduling experiment.
$cpuCount = [int](& docker info --format '{{.NCPU}}')
if ($cpuCount -lt 4) { throw 'Allocate at least four CPUs to Docker Desktop.' }
$config = Get-Content kind.yaml -Raw
$config = $config -replace 'cpu=\d+,memory=1Gi', "cpu=$($cpuCount - 2),memory=1Gi"
[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'kind.yaml'), $config)
Invoke-Checked ./tools/kind.exe @('create', 'cluster', '--name', 'lab5', '--config', 'kind.yaml', '--image', 'kindest/node:v1.37.0@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5', '--wait', '120s')
Invoke-Checked docker @('build', '-t', 'lab5-pairs:1', '.')
Invoke-Checked docker @('pull', 'rustfs/rustfs:1.0.0')
foreach ($item in @(@('lab5-pairs:1', 'pairs'), @('rustfs/rustfs:1.0.0', 'rustfs'))) {
    $archive = "tools/$($item[1]).tar"
    Invoke-Checked docker @('image', 'save', '--platform', 'linux/amd64', '-o', $archive, $item[0])
    Invoke-Checked ./tools/kind.exe @('load', 'image-archive', $archive, '--name', 'lab5')
}
Invoke-Checked python @('-m', 'pip', 'install', '-r', 'requirements.txt')
Invoke-Checked python @('run_experiments.py')
