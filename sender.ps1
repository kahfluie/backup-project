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
