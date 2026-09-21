#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Набор тестов безопасности и функционала для Telegram Bridge:
1. Тест 1: Проверка строгой авторизации chat_id (отклонение неавторизованных запросов).
2. Тест 2: Санитайзинг, экранирование HTML, защита от Path Traversal и Command Injection в IPC ID.
3. Тест 3: Логика Watchdog (корректность детекции процессов и реакция на закрытие).
4. Тест 4: Полный цикл IPC запрос-ответ (генерация запроса, ответ кнопкой/текстом, атомарность, удаление файлов).
"""

import asyncio
import html
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import psutil

# Импортируем тестируемый модуль моста
from telegram_bridge import (
    TelegramBridge,
    atomic_write_json,
    authorized_only,
    build_inline_keyboard,
    convert_to_classic_html,
    format_telegram_message,
    is_antigravity_running,
    is_process_running,
    is_valid_uuid,
    safe_resolve_ipc_path,
)

logger = logging.getLogger("TestSecurityBridge")


class TestChatIdAuthorization(unittest.IsolatedAsyncioTestCase):
    """
    ТЕСТ 1: Проверка фильтрации chat_id и защиты от неавторизованного доступа.
    Архитектурное требование: Доступ разрешён строго для chat_id из конфигурации.
    Любые чужие chat_id должны отклоняться молча, без раскрытия метаданных системы.
    """

    def setUp(self):
        self.allowed_id = 123456789
        self.attacker_id = 9999999999
        self.temp_dir = tempfile.TemporaryDirectory()
        self.bridge = TelegramBridge(
            bot_token="test_fake_token",
            allowed_chat_id=self.allowed_id,
            ipc_dir=Path(self.temp_dir.name),
            enable_watchdog=False
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    async def test_authorized_user_is_allowed(self):
        """Проверка: авторизованный пользователь успешно проходит guard-проверку."""
        mock_update = MagicMock()
        mock_update.effective_chat.id = self.allowed_id
        mock_context = MagicMock()

        called = False

        class DummyService:
            def __init__(self, chat_id):
                self.allowed_chat_id = chat_id

            @authorized_only
            async def protected_action(self, update, context):
                nonlocal called
                called = True
                return "SUCCESS"

        service = DummyService(self.allowed_id)
        result = await service.protected_action(mock_update, mock_context)

        self.assertTrue(called, "Авторизованный вызов должен быть выполнен")
        self.assertEqual(result, "SUCCESS")

    async def test_unauthorized_user_is_rejected(self):
        """Проверка: запрос от злоумышленника (чужой chat_id) отбрасывается без выполнения."""
        mock_update = MagicMock()
        mock_update.effective_chat.id = self.attacker_id
        mock_update.effective_user.username = "evil_hacker"
        mock_update.effective_user.full_name = "Mr. Hacker"
        mock_context = MagicMock()

        called = False

        class DummyService:
            def __init__(self, chat_id):
                self.allowed_chat_id = chat_id

            @authorized_only
            async def protected_action(self, update, context):
                nonlocal called
                called = True
                return "SUCCESS"

        service = DummyService(self.allowed_id)

        with self.assertLogs("TelegramBridge", level="WARNING") as log_capture:
            result = await service.protected_action(mock_update, mock_context)

        self.assertFalse(called, "Неавторизованный вызов НЕ должен выполняться!")
        self.assertIsNone(result)
        self.assertTrue(
            any("ОТКЛОНЕНА НЕАВТОРИЗОВАННАЯ ПОПЫТКА ДОСТУПА" in msg for msg in log_capture.output),
            "В лог должна быть записана тревога безопасности с деталями нарушителя"
        )

    async def test_null_chat_is_rejected(self):
        """Проверка: обновление без effective_chat отклоняется."""
        mock_update = MagicMock()
        mock_update.effective_chat = None
        mock_context = MagicMock()

        called = False

        class DummyService:
            def __init__(self, chat_id):
                self.allowed_chat_id = chat_id

            @authorized_only
            async def protected_action(self, update, context):
                nonlocal called
                called = True

        service = DummyService(self.allowed_id)
        await service.protected_action(mock_update, mock_context)
        self.assertFalse(called)

    async def test_string_int_type_conversion_robustness(self):
        """Проверка: сравнение chat_id устойчиво к типам int vs str."""
        mock_update = MagicMock()
        mock_update.effective_chat.id = "123456789"  # str вместо int
        mock_context = MagicMock()

        called = False

        class DummyService:
            def __init__(self, chat_id):
                self.allowed_chat_id = chat_id  # int

            @authorized_only
            async def protected_action(self, update, context):
                nonlocal called
                called = True

        service = DummyService(self.allowed_id)
        await service.protected_action(mock_update, mock_context)
        self.assertTrue(called, "Строковое и числовое представление одного chat_id должны успешно совпадать")

    async def test_unauthorized_callback_query_ignored_directly(self):
        """Проверка: вызов handle_callback_query от злоумышленника полностью игнорируется."""
        mock_update = MagicMock()
        mock_update.effective_chat.id = self.attacker_id
        mock_update.callback_query = MagicMock(data="some-uuid:0")
        mock_context = MagicMock()

        res = await self.bridge.handle_callback_query(mock_update, mock_context)
        self.assertIsNone(res)
        mock_update.callback_query.answer.assert_not_called()
        mock_update.callback_query.edit_message_text.assert_not_called()

    async def test_unauthorized_text_message_ignored_directly(self):
        """Проверка: вызов handle_text_message от злоумышленника полностью игнорируется."""
        mock_update = MagicMock()
        mock_update.effective_chat.id = self.attacker_id
        mock_update.message = MagicMock(text="Вредоносный ответ")
        mock_context = MagicMock()

        res = await self.bridge.handle_text_message(mock_update, mock_context)
        self.assertIsNone(res)
        mock_update.message.reply_text.assert_not_called()


class TestSecuritySanitizationAndPathTraversal(unittest.TestCase):
    """
    ТЕСТ 2: Тест санитайзинга, защиты от инъекций и Path Traversal.
    Архитектурное требование:
    - Валидация GUID/UUID
    - Невозможность выхода за пределы IPC папки
    - Экранирование спецсимволов HTML
    - Атомарная запись JSON
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ipc_dir = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_uuid_validation(self):
        """Проверка валидации UUID и отсечения вредоносных идентификаторов."""
        valid_uuids = [
            str(uuid.uuid4()),
            "c331d623-0047-47b9-a244-86248720369c",
            "C331D623-0047-47B9-A244-86248720369C",
            "12345678-1234-5678-1234-567812345678"
        ]
        for u in valid_uuids:
            self.assertTrue(is_valid_uuid(u), f"UUID должен быть валидным: {u}")

        malicious_or_invalid = [
            "../../../etc/passwd",
            "..\\..\\Windows\\System32",
            "c331d623-0047-47b9-a244-86248720369c/../../hack",
            "c331d623-0047-47b9-a244-86248720369c; whoami",
            "c331d623-0047-47b9-a244-86248720369c | calc.exe",
            "c331d623-0047-47b9-a244-86248720369c`nmalicious",
            "not-a-uuid",
            "12345",
            "",
            None,
            123456
        ]
        for bad in malicious_or_invalid:
            self.assertFalse(is_valid_uuid(bad), f"Невалидный/опасный ввод должен быть отклонён: {bad}")

    def test_safe_resolve_ipc_path(self):
        """Проверка безопасного построения путей IPC и защиты от Path Traversal."""
        test_id = str(uuid.uuid4())

        # Корректное построение
        resolved = safe_resolve_ipc_path(self.ipc_dir, "request", test_id)
        self.assertEqual(resolved.parent, self.ipc_dir.resolve())
        self.assertEqual(resolved.name, f"request_{test_id}.json")

        # Попытка передать некорректный ID
        with self.assertRaises(ValueError):
            safe_resolve_ipc_path(self.ipc_dir, "request", "../traversal")

        # Попытка передать опасный префикс
        with self.assertRaises(ValueError):
            safe_resolve_ipc_path(self.ipc_dir, "../prefix", test_id)

    def test_html_escaping_in_messages(self):
        """Проверка экранирования спецсимволов HTML в заголовках и теле сообщений."""
        dangerous_title = '<script>alert("XSS")</script> & Antigravity'
        dangerous_prompt = 'Нажмите <b>OK</b> или <a href="http://evil.com">здесь</a> & "test" < > \''

        formatted = format_telegram_message(dangerous_title, dangerous_prompt, status="Wait")

        # Теги не должны присутствовать в открытом виде
        self.assertNotIn("<script>", formatted)
        self.assertNotIn('<a href="http://evil.com">', formatted)
        self.assertNotIn('<b>OK</b>', formatted)

        # Должны быть безопасные HTML-сущности
        self.assertIn("&lt;script&gt;", formatted)
        self.assertIn("&amp; Antigravity", formatted)
        self.assertIn("&lt;b&gt;OK&lt;/b&gt;", formatted)
        self.assertIn("&quot;test&quot;", formatted)

    def test_atomic_write_json(self):
        """Проверка атомарной записи JSON и отсутствия повреждённых временных файлов."""
        test_file = self.ipc_dir / "test_data.json"
        data = {
            "key": "value",
            "number": 42,
            "unicode": "Русский текст с эмодзи 🚀 ✅",
            "nested": {"status": "ok"}
        }

        atomic_write_json(test_file, data)

        self.assertTrue(test_file.exists(), "Файл должен быть создан")

        # Проверка содержимого
        with open(test_file, "r", encoding="utf-8") as f:
            loaded = json.load(f)

        self.assertEqual(loaded, data)

        # Проверка, что временные файлы .tmp_* не остались в папке
        tmp_files = list(self.ipc_dir.glob(".tmp_*"))
        self.assertEqual(len(tmp_files), 0, "Временные файлы должны удаляться после атомарной замены")

    def test_message_truncation_for_telegram_limit(self):
        """Проверка: сообщения длиннее 3500 символов безопасно усекаются, не превышая лимит Telegram (4096)."""
        huge_prompt = "A" * 5000
        formatted = format_telegram_message("Title", huge_prompt)
        self.assertLess(len(formatted), 4000, "Форматированное сообщение обязано укладываться в лимит Telegram API")
        self.assertIn("содержимое усечено", formatted)

    def test_empty_button_text_filtered(self):
        """Проверка: пустые или состоящие из пробелов опции кнопок корректно фильтруются."""
        keyboard = build_inline_keyboard("12345678-1234-1234-1234-123456789abc", ["", "  ", "Вариант 1"])
        self.assertEqual(len(keyboard.inline_keyboard), 1)
        self.assertEqual(keyboard.inline_keyboard[0][0].text, "Вариант 1")


