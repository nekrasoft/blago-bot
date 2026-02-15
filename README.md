# Telegram-бот для саммари тендерных `.doc/.docx/.rar`

Бот работает в Telegram-группе: принимает `.doc`, `.docx`, `.rar`, извлекает текст и отправляет короткое саммари по ключевым условиям тендера.

## Что умеет

- Отслеживает документы в группах.
- Обрабатывает `.doc`, `.docx` и `.rar`.
- Извлекает текст из абзацев и таблиц.
- Для `.rar` распаковывает архив и анализирует документы `.doc/.docx` внутри.
- Делает саммари через OpenAI API.
- Если несколько документов отправлены одним пакетом, делает одно общее саммари по всем файлам.
- Для пакета документов учитывает последнее текстовое сообщение того же пользователя перед пакетом (за последние 30 минут; приоритет сообщению со ссылкой `zakupki.gov.ru/epz/order/...`).
- Делит длинный ответ на несколько сообщений (ограничение Telegram).

## Быстрый запуск

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
pip install -r requirements.txt
cp .env.example .env
```

Заполните `.env`:

- `TELEGRAM_BOT_TOKEN` - токен от BotFather.
- `OPENAI_API_KEY` - API-ключ OpenAI.
- `OPENAI_MODEL` - модель для саммари (по умолчанию `gpt-4.1-mini`).

Запуск:

```bash
python main.py
```

## Настройка Telegram

1. Создайте бота через `@BotFather`.
2. Добавьте его в нужную группу.
3. Отключите Privacy Mode у бота в `@BotFather` (`/setprivacy` -> `Disable`), чтобы бот видел документы в группе.

## Структура

- `main.py` - точка входа.
- `app/telegram_bot.py` - Telegram-обработчики.
- `app/docx_parser.py` - извлечение текста из `.doc/.docx`.
- `app/archive_parser.py` - извлечение документов из `.rar`.
- `app/summarizer.py` - логика саммаризации.
- `app/config.py` - чтение конфигурации из `.env`.

## Важно

- Бот использует polling. Для production лучше перейти на webhook.
- Для поддержки `.doc` нужен хотя бы один инструмент на сервере: `LibreOffice` (рекомендуется), `antiword` или `catdoc`.
- Для поддержки `.rar` нужен хотя бы один архиватор: `unrar`, `7z`, `bsdtar` или `unar`.
- На Ubuntu/Debian можно установить так:

```bash
sudo apt-get update
sudo apt-get install -y libreoffice unrar p7zip-full
```

## Частая ошибка установки

Если видите ошибку вида:

`Could not find a version that satisfies the requirement jiter<1,>=0.10.0`

значит у вас подтянулась несовместимая ветка `openai` для текущего окружения/индекса пакетов.
В проекте уже зафиксирована совместимая версия `openai<2.0.0`, поэтому просто обновите `pip` и повторите установку:

```bash
python -m pip install --upgrade pip setuptools wheel
pip install -r requirements.txt
```
