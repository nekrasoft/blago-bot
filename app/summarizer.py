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
            "Нужно кратко извлечь:\n"
            "1) Предмет закупки\n"
            "2) Что должен предоставить исполнитель\n"
            "3) Требования к участнику\n"
            "4) Сроки/этапы\n"
            "5) Финансовые условия (НМЦК, обеспечение, штрафы)\n"
            "6) Территориальность: где фактически выполняются работы/услуги, точки погрузки/разгрузки, "
            "полигоны/объекты исполнения (не путать с адресом заказчика)\n"
            "7) Тип процедуры: котировка/аукцион/конкурс/иной вид закупки\n"
            "8) Unit-экономика: ставки за тонну и/или за м3, а также расчет цены 1 машины 30 м3 "
            "(если есть цена за м3: цена_машины = цена_за_м3 * 30; если есть только цена за тонну — "
            "если есть цена за кг: цена_тонны = цена_кг * 1000)\n"
            "9) Ключевые риски/неясности"
        )
        return await self._ask_llm(prompt, chunk)

    async def _final_summary(self, content: str, file_name: str | None = None) -> str:
        title = f"Файл: {file_name}\n\n" if file_name else ""
        prompt = (
            f"Сформируй итоговое саммари на языке: {self.language}.\n"
            "Стиль: деловой, коротко, без воды.\n"
            "Формат:\n"
            "- Кратко о закупке\n"
            "- Тип закупки/процедуры: котировка, аукцион, конкурс или другое\n"
            "- Территориальность: где именно будет выполняться деятельность "
            "(объекты, локации, точки погрузки/разгрузки)\n"
            "- Unit-экономика:\n"
            "  цена за тонну\n"
            "  цена за м3\n"
            "  цена машины 30 м3 (если возможно рассчитать)\n"
            "- Основные требования\n"
            "- Документы/условия участия\n"
            "- Ключевые сроки\n"
            "- Деньги/гарантии\n"
            "- Риски и что уточнить\n"
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
