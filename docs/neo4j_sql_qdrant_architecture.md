# Kien truc ket hop PostgreSQL + Qdrant + Neo4j

## 1. Muc tieu

Tai lieu nay mo ta cach ket hop 3 he thong luu tru:

- `PostgreSQL`: metadata, trang thai, quyen truy cap, du lieu giao dich.
- `Qdrant`: vector retrieval (semantic search top-K nhanh).
- `Neo4j`: quan he do thi (concept, prerequisite, related chunks, citation).

Muc tieu la vua giu duoc tinh on dinh giao dich◊ (SQL), vua co semantic retrieval manh (Qdrant), vua truy van quan he hoc thuat linh hoat (Neo4j).

## 2. Phan cong vai tro

| Thanh phan | Vai tro chinh | Khong nen dung cho |
|---|---|---|
| PostgreSQL | Source of truth cho `documents`, `chunks`, ACL, status pipeline, card/quiz | Vector ANN, graph traversal sau |
| Qdrant | Tim chunk lien quan theo embedding, score cosine | Transaction business, join phuc tap |
| Neo4j | Truy van quan he: concept graph, prerequisite path, section lien quan | Source of truth giao dich |

## 3. He thong se lam nhung gi

### 3.1 Ingest tai lieu

1. API nhan file, tao `document` trong SQL (status = `QUEUED`).
2. Pipeline parse -> chunk -> embed.
3. Luu chunk metadata vao SQL.
4. Upsert vector va payload vao Qdrant.
5. Project quan he sang Neo4j (chunk -> concept -> section -> course).
6. Cap nhat SQL status = `DONE`.

### 3.2 Search/Retrieval

1. SQL loc pham vi truy cap (`owner_id`, `course_id`, `is_published`, ...).
2. Qdrant tim top-K chunk theo query embedding trong pham vi duoc phep.
3. Neo4j mo rong context theo quan he (concept lien quan, prerequisite).
4. Rerank ket qua (score semantic + score graph + rule business).
5. Tra ve ket qua co citation ro rang (`chunk_id`, heading, page, relation path).

### 3.3 Chatbot RAG

1. Nhan cau hoi.
2. Semantic retrieve tu Qdrant.
3. Graph expansion tu Neo4j de bo sung context hoc thuat.
4. Tao prompt co citation.
5. Ghi log chat va feedback vao SQL.

## 4. Data ownership (quan trong)

Quy tac de tranh roi:

- SQL la `source of truth`.
- Qdrant va Neo4j la `derived stores` (du lieu suy dien tu SQL + pipeline output).
- Xoa/chinh sua o SQL phai phat su kien de cap nhat Qdrant/Neo4j.

## 5. Schema de xuat

### 5.1 SQL (toi thieu)

- `documents(id, owner_id, course_id, title, file_key, status, created_at, updated_at)`
- `chunks(id, document_id, chunk_index, heading_path, page_start, page_end, token_count, content, created_at, updated_at)`
- `concepts(id, name, canonical_name)`
- `chunk_concepts(chunk_id, concept_id, confidence)`
- `outbox_events(id, aggregate_type, aggregate_id, event_type, payload_json, status, created_at)`

### 5.2 Qdrant payload (toi thieu)

```json
{
  "chunk_id": "uuid",
  "document_id": "uuid",
  "course_id": "uuid",
  "owner_id": "uuid",
  "doc_type": "pdf|docx|pptx|markdown",
  "chunk_index": 12,
  "heading_path": ["Chapter 1", "1.2 Basics"],
  "page_number": 4,
  "language": "vi",
  "content": "...",
  "enriched_content": "..."
}
```

Index payload can co:

- `document_id`
- `course_id`
- `owner_id`
- `doc_type`
- `language`

### 5.3 Neo4j model (toi thieu)

Node:

- `(:Course {id})`
- `(:Document {id})`
- `(:Chunk {id, chunk_index})`
- `(:Concept {id, name})`

Edge:

- `(:Course)-[:HAS_DOCUMENT]->(:Document)`
- `(:Document)-[:HAS_CHUNK]->(:Chunk)`
- `(:Chunk)-[:MENTIONS {confidence}]->(:Concept)`
- `(:Concept)-[:PREREQUISITE_OF]->(:Concept)`
- `(:Chunk)-[:NEXT]->(:Chunk)` (thu tu hoc)

## 6. Luong dong bo du lieu de an toan

Nen dung `Outbox Pattern`:

1. Trong cung transaction SQL: ghi du lieu business + ghi `outbox_events`.
2. Worker doc outbox, day update sang Qdrant/Neo4j.
3. Danh dau event `DONE` khi ca 2 ben cap nhat thanh cong.
4. Neu loi, retry idempotent theo `event_id`.

Loi ich:

- Tranh dual-write khong nhat quan.
- Co kha nang replay khi can rebuild Qdrant/Neo4j.

## 7. Query mau

### 7.1 SQL filter truoc retrieval

```sql
SELECT c.id
FROM chunks c
JOIN documents d ON d.id = c.document_id
WHERE d.course_id = :course_id
  AND d.owner_id = :owner_id
  AND d.status = 'DONE';
```

### 7.2 Qdrant semantic search

- Query embedding tu user question.
- Filter payload theo `course_id`, `owner_id`.
- Lay top-K chunks.

### 7.3 Neo4j graph expansion

```cypher
MATCH (c:Chunk)-[:MENTIONS]->(k:Concept)<-[:MENTIONS]-(n:Chunk)
WHERE c.id IN $topKChunkIds
RETURN n.id AS related_chunk_id, count(*) AS overlap
ORDER BY overlap DESC
LIMIT 20
```

## 8. Loi ich va trade-off

### Loi ich

- Search co context sau hon.
- Ho tro recommendation va prerequisite learning path tot hon.
- Giam hallucination khi RAG co them graph context.

### Trade-off

- Van hanh phuc tap hon (3 stores).
- Can co chien luoc sync nghiem tuc.
- Tang chi phi monitoring, backup, migration.

## 9. Rollout de xuat (thuc dung)

### Phase A (ngan han)

1. Chuan hoa metadata SQL + payload Qdrant (`course_id`, `owner_id`).
2. Dam bao flow parse/chunk/embed/search on dinh.

### Phase B

1. Tao `IGraphStore` + `Neo4jGraphAdapter`.
2. Project `Document/Chunk/Concept` sang Neo4j qua outbox worker.

### Phase C

1. Hybrid retrieval: SQL filter -> Qdrant top-K -> Neo4j expansion -> rerank.
2. Do luong KPI: Recall@K, MRR, latency p95, answer groundedness.

## 10. Khi nao KHONG nen dung Neo4j

Khong nen dung Neo4j neu:

- Chi can search semantic co ban.
- Chua co use case do thi ro rang (prerequisite/recommendation/citation graph).
- Team chua co bandwidth van hanh them 1 database.

Trong truong hop do, `SQL + Qdrant` la du de ship nhanh va giam complexity.

