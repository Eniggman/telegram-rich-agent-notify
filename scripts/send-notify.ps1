<#
.SYNOPSIS
    Отправляет уведомления и интерактивные запросы в Telegram через Bot API 10.1+ (sendRichMessage, sendMessage, sendDocument).

.DESCRIPTION
    Архитектурное решение:
    1. Rich Messages (Bot API 10.1+):
       Поддерживает отправку форматированных сообщений методом sendRichMessage с лимитом до 32 768 символов
       (вместо стандартных 4 096), интерактивные сворачиваемые блоки <details><summary>...</summary>...</details>,
       раскрываемые цитаты <blockquote expandable>...</blockquote>, заголовки, списки и таблицы.
    2. Бесшовный Fallback на sendMessage:
       При возникновении ошибки парсинга или недоступности метода sendRichMessage разметка автоматически
       преобразуется в классический Telegram HTML (<details> конвертируется в <blockquote expandable>),
       обеспечивая 100% гарантированную доставку на любые версии клиентов.
    3. Автоматическая отправка вложений (sendDocument):
       Если объём текста превышает даже лимит Rich Message (32 000 символов) или явно указан флаг -AsDocument,
       в чат отправляется краткая сводка-карточка, а полный лог/текст автоматически выгружается в виде
       прикреплённого файла .log/.txt через multipart/form-data.
    4. Двусторонний интерактивный режим (-WaitReply):
       Делегирует исполнение wait-reply.ps1 и демону telegram_bridge.py для ожидания выбора пользователя.

.PARAMETER Message
    Основной текст уведомления или краткая выжимка отчета.

.PARAMETER Status
    Уровень статуса: 'Success' (✅), 'Error' (❌), 'Wait' (⏳), 'Info' (ℹ️, по умолчанию).

.PARAMETER Title
    Заголовок карточки сообщения (по умолчанию 'Antigravity').

.PARAMETER Details
    Подробный текст (логи, стектрейс, diff, таблица), автоматически прячется под сворачиваемый спойлер <details>.

.PARAMETER DetailsSummary
    Заголовок кнопки раскрытия спойлера деталей (по умолчанию 'Подробности').

.PARAMETER ExpandableBlockquote
    Оформлять блок деталей в раскрываемую цитату <blockquote expandable> вместо <details>.

.PARAMETER FilePath
    Путь к локальному файлу для прямой отправки документом через sendDocument.

.PARAMETER AsDocument
    Принудительно отправить всё содержимое сообщения отдельным файлом .log с карточкой в чате.

.PARAMETER DocumentName
    Имя прикрепляемого файла документа (по умолчанию 'notification_report.log').

.PARAMETER Options
    Массив вариантов выбора для инлайн-кнопок в Telegram (при -WaitReply).

.PARAMETER WaitReply
    Переключатель интерактивного режима ожидания ответа пользователя.

.PARAMETER TimeoutSeconds
    Таймаут ожидания ответа в секундах (по умолчанию 300 с).

.PARAMETER PassThru
    Возвращать структурированный объект ответа при -WaitReply.

.PARAMETER NoAutoStart
    Не стартовать telegram_bridge.py автоматически при -WaitReply.

.PARAMETER RawHtml
    Передавать переданный HTML без авто-экранирования спецсимволов.

.PARAMETER NoRich
    Отключить метод sendRichMessage и использовать только классический sendMessage.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Message,

    [Parameter(Position = 1)]
    [ValidateSet('Success', 'Error', 'Info', 'Wait')]
    [string]$Status = 'Info',

    [Parameter(Position = 2)]
    [string]$Title = 'Antigravity',

    [Parameter()]
    [string]$Details,

    [Parameter()]
    [string]$DetailsSummary = 'Подробности',

    [Parameter()]
    [switch]$ExpandableBlockquote,

    [Parameter()]
    [string]$FilePath,

    [Parameter()]
    [switch]$AsDocument,

    [Parameter()]
    [string]$DocumentName = 'notification_report.log',

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Options,

    [Parameter()]
    [switch]$WaitReply,

    [Parameter()]
    [int]$TimeoutSeconds = 300,

    [Parameter()]
    [switch]$PassThru,

    [Parameter()]
    [switch]$NoAutoStart,

    [Parameter()]
    [switch]$RawHtml,

    [Parameter()]
    [switch]$NoRich
)

# Установка UTF-8 кодировки для корректного вывода кириллицы в консоли Windows
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch {
}

