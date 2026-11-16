"""Unit tests. LLM, embeddings and Elasticsearch are all faked: no network, no API key."""

import asyncio
import re
import sys
import zlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents import build_graph, run_triage  # noqa: E402
from app.config import ProviderError, Settings  # noqa: E402
from app.main import app, get_graph, get_store  # noqa: E402
from app.rag_pipeline import RunbookStore, chunk_markdown, normalize_service  # noqa: E402
from app.schemas import CommandStep, RemediationPlan, RunbookChunk, TriageResult  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = Settings(_env_file=None, gemini_api_key="test", runbooks_dir=str(ROOT / "runbooks"))


class FakeEmbeddings:
    """Hashed bag-of-words: deterministic and topic-sensitive."""

    def _vec(self, text: str) -> list[float]:
        v = np.zeros(256)
        for tok in re.findall(r"[a-z0-9]+", text.lower()):
            v[zlib.crc32(tok.encode()) % 256] += 1
        return v.tolist()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


class FakeLLM:
    def __init__(self, triage: TriageResult, plan: RemediationPlan) -> None:
        self._out = {TriageResult: triage, RemediationPlan: plan}
        self.schemas_called: list[type] = []

    def with_structured_output(self, schema: type) -> "FakeLLM":
        self.schemas_called.append(schema)
        self._current = schema
        return self

    def invoke(self, _messages: list) -> object:
        return self._out[self._current].model_copy(deep=True)


def offline_es() -> MagicMock:
    es = MagicMock()
    es.ping.return_value = False
    return es


def make_store(es: MagicMock | None = None) -> RunbookStore:
    return RunbookStore(SETTINGS, embeddings=FakeEmbeddings(), es_client=es or offline_es())


KAFKA_TRIAGE = TriageResult(
    affected_service="Kafka",
    error_type="Consumer rebalance loop",
    severity="P2",
    search_query="kafka consumer lag CommitFailedException max.poll.interval.ms rebalance",
)


def kafka_plan(citations: list[str], found: bool = True) -> RemediationPlan:
    return RemediationPlan(
        runbook_found=found,
        root_cause_hypothesis="Poll loop exceeded max.poll.interval.ms.",
        steps=[CommandStep(description="Describe group", command="kafka-consumer-groups.sh --describe")],
        escalation="Kafka platform team",
        rollback="kubectl rollout undo",
        citations=citations,
    )


# ------------------------------------------------------------------ chunking
def test_chunking_is_header_aware_and_ignores_code_comments():
    md = (
        "---\ntitle: T\nservices: Kafka, Spring Boot\n---\n"
        "# Top\n\nintro\n\n## Step 1\n\n```bash\n# not a heading\nls\n```\n\n### Sub\n\ntext\n"
    )
    chunks = chunk_markdown(md, "x.md")
    assert [c.heading for c in chunks] == ["Top", "Top > Step 1", "Top > Step 1 > Sub"]
    assert "# not a heading" in chunks[1].content
    assert chunks[0].services == ["kafka", "springboot"]


def test_oversized_section_splits_without_breaking_code_fence():
    para = "word " * 60
    md = f"# H\n\n{para}\n\n```\ncode line\n\nstill code\n```\n\n{para}\n"
    chunks = chunk_markdown(md, "x.md", max_chars=400)
    assert len(chunks) >= 2
    assert all(c.content.count("```") % 2 == 0 for c in chunks)
    assert all(c.services == ["general"] for c in chunks)


def test_normalize_service():
    assert normalize_service("Spring Boot") == "springboot"
    assert normalize_service("Elasticsearch") == "elasticsearch"


def test_real_runbooks_all_chunk():
    for f in (ROOT / "runbooks").glob("*.md"):
        assert chunk_markdown(f.read_text(encoding="utf-8"), f.name), f.name


# ------------------------------------------------------------------- storage
def test_faiss_fallback_when_es_offline_and_hybrid_ranking():
    store = make_store()
    info = store.ingest()
    assert info.backend == "faiss" and not info.es_available and info.files == 6
    cases = {
        ("redis maxmemory OOM command not allowed eviction", "Redis"): "redis-oom-eviction.md",
        ("kafka consumer lag rebalance", "Kafka"): "kafka-consumer-lag.md",
        ("HikariPool connection is not available timed out", "SpringBoot"): "springboot-hikari-pool-exhaustion.md",
    }
    for (query, svc), expected in cases.items():
        hits, backend = store.search(query, svc, 3)
        assert backend == "faiss" and len(hits) == 3
        assert hits[0].source == expected
        assert hits[0].score >= hits[1].score


def test_es_search_failure_falls_back_to_faiss():
    es = MagicMock()
    es.ping.return_value = True
    es.search.side_effect = RuntimeError("es down")
    store = make_store(es)
    with patch("app.rag_pipeline.helpers.bulk"):
        assert store.ingest().backend == "elasticsearch"
    hits, backend = store.search("redis maxmemory eviction", "Redis")
    assert backend == "faiss" and hits[0].source == "redis-oom-eviction.md"


