"""LanceDB store for vault chunks.

The schema is fixed in `vault_chunks_schema()`. Use `open_store()` to get a
`VaultChunkStore` instance bound to the configured LanceDB path.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa

import lancedb
from rag.config import get_settings

EMBEDDING_DIM = 1024


def vault_chunks_schema(vector_dim: int = EMBEDDING_DIM) -> pa.Schema:
    """Pyarrow schema for the vault_chunks table.

    Keep this in sync with `chunk_to_row()` below.
    """
    return pa.schema(
        [
            pa.field("chunk_id", pa.string(), nullable=False),
            pa.field("file_path", pa.string(), nullable=False),
            pa.field("vault_relative", pa.string(), nullable=False),
            pa.field("title", pa.string(), nullable=False),
            pa.field("header_path", pa.string(), nullable=True),
            pa.field("doc_type", pa.string(), nullable=False),
            pa.field("top_folder", pa.string(), nullable=True),
            pa.field("company", pa.string(), nullable=True),
            pa.field("person", pa.string(), nullable=True),
            pa.field("date", pa.string(), nullable=True),  # ISO YYYY-MM-DD
            pa.field("event_stage", pa.string(), nullable=True),
            pa.field("investment_stage", pa.string(), nullable=True),
            pa.field("pipeline_stage", pa.string(), nullable=True),
            pa.field("status", pa.string(), nullable=True),
            pa.field("meeting_type", pa.string(), nullable=True),
            pa.field("tags", pa.list_(pa.string()), nullable=True),
            pa.field("people", pa.list_(pa.string()), nullable=True),
            pa.field("source", pa.string(), nullable=False),  # md / pdf
            pa.field("parent_path", pa.string(), nullable=True),
            pa.field("text", pa.string(), nullable=False),
            pa.field("frontmatter_json", pa.string(), nullable=True),
            pa.field("chunk_idx", pa.int32(), nullable=False),
            pa.field("vector", pa.list_(pa.float32(), vector_dim), nullable=False),
            pa.field("ingested_at", pa.string(), nullable=False),  # ISO timestamp
        ]
    )


def _to_iso_date(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    s = str(v).strip()
    return s or None


def chunk_to_row(
    chunk_meta: dict[str, Any],
    chunk_text: str,
    vector: np.ndarray | list[float],
    chunk_id: str,
    chunk_idx: int,
    header_path: str,
    raw_frontmatter: dict[str, Any] | None = None,
    source: str = "md",
    parent_path: str | None = None,
) -> dict[str, Any]:
    """Build a single LanceDB row from chunk text + metadata + vector."""
    if isinstance(vector, np.ndarray):
        vec_list = vector.astype(np.float32).tolist()
    else:
        vec_list = [float(x) for x in vector]

    fm_json = (
        json.dumps(_jsonable(raw_frontmatter), ensure_ascii=False)
        if raw_frontmatter
        else None
    )

    row: dict[str, Any] = {
        "chunk_id": chunk_id,
        "file_path": chunk_meta["file_path"],
        "vault_relative": chunk_meta["vault_relative"],
        "title": chunk_meta["title"],
        "header_path": header_path or None,
        "doc_type": chunk_meta["doc_type"],
        "top_folder": chunk_meta.get("top_folder"),
        "company": chunk_meta.get("company"),
        "person": chunk_meta.get("person"),
        "date": _to_iso_date(chunk_meta.get("date")),
        "event_stage": chunk_meta.get("event_stage"),
        "investment_stage": chunk_meta.get("investment_stage"),
        "pipeline_stage": chunk_meta.get("pipeline_stage"),
        "status": chunk_meta.get("status"),
        "meeting_type": chunk_meta.get("meeting_type"),
        "tags": list(chunk_meta.get("tags") or []),
        "people": list(chunk_meta.get("people") or []),
        "source": source,
        "parent_path": parent_path,
        "text": chunk_text,
        "frontmatter_json": fm_json,
        "chunk_idx": int(chunk_idx),
        "vector": vec_list,
        "ingested_at": datetime.utcnow().isoformat(timespec="seconds"),
    }
    return row


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(x) for x in obj]
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


# --- Store wrapper ---------------------------------------------------------
class VaultChunkStore:
    """Thin wrapper around a LanceDB table."""

    def __init__(self, db_path: Path, table_name: str = "vault_chunks") -> None:
        self.db_path = db_path
        self.table_name = table_name
        db_path.mkdir(parents=True, exist_ok=True)
        self._db = lancedb.connect(str(db_path))
        self._schema = vault_chunks_schema()
        self._table: Any = None

    # --- Lifecycle ------------------------------------------------------
    def open_or_create(self) -> Any:
        if self._table is not None:
            return self._table
        existing = self._db.table_names()
        if self.table_name in existing:
            self._table = self._db.open_table(self.table_name)
        else:
            self._table = self._db.create_table(self.table_name, schema=self._schema)
        return self._table

    @property
    def table(self) -> Any:
        return self.open_or_create()

    def count(self) -> int:
        return int(self.table.count_rows())

    # --- Writes --------------------------------------------------------
    def upsert_rows(self, rows: list[dict[str, Any]]) -> int:
        """Insert rows, replacing any with matching `chunk_id`.

        LanceDB's `merge_insert` upsert API is stable as of 0.13+. We use that
        when present and fall back to delete+insert otherwise.
        """
        if not rows:
            return 0
        table = self.open_or_create()
        try:
            (
                table.merge_insert("chunk_id")
                .when_matched_update_all()
                .when_not_matched_insert_all()
                .execute(rows)
            )
        except (AttributeError, NotImplementedError):
            ids = [r["chunk_id"] for r in rows]
            quoted = ",".join(f"'{i}'" for i in ids)
            table.delete(f"chunk_id IN ({quoted})")
            table.add(rows)
        return len(rows)

    def delete_by_file(self, file_path: str) -> int:
        """Delete all chunks belonging to a single source file."""
        table = self.open_or_create()
        # Escape single quotes for SQL.
        escaped = file_path.replace("'", "''")
        before = table.count_rows()
        table.delete(f"file_path = '{escaped}'")
        return before - table.count_rows()

    def all_file_paths(self) -> list[str]:
        df = self.table.to_pandas()
        if len(df) == 0 or "file_path" not in df.columns:
            return []
        return sorted(set(df["file_path"].tolist()))

    # --- Reads ---------------------------------------------------------
    def search_by_company(self, company: str, limit: int = 10) -> list[dict[str, Any]]:
        """Exact metadata match on the `company` field."""
        escaped = company.replace("'", "''")
        df = (
            self.table.search()
            .where(f"company = '{escaped}'")
            .limit(limit)
            .to_pandas()
        )
        return df.to_dict(orient="records")

    def search_by_filter(
        self,
        where: str,
        limit: int = 50,
        columns: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Run a metadata-only filter (no vector search)."""
        q = self.table.search().where(where).limit(limit)
        df = q.to_pandas()
        if columns:
            df = df[[c for c in columns if c in df.columns]]
        return df.to_dict(orient="records")

    def vector_search(
        self,
        vector: np.ndarray | list[float],
        limit: int = 30,
        where: str | None = None,
    ) -> list[dict[str, Any]]:
        """ANN search with optional metadata filter."""
        if isinstance(vector, np.ndarray):
            vec = vector.astype(np.float32).tolist()
        else:
            vec = [float(x) for x in vector]
        q = self.table.search(vec).limit(limit)
        if where:
            q = q.where(where, prefilter=True)
        df = q.to_pandas()
        return df.to_dict(orient="records")

    # --- Index management ----------------------------------------------
    def create_vector_index(self, num_partitions: int = 64) -> None:
        """Create an IVF_PQ index. Call after the first bulk ingest.

        With our small vault (< 10K chunks) flat search is plenty fast, so this
        is optional. We expose it for future-proofing.
        """
        try:
            self.table.create_index(
                vector_column_name="vector",
                num_partitions=num_partitions,
            )
        except Exception:
            # Older lancedb expects different kwargs; let the caller handle.
            raise


def open_store(table_name: str | None = None) -> VaultChunkStore:
    """Convenience: build a store from the loaded settings."""
    s = get_settings()
    name = table_name or s.storage.table_name
    return VaultChunkStore(db_path=s.storage.resolved_path, table_name=name)


def chunks_from_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pass-through, kept for symmetry with future Pydantic models."""
    return list(rows)
