# Windows equivalent of integration.sh. Uses only disposable keys and volumes.
# Windows PowerShell treats native stderr progress as nonterminating errors.
# Check native exit codes explicitly instead of stopping on Compose progress.
$ErrorActionPreference = 'Continue'
$projectRoot = Split-Path $PSScriptRoot -Parent
$testParent = Join-Path $projectRoot '.tools'
$testRoot = Join-Path $testParent ('security-test-' + [guid]::NewGuid().ToString('N'))
$testProject = Split-Path $testRoot -Leaf
$utf8 = New-Object System.Text.UTF8Encoding($false)
$oldTestDir = $env:TEST_DIR
$oldTestProject = $env:TEST_PROJECT
$env:TEST_DIR = $testRoot.Replace('\', '/')
$env:TEST_PROJECT = $testProject
$composeOptions = @('-f', (Join-Path $PSScriptRoot 'compose.integration.yml'))

function Invoke-TestCompose {
    & docker compose @composeOptions @args 2>&1 | ForEach-Object { $_.ToString() }
    if ($LASTEXITCODE -ne 0) { throw "Compose failed ($LASTEXITCODE): $args" }
}

function Write-TestFile($relativePath, $content) {
    [IO.File]::WriteAllText((Join-Path $testRoot $relativePath), $content, $utf8)
}

New-Item -ItemType Directory -Force (Join-Path $testRoot 'source/documents'), (Join-Path $testRoot 'source/photos') | Out-Null
Write-TestFile 'source/documents/hello.txt' "hello backup`n"
Write-TestFile 'source/photos/photo.txt' "photo sample`n"
Write-TestFile 'known_hosts' ''
Write-TestFile 'sender.json' @'
{"backup_dir":"/backups","remote":{"enabled":true,"host":"receiver","port":22,
"bandwidth_kib_per_second":512,"transfer_timeout_seconds":60,"acceptance_timeout_seconds":30},
"folders":[{"id":"documents","path":"/sources/documents"},
{"id":"photos","path":"/sources/photos","keep":2},
{"id":"disabled","path":"/sources/not-mounted"}]}
'@
Write-TestFile 'receiver.json' @'
{"default_keep":3,"folders":{"photos":{"keep":2}},"senders":{
"backup":{"authorized_keys_file":"/run/secrets/authorized_keys","folders":["documents","photos"]},
"backup_other":{"authorized_keys_file":"/run/secrets/authorized_other_keys","folders":["other"]}}}
'@

try {
    Invoke-TestCompose build
    @'
import subprocess
for name in ('backup_key', 'other_key'):
    subprocess.run(['ssh-keygen','-q','-t','ed25519','-N','','-f','/test/' + name], check=True)
'@ | docker run --rm -i --mount "type=bind,source=$testRoot,target=/test" --entrypoint python ($testProject + '-sender') -
    if ($LASTEXITCODE -ne 0) { throw 'Could not generate disposable keys' }
    Copy-Item -LiteralPath (Join-Path $testRoot 'backup_key.pub') -Destination (Join-Path $testRoot 'authorized_keys')
    Copy-Item -LiteralPath (Join-Path $testRoot 'other_key.pub') -Destination (Join-Path $testRoot 'authorized_other_keys')
    Invoke-TestCompose up -d receiver
    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        'import socket; socket.create_connection(("127.0.0.1", 22), 1).close()' |
            docker compose @composeOptions exec -T receiver python - 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { $ready = $true; break }
        Start-Sleep -Seconds 1
    }
    if (-not $ready) { throw 'Receiver did not become ready' }
    $publicKey = Invoke-TestCompose exec -T receiver cat /host-keys/ssh_host_ed25519_key.pub
    Write-TestFile 'known_hosts' ("receiver " + ($publicKey -join "`n").Trim() + "`n")
    Invoke-TestCompose run --rm sender --check-config
    Invoke-TestCompose exec -T receiver python -m backupkit.receiver --check-config
    for ($iteration = 0; $iteration -lt 5; $iteration++) {
        Invoke-TestCompose run --rm sender --once
    }
    @'
from pathlib import Path
from backupkit.common import archives
assert len(archives(Path('/backups/documents'))) == 3
assert len(archives(Path('/backups/photos'))) == 2
assert not Path('/backups/disabled').exists()
assert not list(Path('/backups').rglob('*.partial'))
'@ | docker compose @composeOptions run --rm -T --entrypoint python sender -
    if ($LASTEXITCODE -ne 0) { throw 'Local retention assertions failed' }
    Invoke-TestCompose run --rm --entrypoint python sender /tests/security_probe.py sender
    Invoke-TestCompose exec -T receiver python /tests/security_probe.py receiver
    Invoke-TestCompose exec -T receiver python -m backupkit.receiver --dry-run
    Invoke-TestCompose stop receiver
    Invoke-TestCompose run --rm --entrypoint python receiver /tests/security_probe.py age
    Invoke-TestCompose run --rm receiver --once
    Invoke-TestCompose run --rm --entrypoint python receiver /tests/security_probe.py retention
    Write-Output 'PASS: SCP transfer, protected acceptance, identity isolation, attacks, and retention.'
} catch {
    docker compose @composeOptions logs
    throw
} finally {
    docker compose @composeOptions down --volumes --remove-orphans 2>&1 | ForEach-Object { $_.ToString() }
    $cleanupSucceeded = $LASTEXITCODE -eq 0
    $env:TEST_DIR = $oldTestDir
    $env:TEST_PROJECT = $oldTestProject
    $resolvedTestRoot = [IO.Path]::GetFullPath($testRoot)
    $expectedParent = [IO.Path]::GetFullPath($testParent).TrimEnd('\') + '\'
    if ($cleanupSucceeded -and $resolvedTestRoot.StartsWith($expectedParent, [StringComparison]::OrdinalIgnoreCase) -and
        (Split-Path $resolvedTestRoot -Leaf) -match '^security-test-[0-9a-f]{32}$') {
        Remove-Item -LiteralPath $resolvedTestRoot -Recurse -Force
    }
}
