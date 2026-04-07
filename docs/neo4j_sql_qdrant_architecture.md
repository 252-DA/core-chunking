# Kiến trúc kết hợp PostgreSQL + Qdrant + Neo4j

## 1. Mục tiêu

Hệ thống **Adaptive Micro-learning Platform** — nền tảng học tập vi mô thích ứng dựa trên đồ thị tri thức và AI.

Mục tiêu cốt lõi: **"Kiến thức A là tiền đề của kiến thức B. Nếu hổng A, phải học lại A rồi mới học B."**

Tài liệu này mô tả cách kết hợp 3 hệ thống lưu trữ để phục vụ toàn bộ pipeline:

```
Upload PDF/Video → Parse → Chunk → Embed → Store
                                      ↓
                              Concept Extraction → Knowledge Graph
                                      ↓
                              Quiz Generation → Adaptive Learning Path
                                      ↓
                              RAG Chatbot (Qdrant + Neo4j context)
```

- **PostgreSQL**: source of truth — metadata, trạng thái pipeline, concepts, quiz, user progress, outbox events.
- **Qdrant**: vector retrieval — semantic search top-K cho RAG chatbot và content discovery.
- **Neo4j**: knowledge graph — concept hierarchy, prerequisite paths, heading structure, adaptive traversal.

## 2. Phân công vai trò

| Thành phần | Vai trò chính | Không nên dùng cho |
|---|---|---|
| PostgreSQL | Source of truth cho `documents`, `chunks`, `concepts`, `quizzes`, `user_concept_progress`, ACL, status pipeline, outbox | Vector ANN, graph traversal |
| Qdrant | Tìm chunk liên quan theo embedding, score cosine — phục vụ RAG chatbot | Transaction business, join phức tạp |
| Neo4j | Truy vấn quan hệ: concept graph, **prerequisite path**, heading hierarchy, adaptive learning traversal | Source of truth giao dịch |

## 3. Data Ownership (quan trọng)

- **SQL là `source of truth`**.
- **Qdrant và Neo4j là `derived stores`** (dữ liệu suy diễn từ SQL + pipeline output).
- Xóa/chỉnh sửa ở SQL phải phát sự kiện outbox để cập nhật Qdrant/Neo4j.
- Cả Qdrant lẫn Neo4j đều có thể **rebuild hoàn toàn** từ SQL + file MinIO mà không mất dữ liệu gốc.

## 4. Pipeline Architecture — 3 Workers

Hệ thống chia thành 3 worker chạy độc lập, mỗi worker load **chỉ** dependencies cần thiết:

### 4.1 Core Pipeline (implemented — `document_processing` queue)

```
QUEUED → PARSING → CHUNKING → EMBEDDING → UPSERTING → DONE
```

| Stage | Mô tả | Output |
|---|---|---|
| PARSING | Docling (PDF), python-docx, python-pptx, markdown parser → text + heading structure | ParsedDocument |
| CHUNKING | Heading-aware chunker — split theo heading hierarchy | chunks[] |
| EMBEDDING | BGE-M3 1024-dim encode | vectors[] |
| UPSERTING | Upsert Qdrant + insert chunk metadata SQL + append `heading_graph_project` outbox event | Qdrant points, SQL rows, outbox event |

**Khi DONE**: document đã searchable ngay (Qdrant có vectors). Heading graph sẽ được project bất đồng bộ bởi Outbox Worker.

Entry points:
- **Sync**: gRPC → `ProcessDocumentUseCase` → `PipelineCore.run()`
- **Async**: gRPC → `EnqueueDocumentUseCase` → BullMQ → `DocumentWorkerContainer` → `RunPipelineUseCase` → `PipelineCore.run()`

**Lưu ý**: `PipelineCore` không write Neo4j inline — chỉ append outbox event. Document Worker dùng `NoopGraphStore`.

### 4.2 Enrichment Pipeline (implemented — `document_enrichment` queue)

```
DONE → ENRICHING → ENRICHED
```

