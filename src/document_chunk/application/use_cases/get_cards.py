from dataclasses import dataclass

from document_chunk.application.dto.generation_dto import CardSection, CardsResponse, LessonCardItem
from document_chunk.domain.ports.metadata_store import IMetadataStore
from document_chunk.shared.result import Err, Ok, Result


@dataclass(frozen=True)
class GetCardsRequest:
    document_id: str


class GetCardsUseCase:
    def __init__(self, metadata_store: IMetadataStore) -> None:
        self._metadata_store = metadata_store

    def execute(self, request: GetCardsRequest) -> Result[CardsResponse | None, Exception]:
        document_result = self._metadata_store.get(request.document_id)
        if document_result.is_err():
            return Err(document_result.error)
        if document_result.unwrap() is None:
            return Ok(None)

        cards_result = self._metadata_store.list_lesson_cards(request.document_id)
        if cards_result.is_err():
            return Err(cards_result.error)

        grouped_cards: dict[tuple[str, ...], list[LessonCardItem]] = {}
        for stored in cards_result.unwrap():
            key = stored.heading_path
            grouped_cards.setdefault(key, []).append(
                LessonCardItem(
                    card_id=stored.card_id,
                    chunk_id=stored.primary_chunk_id,
                    heading_path=list(stored.heading_path),
                    title=stored.title,
                    bullets=list(stored.bullets),
                    key_insight=stored.key_insight,
                    card_index=stored.card_index,
                )
            )

        sections = [
            CardSection(
                heading_path=list(heading_path),
                cards=cards,
            )
            for heading_path, cards in grouped_cards.items()
        ]

        return Ok(
            CardsResponse(
                document_id=request.document_id,
                sections=sections,
                total_cards=sum(len(section.cards) for section in sections),
            )
        )
