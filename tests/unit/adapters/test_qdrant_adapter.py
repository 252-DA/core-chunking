from document_chunk.adapters.vector_db.qdrant_adapter import QdrantAdapter


def test_payload_round_trip_preserves_embedding_input(sample_chunk, qdrant_config):
    adapter = QdrantAdapter(qdrant_config)

    payload = adapter._chunk_to_payload(sample_chunk)
    restored = adapter._payload_to_chunk(payload)

    assert payload["embedding_input"] == sample_chunk.embedding_input
    assert restored.embedding_input == sample_chunk.embedding_input


def test_structural_payload_and_default_toc_filter(sample_chunk, qdrant_config):
    from dataclasses import replace
    from document_chunk.domain.entities.search import SearchFilter
    adapter = QdrantAdapter(qdrant_config)
    sample_chunk.metadata = replace(sample_chunk.metadata, content_type='table', token_count=400,
        page_start=2, page_end=4, part_index=1, part_count=3, section_id='section', chunker_version='structural-v2')
    restored = adapter._payload_to_chunk(adapter._chunk_to_payload(sample_chunk))
    assert restored.metadata == sample_chunk.metadata
    assert adapter._build_filter(SearchFilter()).must_not[0].match.any == ['toc']
    assert adapter._build_filter(SearchFilter(exclude_content_types=())) is None


def test_filter_excludes_hidden_document_ids_next_to_toc(qdrant_config):
    from document_chunk.domain.entities.search import SearchFilter
    adapter = QdrantAdapter(qdrant_config)
    built = adapter._build_filter(SearchFilter(exclude_document_ids=('hidden-1', 'hidden-2')))
    assert built.must_not[0].match.any == ['toc']
    assert built.must_not[1].key == 'document_id'
    assert built.must_not[1].match.any == ['hidden-1', 'hidden-2']
    only_hidden = adapter._build_filter(SearchFilter(exclude_content_types=(), exclude_document_ids=('hidden-1',)))
    assert [c.match.any for c in only_hidden.must_not] == [['hidden-1']]


def test_search_drops_hidden_documents_but_still_fills_top_k(sample_chunk, qdrant_config):
    import uuid
    from dataclasses import replace
    from qdrant_client import QdrantClient
    from qdrant_client.http import models
    from document_chunk.domain.entities.embedding import Embedding
    from document_chunk.domain.entities.search import SearchFilter
    adapter = QdrantAdapter(qdrant_config)
    client = QdrantClient(':memory:')
    client.create_collection(qdrant_config.collection_name, vectors_config=models.VectorParams(size=3, distance=models.Distance.COSINE))
    adapter.__dict__['_client'] = client
    embed = lambda c, v: Embedding(c.id, v, 'test', 3)
    # Tài liệu bị ẩn gần câu hỏi nhất; hai tài liệu còn lại xa hơn.
    hidden = replace(sample_chunk, id=str(uuid.uuid4()), metadata=replace(sample_chunk.metadata, document_id='hidden'))
    shown = [
        replace(sample_chunk, id=str(uuid.uuid4()), metadata=replace(sample_chunk.metadata, document_id=f'shown-{i}'))
        for i in range(2)
    ]
    vectors = [(1., 0., 0.), (0.9, 0.1, 0.), (0.8, 0.2, 0.)]
    chunks = [hidden, *shown]
    assert adapter.upsert(chunks, [embed(c, v) for c, v in zip(chunks, vectors)]).is_ok()

    result = adapter.search([1., 0., 0.], top_k=2, filters=SearchFilter(exclude_document_ids=('hidden',)))

    assert [r.chunk.metadata.document_id for r in result.unwrap()] == ['shown-0', 'shown-1']
    client.close()


def test_snapshot_cleanup_keeps_new_ids_and_other_documents(sample_chunk, qdrant_config):
    import uuid
    from dataclasses import replace
    from qdrant_client import QdrantClient
    from qdrant_client.http import models
    from document_chunk.domain.entities.embedding import Embedding
    adapter = QdrantAdapter(qdrant_config)
    client = QdrantClient(':memory:')
    client.create_collection(qdrant_config.collection_name, vectors_config=models.VectorParams(size=3, distance=models.Distance.COSINE))
    adapter.__dict__['_client'] = client
    old = replace(sample_chunk, id=str(uuid.uuid4()))
    new = replace(sample_chunk, id=str(uuid.uuid4()))
    other = replace(sample_chunk, id=str(uuid.uuid4()), metadata=replace(sample_chunk.metadata, document_id='other'))
    embed = lambda c: Embedding(c.id, (1., 0., 0.), 'test', 3)
    assert adapter.upsert([old, other], [embed(old), embed(other)]).is_ok()
    assert adapter.upsert([new], [embed(new)]).is_ok()
    assert adapter.delete_stale(new.document_id, [new.id]).is_ok()
    assert {p.id for p in client.scroll(qdrant_config.collection_name)[0]} == {new.id, other.id}
    assert adapter.delete_stale(new.document_id, []).is_ok()
    assert {p.id for p in client.scroll(qdrant_config.collection_name)[0]} == {other.id}
    client.close()
