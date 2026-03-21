import asyncio
import logging
import sys


def configure_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def ensure_python_version():
    if sys.version_info < (3, 10):
        sys.stderr.write(
            "Python 3.10+ is required. Run the bot with: python3 main.py\n"
        )
        return False
    return True


def main():
    if not ensure_python_version():
        raise SystemExit(1)

    from app.config import load_settings

    configure_logging()
    settings = load_settings()

    if settings.bot_platform == "max":
        from app.max_bot import TenderMaxBot

        bot = TenderMaxBot(settings)
        asyncio.run(bot.run())
    else:
        from app.telegram_bot import TenderTelegramBot

        bot = TenderTelegramBot(settings)
        bot.run()


if __name__ == "__main__":
    main()
