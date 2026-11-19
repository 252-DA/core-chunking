"""Exact, bounded token counting; never silently approximate the embedder."""

from functools import lru_cache

from document_chunk.infrastructure.config import ChunkerConfig


class TokenCounter:
    def __init__(self, config: ChunkerConfig):
        from tokenizers import Tokenizer

        try:
            self.tokenizer = (
                Tokenizer.from_file(config.tokenizer_path)
                if config.tokenizer_path
                else Tokenizer.from_pretrained(config.tokenizer)
            )
            # Counting must not inherit truncation/padding saved in tokenizer.json.
            self.tokenizer.no_truncation()
            self.tokenizer.no_padding()
        except Exception as exc:
            raise ValueError("Cannot load exact tokenizer; set CHUNKER__TOKENIZER_PATH") from exc
        self.count = lru_cache(maxsize=2048)(self._count)

    def _count(self, text: str) -> int:
        return len(self.tokenizer.encode(text, add_special_tokens=False).ids)

    def count_many(self, texts: list[str]) -> list[int]:
        return [len(e.ids) for e in self.tokenizer.encode_batch(texts, add_special_tokens=False)]

    def prefix(self, text: str, budget: int) -> str:
        """A bounded prefix without assuming token counts are additive."""
        if budget <= 0:
            return ""
        if self.count(text) <= budget:
            return text
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.count(text[:mid]) <= budget:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo]
