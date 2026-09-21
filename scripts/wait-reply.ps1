<#
.SYNOPSIS
    Интерактивный запрос подтверждения или ответа пользователя через Telegram.

.DESCRIPTION
    Скрипт формирует IPC-запрос с уникальным UUID, при необходимости автоматически
    поднимает фоновый сервис telegram_bridge.py, отправляет интерактивные кнопки или
    текстовый вопрос в доверенный чат Telegram и блокирует выполнение до получения ответа
    либо истечения заданного таймаута.

.PARAMETER Prompt
    Текст вопроса или описания решения, требующего выбора.

.PARAMETER Options
    Массив вариантов ответов (будут отображены как Inline-кнопки в Telegram).

.PARAMETER TimeoutSeconds
    Максимальное время ожидания ответа в секундах (по умолчанию 300 с = 5 мин).

.PARAMETER Status
    Уровень статуса ('Wait', 'Info', 'Success', 'Error'). По умолчанию 'Wait'.

.PARAMETER Title
    Заголовок карточки сообщения. По умолчанию 'Antigravity'.

.PARAMETER PassThru
    Возвращать ли полный объект ответа (включая индекс выбранной кнопки и тип) вместо простой строки.

.PARAMETER NoAutoStart
    Не запускать фоновый сервис telegram_bridge.py автоматически (для тестов или ручного режима).
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Prompt,

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Options,

    [Parameter()]
    [int]$TimeoutSeconds = 300,

    [Parameter()]
    [ValidateSet('Success', 'Error', 'Info', 'Wait')]
    [string]$Status = 'Wait',

    [Parameter()]
    [string]$Title = 'Antigravity',

    [Parameter()]
    [string]$Details,

    [Parameter()]
    [string]$DetailsSummary = 'Подробности',

    [Parameter()]
    [switch]$ExpandableBlockquote,

    [Parameter()]
    [switch]$PassThru,

    [Parameter()]
    [switch]$NoAutoStart
)

# Установка UTF-8 для чистого вывода в консоль и конвейеры без искажения кириллицы
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch {
}

# Каталоги и пути IPC
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ipcDir = if ($env:TELEGRAM_IPC_DIR) { $env:TELEGRAM_IPC_DIR } else { Join-Path (Split-Path -Parent $scriptDir) 'ipc' }
$bridgeScript = Join-Path $scriptDir 'telegram_bridge.py'

# Гарантируем наличие IPC каталога
if (-not (Test-Path -LiteralPath $ipcDir)) {
    New-Item -ItemType Directory -Path $ipcDir -Force | Out-Null
}

# Генерация криптографически стойкого UUIDv4 для защиты от коллизий и Path Traversal
$reqId = [guid]::NewGuid().ToString()
$reqFile = Join-Path $ipcDir "request_$reqId.json"
$respFile = Join-Path $ipcDir "response_$reqId.json"
$tmpFile = Join-Path $ipcDir ".tmp_req_$reqId.tmp"

# Нормализация вариантов ответов:
# Если передан массив из нескольких элементов, каждый элемент сохраняется как отдельный вариант (запятые внутри названий не разделяются).
# Если передан ровно один строковый элемент, содержащий запятые, он разделяется на список вариантов для удобства вызова из однострочного CLI.
$parsedOptions = @()
if ($Options) {
    if ($Options.Count -eq 1 -and $Options[0] -match ',') {
        $parsedOptions = ($Options[0] -split ',') | ForEach-Object { $_.Trim().Trim('"').Trim("'") } | Where-Object { $_ }
    } else {
        foreach ($opt in $Options) {
            $cleaned = $opt.Trim().TrimEnd(',').Trim().Trim('"').Trim("'")
            if (-not [string]::IsNullOrWhiteSpace($cleaned)) {
                $parsedOptions += $cleaned
            }
        }
    }
}

