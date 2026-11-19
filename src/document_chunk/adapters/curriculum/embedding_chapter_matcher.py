"""
EmbeddingChapterMatcher — so nội dung với hồ sơ từng chương bằng embedding.

Chỉ nhận chương khi (1) điểm cosine đủ cao và (2) bỏ xa chương đứng thứ hai.
Điều kiện thứ hai quan trọng hơn: đề cương có những chương gần nhau (dữ liệu
dòng ↔ công cụ truyền dẫn dữ liệu), và một bộ slide chung chung sẽ khớp nhiều
chương gần như ngang nhau — khi đó để giảng viên chọn còn hơn đoán.
"""
import math

from document_chunk.domain.ports.chapter_matcher import ChapterMatch, ChapterProfile, IChapterMatcher
from document_chunk.domain.ports.embedder import IEmbedder
from document_chunk.shared.logger import get_logger
from document_chunk.shared.result import Err, Ok, Result

logger = get_logger(__name__)

_BATCH = 64


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


class EmbeddingChapterMatcher(IChapterMatcher):
    # Ngưỡng hiệu chỉnh với voyage-4 trên đề cương CO3137: tài liệu đúng chương
    # đạt 0.73–0.88, bỏ xa chương thứ hai 0.10–0.21; văn bản chung chung
    # ("Course overview, grading policy") chỉ 0.51; "Kafka" chia gần đều giữa
    # chương 5 và 7 (cách 0.009) — đúng là trường hợp nên để giảng viên chọn.
    def __init__(
        self,
        embedder: IEmbedder,
        *,
        min_score: float = 0.65,
        min_margin: float = 0.05,
        strict_min_score: float = 0.70,
        strict_min_margin: float = 0.08,
    ) -> None:
        self._embedder = embedder
        self._thresholds = {False: (min_score, min_margin), True: (strict_min_score, strict_min_margin)}

    def match(
        self,
        texts: list[str],
        chapters: list[ChapterProfile],
        *,
        strict: bool = False,
    ) -> Result[list[ChapterMatch], Exception]:
        if not texts:
            return Ok([])
        if not chapters:
            return Ok([ChapterMatch(chapter_code=None, score=0.0, margin=0.0) for _ in texts])

        vectors_result = self._embed([c.text for c in chapters] + texts)
        if vectors_result.is_err():
            return Err(vectors_result.error)
        vectors = [_normalize(v) for v in vectors_result.unwrap()]
        chapter_vectors, text_vectors = vectors[: len(chapters)], vectors[len(chapters):]

        min_score, min_margin = self._thresholds[strict]
        matches: list[ChapterMatch] = []
        for vector in text_vectors:
            ranked = sorted(
                ((_dot(vector, cv), chapter.code) for cv, chapter in zip(chapter_vectors, chapters)),
                reverse=True,
            )
            best_score, best_code = ranked[0]
            runner_score, runner_code = ranked[1] if len(ranked) > 1 else (0.0, None)
            margin = best_score - runner_score
            accepted = best_score >= min_score and margin >= min_margin
            matches.append(ChapterMatch(
                chapter_code=best_code if accepted else None,
                score=round(best_score, 4),
                margin=round(margin, 4),
                best_code=best_code,
                runner_up_code=runner_code,
            ))
        return Ok(matches)

    def _embed(self, texts: list[str]) -> Result[list[list[float]], Exception]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), _BATCH):
            result = self._embedder.embed(texts[start:start + _BATCH])
            if result.is_err():
                logger.warning("embedding_chapter_matcher.embed_failed", error=str(result.error))
                return Err(result.error)
            vectors.extend(result.unwrap())
        return Ok(vectors)