class TestWatchdogLogic(unittest.TestCase):
    """
    ТЕСТ 3: Тест логики Watchdog и мониторинга процессов.
    Архитектурное требование:
    - Корректное определение наличия и отсутствия процессов в ОС
    - Своевременный триггер при закрытии отслеживаемого процесса
    """

    def test_is_process_running_with_real_processes(self):
        """Проверка детекции реально запущенного процесса (текущий процесс python)."""
        current_proc_name = psutil.Process().name()
        self.assertTrue(
            is_process_running(current_proc_name),
            f"Текущий процесс {current_proc_name} должен определяться как активный"
        )

    def test_is_process_running_with_nonexistent_process(self):
        """Проверка возврата False для фиктивного несуществующего процесса."""
        fake_name = "definitely_nonexistent_fake_process_999988887777.exe"
        self.assertFalse(
            is_process_running(fake_name),
            "Несуществующий процесс должен возвращать False"
        )

    def test_is_antigravity_running_returns_boolean(self):
        """Проверка специализированной функции is_antigravity_running()."""
        running = is_antigravity_running()
        self.assertIsInstance(running, bool)
        # Если Antigravity.exe запущен в текущей системе, она должна вернуть True
        # (в окружении пользователя Antigravity сейчас запущен)
        logger.info(f"Статус Antigravity.exe в тестовой системе: {running}")

    def test_watchdog_thread_exit_trigger(self):
        """
        Тестирование механизма сторожа: проверка, что при исчезновении процесса
        вызывается обработчик завершения.
        """
        exit_triggered = threading.Event()

        # Эмулируем функцию завершения вместо реального os._exit(0)
        def mock_exit_action():
            exit_triggered.set()

        # Тестовая функция сторожа с инъекцией зависимости проверки процесса
        def test_watchdog(check_fn, exit_fn, interval=0.1, stop_evt=None):
            while not (stop_evt and stop_evt.is_set()):
                time.sleep(interval)
                if not check_fn():
                    exit_fn()
                    break

        process_alive = True

        def fake_is_running():
            return process_alive

        stop_evt = threading.Event()
        t = threading.Thread(
            target=test_watchdog,
            args=(fake_is_running, mock_exit_action, 0.05, stop_evt),
            daemon=True
        )
        t.start()

        # Пока процесс жив — сторож не срабатывает
        time.sleep(0.15)
        self.assertFalse(exit_triggered.is_set(), "Сторож не должен срабатывать, пока процесс жив")

        # Процесс закрывается
        process_alive = False

        # Ожидаем срабатывания сторожа
        exit_success = exit_triggered.wait(timeout=1.0)
        stop_evt.set()

        self.assertTrue(exit_success, "Сторож обязан среагировать и вызвать завершение при закрытии процесса")

    def test_process_running_matching_robustness(self):
        """Проверка устойчивости сопоставления имени процесса с .exe и без .exe."""
        self.assertTrue(is_process_running("python"), "Поиск 'python' без .exe должен быть успешен")
        self.assertTrue(is_process_running("python.exe"), "Поиск 'python.exe' должен быть успешен")
        self.assertTrue(is_process_running("PYTHON.EXE"), "Регистронезависимый поиск должен быть успешен")


