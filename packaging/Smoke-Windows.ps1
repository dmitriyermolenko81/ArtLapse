param(
    [Parameter(Mandatory=$true)][string]$Bundle,
    [Parameter(Mandatory=$true)][string]$Workspace,
    [ValidateSet('ru','en')][string]$Language = 'ru'
)
$ErrorActionPreference = 'Stop'
$bundlePath = [IO.Path]::GetFullPath($Bundle)
$workPath = [IO.Path]::GetFullPath($Workspace)
if ((Test-Path -LiteralPath $workPath) -and
    (Get-ChildItem -LiteralPath $workPath -Force | Select-Object -First 1)) {
    throw 'Smoke workspace must be empty.'
}
$portable = Test-Path -LiteralPath (Join-Path $bundlePath 'portable.txt')
if ($portable) {
    $ffmpegPath = Join-Path $bundlePath 'bin\ffmpeg.exe'
    $ffprobePath = Join-Path $bundlePath 'bin\ffprobe.exe'
} else {
    $ffmpegPath = (Get-Command ffmpeg -ErrorAction Stop).Source
    $ffprobePath = (Get-Command ffprobe -ErrorAction Stop).Source
}
$cli = Join-Path $bundlePath 'sculptcut-cli.exe'
$gui = Join-Path $bundlePath 'SculptCut.exe'
New-Item -ItemType Directory -Path $workPath -Force | Out-Null
$oldEnvironment = @{}
foreach ($key in @('PATH','PYTHONPATH','PYTHONHOME','SCULPTCUT_DATA_DIR',
    'SCULPTCUT_FFMPEG','SCULPTCUT_FFPROBE')) {
    $oldEnvironment[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
}
try {
    $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
    $env:PYTHONPATH = ''
    $env:PYTHONHOME = ''
    $env:SCULPTCUT_DATA_DIR = Join-Path $workPath 'data'
    $env:SCULPTCUT_FFMPEG = $ffmpegPath
    $env:SCULPTCUT_FFPROBE = $ffprobePath
    if ($portable) {
        $env:SCULPTCUT_DATA_DIR = $null
        $env:SCULPTCUT_FFMPEG = $null
        $env:SCULPTCUT_FFPROBE = $null
    }
    & $cli --version
    if ($LASTEXITCODE -ne 0) { throw 'Frozen CLI failed.' }
    & $cli doctor
    if ($LASTEXITCODE -ne 0) { throw 'Media tools failed.' }
    & $cli fixtures (Join-Path $workPath 'fixtures')
    if ($LASTEXITCODE -ne 0) { throw 'Fixture generation failed.' }
    $video = Join-Path $workPath 'fixtures\кириллица с пробелом.mp4'
    $output = Join-Path $workPath 'результат.mp4'
    & $cli run (Join-Path $workPath 'проект') $video --target-seconds 1 --output $output
    if ($LASTEXITCODE -ne 0) { throw 'Frozen pipeline failed.' }
    $metadata = & $ffprobePath -v error -count_frames -show_entries stream=codec_type,nb_read_frames,duration -of json $output | ConvertFrom-Json
    $videoStream = @($metadata.streams | Where-Object { $_.codec_type -eq 'video' })
    $audioStream = @($metadata.streams | Where-Object { $_.codec_type -eq 'audio' })
    if ($videoStream.Count -ne 1 -or $videoStream[0].nb_read_frames -ne '30') {
        throw 'Wrong output video stream or frame count.'
    }
    if ($audioStream.Count -ne 0) { throw 'Default export contains unexpected audio.' }
    & $ffmpegPath -v error -xerror -i $output -f null -
    if ($LASTEXITCODE -ne 0) { throw 'Output decode failed.' }
    & $cli probe (Join-Path $workPath 'missing.mp4')
    if ($LASTEXITCODE -ne 1) { throw 'Missing source was not rejected.' }
    $guiReport = Join-Path $workPath 'gui'
    & $gui --smoke-directory $guiReport --smoke-video $output --smoke-language $Language | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Frozen window failed.' }
    $window = Get-Content -LiteralPath (Join-Path $guiReport 'window.json') -Raw | ConvertFrom-Json
    if (-not $window.frozen -or -not $window.visible -or $window.video_frames -lt 1) {
        throw 'Window did not decode video in frozen runtime.'
    }
    $expectedLabel = if ($Language -eq 'en') { 'Create video' } else { 'Создать ролик' }
    $expectedCancel = if ($Language -eq 'en') { 'Cancel' } else { 'Отмена' }
    if ($window.create_label -ne $expectedLabel -or $window.qt_cancel -ne $expectedCancel) {
        throw 'Main creation button has an unexpected label.'
    }
    if ($portable -and -not (Test-Path -LiteralPath (Join-Path $bundlePath 'data\settings.ini'))) {
        throw 'Portable settings were not written beside the application.'
    }
    @{status='PASS'; portable=$portable; windows=[Environment]::OSVersion.VersionString;
      frames=30; gui=$window; clean_machine='NOT_VERIFIED_BY_THIS_SCRIPT'} |
        ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $workPath 'smoke.json')
} finally {
    foreach ($key in $oldEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($key, $oldEnvironment[$key], 'Process')
    }
}
