# Research Prompt — Micro-Content & Quiz Generation Pipeline

> Dùng prompt này để nghiên cứu với các AI model (Claude, GPT, Gemini...).
> Copy toàn bộ nội dung bên dưới.

---

## Context

Tôi đang xây dựng một hệ thống **Micro-Content & Quiz Generation** cho sinh viên đại học. Hệ thống nhận tài liệu học thuật (PDF, DOCX, PPTX, Markdown) từ giảng viên, xử lý qua pipeline rồi phục vụ cho:

- **Semantic search**: sinh viên tìm kiếm nội dung theo ngữ nghĩa
- **RAG chatbot**: chatbot trả lời câu hỏi dựa trên tài liệu
- **Auto-generated quiz**: tự sinh câu hỏi ôn tập từ nội dung, gắn với concept graph

## Kiến trúc hiện tại (đã implement)

### Tech stack

- **PostgreSQL**: source of truth — metadata document, chunk metadata, trạng thái pipeline, ACL
- **Qdrant**: vector store — lưu embedding chunks, semantic search top-K
- **Neo4j**: graph store — heading hierarchy graph (Document → Heading → Subheading → Chunk)
- **MinIO**: object storage — raw file
- **Redis + BullMQ**: job queue — async pipeline processing
- **BGE-M3**: embedding model — 1024-dim, multilingual (vi/en)
- **gRPC**: API layer

### Core Pipeline (đã chạy)

```
QUEUED → PARSING → CHUNKING → EMBEDDING → UPSERTING → DONE
```

1. **PARSING**: Docling (PDF), python-docx (DOCX), python-pptx (PPTX), markdown parser → extract text + heading structure
2. **CHUNKING**: Heading-aware chunker — split theo heading hierarchy, giữ nguyên heading_path metadata
3. **EMBEDDING**: BGE-M3 encode chunks → 1024-dim vectors
4. **UPSERTING**: Upsert vectors + payload vào Qdrant, insert chunk metadata vào PostgreSQL, project heading graph sang Neo4j (via outbox pattern)
5. **Status tracking**: state machine với `last_success_state` để resume nếu fail giữa chừng

### Data flow

- SQL là source of truth. Qdrant và Neo4j là derived stores (rebuild được từ SQL + MinIO)
- Dùng Outbox Pattern: ghi outbox_events trong cùng transaction SQL → worker đọc và sync sang Qdrant/Neo4j
- Delete document phải xóa cả 3 stores (outbox event trong cùng transaction với DELETE)

### Neo4j heading graph (đã có)

```
(Course)-[:HAS_DOCUMENT]->(Document)
(Document)-[:HAS_HEADING]->(Heading)
(Heading)-[:HAS_SUBHEADING]->(Heading)      -- recursive
(Heading)-[:HAS_CHUNK]->(Chunk)
(Document)-[:HAS_CHUNK]->(Chunk)
(Chunk)-[:NEXT]->(Chunk)                     -- sequential order
```

### Search hiện tại

Qdrant cosine similarity + metadata filter (course_id, owner_id, doc_type, language) → return top-K chunks. Chưa có graph expansion.

## Quyết định kiến trúc đã chốt

### 2-Worker Architecture

Tôi đã quyết định tách enrichment (concept extraction, concept graph) ra khỏi core pipeline thành worker riêng:

```
Core Worker (document_processing queue) — đã có:
  QUEUED → PARSING → CHUNKING → EMBEDDING → UPSERTING → DONE (searchable ngay)

Enrichment Worker (document_enrichment queue) — planned:
  DONE → ENRICHING → ENRICHED (search quality tốt hơn)
```

Lý do:
- Core pipeline phải DONE nhanh để user search được ngay
- LLM calls (concept extraction) có failure domain khác (rate limit, timeout) → retry riêng
- Scale độc lập: core = CPU/GPU bound, enrichment = IO bound

Cùng repo, khác entry point: `python -m document_chunk.infrastructure.queue.enrichment_worker`

### Concept Extraction — 2 phases

- **Phase 1 (đơn giản)**: Heading = Concept. Mỗi heading trong document là 1 concept node trong Neo4j. Zero LLM cost.
- **Phase 2 (khi cần)**: LLM 1-call extract + canonicalize. Gộp extract + normalize vào 1 prompt. Dedup bằng embedding similarity (BGE-M3, cosine > threshold) thay vì Levenshtein.
- Bỏ PREREQUISITE_OF tự động → dùng document order (chunk_index) làm implicit prerequisite.
- Bỏ SAME_AS relationship → merge vào 1 node khi canonicalize.

### Hybrid Retrieval — Retrieve-then-Expand (không rerank)

- Qdrant top-K giữ nguyên thứ tự (signal mạnh nhất)
- Neo4j chỉ append thêm related chunks (sibling cùng heading, adjacent chunks) — không re-score
- Không intent classification ban đầu — 1 strategy chung
- Reranking formula chỉ thêm khi có eval data

## Các vấn đề còn mở cần nghiên cứu

### Vấn đề 1: Concept Extraction — chi tiết implementation