| Stage | Mô tả | Output |
|---|---|---|
| ENRICHING | Concept extraction (heading → concept) → canonicalize → SQL upsert concepts/mentions → append `concept_graph_project` outbox event | concepts[], chunk_concepts[], outbox event |

**Phase 1 (implemented)**: Heading = Concept (deterministic, zero LLM cost).
**Phase 2 (planned)**: LLM 1-call extraction khi Phase 1 không đủ.

**Tại sao tách riêng:**
- **Failure domain khác nhau**: Core pipeline deterministic (fail = file hỏng). Enrichment có thể non-deterministic khi dùng LLM → retry strategy khác.
- **Latency**: Core pipeline vài giây → vài chục giây. User cần search ngay, không nên block.
- **Scale độc lập**: Core worker CPU/GPU bound (embedding). Enrichment worker IO bound. Cần số worker khác nhau.

**Trigger**: Core Worker sau khi DONE → enqueue job vào `document_enrichment` queue. Enrichment Worker đọc chunks từ SQL (không truyền content qua queue).

**Nếu enrichment fail**: status giữ `DONE`, log error, retry later. **Không bao giờ về ERROR** — user đã search được rồi.

### 4.3 Outbox Worker (implemented — polling service)

```
Poll outbox_events WHERE status = 'PENDING'
  → heading_graph_project  → Neo4j upsert heading graph
  → concept_graph_project  → Neo4j upsert concept graph + status → ENRICHED
  → document_deleted        → Qdrant delete + Neo4j DETACH DELETE
```

Outbox Worker đảm bảo eventual consistency giữa SQL (source of truth) và derived stores (Qdrant, Neo4j).

**Guard**: trước khi project, check `_ensure_document_still_exists()` — nếu document đã bị xóa, skip event và mark done.

### 4.4 Deployment

Hai repo riêng, worker repo depend on core package:

| Component | Repo | Entry point | Container |
|---|---|---|---|
| gRPC API | `chunking_v2` | `python -m src.delivery.grpc.server` | — |
| Document Worker | `DA/worker` | `document-worker` | `DocumentWorkerContainer` |
| Enrichment Worker | `DA/worker` | `enrichment-worker` | `EnrichmentWorkerContainer` |
| Outbox Worker | `DA/worker` | `outbox-worker` | `OutboxWorkerContainer` |

Mỗi container chỉ load dependencies cần thiết:

| Container | MinIO | Postgres | Qdrant | Neo4j | Embedder | Redis |
|---|:---:|:---:|:---:|:---:|:---:|:---:|
| DocumentWorker | x | x | x | - | x | x |
| EnrichmentWorker | - | x | - | - | - | - |
| OutboxWorker | - | x | x | x | - | - |

## 5. Document Status States

```
QUEUED → PARSING → CHUNKING → EMBEDDING → UPSERTING → DONE → ENRICHING → ENRICHED
                                                        ↑
                                              searchable từ đây
```

- `DONE` = đã search được (Qdrant có vectors). Heading graph đang được project async bởi Outbox Worker.
- `ENRICHING` = đang extract concepts (optional, không block search)
- `ENRICHED` = đã có concept graph (search quality tốt hơn, quiz generation sẵn sàng)
- Nếu enrichment fail: status **quay về `DONE`** (không về `ERROR`) — user vẫn search được, enrichment retry later.

## 6. Schema

### 6.1 PostgreSQL

**Implemented:**

```sql
documents_metadata(
    document_id TEXT PK,
    document_name TEXT NOT NULL,
    doc_type TEXT NOT NULL,          -- pdf | docx | pptx | markdown
    mime_type TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,
    storage_key TEXT,                -- MinIO object path
    course_id TEXT,
    owner_id TEXT,
    language TEXT,
    status TEXT NOT NULL,            -- QUEUED | PARSING | ... | DONE | ENRICHING | ENRICHED | ERROR
    error_msg TEXT,
    metadata_json JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

chunks_metadata(
    chunk_id TEXT PK,
    document_id TEXT NOT NULL,
    chunk_index INT NOT NULL,
    heading_path TEXT[] DEFAULT '{}',
    heading_level INT DEFAULT 0,
    page_number INT,
    content_length INT DEFAULT 0,
    language TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

outbox_events(
    id UUID PK,
    event_type TEXT NOT NULL,        -- heading_graph_project | concept_graph_project | document_deleted
    aggregate_id TEXT NOT NULL,      -- document_id
    payload_json JSONB NOT NULL,
    status TEXT DEFAULT 'PENDING',   -- PENDING | DONE | FAILED
    attempts INT DEFAULT 0,
    error_msg TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
```