# Архитектурное решение: Делегирование интерактивного режима скрипту wait-reply.ps1.
# Это разделяет ответственность между односторонними уведомлениями и IPC-мостом с таймаутами.
if ($WaitReply) {
    $waitScript = Join-Path $PSScriptRoot "wait-reply.ps1"
    $params = @{
        Prompt         = $Message
        Status         = $Status
        Title          = $Title
        TimeoutSeconds = $TimeoutSeconds
    }
    if ($Details) {
        $params['Details'] = $Details
    }
    if ($DetailsSummary) {
        $params['DetailsSummary'] = $DetailsSummary
    }
    if ($ExpandableBlockquote) {
        $params['ExpandableBlockquote'] = $true
    }
    if ($Options) {
        $params['Options'] = $Options
    }
    if ($PassThru) {
        $params['PassThru'] = $true
    }
    if ($NoAutoStart) {
        $params['NoAutoStart'] = $true
    }
    & $waitScript @params
    return
}

# Активация TLS 1.2 для совместимости старых сборок Windows PowerShell с серверами Telegram
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch {
}

# Чтение и проверка конфигурационного файла Telegram бота
$configCandidates = @(
    $env:TELEGRAM_CONFIG_PATH,
    (Join-Path $PSScriptRoot "..\telegram.json"),
    (Join-Path (Split-Path -Parent $PSScriptRoot) "telegram.json"),
    (Join-Path $env:USERPROFILE ".gemini\config\telegram.json"),
    (Join-Path $env:HOME ".gemini/config/telegram.json")
)
$configPath = $configCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1

if (-not $configPath -or -not (Test-Path -LiteralPath $configPath)) {
    Write-Error "Конфигурационный файл telegram.json не найден ни в одном из стандартных путей: ~/.gemini/config/telegram.json, корня скилла или `$env:TELEGRAM_CONFIG_PATH."
    exit 1
}

try {
    $rawConfig = [System.IO.File]::ReadAllText($configPath, [System.Text.Encoding]::UTF8)
    $config = $rawConfig | ConvertFrom-Json
} catch {
    Write-Error "Ошибка чтения конфигурации telegram.json: $_"
    exit 1
}

$botToken = $config.bot_token
$chatId = $config.chat_id

if ([string]::IsNullOrWhiteSpace($botToken)) {
    Write-Error "Поле 'bot_token' отсутствует в $configPath"
    exit 1
}

# Автоматическое определение chat_id через getUpdates при первом запуске
if (-not $chatId) {
    try {
        $updates = Invoke-RestMethod -Uri "https://api.telegram.org/bot$botToken/getUpdates"
        if ($updates.ok -and $updates.result -and $updates.result.Count -gt 0) {
            $lastUpdate = $updates.result[-1]
            if ($lastUpdate.message -and $lastUpdate.message.chat) {
                $chatId = $lastUpdate.message.chat.id
            } elseif ($lastUpdate.my_chat_member -and $lastUpdate.my_chat_member.chat) {
                $chatId = $lastUpdate.my_chat_member.chat.id
            }
            if ($chatId) {
                $config.chat_id = $chatId
                $updatedJson = $config | ConvertTo-Json
                [System.IO.File]::WriteAllText($configPath, $updatedJson, [System.Text.Encoding]::UTF8)
            }
        }
    } catch {
        Write-Warning "Не удалось автоматически определить chat_id: $_"
    }
}

if (-not $chatId) {
    Write-Error "chat_id не настроен. Пожалуйста, отправьте команду /start боту в Telegram."
    exit 1
}

