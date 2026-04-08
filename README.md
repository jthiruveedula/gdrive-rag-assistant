# 🗂️ GDrive RAG Assistant

> A citation-first research copilot for Google Drive that turns scattered documents into grounded, source-linked answers — continuously fresh, enterprise-ready.

[![Build](https://img.shields.io/badge/build-passing-brightgreen)](https://github.com/jthiruveedula/gdrive-rag-assistant/actions)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![GCP](https://img.shields.io/badge/GCP-Vertex%20AI%20%7C%20BigQuery%20%7C%20Pub%2FSub-4285F4)](https://cloud.google.com/)
[![Google Drive](https://img.shields.io/badge/source-Google%20Drive%20%2B%20GCS-0F9D58)](https://drive.google.com/)
[![RAG](https://img.shields.io/badge/RAG-citation--first-8E75FF)](https://github.com/jthiruveedula/gdrive-rag-assistant)
[![Latency](https://img.shields.io/badge/p95%20retrieval-%3C50ms-yellow)](https://github.com/jthiruveedula/gdrive-rag-assistant)
[![Token Budget](https://img.shields.io/badge/token%20budget-6K%20in%20%7C%201K%20out-orange)](https://github.com/jthiruveedula/gdrive-rag-assistant)
[![License](https://img.shields.io/badge/license-MIT-lightgrey)](LICENSE)

---

## ✨ Why This Exists

Your organisation's knowledge lives in Google Drive — Docs, Sheets, Slides, PDFs — but it's invisible to search and unreachable to AI.  
`gdrive-rag-assistant` changes that: every document is continuously ingested, chunked, embedded, and indexed so Gemini can answer any question with **precise inline citations** pointing back to the exact source file and section.

---

## 🏗️ Architecture

```
Google Drive / GCS
       │  (Drive Change API / GCS Object Notifications)
       ▼
  Cloud Pub/Sub  ─────────────────────────┐
       │                             (dead-letter DLQ)
       ▼
  Dataflow Streaming Job
  ┌────────────────────────────────────┐
  │ DriveReader → Chunker → Embedder  │  ← text-embedding-004
  │           → BigQueryWriter        │
  └────────────────────────────────────┘
       │
       ▼
  BigQuery  (chunks + VECTOR_INDEX, IVF 256 centroids)
       │
       ▼
  Cloud Run  ── FastAPI /ask ──► Gemini 2.0 Flash
       │                                │
  Streamlit UI ◄──── cited answer ───────┘
```

---

## ⚡ Key Capabilities

| Feature | Detail |
|---|---|
| **Continuous ingestion** | Drive Change API → Pub/Sub → Dataflow (real-time delta) |
| **Vector search** | BigQuery `VECTOR_SEARCH` (IVF, 256 centroids, <50ms p95) |
| **Citation-first generation** | `[source: <file_id>#<chunk_id>]` inline in every answer |
| **Token governance** | Hard budget: 6 000 input / 1 024 output, enforced in `generator.py` |
| **Re-ranking** | Cross-encoder rerank top-8 → top-3 before generation |
| **Access control** | Drive permission passthrough (planned — see Roadmap) |

---

## 📁 Repo Structure

```
gdrive-rag-assistant/
├── api/                    # FastAPI /ask service (Cloud Run)
│   ├── main.py
│   ├── retriever.py
│   ├── generator.py
│   └── authz.py            # 🆕 Access-aware retrieval filter
├── ingestion/              # Dataflow Beam pipeline
│   ├── pipeline_main.py
│   └── transforms/
├── indexer/                # Corpus build & incremental sync
│   ├── build_corpus.py
│   └── incremental_sync.py
├── observability/          # 🆕 Corpus health & metrics
│   ├── index_health.py
│   └── metrics.py
├── ui/                     # Streamlit chat UI
├── tests/
├── docs/
└── notebooks/
```

---

## 🚀 Quickstart

```bash
# 1. Authenticate
gcloud auth application-default login

# 2. Start locally (Dev Container)
docker-compose up
# API → http://localhost:8080
# UI  → http://localhost:8501

# 3. GCP deploy
cd infra && terraform init && terraform apply -var-file=environments/dev.tfvars
python indexer/build_corpus.py --project=$PROJECT --bucket=$BUCKET
python ingestion/pipeline_main.py --runner=DataflowRunner
gcloud run deploy gdrive-rag-api --source api/ --region=us-central1
```

---

## 💬 Example Questions

```
"Summarise the Q4 2025 board deck"
"What is our data retention policy for customer PII?"
"Compare the onboarding process in the old and new HR docs"
"Who owns the incident response runbook?"
```

---

## 📊 LLM Usage

| Parameter | Value |
|---|---|
| Model | `gemini-2.0-flash-001` |
| Embedding | `text-embedding-004` (768 dims) |
| Max input tokens | 6 000 |
| Max output tokens | 1 024 |
| Temperature | 0.2 |
| Citation format | `[source: <file_id>#<chunk_id>]` |

---

## 🔭 Observability

- **Corpus health**: `observability/index_health.py` — file count, sync lag, parse failures, avg freshness
- **Request metrics**: latency, token spend, retrieval hit rate per request
- **Ingestion telemetry**: chunk throughput, error rate by file type

---

## 🛣️ Roadmap

### Now / Next
- [ ] **Drive Sync Indexer** — incremental change-token-based sync with delete propagation
- [ ] **Citation Panel UI** — file owner, modified time, deep Drive link per source
- [ ] **Research Modes** — search / summarise / compare / synthesise routing
- [ ] **Corpus Health Dashboard** — admin endpoint + Streamlit page
- [ ] **Access-Aware Retrieval** — Drive ACL passthrough at query time

### Future / Wow
- [ ] **Auto-Briefing Agent** — multi-doc synthesis with timeline and contradiction detection
- [ ] **Expert Lens Personalization** — role-based answer style and depth
- [ ] **Source Conflict Detector** — surface contradictory claims across documents
- [ ] **Collaborative Research Threads** — shared workspaces with pinned sources
- [ ] **Evidence Graph** — entity + claim relationship map across the corpus

---

## 🤝 Contributing

PRs welcome. Run `make lint test` before opening a PR.

## 📄 License

MIT — see [LICENSE](LICENSE)