**Implemented (Phase B — enrichment):**

```sql
concepts(id, name, canonical_name UNIQUE, slug VARCHAR UNIQUE,
         domain, category, language, created_at)
-- category: definition | theorem | algorithm | formula | other
-- slug: normalized English name từ LLM (dùng làm dedup key)

chunk_concepts(chunk_id FK, concept_id FK,
               confidence FLOAT, source VARCHAR,
               -- source: "llm" | "heading" | "manual"
               created_at)
```

**Planned (Phase C — prerequisite + quiz + adaptive learning):**

```sql
concept_prerequisites(
    concept_id TEXT NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    prerequisite_id TEXT NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    source TEXT NOT NULL DEFAULT 'manual',
    -- source: "manual" (giảng viên define) | "auto" (co-occurrence analysis)
    created_by TEXT,              -- user_id của người tạo (NULL nếu auto)
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (concept_id, prerequisite_id),
    CHECK (concept_id != prerequisite_id)
);

quizzes(
    id UUID PRIMARY KEY,
    concept_id TEXT NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    chunk_id TEXT REFERENCES chunks(id) ON DELETE SET NULL,
    question TEXT NOT NULL,
    options JSONB NOT NULL,       -- ["option A", "option B", "option C", "option D"]
    correct_index INT NOT NULL,   -- 0-based index trong options
    explanation TEXT,             -- giải thích đáp án (LLM generate)
    difficulty TEXT NOT NULL DEFAULT 'medium',  -- easy | medium | hard
    source TEXT NOT NULL DEFAULT 'llm',         -- "llm" | "manual"
    language TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_quizzes_concept_id ON quizzes(concept_id);

quiz_attempts(
    id UUID PRIMARY KEY,
    user_id TEXT NOT NULL,
    quiz_id UUID NOT NULL REFERENCES quizzes(id) ON DELETE CASCADE,
    selected_index INT NOT NULL,
    is_correct BOOLEAN NOT NULL,
    answered_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_quiz_attempts_user ON quiz_attempts(user_id, answered_at);

user_concept_progress(
    user_id TEXT NOT NULL,
    concept_id TEXT NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'not_started',
    -- status: "not_started" | "in_progress" | "weak" | "mastered"
    score FLOAT NOT NULL DEFAULT 0.0,          -- 0.0 → 1.0
    total_attempts INT NOT NULL DEFAULT 0,
    correct_attempts INT NOT NULL DEFAULT 0,
    last_attempt_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, concept_id)
);

-- Rule: score = correct_attempts / total_attempts
-- "weak"     = score < 0.5 AND total_attempts >= 3
-- "mastered" = score >= 0.8 AND total_attempts >= 3
```

### 6.2 Qdrant payload

```json
{
  "chunk_id": "uuid",
  "document_id": "uuid",
  "course_id": "uuid",
  "owner_id": "uuid",
  "doc_type": "pdf|docx|pptx|markdown|video_transcript",
  "chunk_index": 12,
  "heading_path": ["Chapter 1", "1.2 Basics"],
  "heading_level": 2,
  "page_number": 4,
  "language": "vi",
  "content": "...",
  "enriched_content": "Chapter 1 > 1.2 Basics\n\n..."
}
```

Indexed payload fields: `document_id`, `course_id`, `owner_id`, `doc_type`, `language`

### 6.3 Neo4j Graph Schema

**Nodes:**