Phase 1 (Heading = Concept) khá straightforward. Câu hỏi tập trung vào Phase 2 (LLM 1-call):

**Câu hỏi:**
1. LLM 1-call prompt nên thiết kế thế nào cho tiếng Việt academic content? Có cần structured output (JSON mode) không? Prompt mẫu?
2. Embedding-based dedup (BGE-M3) cho tiếng Việt academic terms — threshold nào hợp lý? Có cần fine-tune không?
3. Khi nào Phase 1 (Heading = Concept) thực sự không đủ? Có metrics nào để đo và quyết định chuyển sang Phase 2?
4. Concept `slug` (canonical English name) — LLM có generate ổn định không? Hay nên dùng approach khác?
5. RELATED_TO edges dựa trên co-occurrence (2 concepts cùng chunk) — threshold bao nhiêu co-occurrences thì tạo edge?

**Từ khóa tìm hiểu:**
- "LLM-based entity normalization"
- "zero-shot entity linking"
- "embedding-based entity deduplication"
- "heading-based topic modeling"

### Vấn đề 2: Hybrid Retrieval — graph expansion chi tiết

Chiến lược Retrieve-then-Expand đã chốt. Câu hỏi về implementation:

**Câu hỏi:**
1. Graph expansion: sibling chunks (cùng heading) vs adjacent chunks (NEXT) vs cả hai — cái nào giá trị hơn cho RAG context?
2. Bao nhiêu expanded chunks nên append? Fixed limit hay proportional to top-K?
3. Khi có concept graph (Phase 2), expansion query nên thêm gì? `(Chunk)-[:MENTIONS]->(Concept)<-[:MENTIONS]-(OtherChunk)` có đáng không?
4. Có framework/tool nào lightweight để eval retrieval quality? (RAGAS, DeepEval, custom?) Bao nhiêu golden queries là đủ để bắt đầu đo?
5. Khi nào nên chuyển từ Retrieve-then-Expand sang Retrieve-then-Rerank? Dấu hiệu nào cho thấy cần reranking?

**Từ khóa tìm hiểu:**
- "retrieve-then-expand"
- "graph-augmented retrieval"
- "RAG evaluation framework"
- "retrieval quality metrics for education"

### Vấn đề 3: Enrichment Worker — pipeline design

**Câu hỏi:**
1. Enrichment worker nên xử lý tất cả chunks của 1 document trong 1 job, hay chia nhỏ per-chunk?
2. LLM rate limiting strategy: batch N chunks per LLM call? Hay 1 call per chunk? Trade-off giữa latency vs token efficiency?
3. Nếu enrichment fail giữa chừng (đã extract 30/50 chunks), nên retry từ đầu hay resume từ chunk 31?
4. Enrichment worker có nên chạy lại khi document được re-upload (cập nhật nội dung)? Hay chỉ chạy cho documents mới?
5. Với BullMQ, cách tốt nhất để implement "DONE triggers enrichment job" — emit trong PipelineCore hay dùng BullMQ flow (parent-child jobs)?

### Vấn đề 4: Quiz Generation (core of v1 thesis)

**Context**: Thesis v1 là "Micro-Content & Quiz Generation" — LLM sinh quiz từ concept-grounded chunks. Baseline so sánh: "naive GPT prompt" (raw text, không concept/chunk structure) vs pipeline output.

**Câu hỏi:**
1. Nên sinh quiz trong enrichment pipeline (eager) hay khi user request (lazy)? Trade-off?
2. Nếu eager: quiz generation nên là stage riêng trong enrichment worker hay worker thứ 3?
3. Quiz quality: cần concept graph để sinh quiz tốt hơn, hay chỉ cần chunks là đủ?
4. Bloom taxonomy levels — LLM có phân loại câu hỏi theo level tốt không? Prompt design thế nào?
5. Có cần human-in-the-loop (giảng viên review quiz trước khi publish) không? Nếu có, workflow thế nào?
6. Evaluation rubric: ngoài "relevance, difficulty accuracy, distractor quality", còn metric nào nên đo?

## Constraints

- Team nhỏ (2-3 người), bandwidth hạn chế → ưu tiên giải pháp đơn giản, ít maintenance
- Tài liệu chủ yếu tiếng Việt, một phần tiếng Anh
- Scale: ~1000 documents, ~100K chunks (university-level, không phải enterprise)
- Budget LLM hạn chế → minimize số lần gọi LLM
- Ưu tiên: ship đơn giản trước, đo kết quả, rồi thêm complexity — không over-engineer upfront

## Output mong muốn

Hãy phân tích từng vấn đề ở trên và đưa ra:
1. **Recommendation cụ thể** cho context của tôi (team nhỏ, scale nhỏ, tiếng Việt)
2. **Trade-off** rõ ràng cho mỗi lựa chọn
3. **Thứ tự ưu tiên** implement cái gì trước
4. **Ví dụ cụ thể** (query mẫu, schema mẫu, prompt mẫu) khi có thể
5. **Từ khóa / paper / tool** để tôi tìm hiểu thêm
