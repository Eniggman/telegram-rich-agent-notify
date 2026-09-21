#!/usr/bin/env python3

"""
Двусторонний Telegram-мост для Antigravity.

Архитектурная концепция:
1. Автономный фоновый сервис, связывающий терминальные агенты Antigravity и Telegram.
2. Жизненный цикл привязан к родительскому процессу Antigravity.exe: при его завершении
   мост мгновенно завершает работу через watchdog-поток с использованием psutil.
3. Полная изоляция и безопасность: доступ разрешён строго для chat_id из конфигурации.
   Чужие запросы отбрасываются без утечки метаданных.
4. Межпроцессное взаимодействие (IPC) построено на файловом обмене JSON с валидацией UUID,
   защитой от Path Traversal и атомарной записью через временные файлы для исключения Race Condition.
"""

import argparse
import asyncio
import datetime
import functools
import html
import json
import logging
import msvcrt
import os
import re
import sys
import threading
import time
from pathlib import Path

# Архитектурное решение: принудительная настройка UTF-8 вывода для Windows консолей
if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
import uuid
from typing import Any

import psutil
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Настройка структурированного логирования для отслеживания событий безопасности и IPC
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] [%(name)s]: %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("TelegramBridge")

# Регулярное выражение для строгой валидации UUIDv4 идентификаторов запросов.
# Защищает от Path Traversal, внедрения спецсимволов и подделки файловых имён.
UUID_REGEX = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

DEFAULT_CONFIG_PATH = Path(os.environ.get("TELEGRAM_CONFIG_PATH") or (Path.home() / ".gemini" / "config" / "telegram.json"))
DEFAULT_IPC_DIR = Path(__file__).resolve().parent.parent / "ipc"
DEFAULT_WATCH_PROCESS = "Antigravity.exe"


def is_valid_uuid(val: Any) -> bool:
    """
    Архитектурное решение: строгая проверка идентификатора запроса по формату UUID.
    Любые строки с относительными путями (../), разделителями каталогов или инъекциями
    отсекаются на этом этапе до обращения к файловой системе.
    """
    if not isinstance(val, str):
        return False
    return bool(UUID_REGEX.match(val.strip()))


def safe_resolve_ipc_path(ipc_dir: Path, prefix: str, req_id: str) -> Path:
    """
    Безопасное разрешение пути к файлу в IPC каталоге.
    Исключает уязвимости Path Traversal путём валидации UUID и проверки,
    что результирующий путь физически находится внутри доверенного каталога IPC.
    """
    if not is_valid_uuid(req_id):
        raise ValueError(f"Некорректный формат UUID запроса: {req_id!r}")

    if not re.match(r"^[a-zA-Z0-9_]+$", prefix):
        raise ValueError(f"Недопустимый префикс файла IPC: {prefix!r}")

    resolved_ipc = ipc_dir.resolve()
    target_path = (resolved_ipc / f"{prefix}_{req_id}.json").resolve()

    # Проверка выхода за пределы доверенной директории (Path Traversal guard)
    if target_path.parent != resolved_ipc:
        raise ValueError(f"Попытка выхода за пределы IPC директории: {target_path}")

    return target_path


def atomic_write_json(file_path: Path, data: dict[str, Any]) -> None:
    """
    Атомарная запись JSON через временный файл и последующее переименование (os.replace).
    Архитектурное решение против Race Condition:
    Читающий процесс (PowerShell или другой поток) никогда не прочитает частично записанный
    или повреждённый файл. В Windows операция os.replace атомарна в пределах одной файловой системы,
    поэтому временный файл создаётся строго в той же директории.
    """
    parent_dir = file_path.parent
    parent_dir.mkdir(parents=True, exist_ok=True)
    temp_file = parent_dir / f".tmp_{uuid.uuid4().hex}"

    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_file, file_path)
    except Exception as e:
        logger.error(f"Ошибка атомарной записи в {file_path}: {e}")
        if temp_file.exists():
            try:
                temp_file.unlink()
            except OSError:
                pass
        raise


