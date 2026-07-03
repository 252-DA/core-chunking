# Core Chunking Phase 1 Alignment Checklist

Muc tieu: dong bo core service voi thiet ke trong report, giam risk truoc khi mo rong queue/generate/chat/video.

## Scope
- Repo: `core-chunking`
- Phase: M1 (contract + data consistency)
- Out of scope: worker queue, LLM generation, chatbot RAG streaming, video pipeline

## A. Contract Alignment
- [ ] A1. Canonical metadata keys cho ingest request: `course_id`, `owner_id`, `source_file_name`
- [ ] A2. Normalize metadata aliases (`courseId`, `ownerId`, `fileName`...) ve canonical keys
- [x] A3. Luu metadata request vao `ParsedDocument.metadata` de adapter sau co the dung
- [x] A4. Preserve ten file goc tu client (khong dung temp filename trong service)

## B. Data Consistency
- [x] B1. Dong bo `ParsedDocument.document` ve `Document` cua use case sau parse
- [x] B2. Dam bao `chunk.metadata.document_id` trung voi `ProcessDocumentResponse.document_id`
- [x] B3. Dam bao `storage_key` dung ten file goc

## C. Retrieval Metadata (Foundation for Phase 2)
- [ ] C1. Mo rong `ChunkMetadata` voi `course_id`, `owner_id`
- [ ] C2. Propagate `course_id`, `owner_id` tu parsed metadata vao chunk
- [ ] C3. Luu `course_id`, `owner_id` vao Qdrant payload
- [ ] C4. Ho tro filter `course_id`, `owner_id` trong `SearchFilter` va Qdrant filter builder

## D. Quality Gate
- [ ] D1. Unit test: `process_document` giu document_id/ten file goc va metadata canon
- [ ] D2. Unit test: `heading_chunker` propagate `course_id`, `owner_id`
- [ ] D3. Unit test: `qdrant_adapter` payload map/reconstruct dung metadata moi
- [ ] D4. Integration test: ingest -> search theo `course_id`

## E. Next After M1
- [ ] E1. M2: async orchestration (Redis queue + state machine + retry/backoff)
- [ ] E2. M3: generation service (summary/quiz) + HITL review state
- [ ] E3. M4: chat service (RAG + citation + cache)
- [ ] E4. M5: media pipeline (Whisper + FFmpeg clip)

## Definition of Done (M1)
- [ ] Khong con mismatch document_id giua response va chunk payload
- [ ] Co the truy vet chunk theo `course_id`/`owner_id` trong Qdrant
- [ ] ProcessDocument luu dung ten file goc thay vi temp file name
- [ ] Co test bao ve regression cho cac behavior tren