| Node | Properties | Status | Ghi chú |
|---|---|---|---|
| `(:Course)` | id | Implemented | |
| `(:Owner)` | id | Implemented | |
| `(:Document)` | id, name, doc_type | Implemented | |
| `(:Heading)` | id, title, level, document_id | Implemented | id = hash(doc_id + heading_path) |
| `(:Chunk)` | id, document_id, chunk_index, page_number, language | Implemented | id = UUID từ PostgreSQL |
| `(:Concept)` | id, name, canonical_name, slug, domain, category, language | Implemented | id = slug (normalized) |

**Relationships:**

```
-- Đã implement (heading graph):
(Course)-[:HAS_DOCUMENT]->(Document)
(Owner)-[:OWNS_DOCUMENT]->(Document)
(Document)-[:HAS_HEADING]->(Heading)         -- top-level headings only
(Heading)-[:HAS_SUBHEADING]->(Heading)       -- recursive hierarchy
(Heading)-[:HAS_CHUNK]->(Chunk)              -- leaf heading → chunk
(Document)-[:HAS_CHUNK]->(Chunk)
(Chunk)-[:NEXT]->(Chunk)                     -- sequential order

-- Đã implement (concept graph — Phase B):
(Chunk)-[:MENTIONS {confidence, source}]->(Concept)

-- Planned (prerequisite + adaptive learning — Phase C):
(Concept)-[:PREREQUISITE_OF {source, created_by}]->(Concept)
-- source: "manual" | "auto"
-- Đọc: "A PREREQUISITE_OF B" = "phải nắm A trước khi học B"
-- Ví dụ: (derivative)-[:PREREQUISITE_OF]->(integration)

(Concept)-[:RELATED_TO {weight}]->(Concept)  -- co-occurrence trong cùng chunk/heading
```

Tất cả writes dùng `MERGE` để đảm bảo idempotent — safe to retry.

**Lưu ý thiết kế:**
- `PREREQUISITE_OF` Phase C ban đầu là **manual** (giảng viên define). Phase sau có thể auto-suggest dựa trên document order + co-occurrence, nhưng luôn cần giảng viên confirm.
- Bỏ `SAME_AS` → merge luôn vào 1 node khi canonicalize (đơn giản hơn, tránh transitive closure problem).
- **Cycle detection**: khi thêm prerequisite edge, phải check cycle trước — `(A)-[:PREREQUISITE_OF*]->(B)` rồi thêm `(B)-[:PREREQUISITE_OF]->(A)` sẽ tạo deadlock learning path.

## 7. Concept Extraction Strategy

### Phase 1 — Heading = Concept (implemented)

Heading structure đã có → mỗi heading là 1 topic/concept. Không cần NER, không cần LLM.

- `RunEnrichmentUseCase` extract headings từ `StoredChunkMetadata.heading_path`
- Normalize: clean whitespace → lowercase → slugify (handle Vietnamese đ/Đ)
- Dedup: `concepts_by_id[slug]` — cùng slug = cùng concept
- Upsert `concepts` + `chunk_concepts` vào SQL
- Append `concept_graph_project` outbox event → Outbox Worker project sang Neo4j

Khi nào đủ: RAG search cơ bản, quiz generation theo chương/mục, prerequisite mapping manual.
Khi nào không đủ: heading quá chung (ví dụ "Chapter 1") hoặc 1 chunk chứa nhiều concepts không phản ánh trong heading.

### Phase 2 — LLM 1-Call (planned — khi Phase 1 không đủ)

Gộp extract + canonicalize vào 1 LLM call duy nhất:

```
Extract key academic concepts from this chunk.
For each concept, return:
- name (Vietnamese)
- slug (canonical English name, lowercase, hyphenated)
- category: definition | theorem | algorithm | formula | other
```

Output: structured JSON (JSON mode). LLM tự normalize "đạo hàm" → "derivative", "ma trận nghịch đảo" → "inverse-matrix".

**Dedup**: dùng BGE-M3 embed concept names, cosine > 0.85 → coi là same concept → merge vào node có nhiều MENTIONS hơn. Không dùng Levenshtein (kém với tiếng Việt).

