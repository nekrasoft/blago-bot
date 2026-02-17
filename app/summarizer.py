from __future__ import annotations

from typing import Iterable

from openai import AsyncOpenAI


class TenderSummarizer:
    def __init__(
        self,
        api_key: str,
        model: str,
        language: str,
        max_doc_chars: int,
        chunk_size: int,
        chunk_overlap: int,
    ) -> None:
        self.client = AsyncOpenAI(api_key=api_key)
        self.model = model
        self.language = language
        self.max_doc_chars = max_doc_chars
        self.chunk_size = chunk_size
        self.chunk_overlap = min(chunk_overlap, max(0, chunk_size // 2))

    async def summarize(self, text: str, file_name: str | None = None) -> str:
        normalized = "\n".join(line.strip() for line in text.splitlines() if line.strip())
        if not normalized:
            return "Не удалось извлечь текст из файла."

        if len(normalized) > self.max_doc_chars:
            normalized = normalized[: self.max_doc_chars]

        chunks = list(self._split_text(normalized))

        if len(chunks) == 1:
            return await self._final_summary(chunks[0], file_name=file_name)

        partials: list[str] = []
        for idx, chunk in enumerate(chunks, start=1):
            partial = await self._chunk_summary(chunk, idx=idx, total=len(chunks))
            partials.append(partial)

        combined = "\n\n".join(partials)
        return await self._final_summary(combined, file_name=file_name)

    def _split_text(self, text: str) -> Iterable[str]:
        if len(text) <= self.chunk_size:
            yield text
            return

        start = 0
        while start < len(text):
            end = min(len(text), start + self.chunk_size)
            yield text[start:end]
            if end >= len(text):
                break
            start = max(0, end - self.chunk_overlap)

    async def _chunk_summary(self, chunk: str, idx: int, total: int) -> str:
        prompt = (
            f"Ты анализируешь часть тендерной документации ({idx}/{total}). "
            "Верни только факты. Если данных нет, так и напиши.\n\n"
            "КРИТИЧЕСКИ ВАЖНО: максимально полно извлекай требования к исполнителю работ.\n"
            "Особенно отметь:\n"
            "- требования к регистрации (в субъекте РФ/регионе, ЕГРЮЛ/ЕГРИП)\n"
            "- статус ИП/ООО/иные организационные требования\n"
            "- СРО (членство, допуски, уровни ответственности, регион регистрации)\n"
            "- лицензии/разрешения/аккредитации\n"
            "- договоры с полигонами/объектами утилизации/переработки\n"
            "- требования к технике, персоналу, опыту и аналогичным контрактам\n"
            "- обязательные подтверждающие документы по этим пунктам\n\n"
            "Нужно кратко извлечь:\n"
            "1) Предмет закупки\n"
            "2) Что должен предоставить исполнитель\n"
            "3) Требования к участнику (подробно, как ключевой блок)\n"
            "4) Сроки/этапы\n"
            "5) Финансовые условия (НМЦК, обеспечение, штрафы)\n"
            "6) Территориальность: где фактически выполняются работы/услуги, точки погрузки/разгрузки, "
            "полигоны/объекты исполнения (не путать с адресом заказчика)\n"
            "7) Тип процедуры: котировка/аукцион/конкурс/иной вид закупки\n"
            "8) Unit-экономика: ставки за тонну и/или за м3, а также расчет цены 1 машины 30 м3 "
            "(если есть цена за м3: цена_машины = цена_за_м3 * 30; если есть только цена за тонну — "
            "если есть цена за кг: цена_тонны = цена_кг * 1000)\n"
            "9) Контактные данные заказчика или представителя заказчика для связи: ФИО, должность, "
            "телефон, email, отдел/подразделение, график связи (если есть), а также откуда в документе "
            "взяты контакты\n"
            "10) Ключевые риски/неясности"
        )
        return await self._ask_llm(prompt, chunk)

    async def _final_summary(self, content: str, file_name: str | None = None) -> str:
        title = f"Файл: {file_name}\n\n" if file_name else ""
        prompt = (
            f"Сформируй итоговое саммари на языке: {self.language}.\n"
            "Стиль: деловой, коротко, без воды.\n"
            "Выведи строго в формате с отдельными заголовками строками:\n"
            "Кратко о закупке:\n"
            "Тип закупки/процедуры:\n"
            "Территориальность:\n"
            "Unit-экономика:\n"
            "Требования к исполнителю работ:\n"
            "Контактные данные заказчика/представителя:\n"
            "Основные требования:\n"
            "Документы/условия участия:\n"
            "Ключевые сроки:\n"
            "Деньги/гарантии:\n"
            "Риски и что уточнить:\n"
            "В блоке 'Требования к исполнителю работ' отдельно перечисли:\n"
            "- регистрация (в т.ч. региональные ограничения)\n"
            "- ИП/ООО/правовая форма\n"
            "- СРО/допуски\n"
            "- лицензии\n"
            "- договоры с полигонами/утилизацией\n"
            "- опыт, техника, персонал\n"
            "- какие документы подтверждают каждый пункт\n"
            "Для блока Unit-экономика отдельно укажи:\n"
            "- цену за тонну\n"
            "- цену за м3\n"
            "- цену машины 30 м3 (если возможно рассчитать)\n"
            "Для блока 'Контактные данные заказчика/представителя' укажи:\n"
            "- ФИО\n"
            "- должность/роль\n"
            "- телефон\n"
            "- email\n"
            "- источник в документе (раздел/пункт)\n"
            "Если контактов нет, так и напиши: 'Контакты в документах не найдены'.\n"
            "Если данных нет, явно укажи это. Ничего не придумывай."
        )
        summary = await self._ask_llm(prompt, content)
        return f"{title}{summary}".strip()

    async def _ask_llm(self, instruction: str, content: str) -> str:
        completion = await self.client.chat.completions.create(
            model=self.model,
            temperature=0.2,
            messages=[
                {
                    "role": "system",
                    "content": "Ты помощник по анализу тендерной документации.",
                },
                {
                    "role": "user",
                    "content": f"{instruction}\n\nТекст:\n{content}",
                },
            ],
        )
        return completion.choices[0].message.content or "Не удалось сформировать саммари."
