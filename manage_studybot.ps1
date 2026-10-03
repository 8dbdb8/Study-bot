param(
    [ValidateSet('Menu', 'Start', 'Stop', 'Restart', 'Status')]
    [string]$Action = 'Menu'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$TaskName = 'StudyBot'
$script:BotDirectory = $null

function Get-BotTask {
    try {
        return Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    } catch {
        throw "StudyBotのタスクを読み込めません: $($_.Exception.Message)"
    }
}

function Get-BotDirectory {
    if ($null -ne $script:BotDirectory) {
        return $script:BotDirectory
    }
    $task = Get-BotTask
    $taskAction = $task.Actions | Select-Object -First 1
    if ($null -eq $taskAction) {
        throw 'StudyBotの起動先がタスクに設定されていません。'
    }
    $directory = $taskAction.WorkingDirectory
    if ([string]::IsNullOrWhiteSpace($directory)) {
        $directory = Split-Path -Parent $taskAction.Execute
    }
    if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
        throw "Botのフォルダが見つかりません: $directory"
    }
    $script:BotDirectory = [System.IO.Path]::GetFullPath($directory)
    return $script:BotDirectory
}

function Get-BotProcesses {
    $directory = Get-BotDirectory
    $venvPython = [System.IO.Path]::GetFullPath(
        (Join-Path $directory '.venv\Scripts\python.exe')
    )
    $botFile = [System.IO.Path]::GetFullPath((Join-Path $directory 'bot.py'))
    $allPython = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'")
    $launchers = @($allPython | Where-Object {
        [string]::Equals(
            $_.ExecutablePath, $venvPython,
            [System.StringComparison]::OrdinalIgnoreCase
        ) -and $_.CommandLine -match '(?i)(?:^|\s)-u\s+' -and
        $_.CommandLine -match '(?i)bot\.py'
    })
    $launcherIds = @($launchers | ForEach-Object { [int]$_.ProcessId })
    $withFullPath = @($allPython | Where-Object {
        $_.CommandLine -and $_.CommandLine.IndexOf(
            $botFile, [System.StringComparison]::OrdinalIgnoreCase
        ) -ge 0
    })
    $children = @($allPython | Where-Object {
        $launcherIds -contains [int]$_.ParentProcessId -and
        $_.CommandLine -match '(?i)bot\.py'
    })
    return @($launchers + $withFullPath + $children |
        Sort-Object ProcessId -Unique)
}

function Show-BotStatus {
    $task = Get-BotTask
    $processes = @(Get-BotProcesses)
    if ($processes.Count -gt 0) {
        $ids = ($processes | ForEach-Object { $_.ProcessId }) -join ', '
        if ($task.State -eq 'Running') {
            Write-Host "状態: 起動中（PID: $ids）" -ForegroundColor Green
        } else {
            Write-Host "状態: Botは起動中、タスクは$($task.State)（PID: $ids）" -ForegroundColor Yellow
        }
    } elseif ($task.State -eq 'Running') {
        Write-Host '状態: 起動処理中、またはBotが終了しています。' -ForegroundColor Yellow
    } else {
        Write-Host '状態: 停止中' -ForegroundColor Gray
    }
}

function Start-Bot {
    $directory = Get-BotDirectory
    $existing = @(Get-BotProcesses)
    if ($existing.Count -gt 0) {
        Write-Host 'Botはすでに起動しています。' -ForegroundColor Green
        return
    }
    if ((Get-BotTask).State -eq 'Running') {
        Write-Host 'タスクは起動処理中です。少し待ってから状態を確認してください。' -ForegroundColor Yellow
        return
    }
    foreach ($file in @('bot.py', '.venv\Scripts\python.exe', '.env')) {
        if (-not (Test-Path -LiteralPath (Join-Path $directory $file) -PathType Leaf)) {
            throw "起動に必要なファイルがありません: $file"
        }
    }
    Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        Start-Sleep -Milliseconds 500
        if (@(Get-BotProcesses).Count -gt 0) {
            Write-Host 'Botの起動を確認しました。Discordへの接続には数秒かかる場合があります。' -ForegroundColor Green
            return
        }
        if ((Get-BotTask).State -ne 'Running' -and $attempt -ge 3) {
            break
        }
    }
    throw "Botを起動できませんでした。$(Join-Path $directory 'studybot.log') を確認してください。"
}

function Stop-Bot {
    if ((Get-BotTask).State -eq 'Running') {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    }
    Start-Sleep -Milliseconds 500
    $remaining = @(Get-BotProcesses)
    if ($remaining.Count -gt 0) {
        $ids = @($remaining | ForEach-Object { [int]$_.ProcessId })
        Stop-Process -Id $ids -Force -ErrorAction SilentlyContinue
    }
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        if (@(Get-BotProcesses).Count -eq 0 -and (Get-BotTask).State -ne 'Running') {
            Write-Host 'Botを停止しました。' -ForegroundColor Green
            return
        }
        Start-Sleep -Milliseconds 300
    }
    throw 'Botを停止できませんでした。状態を確認してください。'
}

function Invoke-BotAction([string]$requested) {
    switch ($requested) {
        'Start' { Start-Bot }
        'Stop' { Stop-Bot }
        'Restart' { Stop-Bot; Start-Bot }
        'Status' { Show-BotStatus }
    }
}

if ($Action -ne 'Menu') {
    try {
        Invoke-BotAction $Action
        exit 0
    } catch {
        [Console]::Error.WriteLine("エラー: $($_.Exception.Message)")
        exit 1
    }
}

while ($true) {
    Clear-Host
    Write-Host '=== StudyBot 操作 ===' -ForegroundColor Cyan
    try {
        Show-BotStatus
    } catch {
        Write-Host "状態確認エラー: $($_.Exception.Message)" -ForegroundColor Red
    }
    Write-Host ''
    Write-Host '1  起動'
    Write-Host '2  停止'
    Write-Host '3  再起動'
    Write-Host '4  状態確認'
    Write-Host '0  閉じる'
    $choice = (Read-Host '番号を入力').Trim()
    if ($choice -eq '0') { break }
    $requested = switch ($choice) {
        '1' { 'Start' }
        '2' { 'Stop' }
        '3' { 'Restart' }
        '4' { 'Status' }
        default { $null }
    }
    if ($null -eq $requested) {
        Write-Host '0～4の番号を入力してください。' -ForegroundColor Yellow
    } else {
        try {
            Invoke-BotAction $requested
        } catch {
            Write-Host "エラー: $($_.Exception.Message)" -ForegroundColor Red
        }
    }
    [void](Read-Host 'Enterキーでメニューに戻る')
}