**RELATED_TO**: 2 concepts xuất hiện trong cùng chunk hoặc cùng heading section → tạo edge, weight = co-occurrence count.

## 8. Prerequisite Engine & Adaptive Learning (planned)

### 8.1 Prerequisite Management

**Thêm prerequisite** (giảng viên hoặc auto-suggest):

```cypher
-- Cycle check trước khi thêm:
OPTIONAL MATCH path = (target:Concept {id: $prerequisite_id})<-[:PREREQUISITE_OF*1..10]-(source:Concept {id: $concept_id})
WITH path
WHERE path IS NOT NULL
RETURN count(path) > 0 AS would_create_cycle
```

Nếu `would_create_cycle = false`:

```cypher
MATCH (a:Concept {id: $prerequisite_id})
MATCH (b:Concept {id: $concept_id})
MERGE (a)-[:PREREQUISITE_OF {source: $source, created_by: $user_id}]->(b)
```

### 8.2 Prerequisite Traversal — "Học lại gì khi hổng concept X?"

Khi user fail quiz cho concept X (score < 0.5):

```cypher
-- Tìm tất cả prerequisite cần học lại (BFS ngược):
MATCH path = (prereq:Concept)-[:PREREQUISITE_OF*1..5]->(target:Concept {id: $concept_id})
RETURN prereq.id AS concept_id,
       prereq.name AS name,
       prereq.slug AS slug,
       length(path) AS depth
ORDER BY depth DESC
```

Worker/API kết hợp với `user_concept_progress` trong SQL để filter:
- Chỉ trả prerequisite có `status != 'mastered'`
- Sort theo depth DESC (học prerequisite sâu nhất trước)

### 8.3 Knowledge Map — "Bản đồ tri thức cá nhân"

**Query: lấy concept graph + user progress cho 1 course:**

```cypher
-- Lấy tất cả concepts liên quan đến course:
MATCH (c:Course {id: $course_id})-[:HAS_DOCUMENT]->(d:Document)-[:HAS_CHUNK]->(chunk:Chunk)-[:MENTIONS]->(concept:Concept)
WITH DISTINCT concept
OPTIONAL MATCH (concept)-[:PREREQUISITE_OF]->(target:Concept)
OPTIONAL MATCH (prereq:Concept)-[:PREREQUISITE_OF]->(concept)
RETURN concept.id AS id,
       concept.name AS name,
       concept.slug AS slug,
       concept.category AS category,
       collect(DISTINCT target.id) AS unlocks,      -- concept này là tiền đề của gì
       collect(DISTINCT prereq.id) AS requires      -- concept này cần học trước gì
```

Frontend (D3.js/vis.js) nhận graph data + join với `user_concept_progress` SQL:
- **Xanh**: mastered (score >= 0.8)
- **Vàng**: in_progress (0.5 <= score < 0.8)
- **Đỏ**: weak (score < 0.5)
- **Xám**: not_started

### 8.4 Dynamic Learning Path — "Gợi ý học gì tiếp?"

Logic tính toán (trong application layer, không phải Neo4j):

```
1. Lấy tất cả concepts trong course (Neo4j query trên)
2. Join với user_concept_progress (SQL)
3. Tìm concepts "ready to learn":
   - status = "not_started" hoặc "weak"
   - TẤT CẢ prerequisites đều "mastered"
4. Sort theo:
   - Ưu tiên "weak" trước "not_started" (ôn lại trước khi học mới)
   - Ưu tiên concept có nhiều dependents (unlock nhiều bài hơn)
5. Trả top-N concepts → map sang chunks/quizzes
```

## 9. Hybrid Retrieval (planned)

### Chiến lược: Retrieve-then-Expand (không rerank)

Khi chưa có eval data, giữ đơn giản:

1. **Qdrant top-K** (cosine similarity) — ranked by semantic score, đây là signal mạnh nhất
2. **Neo4j expand** — append thêm related chunks, **không xáo trộn thứ tự Qdrant**
3. **Dedup** by chunk_id