# Формирование структуры IPC-запроса
$reqData = [ordered]@{
    id                    = $reqId
    prompt                = $Prompt
    options               = $parsedOptions
    status                = $Status
    title                 = $Title
    details               = $Details
    details_summary       = $DetailsSummary
    expandable_blockquote = [bool]$ExpandableBlockquote
    timeout_seconds       = $TimeoutSeconds
    created_at            = [double]([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())
}

# Архитектурное решение: Атомарная запись через временный файл.
# Исключает состояние гонки (Race Condition), когда демон моста начинает читать
# файл до того, как PowerShell завершил буферизованную запись на диск.
# Файл создаётся в первую очередь, чтобы фоновый сервис мог обработать его без задержек.
$jsonString = $reqData | ConvertTo-Json -Depth 5
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($tmpFile, $jsonString, $utf8NoBom)
Move-Item -LiteralPath $tmpFile -Destination $reqFile -Force

# Архитектурное решение: Самовосстановление и автозапуск моста (Self-Healing Bridge).
# Проверка активности моста через быстрый PID-файл и детекцию блокировки lock-файла (< 5 мс).
# Если мост ещё не запущен, скрипт автоматически стартует его в скрытом фоновом режиме.
if (-not $NoAutoStart) {
    $isBridgeRunning = $false
    $pidFile = Join-Path $ipcDir "bridge.pid"
    $lockFile = Join-Path $ipcDir "bridge.lock"

    if (Test-Path -LiteralPath $pidFile) {
        try {
            $savedPid = [int]((Get-Content -LiteralPath $pidFile -Raw).Trim())
            $proc = Get-Process -Id $savedPid -ErrorAction SilentlyContinue
            if ($proc -and $proc.ProcessName -like "*python*") {
                $isBridgeRunning = $true
            }
        } catch {
            $isBridgeRunning = $false
        }
    }

    # Дополнительная проверка через попытку блокировки файла
    if (-not $isBridgeRunning -and (Test-Path -LiteralPath $lockFile)) {
        try {
            $fs = [System.IO.File]::Open($lockFile, [System.IO.FileMode]::Open, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
            $fs.Close()
            $isBridgeRunning = $false
        } catch {
            # Файл заблокирован работающим экземпляром моста
            $isBridgeRunning = $true
        }
    }

    if (-not $isBridgeRunning) {
        # Запускаем мост в скрытом окне python со строгим лимитом жизни 1 час (3600 с)
        Start-Process -FilePath "python" -ArgumentList "`"$bridgeScript`" --lifetime 3600" -WindowStyle Hidden
        # Пауза для инициализации и захвата блокировки сокета/файла
        Start-Sleep -Milliseconds 1500
        if (Test-Path -LiteralPath $pidFile) {
            try {
                $newPid = [int]((Get-Content -LiteralPath $pidFile -Raw).Trim())
                $newProc = Get-Process -Id $newPid -ErrorAction SilentlyContinue
                if ($newProc) {
                    $isBridgeRunning = $true
                }
            } catch {
            }
        }
        if (-not $isBridgeRunning) {
            Write-Warning "Фоновый сервис telegram_bridge.py не подтвердил запуск за 1.5 с. Проверьте telegram.json и активность Antigravity.exe."
        }
    }
}

$deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
$receivedResponse = $null

try {
    # Цикл активного ожидания ответа из Telegram с эргономичным интервалом опроса (300 мс)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-Path -LiteralPath $respFile) {
            # Пауза 50 мс для гарантии сброса файлового буфера операционной системы
            Start-Sleep -Milliseconds 50
            try {
                $rawContent = [System.IO.File]::ReadAllText($respFile, [System.Text.Encoding]::UTF8)
                if (-not [string]::IsNullOrWhiteSpace($rawContent)) {
                    $receivedResponse = $rawContent | ConvertFrom-Json
                    break
                }
            } catch {
                # При временной блокировке файла читаем на следующей итерации
            }
        }
        Start-Sleep -Milliseconds 300
    }
} finally {
    # Гарантированная очистка временных IPC файлов при любом исходе (успех, таймаут, Ctrl+C)
    if (Test-Path -LiteralPath $reqFile) {
        Remove-Item -LiteralPath $reqFile -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $respFile) {
        Remove-Item -LiteralPath $respFile -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $tmpFile) {
        Remove-Item -LiteralPath $tmpFile -Force -ErrorAction SilentlyContinue
    }
}

if (-not $receivedResponse) {
    Write-Error "Истекло время ожидания ответа из Telegram ($TimeoutSeconds с) для запроса $reqId."
    exit 2
}

# Вывод результата: строковое значение для конвейера или полный объект при флаге -PassThru
if ($PassThru) {
    Write-Output $receivedResponse
} else {
    Write-Output $receivedResponse.reply
}
