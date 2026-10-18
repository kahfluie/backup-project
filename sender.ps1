[CmdletBinding(PositionalBinding = $false)]
param(
    [string]$EnvFile,
    [string]$ComposeFile = 'compose.sender.yml',
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$ComposeArguments
)

$ErrorActionPreference = 'Stop'
if (-not $ComposeArguments) {
    throw 'Supply a Compose command, for example: .\sender.ps1 up -d'
}

$composeOptions = @('compose', '--project-directory', $PSScriptRoot)
if ($EnvFile) {
    $composeOptions += @('--env-file', (Resolve-Path -LiteralPath $EnvFile).Path)
}
$selectedComposeFile = if ([IO.Path]::IsPathRooted($ComposeFile)) { $ComposeFile } else { Join-Path $PSScriptRoot $ComposeFile }
$resolved = & docker @composeOptions -f $selectedComposeFile config --format json
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$project = ($resolved -join "`n") | ConvertFrom-Json
$disabled = @()
foreach ($property in $project.services.sender.environment.PSObject.Properties) {
    if ($property.Name -cmatch '^(FOLDER[0-9]+)_PATH$' -and
        [string]$property.Value -match '^\s*none\s*$') {
        $folder = $property.Name.Substring(0, $property.Name.Length - 5).ToLowerInvariant()
        $disabled += "/sources/$folder"
    }
}
$project.services.sender.volumes = @($project.services.sender.volumes | Where-Object {
    $_.target -cnotin $disabled
})
$sendToRemote = ([string]$project.services.sender.environment.SEND_TO_REMOTE).Trim().ToLowerInvariant()
if ($sendToRemote -cnotin @('', 'true', 'false')) {
    throw 'SEND_TO_REMOTE must be true or false'
}
if ($sendToRemote -ceq 'false') {
    $sshSecrets = @('backup_key', 'known_hosts')
    $project.services.sender | Add-Member -Force -NotePropertyName secrets -NotePropertyValue @(
        $project.services.sender.secrets | Where-Object {
            $name = if ($_ -is [string]) { $_ } else { $_.source }
            $name -cnotin $sshSecrets
        }
    )
    $usedSecrets = @(
        foreach ($service in $project.services.PSObject.Properties.Value) {
            foreach ($secret in $service.secrets) {
                if ($secret -is [string]) { $secret } else { $secret.source }
            }
        }
    )
    foreach ($name in $sshSecrets) {
        if ($name -cnotin $usedSecrets -and $null -ne $project.secrets) {
            $project.secrets.PSObject.Properties.Remove($name)
        }
    }
}
$temporary = [System.IO.Path]::GetTempFileName()
try {
    # Compose's rendered config already escapes literal dollar signs for reuse.
    $json = $project | ConvertTo-Json -Depth 100
    [System.IO.File]::WriteAllText($temporary, $json, [System.Text.UTF8Encoding]::new($false))
    & docker @composeOptions -f $temporary @ComposeArguments
    $resultCode = $LASTEXITCODE
} finally {
    Remove-Item -LiteralPath $temporary -Force
}
exit $resultCode
