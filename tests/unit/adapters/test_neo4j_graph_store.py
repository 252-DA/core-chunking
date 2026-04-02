from unittest.mock import MagicMock

from src.adapters.graph.neo4j_graph_store import Neo4jGraphStore
from src.infrastructure.config import Neo4jConfig


class TestNeo4jGraphStore:
    def test_delete_document_removes_nested_headings(self):
        session = MagicMock()
        driver = MagicMock()
        driver.session.return_value.__enter__.return_value = session

        store = Neo4jGraphStore(
            Neo4jConfig(
                enabled=True,
                uri="bolt://localhost:7687",
                username="neo4j",
                password="neo4j",
                database="neo4j",
            )
        )
        store.__dict__["_driver"] = driver

        result = store.delete_document("doc-001")

        assert result.is_ok()
        driver.session.assert_called_once_with(database="neo4j")
        query = session.run.call_args.args[0]
        assert "[:HAS_SUBHEADING*0..]" in query
        assert "collect(DISTINCT sub) AS heading_nodes" in query
        assert "FOREACH (heading IN heading_nodes | DETACH DELETE heading)" in query
        assert session.run.call_args.kwargs["document_id"] == "doc-001"
