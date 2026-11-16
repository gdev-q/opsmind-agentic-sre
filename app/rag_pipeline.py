"""Header-aware Markdown chunking, embedding, and hybrid search (Elasticsearch, FAISS fallback)."""

import logging
import math
import re
import threading
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

from app.config import Settings, build_embeddings, get_settings
from app.schemas import IngestResponse, RunbookChunk

try:  # ES is optional at import time; the store degrades to FAISS without it.
    from elasticsearch import Elasticsearch, helpers
except ImportError:  # pragma: no cover
    Elasticsearch = None  # type: ignore[assignment,misc]
    helpers = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)
_HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*$")


# ----------------------------------------------------------------- chunking
def normalize_service(name: str) -> str:
    return re.sub(r"[\s\-_]", "", name).lower()


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    m = _FRONT_MATTER.match(text)
    if not m:
        return {}, text
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip().lower()] = v.strip()
    return meta, text[m.end():]


def _split_blocks(lines: list[str]) -> list[str]:
    """Split on blank lines, never inside fenced code blocks."""
    blocks: list[str] = []
    cur: list[str] = []
    fence = False
    for ln in lines:
        if ln.strip().startswith("```"):
            fence = not fence
        if not ln.strip() and not fence:
            if cur:
                blocks.append("\n".join(cur))
                cur = []
        else:
            cur.append(ln)
    if cur:
        blocks.append("\n".join(cur))
    return blocks


def _pack(blocks: list[str], max_chars: int) -> list[str]:
    out: list[str] = []
    cur = ""
    for b in blocks:
        if cur and len(cur) + len(b) + 2 > max_chars:
            out.append(cur)
            cur = b
        else:
            cur = f"{cur}\n\n{b}" if cur else b
    if cur:
        out.append(cur)
    return out


def chunk_markdown(text: str, source: str, max_chars: int = 1500) -> list[RunbookChunk]:
    """Split by #/##/### headings (ignoring '#' inside code fences); oversized sections
    are split on block boundaries. Each chunk is prefixed with its heading path."""
    meta, body = parse_front_matter(text)
    services = [normalize_service(s) for s in meta.get("services", "").split(",") if s.strip()]
    services = services or ["general"]

    sections: list[tuple[str, list[str]]] = []
    path = meta.get("title") or source
    buf: list[str] = []
    stack: list[tuple[int, str]] = []
    fence = False
    for line in body.splitlines():
        if line.strip().startswith("```"):
            fence = not fence
        m = None if fence else _HEADING.match(line)
        if m:
            sections.append((path, buf))
            buf = []
            level = len(m.group(1))
            stack = [(lv, t) for lv, t in stack if lv < level] + [(level, m.group(2))]
            path = " > ".join(t for _, t in stack)
        else:
            buf.append(line)
    sections.append((path, buf))

    chunks: list[RunbookChunk] = []
    for heading, lines in sections:
        if not "".join(lines).strip():
            continue
        for piece in _pack(_split_blocks(lines), max_chars):
            chunks.append(
                RunbookChunk(
                    source=source,
                    heading=heading,
                    services=services,
                    content=f"{heading}\n\n{piece}",
                )
            )
    return chunks


def load_runbook_chunks(directory: Path, max_chars: int) -> tuple[list[RunbookChunk], int]:
    files = sorted(directory.glob("*.md"))
    if not files:
        raise FileNotFoundError(f"No .md runbooks found in {directory}")
    chunks: list[RunbookChunk] = []
    for f in files:
        chunks.extend(chunk_markdown(f.read_text(encoding="utf-8"), f.name, max_chars))
    return chunks, len(files)


# ------------------------------------------------------- in-memory fallback
def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _bm25(query: list[str], docs: list[list[str]], k1: float = 1.5, b: float = 0.75) -> np.ndarray:
    n = len(docs)
    avgdl = (sum(len(d) for d in docs) / n) or 1.0
    df = Counter(t for d in docs for t in set(d))
    scores = np.zeros(n)
    for i, d in enumerate(docs):
        tf = Counter(d)
        for t in set(query):
            if t in tf:
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                scores[i] += idf * tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * len(d) / avgdl))
    return scores


def _minmax(a: np.ndarray) -> np.ndarray:
    rng = a.max() - a.min()
    return (a - a.min()) / rng if rng > 0 else np.zeros_like(a)


class _MemoryIndex:
    """FAISS cosine kNN fused with BM25 and a service-metadata boost."""

    def __init__(self) -> None:
        self.chunks: list[RunbookChunk] = []
        self._index: Any = None
        self._tokens: list[list[str]] = []

    def __len__(self) -> int:
        return len(self.chunks)

    def build(self, chunks: list[RunbookChunk], vectors: list[list[float]]) -> None:
        import faiss

        mat = np.asarray(vectors, dtype="float32")
        faiss.normalize_L2(mat)
        index = faiss.IndexFlatIP(mat.shape[1])
        index.add(mat)
        self.chunks, self._index = chunks, index
        self._tokens = [_tokenize(f"{c.heading} {c.content}") for c in chunks]

    def search(
        self, query: str, qvec: list[float], service: str | None, k: int
    ) -> list[RunbookChunk]:
        import faiss

        n = len(self.chunks)
        q = np.asarray([qvec], dtype="float32")
        faiss.normalize_L2(q)
        sims, ids = self._index.search(q, n)
        vec = np.zeros(n)
        vec[ids[0]] = sims[0]
        fused = 0.5 * _minmax(vec) + 0.5 * _minmax(_bm25(_tokenize(query), self._tokens))
        if service:
            fused += np.array([0.25 if service in c.services else 0.0 for c in self.chunks])
        order = np.argsort(-fused)[:k]
        return [self.chunks[i].model_copy(update={"score": round(float(fused[i]), 4)}) for i in order]


