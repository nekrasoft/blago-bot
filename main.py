from __future__ import annotations

import logging

from app.config import load_settings
from app.telegram_bot import TenderTelegramBot



def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )



def main() -> None:
    configure_logging()
    settings = load_settings()
    bot = TenderTelegramBot(settings)
    bot.run()


if __name__ == "__main__":
    main()