def test_es_hybrid_query_shape_and_parsing():
    es = MagicMock()
    es.ping.return_value = True
    es.search.return_value = {
        "hits": {
            "hits": [
                {
                    "_score": 12.3456,
                    "_source": {
                        "source": "a.md",
                        "heading": "H",
                        "services": ["redis"],
                        "content": "c",
                    },
                }
            ]
        }
    }
    store = make_store(es)
    with patch("app.rag_pipeline.helpers.bulk"):
        store.ingest()
    hits, backend = store.search("q", "Redis", 3)
    kw = es.search.call_args.kwargs
    assert backend == "elasticsearch" and hits[0].score == 12.3456
    assert kw["knn"]["field"] == "embedding" and kw["size"] == 3
    assert any("constant_score" in c for c in kw["query"]["bool"]["should"])  # service boost
    assert any("multi_match" in c for c in kw["query"]["bool"]["should"])  # BM25


# -------------------------------------------------------------------- agents
def test_graph_end_to_end_with_grounded_citations():
    llm = FakeLLM(KAFKA_TRIAGE, kafka_plan(["kafka-consumer-lag.md", "made-up.md"]))
    graph = build_graph(llm=llm, store=make_store(), settings=SETTINGS)
    resp = asyncio.run(run_triage(graph, "CommitFailedException: group has already rebalanced ..."))
    assert resp.triage.affected_service == "Kafka"
    assert resp.retrieval_backend == "faiss" and len(resp.retrieved) == 3
    assert resp.plan.citations == ["kafka-consumer-lag.md"]  # hallucinated file stripped
    assert resp.plan.runbook_found
    assert [t.split("]")[0] for t in resp.trace] == [
        "[log_triage_agent",
        "[rag_retriever_node",
        "[remediation_planner_agent",
    ]


def test_planner_without_valid_citation_is_marked_not_found():
    llm = FakeLLM(KAFKA_TRIAGE, kafka_plan(["made-up.md"]))
    graph = build_graph(llm=llm, store=make_store(), settings=SETTINGS)
    out = graph.invoke({"raw_log": "some kafka log line here", "trace": []})
    assert out["plan"].runbook_found is False
    assert out["plan"].citations == []
    assert "No matching runbook" in out["plan"].root_cause_hypothesis


def test_no_runbook_short_circuits_without_calling_planner_llm():
    store = MagicMock()
    store.search.return_value = ([], "faiss")
    llm = FakeLLM(KAFKA_TRIAGE, kafka_plan(["x.md"]))
    graph = build_graph(llm=llm, store=store, settings=SETTINGS)
    out = graph.invoke({"raw_log": "totally unknown failure text", "trace": []})
    assert llm.schemas_called == [TriageResult]  # planner LLM never invoked
    assert out["plan"].runbook_found is False and out["plan"].steps == []
    assert "No matching runbook" in out["plan"].root_cause_hypothesis


# ----------------------------------------------------------------------- API
class FakeGraph:
    async def ainvoke(self, _state: dict) -> dict:
        chunk = RunbookChunk(source="kafka-consumer-lag.md", heading="H", services=["kafka"], content="c", score=0.9)
        return {
            "triage": KAFKA_TRIAGE,
            "retrieved": [chunk],
            "retrieval_backend": "faiss",
            "plan": kafka_plan(["kafka-consumer-lag.md"]),
            "trace": ["[log_triage_agent] ok"],
        }


@pytest.fixture
def client():
    app.dependency_overrides[get_graph] = lambda: FakeGraph()
    store = make_store()  # one instance so /health sees what /ingest indexed
    app.dependency_overrides[get_store] = lambda: store
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok" and r.json()["es_available"] is False


def test_ingest(client):
    r = client.post("/ingest")
    assert r.status_code == 200 and r.json()["chunks"] > 6


def test_triage_ok(client):
    r = client.post("/triage", json={"log": "CommitFailedException group rebalanced"})
    assert r.status_code == 200
    body = r.json()
    assert body["triage"]["severity"] == "P2" and body["plan"]["citations"] == ["kafka-consumer-lag.md"]


@pytest.mark.parametrize("payload", [{"log": "short"}, {"log": "   \n  "}, {}, {"log": "x" * 20001}])
def test_triage_validation_422(client, payload):
    assert client.post("/triage", json=payload).status_code == 422


def test_triage_provider_error_503_and_upstream_502(client):
    class Boom:
        def __init__(self, exc: Exception) -> None:
            self.exc = exc

        async def ainvoke(self, _s: dict) -> dict:
            raise self.exc

    app.dependency_overrides[get_graph] = lambda: Boom(ProviderError("GEMINI_API_KEY is not set"))
    r = client.post("/triage", json={"log": "some valid log text"})
    assert r.status_code == 503 and "GEMINI_API_KEY" in r.json()["detail"]
    app.dependency_overrides[get_graph] = lambda: Boom(RuntimeError("boom"))
    assert client.post("/triage", json={"log": "some valid log text"}).status_code == 502