# Функция отправки файлов и документов через Bot API метод sendDocument.
# Архитектурное решение: использование .NET HttpClient с MultipartFormDataContent обеспечивает
# кросс-платформенную совместимость как с современным pwsh 7, так и с Windows PowerShell 5.1.
function Send-TelegramDocument {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Token,

        [Parameter(Mandatory = $true)]
        [string]$TargetChatId,

        [Parameter()]
        [byte[]]$DocumentBytes,

        [Parameter()]
        [string]$TargetFilePath,

        [Parameter()]
        [string]$FileName = 'notification_report.log',

        [Parameter()]
        [string]$Caption = '',

        [Parameter()]
        [string]$ParseMode = 'HTML'
    )

    Add-Type -AssemblyName System.Net.Http

    $handler = New-Object System.Net.Http.HttpClientHandler
    $client = New-Object System.Net.Http.HttpClient($handler)
    $client.Timeout = [TimeSpan]::FromSeconds(60)

    try {
        $form = New-Object System.Net.Http.MultipartFormDataContent

        # Поле chat_id
        $chatContent = New-Object System.Net.Http.StringContent($TargetChatId.ToString())
        $form.Add($chatContent, "chat_id")

        # Подпись к документу (ограничена 1000 знаками по спецификации Telegram)
        if (-not [string]::IsNullOrWhiteSpace($Caption)) {
            $safeCaption = if ($Caption.Length -gt 1000) { $Caption.Substring(0, 990) + "..." } else { $Caption }
            $captionContent = New-Object System.Net.Http.StringContent($safeCaption, [System.Text.Encoding]::UTF8)
            $form.Add($captionContent, "caption")
            if (-not [string]::IsNullOrWhiteSpace($ParseMode)) {
                $pmContent = New-Object System.Net.Http.StringContent($ParseMode)
                $form.Add($pmContent, "parse_mode")
            }
        }

        # Прикрепление файла: с диска или напрямую из байтового массива в памяти
        if ($TargetFilePath -and (Test-Path -LiteralPath $TargetFilePath)) {
            $fileBytes = [System.IO.File]::ReadAllBytes($TargetFilePath)
            $actualName = [System.IO.Path]::GetFileName($TargetFilePath)
            $fileContent = [System.Net.Http.ByteArrayContent]::new($fileBytes)
            $fileContent.Headers.ContentType = [System.Net.Http.Headers.MediaTypeHeaderValue]::Parse("application/octet-stream")
            $form.Add($fileContent, "document", $actualName)
        } elseif ($DocumentBytes) {
            $fileContent = [System.Net.Http.ByteArrayContent]::new($DocumentBytes)
            $fileContent.Headers.ContentType = [System.Net.Http.Headers.MediaTypeHeaderValue]::Parse("text/plain; charset=utf-8")
            $form.Add($fileContent, "document", $FileName)
        } else {
            throw "Не переданы данные для отправки документа."
        }

        $url = "https://api.telegram.org/bot$Token/sendDocument"
        $response = $client.PostAsync($url, $form).Result
        $responseStr = $response.Content.ReadAsStringAsync().Result
        $resJson = $responseStr | ConvertFrom-Json
        return $resJson
    } finally {
        $client.Dispose()
    }
}

# Функция безопасного преобразования Rich HTML в классический HTML для метода sendMessage.
# Преобразует неподдерживаемые в обычном режиме теги (<details>, <h1>-<h6>, <table>, <hr>)
# в безопасные эквиваленты (<blockquote expandable>, <b>, списки и разделители).
function Convert-RichToClassicHtml {
    param([string]$Html)

    if ([string]::IsNullOrEmpty($Html)) { return "" }

    $res = $Html

    # 1. Сворачиваемые блоки details -> раскрываемая цитата blockquote expandable:
    $res = [System.Text.RegularExpressions.Regex]::Replace(
        $res,
        '(?is)<details[^>]*>\s*<summary[^>]*>(.*?)</summary>(.*?)</details>',
        "<blockquote expandable><b>`$1</b>`n`$2</blockquote>"
    )

    # 2. Заголовки h1-h6 -> полужирный текст
    $res = [System.Text.RegularExpressions.Regex]::Replace(
        $res,
        '(?is)<h[1-6][^>]*>(.*?)</h[1-6]>',
        "`n<b>`$1</b>`n"
    )

    # 3. Горизонтальная черта hr -> текстовый разделитель
    $res = [System.Text.RegularExpressions.Regex]::Replace(
        $res,
        '(?i)<hr\s*/?>',
        "`n----------------------------------------`n"
    )

    # 4. Подвал footer -> курсив
    $res = [System.Text.RegularExpressions.Regex]::Replace(
        $res,
        '(?is)<footer[^>]*>(.*?)</footer>',
        "`n<i>`$1</i>`n"
    )

    # 5. Боковые врезки aside -> обычная цитата blockquote
    $res = [System.Text.RegularExpressions.Regex]::Replace(
        $res,
        '(?is)<aside[^>]*>(.*?)</aside>',
        "<blockquote>`$1</blockquote>"
    )

    # 6. Элементы списков li -> маркер пункта
    $res = [System.Text.RegularExpressions.Regex]::Replace(
        $res,
        '(?is)<li[^>]*>(.*?)</li>',
        "• `$1`n"
    )

    # 7. Удаление несовместимых контейнерных тегов (table, tr, th, td, caption, ul, ol, figure, figcaption, p, div)
    $res = [System.Text.RegularExpressions.Regex]::Replace(
        $res,
        '(?i)</?(ul|ol|table|tr|th|td|caption|figure|figcaption|p|div)\b[^>]*>',
        ""
    )

    # 8. Схлопывание лишних переводов строк
    $res = [System.Text.RegularExpressions.Regex]::Replace($res, '(\r?\n){3,}', "`n`n")

    return $res.Trim()
}