def convert_to_classic_html(text: str) -> str:
    """
    Преобразование Rich-разметки (Bot API 10.1+) в классический Telegram HTML.
    Сворачиваемые блоки <details> преобразуются в раскрываемые цитаты <blockquote expandable>.
    Заголовки h1-h6 преобразуются в полужирный текст, разделители hr — в черту,
    а неподдерживаемые контейнеры безопасно удаляются.
    """
    if not text:
        return ""
    res = text
    # Сворачиваемые блоки details -> раскрываемая цитата blockquote expandable
    res = re.sub(
        r"(?is)<details[^>]*>\s*<summary[^>]*>(.*?)</summary>(.*?)</details>",
        r"<blockquote expandable><b>\1</b>\n\2</blockquote>",
        res,
    )
    # Заголовки h1-h6 -> полужирный текст
    res = re.sub(r"(?is)<h[1-6][^>]*>(.*?)</h[1-6]>", r"\n<b>\1</b>\n", res)
    # Разделитель hr -> черта
    res = re.sub(r"(?i)<hr\s*/?>", r"\n----------------------------------------\n", res)
    # Подвал footer -> курсив
    res = re.sub(r"(?is)<footer[^>]*>(.*?)</footer>", r"\n<i>\1</i>\n", res)
    # Врезка aside -> цитата
    res = re.sub(r"(?is)<aside[^>]*>(.*?)</aside>", r"<blockquote>\1</blockquote>", res)
    # Элементы списков li -> маркер
    res = re.sub(r"(?is)<li[^>]*>(.*?)</li>", r"• \1\n", res)
    # Удаление несовместимых тегов (table, tr, th, td, caption, ul, ol, figure, figcaption, p, div)
    res = re.sub(r"(?i)</?(ul|ol|table|tr|th|td|caption|figure|figcaption|p|div)\b[^>]*>", "", res)
    # Схлопывание избыточных переводов строк
    res = re.sub(r"(\r?\n){3,}", "\n\n", res)
    return res.strip()


def format_telegram_message(
    title: str,
    prompt: str,
    status: str = "Wait",
    timestamp_str: str | None = None,
    details: str | None = None,
    details_summary: str = "Подробности",
    rich_mode: bool = False,
    expandable_blockquote: bool = False,
) -> str:
    """
    Форматирование текста сообщения для Telegram в формате HTML.
    Безопасность: все входящие параметры экранируются через html.escape(..., quote=True),
    чтобы исключить инъекции вредоносной разметки или XSS-подобных спецсимволов.

    В стандартном режиме (rich_mode=False) строго соблюдается лимит Telegram (4096 символов).
    В режиме Rich Message (rich_mode=True) поддерживается до 32 000 символов и интерактивные
    сворачиваемые блоки <details><summary>...</summary>...</details> или <blockquote expandable>.
    """
    emoji_map = {
        "Success": "✅",
        "Error": "❌",
        "Wait": "⏳",
        "Info": "ℹ️"
    }
    emoji = emoji_map.get(status, "⏳")
    time_str = timestamp_str or datetime.datetime.now().strftime("%H:%M:%S")

    str_title = str(title)
    if len(str_title) > 200:
        str_title = str_title[:197] + "..."

    str_prompt = str(prompt)
    if not rich_mode and len(str_prompt) > 3500:
        str_prompt = str_prompt[:3450] + "\n... [содержимое усечено для лимита Telegram]"
    elif rich_mode and len(str_prompt) > 30000:
        str_prompt = str_prompt[:29500] + "\n... [содержимое усечено для лимита Rich Message]"

    safe_title = html.escape(str_title, quote=True)
    safe_prompt = html.escape(str_prompt, quote=True)

    details_block = ""
    if details:
        str_details = str(details)
        if not rich_mode and len(str_details) > 1000:
            str_details = str_details[:950] + "..."
        elif rich_mode and len(str_details) > 25000:
            str_details = str_details[:24500] + "\n... [усечено]"

        safe_details = html.escape(str_details, quote=True)
        safe_sum = html.escape(details_summary or "Подробности", quote=True)

        if expandable_blockquote or not rich_mode:
            details_block = f"\n\n<blockquote expandable><b>{safe_sum}</b>\n{safe_details}</blockquote>"
        else:
            details_block = f"\n\n<details><summary>{safe_sum}</summary>\n{safe_details}\n</details>"

    return f"{emoji} <b>{safe_title}</b> <code>[{time_str}]</code>\n\n{safe_prompt}{details_block}"


def build_inline_keyboard(req_id: str, options: list[str]) -> InlineKeyboardMarkup:
    """
    Построение сетки интерактивных кнопок Telegram.
    Разметка адаптируется под длину текста: короткие кнопки компонуются по 2 в ряд
    для эргономики экрана смартфона, длинные — по одной на строку.
    Лимит callback_data в Telegram составляет 64 байта; формат '{uuid}:{idx}' занимает 38-40 байт,
    что полностью безопасно и укладывается в спецификацию Telegram Bot API.
    Пустые элементы отфильтровываются, исключая ошибку Telegram BUTTON_TEXT_INVALID.
    """
    valid_options = [str(opt).strip() for opt in options if str(opt).strip()]
    if not valid_options:
        return InlineKeyboardMarkup([])

    buttons = []
    short_options = all(len(opt) <= 18 for opt in valid_options) and len(valid_options) <= 4

    if short_options:
        row = []
        for idx, opt in enumerate(valid_options):
            cb_data = f"{req_id}:{idx}"
            display_text = opt if len(opt) <= 30 else opt[:27] + "..."
            row.append(InlineKeyboardButton(text=display_text, callback_data=cb_data))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
    else:
        for idx, opt in enumerate(valid_options):
            cb_data = f"{req_id}:{idx}"
            display_text = opt if len(opt) <= 50 else opt[:47] + "..."
            buttons.append([InlineKeyboardButton(text=display_text, callback_data=cb_data)])

    return InlineKeyboardMarkup(buttons)


