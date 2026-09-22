# Builds installable zips of the add-on into dist/.
#   handoff-extension.zip  Blender 4.2+ (blender_manifest.toml at the zip root)
#   handoff-legacy.zip     Blender 3.6-4.1 (a handoff/ folder inside the zip)
# With Blender on PATH, `blender --command extension build --source-dir handoff --output-dir dist`
# produces the extension zip too, and validates the manifest while it's at it.

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$dist = Join-Path $root 'dist'
$stage = Join-Path $dist 'stage\handoff'

Remove-Item -Recurse -Force $dist -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $stage | Out-Null
Copy-Item -Recurse (Join-Path $root 'handoff\*') $stage
Get-ChildItem $stage -Recurse -Directory -Filter '__pycache__' | Remove-Item -Recurse -Force

Compress-Archive -Path (Join-Path $stage '*') -DestinationPath (Join-Path $dist 'handoff-extension.zip')
Compress-Archive -Path $stage -DestinationPath (Join-Path $dist 'handoff-legacy.zip')
Remove-Item -Recurse -Force (Join-Path $dist 'stage')

Get-ChildItem $dist -Filter *.zip | ForEach-Object { "{0}  {1:N0} bytes" -f $_.Name, $_.Length }
