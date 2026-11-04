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


def test_list_page_chunks_resolves_and_filters_by_course_when_given():
    cursor = _Cursor(
        [
            [("00000000-0000-0000-0000-000000000099",)],
            [("c1", "d1", 0, ["Ch 1"], 3, "nội dung trang 3")],
        ]
    )
    chunks = _Repo(cursor).list_page_chunks("d1", 3, course_id="CO3137").unwrap()
    assert len(chunks) == 1
    assert chunks[0].chunk_id == "c1"
    assert any("d.course_id = %s::uuid" in s for s in cursor.sql)
    assert cursor.params[-1][2] == "00000000-0000-0000-0000-000000000099"


# ---- tài liệu bị ẩn ở LMS (lms_published = false) không được lọt vào công cụ chatbot ----

_NOT_HIDDEN = "d.lms_published IS NOT FALSE"


def test_list_course_documents_skips_unpublished():
    cursor = _Cursor([[("course-uuid",)], []])
    _Repo(cursor).list_course_documents("CO3137").unwrap()
    assert _NOT_HIDDEN in cursor.sql[-1]


def test_list_page_chunks_skips_unpublished():
    cursor = _Cursor([[("course-uuid",)], []])
    _Repo(cursor).list_page_chunks("d1", 3, course_id="CO3137").unwrap()
    assert _NOT_HIDDEN in cursor.sql[-1]


def test_neighbor_anchor_and_window_queries_both_skip_unpublished():
    cursor = _Cursor([[("d1", 4)], [("c3", "d1", 3, ["Ch 1"], 2, "x")]])
    chunks = _Repo(cursor).get_neighbor_chunks("c4", 1).unwrap()
    assert len(chunks) == 1
    assert len(cursor.sql) == 2
    assert all(_NOT_HIDDEN in sql for sql in cursor.sql)
    assert "JOIN documents d" in cursor.sql[1]


def test_neighbors_of_hidden_anchor_return_nothing_without_second_query():
    cursor = _Cursor([[]])  # anchor bị loại vì tài liệu bị ẩn
    assert _Repo(cursor).get_neighbor_chunks("c4", 1).unwrap() == []
    assert len(cursor.sql) == 1


def test_relation_seeds_and_edges_skip_unpublished():
    cursor = _Cursor(
        [
            [("course-uuid",)],
            [("deadlock", 3)],
            [_edge("r1", "A", "cause", "Deadlock", "a", "deadlock")],
        ]
    )
    _Repo(cursor).find_relations("CO3137", "deadlock", 1, 10).unwrap()
    seed_sql, edge_sql = cursor.sql[1], cursor.sql[2]
    assert seed_sql.count(_NOT_HIDDEN) == 2  # cả hai nhánh UNION
    assert _NOT_HIDDEN in edge_sql


def test_worker_paths_still_see_unpublished_documents():
    cursor = _Cursor([[]])
    _Repo(cursor).list_document_chunks("d1").unwrap()
    assert "lms_published" not in cursor.sql[0]


def test_list_unpublished_document_ids_scopes_to_resolved_course():
    cursor = _Cursor([[("course-uuid",)], [("doc-a",), ("doc-b",)]])
    ids = _Repo(cursor).list_unpublished_document_ids("CO3137").unwrap()
    assert ids == ["doc-a", "doc-b"]
    assert "d.lms_published IS FALSE" in cursor.sql[-1]
    assert cursor.params[-1] == ("course-uuid", "course-uuid")


def test_list_unpublished_document_ids_without_course_covers_every_course():
    cursor = _Cursor([[("doc-a",)]])
    ids = _Repo(cursor).list_unpublished_document_ids().unwrap()
    assert ids == ["doc-a"]
    assert len(cursor.sql) == 1  # không tra khóa học
    assert cursor.params[-1] == (None, None)