def is_process_running(process_name: str) -> bool:
    """
    Проверка активности процесса по имени через psutil.
    Регистронезависимое сравнение с учётом специфики процессов Windows (с суффиксом .exe или без него).
    Перехватываются исключения отсутствия прав доступа к системным процессам.
    """
    target_lower = process_name.lower()
    target_base = target_lower.removesuffix(".exe")
    target_exe = f"{target_base}.exe"

    for proc in psutil.process_iter(["name"]):
        try:
            name = proc.info.get("name")
            if name:
                n_lower = name.lower()
                if n_lower == target_lower or n_lower == target_base or n_lower == target_exe:
                    return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return False


def is_antigravity_running() -> bool:
    """Специализированная проверка активности Antigravity.exe."""
    return is_process_running("Antigravity.exe")


def acquire_single_instance_lock(ipc_dir: Path) -> Any | None:
    """
    Гарантия единственного экземпляра бота через эксклюзивную файловую блокировку msvcrt.
    Архитектурное решение:
    В Windows параллельный запуск двух инстансов с одним bot_token вызывает конфликт
    getUpdates в Telegram Bot API. Эксклюзивная блокировка bridge.lock гарантирует,
    что второй инстанс мгновенно завершит работу без порчи данных и конфликтов с API.
    При падении или завершении процесса ОС Windows автоматически снимет блокировку.
    """
    ipc_dir.mkdir(parents=True, exist_ok=True)
    lock_file_path = ipc_dir / "bridge.lock"

    try:
        lock_file = open(lock_file_path, "w+")
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        pid_str = str(os.getpid())
        lock_file.write(f"PID={pid_str}\nSTARTED={datetime.datetime.now().isoformat()}\n")
        lock_file.flush()
        # Запись PID файла для мгновенной проверки из PowerShell без тяжёлых WMI/CIM запросов
        pid_file_path = ipc_dir / "bridge.pid"
        try:
            with open(pid_file_path, "w", encoding="utf-8") as pf:
                pf.write(pid_str)
        except Exception:
            pass
        return lock_file
    except OSError:
        return None


