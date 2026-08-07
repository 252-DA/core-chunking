from document_chunk.adapters.vector_db.qdrant_adapter import QdrantAdapter


def test_payload_round_trip_preserves_embedding_input(sample_chunk, qdrant_config):
    adapter = QdrantAdapter(qdrant_config)

    payload = adapter._chunk_to_payload(sample_chunk)
    restored = adapter._payload_to_chunk(payload)

    assert payload["embedding_input"] == sample_chunk.embedding_input
    assert restored.embedding_input == sample_chunk.embedding_input
