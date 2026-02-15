# Telegram-бот для саммари тендерных `.docx`

Бот работает в Telegram-группе: принимает `.docx`, извлекает текст и отправляет короткое саммари по ключевым условиям тендера.

## Что умеет

- Отслеживает документы в группах.
- Обрабатывает только `.docx`.
- Извлекает текст из абзацев и таблиц.
- Делает саммари через OpenAI API.
- Делит длинный ответ на несколько сообщений (ограничение Telegram).

## Быстрый запуск

```bash
python3 -m venv .venv
source .venv/bin/activate
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
- `app/docx_parser.py` - извлечение текста из `.docx`.
- `app/summarizer.py` - логика саммаризации.
- `app/config.py` - чтение конфигурации из `.env`.

## Важно

- Бот использует polling. Для production лучше перейти на webhook.
- Для старого `.doc` потребуется конвертация в `.docx`.