# Выбор эмодзи статуса
$emoji = switch ($Status) {
    'Success' { [char]0x2705 }             # Белая галочка на зелёном фоне
    'Error'   { [char]0x274C }             # Красный крест
    'Wait'    { [char]0x23F3 }             # Песочные часы
    Default   { [char]0x2139 + [char]0xFE0F } # Информационный значок (ℹ️)
}

$timeStr = (Get-Date).ToString("HH:mm:ss")
$safeTitle = $Title.Replace('&', '&amp;').Replace('<', '&lt;').Replace('>', '&gt;')

# Форматирование основного сообщения
$msgFormatted = $Message -replace '(?i)<br\s*/?>', "`n"
if ($RawHtml -or ($msgFormatted -match '</?(b|i|code|a|u|s|pre|blockquote|details|table|h[1-6]|ul|ol|li)\b')) {
    $safeMsg = $msgFormatted
} else {
    $safeMsg = $msgFormatted.Replace('&', '&amp;').Replace('<', '&lt;').Replace('>', '&gt;')
}

# Формирование блока подробностей Details при наличии
$detailsHtml = ""
if (-not [string]::IsNullOrWhiteSpace($Details)) {
    $safeSummary = $DetailsSummary.Replace('&', '&amp;').Replace('<', '&lt;').Replace('>', '&gt;')
    $detailsFormatted = $Details -replace '(?i)<br\s*/?>', "`n"
    if (-not $RawHtml -and -not ($detailsFormatted -match '</?(b|i|code|pre|a)\b')) {
        $detailsFormatted = $detailsFormatted.Replace('&', '&amp;').Replace('<', '&lt;').Replace('>', '&gt;')
    }

    if ($ExpandableBlockquote) {
        $detailsHtml = "`n`n<blockquote expandable><b>$safeSummary</b>`n$detailsFormatted</blockquote>"
    } else {
        $detailsHtml = "`n`n<details><summary>$safeSummary</summary>`n$detailsFormatted`n</details>"
    }
}

# Полный текст сообщения с шапкой
$headerHtml = "$emoji <b>$safeTitle</b> <code>[$timeStr]</code>"
$fullRichHtml = "$headerHtml`n`n$safeMsg$detailsHtml"

# Определение необходимости отправки в виде файла .log:
# 1. Явный флаг -AsDocument
# 2. Превышение лимита Telegram Rich Message (32 000 символов)
$requiresDocumentAttachment = $AsDocument -or ($fullRichHtml.Length -gt 32000)

if ($requiresDocumentAttachment) {
    Write-Host "Объём данных ($($fullRichHtml.Length) знаков) требует отправки отчета в виде вложенного файла: $DocumentName"

    # Создаем компактную выжимку для чата
    $summaryText = if ($safeMsg.Length -gt 1500) {
        $safeMsg.Substring(0, 1450) + "... [содержимое усечено для превью]"
    } else {
        $safeMsg
    }

    $rawBytes = [System.Text.Encoding]::UTF8.GetBytes("$Title`nStatus: $Status`nTime: $timeStr`n`n--- MESSAGE ---`n$Message`n`n--- DETAILS ---`n$Details")
    $sizeKb = [math]::Round($rawBytes.Length / 1KB, 1)

    $cardHtml = "$headerHtml`n`n$summaryText`n`n📄 <b>Полный отчет прикреплен файлом:</b> <code>$DocumentName</code> ($sizeKb KB)"

    # Отправляем карточку в чат
    $cardPayload = @{
        chat_id      = $chatId
        rich_message = @{ html = $cardHtml }
    } | ConvertTo-Json -Depth 5

    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($cardPayload)
        $resCard = Invoke-RestMethod -Uri "https://api.telegram.org/bot$botToken/sendRichMessage" `
            -Method Post `
            -ContentType "application/json; charset=utf-8" `
            -Body $bytes
    } catch {
        # Fallback карточки на классический sendMessage
        $classicCard = Convert-RichToClassicHtml $cardHtml
        $classicPayload = @{ chat_id = $chatId; text = $classicCard; parse_mode = "HTML" } | ConvertTo-Json
        $resCard = Invoke-RestMethod -Uri "https://api.telegram.org/bot$botToken/sendMessage" `
            -Method Post `
            -ContentType "application/json; charset=utf-8" `
            -Body ([System.Text.Encoding]::UTF8.GetBytes($classicPayload))
    }

    # Отправляем файл документа через sendDocument
    try {
        $docRes = Send-TelegramDocument -Token $botToken -TargetChatId $chatId -DocumentBytes $rawBytes -FileName $DocumentName -Caption "$safeTitle - полный отчет"
        if ($docRes.ok) {
            Write-Host "Файл отчета ($DocumentName) успешно доставлен в Telegram ($chatId)"
        } else {
            Write-Warning "Ошибка отправки файла отчета: $($docRes | ConvertTo-Json -Compress)"
        }
    } catch {
        Write-Error "Ошибка при отправке файла отчета в Telegram: $_"
        exit 1
    }

    return
}

