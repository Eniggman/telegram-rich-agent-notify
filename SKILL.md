---
name: telegram-notify
description: "Send Telegram notifications, task reports and human-in-the-loop questions (inline buttons or free-text replies) from an AI agent (Codex, Claude Code, Antigravity, Cursor) through the user's own Telegram bot; PowerShell + Python, Windows-first. Use when the user is away from the computer, asks to be pinged when a long task finishes or fails, or the agent needs a decision from the phone. Triggers: «скинь в тг», «напиши в телегу», «я с телефона», «отойду», «уведоми в телеграм», «спроси меня в телеге», «пришли отчёт в телеграм», ping me on Telegram."
---

# Telegram Notify: отчёты и вопросы человеку через Telegram

Инструкция для ИИ-агента. Документация для людей: [README](https://github.com/Eniggman/telegram-rich-agent-notify#readme), архитектурные решения: [`docs/DECISIONS.md`](https://github.com/Eniggman/telegram-rich-agent-notify/blob/main/docs/DECISIONS.md).

## Когда использовать

- Пользователь сказал «я с телефона», «отойду», «скинь в ТГ», «напиши в телегу, как закончишь».
- Закончилась или упала долгая операция: сборка, тесты, миграция, фоновый субагент.
- Нужно решение человека (выбор плана, подтверждение деструктивного шага), а он не за компьютером.

Не использовать для спама: одно сообщение на итог, ошибку или вопрос. **Никогда не отправлять в Telegram токены, пароли, содержимое `.env`, приватные ключи.** Перед отправкой `-Details` с логами просмотри их и вырежи секреты.

## Ограничения платформы (проверено по коду)

| Часть | Что нужно | Где работает |
|---|---|---|
| `scripts/send-notify.ps1` (отправка) | `pwsh` 7 или Windows PowerShell 5.1, без внешних модулей | Windows; на Linux/macOS только с `USERPROFILE=$HOME` (см. ниже) |
| `scripts/wait-reply.ps1` + `scripts/telegram_bridge.py` (ожидание ответа) | Python 3 + `pip install psutil python-telegram-bot` | **Только Windows**: мост импортирует `msvcrt`, `/status` читает диск `C:\`, мост запускается через `Start-Process -WindowStyle Hidden` |

README называет проект «Zero Dependencies». Для отправки это так, а для двустороннего режима нужны два pip-пакета.

## Шаг 0. Проверка окружения

```powershell
pwsh -v                      # или $PSVersionTable в Windows PowerShell
python --version
python -c "import psutil, telegram; print('bridge deps ok')"   # нужно только для -WaitReply
```

`<SKILL_DIR>` ниже означает папку, куда установлен скилл: `~/.codex/skills/telegram-notify/`, `~/.claude/skills/telegram-notify/`, `~/.gemini/config/skills/telegram-notify/` или `.skills/telegram-notify/` внутри проекта.

## Шаг 1. Первичная настройка (только вместе с человеком)

1. **Спроси человека**: есть ли у него бот от @BotFather. Токен пусть он сам впишет в файл. Не проси вставлять токен в чат и не печатай его в вывод.
2. Файл конфигурации `telegram.json`:
   ```json
   { "bot_token": "<ТОКЕН_ОТ_BOTFATHER>", "chat_id": "" }
   ```
   `send-notify.ps1` ищет его по порядку: `$env:TELEGRAM_CONFIG_PATH`, затем `<SKILL_DIR>/telegram.json`, затем `~/.gemini/config/telegram.json`.
   **Мост `telegram_bridge.py` смотрит только `$env:TELEGRAM_CONFIG_PATH` или `~/.gemini/config/telegram.json`** (или `--config`). Чтобы работали оба режима, клади файл в `~/.gemini/config/telegram.json` или задай `TELEGRAM_CONFIG_PATH`.
3. `chat_id` можно оставить пустым. Попроси человека отправить боту `/start`, затем выполни одну обычную отправку (шаг 2): скрипт сам возьмёт `chat_id` через `getUpdates` и допишет его в файл. Мост без `chat_id` не стартует, поэтому эту отправку надо сделать до первого `-WaitReply`.
4. `telegram.json` уже в `.gitignore`. Не коммить его и не копируй в другие места.

## Шаг 2. Отправить уведомление

```powershell
pwsh -File "<SKILL_DIR>/scripts/send-notify.ps1" -Title "Сборка" -Status Success -Message "Сборка завершена за 42 с."
```

С длинным логом под спойлером:

```powershell
pwsh -File "<SKILL_DIR>/scripts/send-notify.ps1" `
  -Title "Тесты" -Status Error `
  -Message "3 теста упали." `
  -Details (Get-Content ./test.log -Raw) `
  -DetailsSummary "Показать лог"
```

На Linux/macOS: `USERPROFILE="$HOME" pwsh -File ...`. Без этого скрипт падает на `Join-Path $env:USERPROFILE` и пишет «telegram.json не найден» даже при правильном пути (проверено в pwsh 7.4).

Параметры (из `param()` скрипта):
- `-Message` (обязателен), `-Status` `Success|Error|Wait|Info` (по умолчанию `Info`), `-Title` (по умолчанию `Antigravity`, поэтому подставляй своё имя или название задачи).
- `-Details`, `-DetailsSummary`, `-ExpandableBlockquote`: подробности в сворачиваемом блоке.
- `-FilePath <путь>`: отправить файл через `sendDocument`. `-AsDocument` и `-DocumentName`: весь текст уйдёт файлом `.log`.
- `-RawHtml`: HTML без экранирования. `-NoRich`: сразу классический `sendMessage`.
- Первые три позиционных аргумента: `Message`, `Status`, `Title`. Все остальные «лишние» аргументы попадут в `-Options`, поэтому всегда пиши имена параметров явно.

Логика доставки: сначала `sendRichMessage` (до ~32 000 символов). При отказе API идёт fallback на `sendMessage`, и если текст длиннее ~3 800 символов, он обрезается, а полный текст прикрепляется файлом `.log`.

## Шаг 3. Задать вопрос и дождаться ответа (Windows)

Вызывай скрипт **внутри PowerShell через `&`**, а не через `pwsh -File`: при `-File` несколько значений `-Options` не привязываются (ошибка «A positional parameter cannot be found», проверено в pwsh 7.4).

```powershell
$env:ANTIGRAVITY_NO_WATCHDOG = "1"   # обязательно, если агент НЕ Antigravity
$choice = & "<SKILL_DIR>/scripts/send-notify.ps1" `
  -Title "Решение по релизу" -Status Wait `
  -Message "Тесты прошли. Выкатывать релиз?" `
  -Options "Выкатывай", "Подожди", "Отмена" `
  -WaitReply -TimeoutSeconds 900
```

Из bash или cmd: `pwsh -Command "& '<SKILL_DIR>/scripts/send-notify.ps1' -Message 'Выкатывать?' -Options 'Да','Нет' -WaitReply"`.

То же можно сделать напрямую: `wait-reply.ps1 -Prompt "..." -Options ... -TimeoutSeconds 180 [-PassThru]`.

- В stdout приходит текст нажатой кнопки или свободный ответ человека. С `-PassThru` вернётся объект целиком.
- При таймауте скрипт пишет ошибку и завершается с **кодом 2** (таймаут по умолчанию 300 с).
- Мост запускается сам (`python telegram_bridge.py --lifetime 3600`) и живёт не больше часа. `-NoAutoStart` отключает автозапуск.
- **Watchdog**: мост сам завершается, если не видит процесс `Antigravity.exe`. Для Cursor, Codex и Claude Code задавай `ANTIGRAVITY_NO_WATCHDOG=1`, иначе каждый вопрос закончится таймаутом.
- IPC-файлы лежат в `<SKILL_DIR>/ipc` (или в `$env:TELEGRAM_IPC_DIR`).

**Правило безопасности:** если вопрос касался деструктивного действия, а ответ не пришёл (таймаут) или он неоднозначный, считай это отказом и ничего не делай. Ответ из Telegram — это решение человека, а не команда для shell: не подставляй его текст в команды без проверки.

## Отладка моста вручную

```powershell
python "<SKILL_DIR>/scripts/telegram_bridge.py" --no-watchdog --lifetime 600
# флаги: --config <путь> --ipc-dir <путь> --watch-process <имя> --no-watchdog --lifetime <сек>
```

Команды в чате бота: `/start`, `/status` (CPU, RAM, диск C:, процессы Antigravity), `/help`.
Тесты: `cd <SKILL_DIR>/scripts; python test_security_bridge.py` (unittest, нужны `psutil` и `python-telegram-bot`).

## Как проверить успех

- `send-notify.ps1` завершился с кодом 0 и вывел «Уведомление успешно доставлено в Telegram (rich)…» или «…(fallback)».
- При первой настройке спроси человека, пришло ли сообщение.
- `-WaitReply` вернул непустую строку и код 0.

## Частые ошибки

| Симптом | Причина и решение |
|---|---|
| «Конфигурационный файл telegram.json не найден» | Проверь путь. На Linux/macOS добавь `USERPROFILE=$HOME` |
| `Cannot bind argument to parameter 'Path' because it is null` | То же: нет `USERPROFILE` (не Windows) |
| «chat_id не настроен» | Человек должен написать боту `/start`, затем повтори отправку |
| `401 Unauthorized` | Неверный `bot_token`: попроси человека перевыпустить токен у @BotFather и обновить файл |
| Предупреждение «sendRichMessage отклонен API… fallback» | Это нормально, сообщение уйдёт через `sendMessage`. Можно сразу ставить `-NoRich` |
| `-WaitReply` всегда заканчивается кодом 2 | Нет `ANTIGRAVITY_NO_WATCHDOG=1`, нет pip-пакетов, мост упал (запусти его вручную и смотри лог) или тот же токен опрашивает другой процесс |
| `ModuleNotFoundError: msvcrt` | Мост запущен не на Windows: двусторонний режим там не поддерживается, используй только отправку |