### Graph expansion query (chung cho mọi query, không phân intent)

```cypher
-- Lấy chunks cùng heading section (sibling chunks):
MATCH (h:Heading)-[:HAS_CHUNK]->(c:Chunk)
WHERE c.id IN $topKChunkIds
WITH h
MATCH (h)-[:HAS_CHUNK]->(sibling:Chunk)
WHERE NOT sibling.id IN $topKChunkIds
RETURN sibling.id AS chunk_id, 'sibling' AS source

UNION

-- Lấy chunks liền kề (trước/sau):
MATCH (c:Chunk)-[:NEXT]->(next:Chunk)
WHERE c.id IN $topKChunkIds AND NOT next.id IN $topKChunkIds
RETURN next.id AS chunk_id, 'next' AS source

UNION

MATCH (prev:Chunk)-[:NEXT]->(c:Chunk)
WHERE c.id IN $topKChunkIds AND NOT prev.id IN $topKChunkIds
RETURN prev.id AS chunk_id, 'prev' AS source
```

Kết quả: Qdrant top-K giữ nguyên thứ tự + append expanded chunks ở cuối.

### Concept-based expansion (Phase C — khi concept graph đã có)

Khi user query liên quan đến concept cụ thể, expand qua prerequisite graph:

```cypher
-- Lấy chunks từ prerequisite concepts (giúp RAG chatbot giải thích nền tảng):
MATCH (c:Chunk)-[:MENTIONS]->(concept:Concept)
WHERE c.id IN $topKChunkIds
WITH DISTINCT concept
MATCH (prereq:Concept)-[:PREREQUISITE_OF]->(concept)
MATCH (prereqChunk:Chunk)-[:MENTIONS]->(prereq)
WHERE NOT prereqChunk.id IN $topKChunkIds
RETURN prereqChunk.id AS chunk_id, 'prerequisite' AS source
LIMIT 5
```

Use case: RAG chatbot giải thích "tích phân" → kéo thêm chunks về "đạo hàm" (prerequisite) để context đầy đủ hơn.

### Khi nào thêm complexity?

- **Intent classification**: chỉ thêm khi có evidence rằng 1 strategy chung không đủ tốt
- **Reranking formula**: chỉ thêm khi có eval dataset (golden queries + expected chunks) để tune weights
- **Prerequisite expansion**: bật khi concept graph + prerequisite edges đã đủ dày

## 10. Đồng bộ dữ liệu an toàn (Outbox Pattern) — implemented

1. Trong cùng transaction SQL: ghi dữ liệu business + ghi `outbox_events`.
2. Outbox Worker poll events `status=PENDING`, push update sang Qdrant/Neo4j.
3. Đánh dấu event `DONE` khi derived stores cập nhật thành công.
4. Nếu lỗi: `mark_outbox_failed()` tăng `attempts`, giữ `PENDING` để retry. Khi `attempts >= max_attempts` → chuyển `FAILED`.

**Event types:**

| Event | Trigger | Outbox Worker action |
|---|---|---|
| `heading_graph_project` | PipelineCore sau UPSERTING | Project Document/Chunk/Heading/NEXT sang Neo4j |
| `concept_graph_project` | RunEnrichmentUseCase sau upsert concepts | Project Concept nodes + MENTIONS edges sang Neo4j, set status → ENRICHED |
| `document_deleted` | DeleteDocumentUseCase | Xóa Qdrant points + Neo4j DETACH DELETE |

**Guard logic**: Outbox Worker check `_ensure_document_still_exists()` trước khi project heading/concept. Nếu document đã bị xóa (race condition), skip event và mark done — tránh project orphan data.

## 11. Xóa Document (DeleteDocument) — implemented

Xóa đồng bộ cả 3 stores, **outbox event trong cùng transaction với DELETE**:

```
BEGIN TRANSACTION
  1. DELETE FROM outbox_events WHERE aggregate_id = doc_id   -- cancel pending projections
  2. INSERT outbox_events (event_type='document_deleted')     -- schedule derived store cleanup
  3. DELETE FROM chunks_metadata WHERE document_id = doc_id
  4. DELETE FROM documents_metadata WHERE document_id = doc_id
COMMIT

5. Outbox Worker picks up 'document_deleted' event:
   - Qdrant: vector_store.delete_by_document(doc_id)
   - Neo4j: graph_store.delete_document(doc_id) → DETACH DELETE Document + Chunks + Headings
   - Mark outbox DONE
```

**Flow**:
- `DeleteDocumentUseCase` chỉ gọi `metadata_store.delete()` — tất cả logic nằm trong 1 SQL transaction.
- Nếu app crash giữa chừng: outbox event đã commit → Outbox Worker sẽ retry cleanup Qdrant/Neo4j.
- Nếu Qdrant delete thành công nhưng Neo4j fail → outbox giữ PENDING, retry lần sau (Qdrant delete idempotent).

## 12. Quiz Generation Pipeline (planned)

### Flow

```
Concept (SQL) → lấy chunks MENTIONS concept → gộp context → LLM generate quiz → store SQL
```

### Chi tiết

1. **Trigger**: Manual (giảng viên chọn concept → "Generate quiz") hoặc batch (sau enrichment xong).
2. **Input**: concept_id → query `chunk_concepts` → lấy chunk content từ Qdrant payload.
3. **LLM Prompt** (1 call per concept, gộp nhiều chunks):

```
Based on the following content about "{concept_name}", generate {n} multiple-choice questions.

Content:
{chunk_contents joined}

For each question, return JSON:
{
  "question": "...",
  "options": ["A", "B", "C", "D"],
  "correct_index": 0,
  "explanation": "...",
  "difficulty": "easy|medium|hard"
}
```

4. **Output**: Upsert vào `quizzes` table. Mỗi quiz link concept_id + chunk_id (chunk chính tạo ra câu hỏi).
5. **Review**: Giảng viên có thể edit/approve trước khi publish cho học viên.

### Quiz Evaluation → User Progress

```
Học viên trả lời quiz
    ↓
INSERT quiz_attempts (user_id, quiz_id, selected_index, is_correct)
    ↓
UPSERT user_concept_progress:
    total_attempts += 1
    correct_attempts += (1 if is_correct)
    score = correct_attempts / total_attempts
    status = CASE
        WHEN total_attempts < 3        THEN 'in_progress'
        WHEN score >= 0.8              THEN 'mastered'
        WHEN score < 0.5               THEN 'weak'
        ELSE 'in_progress'
    END
    ↓
Nếu status = 'weak':
    → Trigger prerequisite traversal (section 8.2)
    → Gợi ý học lại prerequisite concepts
```

### Worker

Quiz generation có thể là use case sync (giảng viên bấm generate → chờ response) hoặc async worker nếu batch:
- Queue: `quiz_generation`
- Payload: `{ concept_id, num_questions, difficulty }`
- Container: chỉ cần Postgres + LLM client (không cần Qdrant/Neo4j/Embedder)

## 13. Video Pipeline (planned)

### Flow

```
Video/Audio file → Whisper STT → Transcript text
    ↓
Segment by silence / sentence boundary → TimestampedSection[]
    ↓
Chunk (reuse HeadingChunker hoặc sentence-window chunker)
    ↓
Embed → Upsert Qdrant → Core Pipeline bình thường
```

### Implementation

1. **New parser**: `WhisperTranscriptParser` implements `IParser`
   - Input: video/audio file (mp4, mp3, wav, m4a)
   - Process: Whisper API hoặc local whisper model → transcript với timestamps
   - Output: `ParsedDocument` với sections mapped từ transcript segments

2. **New DocumentType**: `VIDEO_TRANSCRIPT = "video_transcript"`

3. **Chunk metadata mở rộng** (backward-compatible):
   ```
   start_time_ms: int | None    # timestamp bắt đầu segment
   end_time_ms: int | None      # timestamp kết thúc segment
   ```

