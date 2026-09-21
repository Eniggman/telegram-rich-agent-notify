---
name: telegram-notify
description: Universal AI agent skill to send Telegram notifications and task reports via Bot API 10.1+ (Rich Messages), and enable interactive bidirectional human-in-the-loop prompts (buttons, text replies) for Codex, Claude Code, Antigravity, Cursor, and other autonomous agents. Use when the user requests Telegram notification («скинь в тг», «я с телефона», «отойду») or when the agent needs human confirmation.
---

# Telegram Notify & Bridge Skill

Этот скилл обеспечивает как быструю одностороннюю отправку статусов и отчётов, так и **полноценный двусторонний интерактивный диалог** между любыми ИИ-агентами (Codex, Claude Code, Antigravity, Cursor) и разработчиком в Telegram через персонального бота.

## ❓ Когда использовать

1. **Пользователь работает удалённо / с телефона:**
   * Когда пользователь сказал: *«я с телефона»*, *«отойду»*, *«скинь в ТГ»*, *«напиши в телегу как закончишь»*.
2. **После длительных или фоновых операций:**
   * Сборка проекта, тестирование, фоновые субагенты, миграции, очистка диска.
3. **Интерактивный выбор и подтверждения от пользователя (Двусторонний мост):**
   * Когда агенту требуется подтверждение плана, выбор стратегии, ответ на вопрос или одобрение деструктивного действия: бот присылает карточку с Inline-кнопками или запросом текста и ждёт нажатия.

## 🛡️ Архитектура безопасности и надёжности

* **Watchdog-контроль жизненного цикла**: Демон `telegram_bridge.py` работает **только пока запущен Antigravity (`Antigravity.exe`)**. При закрытии Antigravity бот мгновенно и чисто завершает работу через поток-сторож на `psutil`.
* **Строгая авторизация**: Доступ разрешён исключительно для доверенного `chat_id` из конфигурации. Любые сообщения или нажатия кнопок от чужих пользователей немедленно и молча отклоняются.
* **Защита от инъекций и Path Traversal**: Все идентификаторы IPC строго валидируются по стандарту UUIDv4 (`^[0-9a-fA-F-]{36}$`), а тексты сообщений экранируются через безопасный HTML-escape.
* **Изоляция IPC и защита от Race Condition**: Запись JSON-файлов обмена производится атомарно через временные файлы (`os.replace` / Move-Item).

## ⚙️ Конфигурация

Ключи хранятся в изолированном файле настроек:
`~/.gemini/config/telegram.json` (или в корне скилла `telegram.json` / через `$env:TELEGRAM_CONFIG_PATH`)

```json
{
  "bot_token": "YOUR_BOT_TOKEN",
  "chat_id": 123456789
}
```

## 🚀 Использование

### 1. Односторонняя отправка уведомлений (`send-notify.ps1`)

Быстрая прямая отправка сообщений без ожидания:

```powershell
pwsh -File "$env:USERPROFILE\.gemini\config\skills\telegram-notify\scripts\send-notify.ps1" -Message "Деплой на сервер успешно завершён." -Status Success -Title "Деплой"
```

Параметры:
* `-Message` (обязательный) — текст сообщения (краткая выжимка или полный текст).
* `-Details` (опциональный) — расширенный блок (логи, стектрейс, таблица), автоматически прячется под сворачиваемый спойлер `<details>`.
* `-DetailsSummary` (опциональный) — заголовок спойлера деталей (по умолчанию `'Подробности'`).
* `-ExpandableBlockquote` (переключатель) — оформлять подробности в раскрываемую цитату `<blockquote expandable>` вместо спойлера.
* `-FilePath` (опциональный) — прикрепить локальный файл документа через Bot API `sendDocument`.
* `-AsDocument` (переключатель) — отправить весь текст сообщением-карточкой с вложенным файлом `.log`.
* `-Status` (опциональный) — `Success` (✅), `Error` (❌), `Wait` (⏳), `Info` (ℹ️, по умолчанию).
* `-Title` (опциональный) — заголовок сообщения (по умолчанию `'Antigravity'`).
* `-RawHtml` (переключатель) — отправка сырой HTML-разметки (таблицы, карусели, теги).
* `-NoRich` (переключатель) — принудительный режим классического `sendMessage` без Rich API.

#### Пример с длинными логами и сворачиваемым блоком Details:
```powershell
pwsh -File "$env:USERPROFILE\.gemini\config\skills\telegram-notify\scripts\send-notify.ps1" `
    -Title "Сборка проекта" `
    -Status Success `
    -Message "Сборка успешно завершена за 42 секунды." `
    -Details "stdout: Build target ready`nwarning: 0 errors, 2 warnings`nartifacts: release.zip" `
    -DetailsSummary "Показать лог сборки"
```
*(Поддерживает до 32 768 символов через `sendRichMessage`. При превышении 32 000 знаков скрипт автоматически прикрепляет полный лог файлом `.log` через `sendDocument`)*

---

### 2. Интерактивный запрос с ожиданием ответа (`send-notify.ps1 -WaitReply` или `wait-reply.ps1`)

Агент отправляет вопрос с кнопками вариантов и блокирует терминал до получения выбора пользователя в Telegram:

#### Пример с выбором кнопками:
```powershell
pwsh -File "$env:USERPROFILE\.gemini\config\skills\telegram-notify\scripts\send-notify.ps1" `
    -Message "Тесты пройдены. Запустить релизную сборку или перепроверить линтер?" `
    -Options "Запустить релиз", "Перепроверить", "Отмена" `
    -Status Wait `
    -Title "Решение по релизу" `
    -WaitReply
```
*Скрипт вернёт выбранную строку (например, `Запустить релиз`) прямо в stdout PowerShell.*

#### Пример прямого вызова `wait-reply.ps1`:
```powershell
$choice = pwsh -File "$env:USERPROFILE\.gemini\config\skills\telegram-notify\scripts\wait-reply.ps1" `
    -Prompt "Обнаружены конфликты в файлах. Перезаписать?" `
    -Options "Да, перезаписать", "Пропустить" `
    -TimeoutSeconds 180
```
*(Для получения структурированного объекта ответа с индексом кнопки и типом добавьте флаг `-PassThru`)*


---

### 3. Автономный мост (`telegram_bridge.py`)

Мост автоматически запускается скриптами `wait-reply.ps1` при необходимости.
Также его можно запустить вручную в терминале для отладки:

```powershell
python "$env:USERPROFILE\.gemini\config\skills\telegram-notify\scripts\telegram_bridge.py"
```

Команды в чате бота Telegram:
* `/status` — моментальный отчёт о нагрузке CPU, памяти RAM, свободном месте на диске C: и процессах агента.
* `/help` — справочная информация по возможностям бота.
