# gdrive-rag-assistant

> Real-time RAG over Google Drive + GCS with Pub/Sub-triggered ingestion, Vertex AI embeddings, and Gemini Flash citations on GCP.

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![GCP](https://img.shields.io/badge/GCP-Vertex%20AI%20%7C%20BigQuery%20%7C%20Pub%2FSub-orange)](https://cloud.google.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## Overview

`gdrive-rag-assistant` is a production-grade Retrieval-Augmented Generation (RAG) system that keeps a continuously-fresh knowledge base from your organisation's Google Drive and GCS documents — Docs, Sheets, PDFs, Slides — and answers natural-language questions with **cited responses** pointing back to the exact source file and section.

Key capabilities:
- **Continuous ingestion**: Google Drive Change API pushes change tokens to Pub/Sub; a Dataflow streaming job picks them up, chunks and embeds the document delta, and writes to BigQuery.
- **Vector search**: BigQuery Vector Search (VECTOR_INDEX) for sub-second ANN retrieval.
- **Citation-first generation**: Gemini 2.0 Flash answers with inline `[source: <doc_id>#<section>]` citations.
- **Token governance**: Hard token budget (default 6 000 input / 1 024 output) enforced in `generator.py`.

---

## Architecture

```
Google Drive / GCS
       │  (Drive Change API / GCS Object Notifications)
       ▼
  Cloud Pub/Sub  ──────────────────────────────────────┐
       │                                               │
       ▼                                         (dead-letter)
  Dataflow Streaming Job                               │
  ┌────────────────────────┐                     Pub/Sub DLQ
  │ DriveReader transform  │
  │ Chunker transform      │
  │ Embedder transform     │  ← text-embedding-004 (batched)
  │ BigQueryWriter sink    │
  └────────────────────────┘
       │
       ▼
  BigQuery  (chunks + embeddings + VECTOR_INDEX)
       │
       ▼
  Cloud Run  ─── FastAPI /ask ──► Gemini 2.0 Flash
       │                                │
  Streamlit UI ◄──── cited answer ──────┘
```

---

## Data Sources & Ingestion

| Source | Mechanism | Frequency |
|---|---|---|
| Google Drive (Docs, Sheets, Slides) | Drive Change API → Pub/Sub push | Real-time / delta |
| GCS bucket (PDFs, markdown) | GCS Object Notification → Pub/Sub | Real-time |
| Initial full corpus | `indexer/build_corpus.py` batch job | One-time / scheduled refresh |

Dataflow pipeline: `ingestion/pipeline_main.py`  
Beam transforms: `DriveReader → Chunker → Embedder → BigQueryWriter`  
Chunk size: 512 tokens with 64-token overlap (configurable via pipeline options).

---

## RAG / Search Layer

- **Embedding model**: `text-embedding-004` (768 dims) via Vertex AI.
- **Vector store**: BigQuery table `chunks` with a `VECTOR_INDEX` (IVF, 256 centroids).
- **Retrieval**: `api/retriever.py` — top-k=8 ANN search, then cross-encoder re-rank to top-3.
- **Context assembly**: Retrieved chunks concatenated with `[source]` tags before Gemini prompt.

---

## LLM Usage

| Parameter | Value |
|---|---|
| Model | `gemini-2.0-flash-001` |
| Max input tokens | 6 000 (enforced in `generator.py`) |
| Max output tokens | 1 024 |
| Temperature | 0.2 (factual retrieval mode) |
| Grounding | Retrieved chunks injected as context |
| Citation format | `[source: <file_id>#<chunk_id>]` inline |

Token budget is enforced before the API call by `_trim_context()` in `generator.py`. If retrieved context exceeds the budget, chunks are dropped from lowest-score to highest.

---

## Deployment

### Local Dev (devcontainer)
```bash
# 1. Open in VS Code Dev Container
code .
# 2. Authenticate
gcloud auth application-default login
# 3. Start all services
docker-compose up
# API: http://localhost:8080
# UI:  http://localhost:8501
```

### GCP Deployment
```bash
# 1. Provision infra
cd infra && terraform init && terraform apply -var-file=environments/dev.tfvars
# 2. Build initial corpus
python indexer/build_corpus.py --project=$PROJECT --bucket=$BUCKET
# 3. Deploy Dataflow streaming job
python ingestion/pipeline_main.py --runner=DataflowRunner ...
# 4. Deploy API to Cloud Run
gcloud run deploy gdrive-rag-api --source api/ --region=us-central1
```

---

## Repo Structure

```
gdrive-rag-assistant/
├── infra/                  # Terraform: Pub/Sub, BigQuery, Cloud Run, IAM
├── ingestion/              # Dataflow Beam pipeline
│   ├── pipeline_main.py
│   └── transforms/
├── indexer/                # Corpus build & incremental sync
│   ├── build_corpus.py
│   └── incremental_sync.py
├── api/                    # FastAPI /ask service (Cloud Run)
│   ├── main.py
│   ├── retriever.py
│   └── generator.py
├── ui/                     # Streamlit chat UI
├── tests/                  # unit + integration
├── docs/                   # architecture & runbooks
└── notebooks/              # exploration notebooks
```

---

## Roadmap

1. **Multi-turn conversation memory** — store session history in Firestore; inject last-N turns into prompt.
2. **Access-control passthrough** — inherit Drive sharing permissions so users only retrieve docs they can view.
3. **Vertex AI RAG Engine migration path** — `docs/migrate-to-vertex-rag-engine.md` A/B comparison once corpus exceeds 1M chunks.

---

## Contributing

PRs welcome. Please run `make lint test` before opening a PR.
