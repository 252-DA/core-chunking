"""Score saved retrieval runs without contacting a database or loading a model.

JSONL rows: {"expected_path": ["Chapter", "Section"], "document_id": "doc",
            "heading": [{"document_id": "doc", "heading_path": [...]}],
            "structural": [{"document_id": "doc", "heading_path": [...]}]}
Hits are in retrieval order. Use the same questions and embedder for both runs.
"""

import argparse
import json
from pathlib import Path


def score(rows, strategy):
    recalls, reciprocal = [], []
    for row in rows:
        expected = tuple(row["expected_path"])
        if not expected:
            raise ValueError("Labels must identify a non-root section")
        ranks = []
        for rank, hit in enumerate(row[strategy][:10], 1):
            path = tuple(hit["heading_path"])
            if hit["document_id"] != row["document_id"] or not path:
                continue
            if path[: len(expected)] == expected or expected[: len(path)] == path:
                ranks.append(rank)
        recalls.append(int(bool(ranks) and ranks[0] <= 5))
        reciprocal.append(1 / ranks[0] if ranks else 0)
    return {"recall_at_5": sum(recalls) / len(rows), "mrr_at_10": sum(reciprocal) / len(rows)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", type=Path)
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.runs.read_text().splitlines() if line.strip()]
    if not rows:
        parser.error("No labelled retrieval rows")
    print(
        json.dumps(
            {strategy: score(rows, strategy) for strategy in ("heading", "structural")}, indent=2
        )
    )


if __name__ == "__main__":
    main()
