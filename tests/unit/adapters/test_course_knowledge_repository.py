"""Duyệt đồ thị quan hệ của PostgresCourseKnowledgeRepository với con trỏ giả có kịch bản."""
from contextlib import contextmanager

from document_chunk.adapters.metadata.postgres_course_knowledge_repository import (
    PostgresCourseKnowledgeRepository,
)
from document_chunk.domain.ports.course_knowledge_store import NewRelation


class _Cursor:
    """Trả từng lô hàng đã định trước, mỗi ``execute`` một lô; ghi lại câu SQL."""

    def __init__(self, batches):
        self._batches = list(batches)
        self._current = []
        self.sql: list[str] = []
        self.params: list[tuple] = []
        self.rowcount = 1

    def execute(self, sql, params=None):
        self.sql.append(sql)
        self.params.append(params)
        self._current = self._batches.pop(0) if self._batches else []

    def fetchall(self):
        return self._current

    def fetchone(self):
        return self._current[0] if self._current else None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True


class _Repo(PostgresCourseKnowledgeRepository):
    def __init__(self, cursor):
        self.cursor = cursor
        self.conn = _Conn(cursor)

    @contextmanager
    def _connection(self):
        yield self.conn


def _edge(rid, source, rel, target, skey, tkey, page=4):
    return (rid, source, rel, target, f"quote {rid}", f"chunk-{rid}", "doc-1", page, skey, tkey)


def test_no_matching_node_returns_empty():
    cursor = _Cursor([[("course-uuid",)], []])  # course lookup, then no seed nodes
    result = _Repo(cursor).find_relations("CO3137", "unknown", 2, 10)
    assert result.unwrap() == []


def test_two_hops_reach_the_second_neighbour_without_repeats():
    cursor = _Cursor(
        [
            [("course-uuid",)],                    # _resolve_course
            [("deadlock", 3)],                     # seeds
            [                                      # hop 1: edges touching deadlock
                _edge("r1", "Hold and wait", "cause", "Deadlock", "hold and wait", "deadlock"),
                _edge("r2", "Deadlock", "contrast", "Starvation", "deadlock", "starvation"),
            ],
            [                                      # hop 2: edges touching hold and wait / starvation
                _edge("r1", "Hold and wait", "cause", "Deadlock", "hold and wait", "deadlock"),  # đã thấy
                _edge("r3", "Starvation", "related", "Livelock", "starvation", "livelock"),
            ],
        ]
    )
    result = _Repo(cursor).find_relations("CO3137", "deadlock", 2, 10).unwrap()
    assert [r.relation_id for r in result] == ["r1", "r2", "r3"]
    assert [r.hop for r in result] == [1, 1, 2]
    assert result[0].page_number == 4


def test_limit_stops_the_walk_early():
    cursor = _Cursor(
        [
            [("course-uuid",)],
            [("deadlock", 3)],
            [
                _edge("r1", "A", "cause", "Deadlock", "a", "deadlock"),
                _edge("r2", "Deadlock", "cause", "B", "deadlock", "b"),
            ],
        ]
    )
    result = _Repo(cursor).find_relations("CO3137", "deadlock", 2, 1).unwrap()
    assert [r.relation_id for r in result] == ["r1"]


def test_replace_relations_deletes_then_inserts_and_commits():
    cursor = _Cursor([[("course-uuid",)], [], [], []])
    repo = _Repo(cursor)
    relation = NewRelation(
        chunk_id="c1", document_id="d1", source_label="A", source_key="a",
        relation_type="cause", target_label="B", target_key="b",
        evidence_quote="a gây ra b", page_number=2,
    )
    written = repo.replace_relations(
        course_id="CO3137", chunk_ids=["c1"], extraction_version="v1", model="m", relations=[relation]
    ).unwrap()
    assert written == 1
    assert any("DELETE FROM knowledge_relations" in s for s in cursor.sql)
    assert any("INSERT INTO knowledge_relations" in s for s in cursor.sql)
    assert repo.conn.committed


def test_replace_relations_with_no_chunks_is_a_noop():
    cursor = _Cursor([])
    assert _Repo(cursor).replace_relations(
        course_id="x", chunk_ids=[], extraction_version="v1", model=None, relations=[]
    ).unwrap() == 0
    assert cursor.sql == []