# Отправка локального файла, если передан параметр -FilePath
if ($FilePath -and (Test-Path -LiteralPath $FilePath)) {
    try {
        $docRes = Send-TelegramDocument -Token $botToken -TargetChatId $chatId -TargetFilePath $FilePath -Caption "$emoji ${safeTitle}: $safeMsg"
        if ($docRes.ok) {
            Write-Host "Локальный файл ($FilePath) успешно доставлен в Telegram ($chatId)"
            return
        }
    } catch {
        Write-Warning "Не удалось отправить файл $FilePath через sendDocument: $_. Продолжаем текстовую отправку."
    }
}

# Стандартная отправка текста: сначала пробуем Rich Message API (sendRichMessage)
$sentSuccessfully = $false

if (-not $NoRich) {
    try {
        $richPayload = @{
            chat_id      = $chatId
            rich_message = @{
                html = $fullRichHtml
            }
        } | ConvertTo-Json -Depth 5

        $payloadBytes = [System.Text.Encoding]::UTF8.GetBytes($richPayload)
        $richRes = Invoke-RestMethod -Uri "https://api.telegram.org/bot$botToken/sendRichMessage" `
            -Method Post `
            -ContentType "application/json; charset=utf-8" `
            -Body $payloadBytes

        if ($richRes.ok) {
            Write-Host "Уведомление успешно доставлено в Telegram (rich): $Status - $Title"
            $sentSuccessfully = $true
        }
    } catch {
        Write-Warning "sendRichMessage отклонен API: $($_.Exception.Message). Выполняется автоматический fallback на sendMessage..."
    }
}

# Если rich-отправка не удалась или отключена - fallback на классический sendMessage
if (-not $sentSuccessfully) {
    $classicHtml = Convert-RichToClassicHtml $fullRichHtml

    # Если длина превышает лимит classic sendMessage (4096 знаков)
    if ($classicHtml.Length -gt 3800) {
        Write-Warning "Длина сообщения ($($classicHtml.Length) знаков) превышает лимит classic sendMessage. Выполняется безопасное усечение с прикреплением полного лога."
        $truncatedClassic = $classicHtml.Substring(0, 3700) + "`n... [содержимое усечено, полный отчет отправлен файлом]"
        
        # Отправляем усеченное сообщение
        $classicPayload = @{
            chat_id    = $chatId
            text       = $truncatedClassic
            parse_mode = "HTML"
        } | ConvertTo-Json

        $res = Invoke-RestMethod -Uri "https://api.telegram.org/bot$botToken/sendMessage" `
            -Method Post `
            -ContentType "application/json; charset=utf-8" `
            -Body ([System.Text.Encoding]::UTF8.GetBytes($classicPayload))

        # Прикрепляем полный текст логом
        $fullBytes = [System.Text.Encoding]::UTF8.GetBytes("$Title`n`n$Message`n`n$Details")
        Send-TelegramDocument -Token $botToken -TargetChatId $chatId -DocumentBytes $fullBytes -FileName $DocumentName -Caption "$safeTitle (полная версия)" | Out-Null
        Write-Host "Уведомление и полный лог успешно доставлены в Telegram (fallback): $Status - $Title"
        $sentSuccessfully = $true
    } else {
        $classicPayload = @{
            chat_id    = $chatId
            text       = $classicHtml
            parse_mode = "HTML"
        } | ConvertTo-Json

        try {
            $res = Invoke-RestMethod -Uri "https://api.telegram.org/bot$botToken/sendMessage" `
                -Method Post `
                -ContentType "application/json; charset=utf-8" `
                -Body ([System.Text.Encoding]::UTF8.GetBytes($classicPayload))

            if ($res.ok) {
                Write-Host "Уведомление успешно доставлено в Telegram: $Status - $Title"
                $sentSuccessfully = $true
            } else {
                Write-Error "Telegram API вернул ошибку: $($res | ConvertTo-Json -Compress)"
                exit 1
            }
        } catch {
            Write-Error "Ошибка при отправке классического сообщения в Telegram: $_"
            exit 1
        }
    }
}