# -------------------------------------------------------------------- store
class RunbookStore:
    def __init__(
        self,
        settings: Settings | None = None,
        embeddings: Any = None,
        es_client: Any = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._embeddings = embeddings
        self._es = es_client
        self._memory = _MemoryIndex()
        self._es_ready = False
        self._lock = threading.Lock()

    # -- helpers
    def _emb(self) -> Any:
        if self._embeddings is None:
            self._embeddings = build_embeddings(self.settings)
        return self._embeddings

    def _client(self) -> Any:
        if self._es is None and Elasticsearch is not None:
            self._es = Elasticsearch(self.settings.elasticsearch_url, request_timeout=10)
        return self._es

    def es_available(self) -> bool:
        try:
            client = self._client()
            return bool(client and client.ping())
        except Exception:  # noqa: BLE001 - any connection problem means "offline"
            return False

    @property
    def backend(self) -> str:
        if self._es_ready:
            return "elasticsearch"
        return "faiss" if len(self._memory) else "none"

    @property
    def chunk_count(self) -> int:
        return len(self._memory)

    # -- ingest
    def ingest(self) -> IngestResponse:
        with self._lock:
            chunks, n_files = load_runbook_chunks(
                Path(self.settings.runbooks_dir), self.settings.chunk_max_chars
            )
            vectors = self._emb().embed_documents([c.content for c in chunks])
            self._memory.build(chunks, vectors)  # always keep the fallback warm
            self._es_ready = False
            es_up = self.es_available()
            if es_up:
                try:
                    self._index_es(chunks, vectors)
                    self._es_ready = True
                except Exception:  # noqa: BLE001
                    logger.exception("Elasticsearch indexing failed; using FAISS fallback")
            else:
                logger.warning("Elasticsearch offline; using in-memory FAISS fallback")
            return IngestResponse(
                files=n_files, chunks=len(chunks), backend=self.backend, es_available=es_up
            )

    def _index_es(self, chunks: list[RunbookChunk], vectors: list[list[float]]) -> None:
        es, idx = self._client(), self.settings.es_index
        if es.indices.exists(index=idx):
            es.indices.delete(index=idx)
        es.indices.create(
            index=idx,
            mappings={
                "properties": {
                    "source": {"type": "keyword"},
                    "heading": {"type": "text"},
                    "services": {"type": "keyword"},
                    "content": {"type": "text"},
                    "embedding": {
                        "type": "dense_vector",
                        "dims": len(vectors[0]),
                        "index": True,
                        "similarity": "cosine",
                    },
                }
            },
        )
        actions = [
            {
                "_index": idx,
                "_source": {**c.model_dump(exclude={"score"}), "embedding": [float(x) for x in v]},
            }
            for c, v in zip(chunks, vectors)
        ]
        helpers.bulk(es, actions)
        es.indices.refresh(index=idx)

    # -- search
    def search(
        self, query: str, service: str | None = None, k: int | None = None
    ) -> tuple[list[RunbookChunk], str]:
        """Return (top-k chunks, backend used). Lazily ingests on first use."""
        k = k or self.settings.top_k
        svc = normalize_service(service) if service else None
        if svc == "other":
            svc = None
        if not len(self._memory):
            self.ingest()
        qvec = self._emb().embed_query(query)
        if self._es_ready:
            try:
                return self._search_es(query, qvec, svc, k), "elasticsearch"
            except Exception:  # noqa: BLE001
                logger.exception("Elasticsearch search failed; falling back to FAISS")
                self._es_ready = False
        return self._memory.search(query, qvec, svc, k), "faiss"

    def _search_es(
        self, query: str, qvec: list[float], service: str | None, k: int
    ) -> list[RunbookChunk]:
        s = self.settings
        should: list[dict[str, Any]] = [
            {
                "multi_match": {
                    "query": query,
                    "fields": ["content", "heading^2"],
                    "boost": s.es_bm25_boost,
                }
            }
        ]
        if service:
            should.append(
                {
                    "constant_score": {
                        "filter": {"term": {"services": service}},
                        "boost": s.es_service_boost,
                    }
                }
            )
        resp = self._client().search(
            index=s.es_index,
            size=k,
            query={"bool": {"should": should, "minimum_should_match": 1}},
            knn={
                "field": "embedding",
                "query_vector": qvec,
                "k": k,
                "num_candidates": max(50, k * 10),
                "boost": s.es_knn_boost,
            },
            source_excludes=["embedding"],
        )
        return [
            RunbookChunk(**h["_source"], score=round(float(h["_score"]), 4))
            for h in resp["hits"]["hits"]
        ]


@lru_cache
def get_store() -> RunbookStore:
    return RunbookStore()