4. **FFmpeg clip extraction** (optional): sau khi có chunks với timestamps, có thể cắt video gốc thành clip ngắn cho mỗi chunk → lưu MinIO → link trong UI.

5. **Neo4j**: Chunk node thêm `start_ms`, `end_ms` — không cần thay đổi graph schema.

### Lưu ý
- Whisper API: ~$0.006/minute — budget cho batch processing
- Local whisper (faster-whisper): free, cần GPU, slower
- Có thể dùng Google Speech-to-Text cho tiếng Việt quality tốt hơn

## 14. Rollout

### Phase A — Core Pipeline (done)
- [x] Chuẩn hóa metadata SQL + payload Qdrant (`course_id`, `owner_id`)
- [x] PostgreSQL connection pooling (`psycopg_pool`)
- [x] Core Pipeline: parse → chunk → embed → upsert (Qdrant + SQL)
- [x] Heading graph projection via outbox (PipelineCore chỉ append event, không write Neo4j inline)
- [x] BullMQ async worker (`document_processing` queue)
- [x] gRPC API: ProcessDocument (sync), EnqueueDocument (async), Search, GetDocumentStatus, HealthCheck
- [x] Flow search cơ bản (Qdrant cosine + metadata filter)

### Phase B — Enrichment + Outbox + Delete (done)
- [x] Worker repo tách riêng (`DA/worker`) với container factory per worker type
- [x] Enrichment Worker: `document_enrichment` queue + `EnrichmentWorkerContainer`
- [x] Concept extraction Phase 1: Heading = Concept (deterministic, zero LLM)
- [x] SQL schema: `concepts`, `chunk_concepts` tables
- [x] Neo4j adapter: `upsert_concept_graph()` — Concept nodes + MENTIONS edges
- [x] Outbox Worker: polling service xử lý `heading_graph_project`, `concept_graph_project`, `document_deleted`
- [x] Outbox Worker guard: `_ensure_document_still_exists()` trước khi project
- [x] DeleteDocument: outbox-first pattern — cancel pending events + append `document_deleted` + delete SQL trong cùng transaction
- [x] Outbox Worker delete: Qdrant `delete_by_document()` + Neo4j `DETACH DELETE`
- [x] Neo4j `delete_document()` method

### Phase C — Prerequisite + Quiz + Hybrid Retrieval (next)
- [ ] SQL schema: `concept_prerequisites`, `quizzes`, `quiz_attempts`, `user_concept_progress`
- [ ] Neo4j: `PREREQUISITE_OF` edge + cycle detection
- [ ] IGraphStore: `upsert_prerequisite()`, `get_prerequisites()`, `get_knowledge_map()`
- [ ] API: prerequisite CRUD (giảng viên define manual)
- [ ] Hybrid retrieval: Qdrant top-K + Neo4j expand (sibling + NEXT chunks)
- [ ] Quiz generation: LLM 1-call per concept → structured JSON → store SQL
- [ ] Quiz evaluation → `user_concept_progress` update
- [ ] Prerequisite traversal: khi user weak → trace prerequisite graph → gợi ý học lại
- [ ] Knowledge Map API: concept graph + user progress cho frontend visualization

### Phase D — Video + RAG Chatbot + Adaptive (future)
- [ ] Video pipeline: Whisper STT → transcript → chunk → embed (new parser)
- [ ] FFmpeg clip extraction (optional): cắt video theo timestamp
- [ ] Concept extraction Phase 2: LLM 1-call + embedding dedup
- [ ] RELATED_TO edges (co-occurrence)
- [ ] Prerequisite auto-suggest: dựa trên document order + co-occurrence, cần giảng viên confirm
- [ ] RAG chatbot: Qdrant search + Neo4j prerequisite expansion → LLM answer grounded
- [ ] Dynamic learning path: tính "ready to learn" concepts dựa trên prerequisite + user progress
- [ ] Concept-based hybrid retrieval expansion (prerequisite chunks trong RAG context)
- [ ] KPI: Recall@K, MRR, latency p95, answer groundedness, learning outcome metrics