def authorized_only(func):
    """
    Декоратор строгой авторизации (Access Control Guard).
    Любое входящее сообщение или callback проверяется на совпадение chat_id
    с доверенным из config/telegram.json.
    Чужие запросы молча отбрасываются с записью в лог тревоги безопасности,
    предотвращая утечку данных и зондирование бота посторонними лицами.
    """
    @functools.wraps(func)
    async def wrapper(self, update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        effective_chat = update.effective_chat
        chat_id = effective_chat.id if effective_chat else None

        if chat_id is None or str(chat_id) != str(self.allowed_chat_id):
            user_desc = (
                f"id={chat_id}, username={getattr(update.effective_user, 'username', 'unknown')}, "
                f"name={getattr(update.effective_user, 'full_name', 'unknown')}"
            )
            logger.warning(f"ОТКЛОНЕНА НЕАВТОРИЗОВАННАЯ ПОПЫТКА ДОСТУПА: {user_desc}")
            return None

        return await func(self, update, context, *args, **kwargs)
    return wrapper


class TelegramBridge:
    """
    Основной контроллер двустороннего моста Telegram.
    Управляет жизненным циклом, опросом IPC каталога, маршрутизацией сообщений и командами.
    """

    def __init__(
        self,
        bot_token: str,
        allowed_chat_id: int,
        ipc_dir: Path = DEFAULT_IPC_DIR,
        watch_process: str = DEFAULT_WATCH_PROCESS,
        enable_watchdog: bool = True,
        watchdog_interval: float = 2.5,
        lifetime_seconds: float = 3600.0,
    ):
        self.bot_token = bot_token
        self.allowed_chat_id = allowed_chat_id
        self.ipc_dir = Path(ipc_dir)
        self.watch_process = watch_process
        self.enable_watchdog = enable_watchdog
        self.watchdog_interval = watchdog_interval
        self.lifetime_seconds = float(lifetime_seconds)
        self.start_time = time.time()

        # Хранилище активных запросов в памяти: {req_id: {message_id, options, formatted_text, created_at, ...}}
        self.active_requests: dict[str, dict[str, Any]] = {}
        # Хранилище завершённых запросов: {req_id: timestamp_finished}
        self.completed_requests: dict[str, float] = {}
        self.lock_handle = None
        self._stop_event = threading.Event()
        self._watchdog_thread: threading.Thread | None = None

    def _cleanup_lock_and_pid(self) -> None:
        """Безопасная очистка файлов блокировки и PID перед выходом."""
        try:
            pid_file = self.ipc_dir / "bridge.pid"
            if pid_file.exists():
                pid_file.unlink()
        except Exception:
            pass
        if self.lock_handle:
            try:
                self.lock_handle.close()
            except Exception:
                pass
        try:
            lock_file = self.ipc_dir / "bridge.lock"
            if lock_file.exists():
                lock_file.unlink()
        except Exception:
            pass

    def start_watchdog(self) -> None:
        """
        Запуск watchdog-потока для контроля времени сессии (строго 1 час)
        и активности процесса Antigravity.exe.
        """
        def _watchdog_worker():
            logger.info(
                f"Watchdog активирован: строгий лимит работы {int(self.lifetime_seconds)} с (1 час), "
                f"мониторинг процесса '{self.watch_process}' раз в {self.watchdog_interval} с."
            )
            while not self._stop_event.wait(self.watchdog_interval):
                now = time.time()

                # 1. Строгий лимит работы сессии моста (1 час)
                if self.lifetime_seconds > 0 and (now - self.start_time >= self.lifetime_seconds):
                    logger.info(
                        f"Истёк строгий лимит работы сессии моста ({int(self.lifetime_seconds)} с = 1 час). "
                        "Плановое автовыключение."
                    )
                    self._cleanup_lock_and_pid()
                    os._exit(0)

                # 2. Проверка закрытия окна Antigravity
                if self.enable_watchdog and not is_process_running(self.watch_process):
                    logger.warning(
                        f"Процесс '{self.watch_process}' не обнаружен в системе. "
                        "Antigravity закрыт -> завершение работы Telegram-моста."
                    )
                    self._cleanup_lock_and_pid()
                    os._exit(0)

        self._watchdog_thread = threading.Thread(
            target=_watchdog_worker,
            daemon=True,
            name="AntigravityProcessWatchdog"
        )
        self._watchdog_thread.start()

    @authorized_only
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Команда /start: приветствие и подтверждение готовности моста."""
        if not update.message:
            return
        text = (
            "🤖 <b>Antigravity Telegram Bridge активен</b>\n\n"
            "Мост обеспечивает двустороннюю связь между Antigravity и Telegram.\n"
            "Вы можете получать запросы подтверждений, выбирать варианты кнопками или текстом.\n\n"
            "Доступные команды:\n"
            "/status — текущее состояние системы и процесса Antigravity\n"
            "/help — подробная справка"
        )
        await update.message.reply_text(text, parse_mode="HTML")

    @authorized_only
    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Команда /help: справка по интерактивному взаимодействию."""
        if not update.message:
            return
        text = (
            "📖 <b>Справка Telegram Bridge</b>\n\n"
            "<b>Интерактивные запросы:</b>\n"
            "Когда агенту требуется подтверждение решения, в чат отправляется сообщение с вариантами выбора.\n"
            "• Нажмите нужную инлайн-кнопку под сообщением.\n"
            "• Либо напишите ответ обычным текстом (или ответом / reply на конкретное сообщение).\n\n"
            "<b>Команды:</b>\n"
            "/status — системный мониторинг (CPU, RAM, диск, статус процесса Antigravity)\n"
            "/help — данное справочное сообщение\n\n"
            "<i>Безопасность: бот принимает команды исключительно от вашего доверенного чата.</i>"
        )
        await update.message.reply_text(text, parse_mode="HTML")

    @authorized_only
    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Команда /status: безопасный сбор метрик ОС и Antigravity.
        Выводит нагрузку на процессор, память, состояние диска C:, аптайм и статус процессов Antigravity.
        Архитектурное решение: используется неблокирующий вызов cpu_percent(interval=None),
        чтобы не блокировать основной поток цикла событий asyncio.
        """
        if not update.message:
            return

        cpu_percent = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage(r"C:\\")
        boot_time = datetime.datetime.fromtimestamp(psutil.boot_time()).strftime("%Y-%m-%d %H:%M:%S")

        # Анализ процессов Antigravity (устойчивый к регистру и суффиксу .exe)
        target_lower = self.watch_process.lower()
        target_base = target_lower.removesuffix(".exe")
        target_exe = f"{target_base}.exe"

        ag_procs = []
        for p in psutil.process_iter(["name", "pid", "memory_info"]):
            try:
                name = p.info.get("name")
                if name:
                    n_lower = name.lower()
                    if n_lower == target_lower or n_lower == target_base or n_lower == target_exe:
                        ag_procs.append(p)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        if ag_procs:
            total_rss_mb = sum(p.info["memory_info"].rss for p in ag_procs if p.info.get("memory_info")) / (1024 * 1024)
            ag_status_text = f"✅ Запущен ({len(ag_procs)} процессов, {total_rss_mb:.1f} MB RAM)"
        else:
            ag_status_text = f"❌ Не обнаружен ({self.watch_process})"

        elapsed = time.time() - self.start_time
        remaining = max(0, self.lifetime_seconds - elapsed) if self.lifetime_seconds > 0 else 0
        rem_min = int(remaining // 60)
        session_info = f"активен {int(elapsed // 60)} мин (до сна: {rem_min} мин)" if self.lifetime_seconds > 0 else "бессрочный"

        msg = (
            "📊 <b>Системный статус Antigravity</b>\n\n"
            f"🖥 <b>Процесс Antigravity:</b> {ag_status_text}\n"
            f"⏳ <b>Сессия бота:</b> {session_info}\n"
            f"⚙️ <b>CPU:</b> {cpu_percent}%\n"
            f"🧠 <b>RAM:</b> {mem.percent}% ({mem.used / (1024**3):.1f} / {mem.total / (1024**3):.1f} GB)\n"
            f"💾 <b>Диск C:</b> {disk.percent}% свободно {disk.free / (1024**3):.1f} GB\n"
            f"⏱ <b>Старт ОС:</b> {boot_time}\n"
            f"📬 <b>Активных запросов IPC:</b> {len(self.active_requests)}"
        )
        await update.message.reply_text(msg, parse_mode="HTML")

    @authorized_only
    async def handle_callback_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Обработка нажатий inline-кнопок.
        Формат данных в кнопке: '{req_id}:{option_index}'.
        Защита от повторных нажатий: после выбора кнопки удаляются, а результат записывается в IPC.
        """
        query = update.callback_query
        raw_data = query.data or ""

        parts = raw_data.split(":", 1)
        if len(parts) != 2:
            await query.answer("Неверный формат данных кнопки.", show_alert=True)
            return

        req_id, idx_str = parts[0], parts[1]
        if not is_valid_uuid(req_id) or not idx_str.isdigit():
            await query.answer("Недопустимый идентификатор запроса.", show_alert=True)
            return

        if req_id not in self.active_requests:
            await query.answer("Срок действия запроса истёк или он уже был обработан.", show_alert=True)
            return

        idx = int(idx_str)
        req_meta = self.active_requests[req_id]
        options = req_meta.get("options", [])

        if idx < 0 or idx >= len(options):
            await query.answer("Выбран несуществующий вариант.", show_alert=True)
            return

        selected_text = options[idx]
        await query.answer(f"Выбрано: {selected_text}")

        # Атомарная запись ответа для PowerShell-скрипта
        resp_payload = {
            "id": req_id,
            "reply": selected_text,
            "option_index": idx,
            "type": "button",
            "timestamp": time.time(),
            "answered_at": datetime.datetime.now().isoformat()
        }

        resp_file = safe_resolve_ipc_path(self.ipc_dir, "response", req_id)
        atomic_write_json(resp_file, resp_payload)

        # Фиксируем запрос в списке завершённых для исключения повторной отправки в Telegram
        self.completed_requests[req_id] = time.time()

        # Обновление сообщения в Telegram: убираем клавиатуру и фиксируем выбранную опцию
        try:
            updated_text = (
                f"{req_meta['formatted_text']}\n\n"
                f"<b>✅ Выбрано:</b> <code>{html.escape(selected_text, quote=True)}</code>"
            )
            classic_text = convert_to_classic_html(updated_text)
            await query.edit_message_text(text=classic_text, parse_mode="HTML", reply_markup=None)
        except Exception as e:
            logger.warning(f"Не удалось обновить текст сообщения Telegram: {e}")

        # Удаляем из списка ожидающих запросов
        self.active_requests.pop(req_id, None)

        # Очищаем обработанный файл запроса
        try:
            req_file = safe_resolve_ipc_path(self.ipc_dir, "request", req_id)
            if req_file.exists():
                req_file.unlink()
        except Exception:
            pass

    @authorized_only
    async def handle_text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Обработка свободных текстовых сообщений пользователя.
        Связывает текстовый ответ с активным IPC-запросом:
        1. Если пользователь ответил реплаем (reply) на сообщение бота — привязка к конкретному запросу.
        2. Иначе — привязка к самому свежему ожидающему запросу.
        """
        if not update.message or not update.message.text:
            return

        user_text = update.message.text.strip()
        target_req_id = None

        # Проверка реплая на конкретное сообщение бота
        if update.message.reply_to_message:
            rep_id = update.message.reply_to_message.message_id
            for r_id, meta in self.active_requests.items():
                if meta.get("message_id") == rep_id:
                    target_req_id = r_id
                    break

        # Если реплая нет, берём последний по времени активный запрос
        if not target_req_id and self.active_requests:
            target_req_id = max(
                self.active_requests.keys(),
                key=lambda k: self.active_requests[k].get("created_at", 0)
            )

        if not target_req_id:
            elapsed = time.time() - self.start_time
            remaining = max(0, self.lifetime_seconds - elapsed) if self.lifetime_seconds > 0 else 0
            rem_min = int(remaining // 60)
            await update.message.reply_text(
                f"ℹ️ Сейчас нет ожидающих вопросов от агента.\n\n"
                f"<i>Бот на связи ещё {rem_min} мин. Как только агенту понадобится твоё решение — сообщение сразу появится в этом чате.</i>",
                parse_mode="HTML"
            )
            return

        req_meta = self.active_requests[target_req_id]

        # Проверяем, совпадает ли текст пользователя с одним из вариантов (опций)
        selected_idx = -1
        req_options = req_meta.get("options", [])
        for idx, opt in enumerate(req_options):
            if user_text.strip().lower() == str(opt).strip().lower():
                selected_idx = idx
                break

        # Атомарная запись текстового ответа в IPC
        resp_payload = {
            "id": target_req_id,
            "reply": user_text,
            "option_index": selected_idx,
            "type": "text",
            "timestamp": time.time(),
            "answered_at": datetime.datetime.now().isoformat()
        }

        resp_file = safe_resolve_ipc_path(self.ipc_dir, "response", target_req_id)
        atomic_write_json(resp_file, resp_payload)

        # Фиксируем запрос в списке завершённых
        self.completed_requests[target_req_id] = time.time()

        # Подтверждение приёма ответа пользователю
        safe_reply = html.escape(user_text, quote=True)
        await update.message.reply_text(f"✅ Принято, передал агенту:\n<code>{safe_reply}</code>", parse_mode="HTML")

        # Редактирование оригинального сообщения запроса
        try:
            updated_text = (
                f"{req_meta['formatted_text']}\n\n"
                f"<b>💬 Ответ текстом:</b> <code>{safe_reply}</code>"
            )
            classic_text = convert_to_classic_html(updated_text)
            await context.bot.edit_message_text(
                chat_id=self.allowed_chat_id,
                message_id=req_meta["message_id"],
                text=classic_text,
                parse_mode="HTML",
                reply_markup=None
            )
        except Exception as e:
            logger.warning(f"Не удалось обновить оригинальное сообщение {req_meta.get('message_id')}: {e}")

        self.active_requests.pop(target_req_id, None)

        # Очищаем обработанный файл запроса
        try:
            req_file = safe_resolve_ipc_path(self.ipc_dir, "request", target_req_id)
            if req_file.exists():
                req_file.unlink()
        except Exception:
            pass

    async def scan_and_process_ipc(self, bot) -> None:
        """
        Фоновый цикл сканирования каталога IPC на появление новых файлов request_<UUID>.json.
        При обнаружении нового запроса формирует сообщение с кнопками и отправляет в Telegram.
        Также очищает устаревшие записи, чей таймаут истёк или файлы удалены.
        """
        if not self.ipc_dir.exists():
            return

        now = time.time()

        # 1. Поиск всех файлов запросов
        for req_file in list(self.ipc_dir.glob("request_*.json")):
            filename = req_file.name
            req_id = filename[len("request_"):-len(".json")]

            # Защита от некорректных имён файлов
            if not is_valid_uuid(req_id):
                logger.warning(f"Пропущен файл с некорректным UUID: {filename}")
                continue

            # Если запрос уже активен в памяти — пропускаем
            if req_id in self.active_requests:
                continue

            # Если запрос уже был завершён ранее — пропускаем
            if req_id in self.completed_requests:
                continue

            # Если файл ответа уже существует в IPC — значит запрос уже выполнен, пропускаем!
            try:
                resp_file = safe_resolve_ipc_path(self.ipc_dir, "response", req_id)
                if resp_file.exists():
                    self.completed_requests[req_id] = now
                    continue
            except Exception:
                pass

            try:
                with open(req_file, "r", encoding="utf-8-sig") as f:
                    data = json.load(f)
            except Exception as e:
                # Если файл пуст или повреждён дольше 30 секунд — архивируем/удаляем
                try:
                    if now - req_file.stat().st_mtime > 30:
                        logger.error(f"Удаление повреждённого файла IPC {filename}: {e}")
                        req_file.unlink(missing_ok=True)
                except Exception:
                    pass
                continue

            # Проверка возраста запроса (TTL): если запрос старее таймаута (по умолчанию 300 с),
            # удаляем его без отправки пользователю в Telegram во избежание спама устаревшими вопросами.
            created_at = data.get("created_at")
            if not created_at:
                try:
                    created_at = req_file.stat().st_mtime
                except Exception:
                    created_at = now

            timeout_sec = float(data.get("timeout_seconds") or data.get("timeout") or 300)
            if now - float(created_at) > max(timeout_sec, 60):
                logger.warning(f"Запрос {req_id} устарел (возраст > {timeout_sec} с). Удаление устаревшего файла без отправки.")
                try:
                    req_file.unlink(missing_ok=True)
                except Exception:
                    pass
                self.completed_requests[req_id] = now
                continue

            prompt = data.get("prompt") or data.get("message") or "Требуется подтверждение"
            title = data.get("title") or "Antigravity"
            status = data.get("status") or "Wait"
            options = data.get("options") or []
            details = data.get("details")
            details_summary = data.get("details_summary") or "Подробности"
            expandable = bool(data.get("expandable_blockquote") or data.get("expandable"))

            formatted_html = format_telegram_message(
                title=title,
                prompt=prompt,
                status=status,
                details=details,
                details_summary=details_summary,
                rich_mode=True,
                expandable_blockquote=expandable,
            )
            keyboard = build_inline_keyboard(req_id, options) if options else None

            sent_msg = None
            # Пробуем отправить через Rich Message API если это реальный Bot и метод доступен
            if hasattr(bot, "_post") and not type(bot).__name__.startswith("Magic"):
                try:
                    rich_payload = {
                        "chat_id": self.allowed_chat_id,
                        "rich_message": {"html": formatted_html}
                    }
                    if keyboard:
                        rich_payload["reply_markup"] = keyboard.to_dict()
                    resp = await bot._post("sendRichMessage", data=rich_payload)
                    if isinstance(resp, dict) and "message_id" in resp:
                        class RichMessageProxy:
                            def __init__(self, msg_id):
                                self.message_id = msg_id
                        sent_msg = RichMessageProxy(resp["message_id"])
                except Exception as e:
                    logger.debug(f"sendRichMessage не удался, fallback на send_message: {e}")

            if sent_msg is None:
                classic_html = convert_to_classic_html(formatted_html)
                try:
                    sent_msg = await bot.send_message(
                        chat_id=self.allowed_chat_id,
                        text=classic_html,
                        parse_mode="HTML",
                        reply_markup=keyboard
                    )
                except Exception as e:
                    logger.error(f"Ошибка при отправке запроса {req_id} в Telegram: {e}")
                    continue

            self.active_requests[req_id] = {
                "message_id": sent_msg.message_id,
                "options": options,
                "formatted_text": formatted_html,
                "created_at": float(created_at),
                "timeout": timeout_sec,
                "file_path": req_file
            }
            logger.info(f"Запрос {req_id} успешно доставлен в Telegram (msg_id={sent_msg.message_id}).")

        # 2. Очистка записей, чьи файлы были удалены вызывающим скриптом (по таймауту или завершению)
        for req_id in list(self.active_requests.keys()):
            req_meta = self.active_requests[req_id]
            req_file = req_meta.get("file_path")
            is_stale_time = now - req_meta.get("created_at", 0) > req_meta.get("timeout", 300)
            file_deleted = req_file and not req_file.exists()

            if file_deleted or is_stale_time:
                logger.info(f"Запрос {req_id} закрыт (файл_удалён={file_deleted}, таймаут={is_stale_time}). Снятие кнопок в Telegram.")
                # Снимаем кнопки в Telegram и помечаем как отменённый/истёкший
                try:
                    await bot.edit_message_text(
                        chat_id=self.allowed_chat_id,
                        message_id=req_meta["message_id"],
                        text=f"{req_meta['formatted_text']}\n\n<i>⏳ Время ожидания ответа истекло (запрос закрыт).</i>",
                        parse_mode="HTML",
                        reply_markup=None
                    )
                except Exception as e:
                    logger.debug(f"Не удалось обновить сообщение Telegram при отмене {req_id}: {e}")

                self.completed_requests[req_id] = now
                self.active_requests.pop(req_id, None)

        # 3. Очистка старых записей completed_requests (храним не более 15 минут)
        for req_id, finished_at in list(self.completed_requests.items()):
            if now - finished_at > 900:
                del self.completed_requests[req_id]

        # 4. Очистка зависших временных файлов .tmp_* старше 60 секунд
        try:
            for tmp_file in self.ipc_dir.glob(".tmp_*"):
                if tmp_file.is_file() and (now - tmp_file.stat().st_mtime > 60):
                    tmp_file.unlink(missing_ok=True)
        except Exception:
            pass

    async def ipc_poll_task(self, application: Application) -> None:
        """Асинхронная задача периодического опроса IPC."""
        logger.info("Фоновая задача опроса IPC запущена.")
        while True:
            try:
                await self.scan_and_process_ipc(application.bot)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Ошибка в цикле сканирования IPC: {e}", exc_info=True)
            await asyncio.sleep(0.8)

    async def post_init(self, application: Application) -> None:
        """Инициализация фоновых асинхронных задач после старта Application."""
        asyncio.create_task(self.ipc_poll_task(application))

    def run(self) -> None:
        """Точка входа запуска бота."""
        # 1. Захват эксклюзивного лока процесса
        self.lock_handle = acquire_single_instance_lock(self.ipc_dir)
        if not self.lock_handle:
            logger.warning("Экземпляр TelegramBridge уже запущен. Завершение работы дубликата.")
            sys.exit(0)

        # 2. Запуск процесса-сторожа (Watchdog)
        self.start_watchdog()

        # 3. Сборка приложения python-telegram-bot
        application = (
            ApplicationBuilder()
            .token(self.bot_token)
            .post_init(self.post_init)
            .build()
        )

        # Регистрация обработчиков команд и событий
        application.add_handler(CommandHandler("start", self.cmd_start))
        application.add_handler(CommandHandler("help", self.cmd_help))
        application.add_handler(CommandHandler("status", self.cmd_status))
        application.add_handler(CallbackQueryHandler(self.handle_callback_query))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text_message))

        logger.info(f"TelegramBridge запущен. Авторизованный chat_id: {self.allowed_chat_id}")

        try:
            application.run_polling(drop_pending_updates=False)
        finally:
            self._stop_event.set()
            if self.lock_handle:
                try:
                    self.lock_handle.close()
                except Exception:
                    pass
            pid_file = self.ipc_dir / "bridge.pid"
            if pid_file.exists():
                try:
                    pid_file.unlink()
                except Exception:
                    pass


def load_config(config_path: Path) -> dict[str, Any]:
    """Загрузка и валидация конфигурационного файла telegram.json."""
    if not config_path.exists():
        raise FileNotFoundError(f"Конфигурационный файл не найден: {config_path}")

    with open(config_path, "r", encoding="utf-8-sig") as f:
        data = json.load(f)

    bot_token = data.get("bot_token")
    chat_id = data.get("chat_id")

    if not bot_token or not str(bot_token).strip():
        raise ValueError(f"Поле 'bot_token' отсутствует в {config_path}")

    if not chat_id:
        raise ValueError(f"Поле 'chat_id' отсутствует в {config_path}")

    return {
        "bot_token": str(bot_token).strip(),
        "chat_id": int(chat_id)
    }


def main():
    parser = argparse.ArgumentParser(description="Двусторонний Telegram-мост для Antigravity")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Путь к telegram.json")
    parser.add_argument("--ipc-dir", type=Path, default=DEFAULT_IPC_DIR, help="Путь к каталогу IPC")
    parser.add_argument("--watch-process", type=str, default=DEFAULT_WATCH_PROCESS, help="Имя процесса для контроля")
    parser.add_argument("--no-watchdog", action="store_true", help="Отключить контроль процесса Antigravity")
    parser.add_argument("--lifetime", type=float, default=3600.0, help="Время работы сессии моста в секундах (по умолчанию 3600 = 1 час)")
    args = parser.parse_args()

    try:
        cfg = load_config(args.config)
    except Exception as e:
        logger.error(f"Не удалось загрузить конфигурацию: {e}")
        sys.exit(1)

    enable_watchdog = not args.no_watchdog and os.getenv("ANTIGRAVITY_NO_WATCHDOG") != "1"

    bridge = TelegramBridge(
        bot_token=cfg["bot_token"],
        allowed_chat_id=cfg["chat_id"],
        ipc_dir=args.ipc_dir,
        watch_process=args.watch_process,
        enable_watchdog=enable_watchdog,
        lifetime_seconds=args.lifetime
    )
    bridge.run()


if __name__ == "__main__":
    main()