class TestIPCRequestResponseCycle(unittest.IsolatedAsyncioTestCase):
    """
    ТЕСТ 4: Тест полного цикла запрос-ответ (Request-Reply Cycle) через IPC.
    Архитектурное требование:
    - Сканирование IPC каталога и отправка сообщения в Telegram
    - Приём ответа по кнопке (Inline Button) -> запись response_<UUID>.json
    - Приём текстового ответа -> запись response_<UUID>.json
    - Очистка временных файлов
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ipc_dir = Path(self.temp_dir.name)
        self.allowed_id = 123456789
        self.bridge = TelegramBridge(
            bot_token="test_fake_token",
            allowed_chat_id=self.allowed_id,
            ipc_dir=self.ipc_dir,
            enable_watchdog=False
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    async def test_ipc_button_reply_cycle(self):
        """Проверка полного цикла обработки IPC с выбором через инлайн-кнопку."""
        req_id = str(uuid.uuid4())
        req_file = self.ipc_dir / f"request_{req_id}.json"

        # 1. Записываем файл запроса в IPC
        req_payload = {
            "id": req_id,
            "prompt": "Файл уже существует. Перезаписать?",
            "options": ["Да, перезаписать", "Отмена"],
            "status": "Wait",
            "title": "Подтверждение"
        }
        atomic_write_json(req_file, req_payload)

        # 2. Мокируем бота Telegram
        mock_bot = MagicMock()
        mock_sent_msg = MagicMock()
        mock_sent_msg.message_id = 1001
        mock_bot.send_message = AsyncMock(return_value=mock_sent_msg)

        # 3. Сканируем IPC директорию
        await self.bridge.scan_and_process_ipc(mock_bot)

        # Проверяем, что сообщение было отправлено в чат
        mock_bot.send_message.assert_called_once()
        call_kwargs = mock_bot.send_message.call_args.kwargs
        self.assertEqual(call_kwargs["chat_id"], self.allowed_id)
        self.assertIn("Подтверждение", call_kwargs["text"])
        self.assertIn("Файл уже существует", call_kwargs["text"])

        # Проверяем, что запрос зарегистрирован в памяти
        self.assertIn(req_id, self.bridge.active_requests)
        self.assertEqual(self.bridge.active_requests[req_id]["message_id"], 1001)

        # 4. Пользователь нажимает первую кнопку ("Да, перезаписать", индекс 0)
        mock_update = MagicMock()
        mock_update.effective_chat.id = self.allowed_id
        mock_update.callback_query = MagicMock()
        mock_update.callback_query.data = f"{req_id}:0"
        mock_update.callback_query.answer = AsyncMock()
        mock_update.callback_query.edit_message_text = AsyncMock()

        mock_context = MagicMock()

        await self.bridge.handle_callback_query(mock_update, mock_context)

        # Проверяем подтверждение клика пользователю
        mock_update.callback_query.answer.assert_called_with("Выбрано: Да, перезаписать")
        mock_update.callback_query.edit_message_text.assert_called_once()

        # 5. Проверяем созданный response-файл в IPC
        resp_file = self.ipc_dir / f"response_{req_id}.json"
        self.assertTrue(resp_file.exists(), "Файл ответа response_<UUID>.json должен существовать")

        with open(resp_file, "r", encoding="utf-8") as f:
            resp_data = json.load(f)

        self.assertEqual(resp_data["id"], req_id)
        self.assertEqual(resp_data["reply"], "Да, перезаписать")
        self.assertEqual(resp_data["option_index"], 0)
        self.assertEqual(resp_data["type"], "button")

        # Запрос должен быть удалён из активных
        self.assertNotIn(req_id, self.bridge.active_requests)

    async def test_ipc_text_reply_cycle(self):
        """Проверка полного цикла обработки IPC с текстовым ответом пользователя."""
        req_id = str(uuid.uuid4())
        req_file = self.ipc_dir / f"request_{req_id}.json"

        # 1. Записываем файл запроса в IPC
        req_payload = {
            "id": req_id,
            "prompt": "Введите новое имя для ветки git:",
            "options": [],
            "status": "Wait",
            "title": "Ввод имени"
        }
        atomic_write_json(req_file, req_payload)

        # 2. Сканируем IPC директорию
        mock_bot = MagicMock()
        mock_sent_msg = MagicMock()
        mock_sent_msg.message_id = 1002
        mock_bot.send_message = AsyncMock(return_value=mock_sent_msg)
        mock_bot.edit_message_text = AsyncMock()

        await self.bridge.scan_and_process_ipc(mock_bot)
        self.assertIn(req_id, self.bridge.active_requests)

        # 3. Пользователь отвечает текстом в Telegram
        mock_update = MagicMock()
        mock_update.effective_chat.id = self.allowed_id
        mock_update.message = MagicMock()
        mock_update.message.text = "feature/telegram-bridge"
        mock_update.message.reply_to_message = None  # свободный текст в чат
        mock_update.message.reply_text = AsyncMock()

        mock_context = MagicMock()
        mock_context.bot = mock_bot

        await self.bridge.handle_text_message(mock_update, mock_context)

        # Проверяем подтверждение приёма ответа
        mock_update.message.reply_text.assert_called_once()
        self.assertIn("feature/telegram-bridge", mock_update.message.reply_text.call_args.args[0])

        # 4. Проверяем файл ответа в IPC
        resp_file = self.ipc_dir / f"response_{req_id}.json"
        self.assertTrue(resp_file.exists(), "Файл ответа response_<UUID>.json должен существовать")

        with open(resp_file, "r", encoding="utf-8") as f:
            resp_data = json.load(f)

        self.assertEqual(resp_data["id"], req_id)
        self.assertEqual(resp_data["reply"], "feature/telegram-bridge")
        self.assertEqual(resp_data["type"], "text")
        self.assertEqual(resp_data["option_index"], -1)

        # Запрос должен быть удалён из активных
        self.assertNotIn(req_id, self.bridge.active_requests)

    async def test_no_duplicate_telegram_send_after_reply(self):
        """
        Проверка защиты от дублирования сообщений:
        После того как запрос был обработан (клик по кнопке), повторный вызов
        scan_and_process_ipc НЕ должен повторно отправлять сообщение в чат Telegram.
        """
        req_id = str(uuid.uuid4())
        req_file = self.ipc_dir / f"request_{req_id}.json"
        atomic_write_json(req_file, {"id": req_id, "prompt": "Тест дублирования", "options": ["Да", "Нет"]})

        mock_bot = MagicMock()
        mock_msg = MagicMock(message_id=5001)
        mock_bot.send_message = AsyncMock(return_value=mock_msg)
        mock_bot.edit_message_text = AsyncMock()

        # Первый скан: отправка в Telegram
        await self.bridge.scan_and_process_ipc(mock_bot)
        self.assertEqual(mock_bot.send_message.call_count, 1)

        # Пользователь отвечает по кнопке
        mock_update = MagicMock()
        mock_update.effective_chat.id = self.allowed_id
        mock_update.callback_query = MagicMock(data=f"{req_id}:0")
        mock_update.callback_query.answer = AsyncMock()
        mock_update.callback_query.edit_message_text = AsyncMock()
        await self.bridge.handle_callback_query(mock_update, MagicMock())

        # Второй скан (даже если файл запроса ещё на диске)
        await self.bridge.scan_and_process_ipc(mock_bot)
        self.assertEqual(mock_bot.send_message.call_count, 1, "send_message НЕ должен вызываться повторно!")

    async def test_stale_request_ttl_purged_without_sending(self):
        """Проверка очистки устаревших запросов (TTL): старые файлы удаляются и НЕ отправляются в Telegram."""
        req_id = str(uuid.uuid4())
        req_file = self.ipc_dir / f"request_{req_id}.json"
        old_time = time.time() - 600  # 10 минут назад
        atomic_write_json(req_file, {
            "id": req_id,
            "prompt": "Старый запрос",
            "options": ["Ок"],
            "created_at": old_time,
            "timeout_seconds": 300
        })

        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()

        await self.bridge.scan_and_process_ipc(mock_bot)

        # Сообщение не должно быть отправлено
        mock_bot.send_message.assert_not_called()
        # Файл должен быть удалён
        self.assertFalse(req_file.exists(), "Устаревший файл запроса должен быть удалён с диска")

    async def test_text_message_resolves_option_index(self):
        """Проверка: если текстовый ввод совпадает с текстом одной из кнопок, option_index определяется корректно."""
        req_id = str(uuid.uuid4())
        req_file = self.ipc_dir / f"request_{req_id}.json"
        atomic_write_json(req_file, {
            "id": req_id,
            "prompt": "Выберите действие:",
            "options": ["Применить фикс", "Отменить"],
            "status": "Wait"
        })

        mock_bot = MagicMock()
        mock_msg = MagicMock(message_id=7001)
        mock_bot.send_message = AsyncMock(return_value=mock_msg)
        mock_bot.edit_message_text = AsyncMock()

        await self.bridge.scan_and_process_ipc(mock_bot)

        # Пользователь отвечает текстом, точно совпадающим с первой опцией
        mock_update = MagicMock()
        mock_update.effective_chat.id = self.allowed_id
        mock_update.message = MagicMock(text="Применить фикс")
        mock_update.message.reply_to_message = None
        mock_update.message.reply_text = AsyncMock()

        mock_context = MagicMock()
        mock_context.bot = mock_bot

        await self.bridge.handle_text_message(mock_update, mock_context)

        resp_file = self.ipc_dir / f"response_{req_id}.json"
        self.assertTrue(resp_file.exists())
        with open(resp_file, "r", encoding="utf-8") as f:
            resp_data = json.load(f)

        self.assertEqual(resp_data["option_index"], 0, "option_index должен быть 0 для 'Применить фикс'")
        self.assertEqual(resp_data["reply"], "Применить фикс")

    async def test_cancelled_request_removes_buttons_in_telegram(self):
        """Проверка: когда файл запроса удалён вызывающим скриптом (отмена/таймаут), кнопки в Telegram снимаются."""
        req_id = str(uuid.uuid4())
        req_file = self.ipc_dir / f"request_{req_id}.json"
        atomic_write_json(req_file, {"id": req_id, "prompt": "Будет отменён", "options": ["Да", "Нет"]})

        mock_bot = MagicMock()
        mock_msg = MagicMock(message_id=8001)
        mock_bot.send_message = AsyncMock(return_value=mock_msg)
        mock_bot.edit_message_text = AsyncMock()

        await self.bridge.scan_and_process_ipc(mock_bot)
        self.assertIn(req_id, self.bridge.active_requests)

        # Внешний скрипт удалил файл запроса
        req_file.unlink()

        # Следующий скан IPC
        await self.bridge.scan_and_process_ipc(mock_bot)

        self.assertNotIn(req_id, self.bridge.active_requests)
        # Бот должен был отредактировать сообщение, сняв клавиатуру (reply_markup=None)
        mock_bot.edit_message_text.assert_called_once()
        call_kwargs = mock_bot.edit_message_text.call_args.kwargs
        self.assertIsNone(call_kwargs.get("reply_markup"))
        self.assertIn("Время ожидания ответа истекло", call_kwargs.get("text"))

    async def test_cmd_status_runs_cleanly(self):
        """Проверка: команда /status формирует непустое сообщение без ошибок."""
        mock_update = MagicMock()
        mock_update.effective_chat.id = self.allowed_id
        mock_update.message = MagicMock()
        mock_update.message.reply_text = AsyncMock()

        mock_context = MagicMock()

        await self.bridge.cmd_status(mock_update, mock_context)
        mock_update.message.reply_text.assert_called_once()
        status_text = mock_update.message.reply_text.call_args.args[0]
        self.assertIn("Системный статус Antigravity", status_text)
        self.assertIn("CPU:", status_text)
        self.assertIn("RAM:", status_text)


class TestRichMessageAndFormatting(unittest.TestCase):
    """
    ТЕСТ 5: Тестирование функций Rich Message (Bot API 10.1+) и безопасного Fallback:
    1. Форматирование со сворачиваемым блоком <details><summary>.
    2. Форматирование с раскрываемой цитатой <blockquote expandable>.
    3. Поддержка длинных сообщений до 30 000 символов в rich_mode.
    4. Преобразование convert_to_classic_html для гарантированной совместимости со старыми клиентами.
    """

    def test_rich_details_formatting(self):
        """Проверка: блок details формируется с корректными тегами summary и экранированием."""
        title = "Сборка"
        prompt = "Сборка завершена успешно"
        details = "Лог сборки: <error> 0 warning: & test"
        summary = "Логи компилятора"

        formatted = format_telegram_message(
            title=title,
            prompt=prompt,
            status="Success",
            details=details,
            details_summary=summary,
            rich_mode=True,
            expandable_blockquote=False,
        )

        self.assertIn("<details>", formatted)
        self.assertIn("<summary>Логи компилятора</summary>", formatted)
        self.assertIn("&lt;error&gt; 0 warning: &amp; test", formatted)
        self.assertIn("</details>", formatted)
        self.assertIn("✅", formatted)

    def test_expandable_blockquote_formatting(self):
        """Проверка: режим expandable_blockquote оборачивает детали в <blockquote expandable>."""
        formatted = format_telegram_message(
            title="Тест цитаты",
            prompt="Основной текст",
            status="Info",
            details="Длинный лог",
            details_summary="Сводка",
            rich_mode=True,
            expandable_blockquote=True,
        )

        self.assertIn("<blockquote expandable>", formatted)
        self.assertIn("<b>Сводка</b>", formatted)
        self.assertIn("Длинный лог", formatted)
        self.assertIn("</blockquote>", formatted)

    def test_rich_length_capacity(self):
        """Проверка: в rich_mode сообщение длиной 15 000 символов НЕ усекается до 3500 знаков."""
        large_prompt = "Z" * 15000
        formatted = format_telegram_message("Title", large_prompt, rich_mode=True)

        self.assertGreater(len(formatted), 14000)
        self.assertNotIn("содержимое усечено для лимита Telegram", formatted)

    def test_convert_to_classic_html_details_to_expandable_blockquote(self):
        """Проверка fallback: <details><summary> преобразуется в <blockquote expandable><b>summary</b>."""
        rich_html = (
            "<h1>Заголовок</h1>"
            "<p>Абзац текста</p>"
            "<details><summary>Логи</summary>Текст подробного лога</details>"
            "<hr/>"
            "<ul><li>Пункт 1</li><li>Пункт 2</li></ul>"
        )

        classic = convert_to_classic_html(rich_html)

        # Не должно остаться тегов, запрещённых в обычном sendMessage
        self.assertNotIn("<details>", classic)
        self.assertNotIn("<summary>", classic)
        self.assertNotIn("<h1>", classic)
        self.assertNotIn("<hr/>", classic)
        self.assertNotIn("<ul>", classic)
        self.assertNotIn("<li>", classic)

        # Должны появиться валидные классические теги
        self.assertIn("<blockquote expandable><b>Логи</b>\nТекст подробного лога</blockquote>", classic)
        self.assertIn("<b>Заголовок</b>", classic)
        self.assertIn("• Пункт 1", classic)
        self.assertIn("• Пункт 2", classic)
        self.assertIn("----------------------------------------", classic)


if __name__ == "__main__":
    unittest.main(verbosity=2)
