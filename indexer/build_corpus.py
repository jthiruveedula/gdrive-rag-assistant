"""indexer/build_corpus.py

Batch job: enumerate all files in Google Drive / GCS bucket,
chunk, embed, and write to BigQuery to bootstrap the RAG corpus.

Run once for initial load; subsequent updates are handled by the
streaming Dataflow pipeline (ingestion/pipeline_main.py).

Usage:
  python -m indexer.build_corpus \\
      --project=my-proj \\
      --drive_folder_id=1BxiMVs0XRA... \\
      --gcs_bucket=my-docs-bucket \\
      --bq_dataset=rag_corpus \\
      --bq_table=chunks \\
      --batch_size=50
"""
from __future__ import annotations

import argparse
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Generator

from google.cloud import bigquery, storage
from googleapiclient.discovery import build as gdrive_build
from google.oauth2 import service_account
import vertexai
from vertexai.language_models import TextEmbeddingModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SUPPORTED_MIME_TYPES = [
    "application/vnd.google-apps.document",
    "application/vnd.google-apps.spreadsheet",
    "application/vnd.google-apps.presentation",
    "application/pdf",
    "text/plain",
    "text/markdown",
]

CHUNK_SIZE = 512   # tokens (approximate; we use char-based split as proxy)
CHUNK_OVERLAP = 64
CHARS_PER_TOKEN = 4  # rough estimate for English text


# ---------------------------------------------------------------------------
# Drive helpers
# ---------------------------------------------------------------------------

def list_drive_files(service, folder_id: str) -> Generator[dict, None, None]:
    """Recursively list all supported files under a Drive folder."""
    page_token = None
    query = (
        f"'{folder_id}' in parents and trashed=false "
        f"and mimeType != 'application/vnd.google-apps.folder'"
    )
    while True:
        resp = service.files().list(
            q=query,
            fields="nextPageToken, files(id, name, mimeType, modifiedTime)",
            pageToken=page_token,
            pageSize=200,
        ).execute()
        for f in resp.get("files", []):
            if f["mimeType"] in SUPPORTED_MIME_TYPES:
                yield f
        page_token = resp.get("nextPageToken")
        if not page_token:
            break


def export_drive_file(service, file_id: str, mime_type: str) -> str:
    """Export a Drive file to plain text.

    TODO: add support for PDF extraction via pdfplumber.
    """
    export_mime = "text/plain"
    if mime_type == "application/vnd.google-apps.spreadsheet":
        export_mime = "text/csv"

    content = service.files().export(
        fileId=file_id, mimeType=export_mime
    ).execute()
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    return str(content)


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def chunk_text(text: str, file_id: str, file_name: str,
               chunk_size: int = CHUNK_SIZE,
               overlap: int = CHUNK_OVERLAP) -> list[dict]:
    """Split text into overlapping chunks and return structured records."""
    char_size = chunk_size * CHARS_PER_TOKEN
    char_overlap = overlap * CHARS_PER_TOKEN
    chunks = []
    start = 0
    chunk_idx = 0
    while start < len(text):
        end = min(start + char_size, len(text))
        chunk_text_content = text[start:end]
        chunks.append({
            "chunk_id": f"{file_id}_{chunk_idx:04d}",
            "file_id": file_id,
            "file_name": file_name,
            "section": f"chunk_{chunk_idx:04d}",
            "text": chunk_text_content,
            "char_start": start,
            "char_end": end,
        })
        start += char_size - char_overlap
        chunk_idx += 1
    logger.debug("Chunked %s -> %d chunks", file_name, len(chunks))
    return chunks


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

def embed_chunks(chunks: list[dict], model: TextEmbeddingModel,
                 batch_size: int = 50) -> list[dict]:
    """Add 'embedding' field to each chunk dict using Vertex AI text-embedding-004."""
    texts = [c["text"] for c in chunks]
    embeddings = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i: i + batch_size]
        response = model.get_embeddings(batch)
        embeddings.extend([r.values for r in response])
        time.sleep(0.1)  # gentle rate-limit
    for chunk, emb in zip(chunks, embeddings):
        chunk["embedding"] = emb
    return chunks


# ---------------------------------------------------------------------------
# BigQuery write
# ---------------------------------------------------------------------------

BQ_SCHEMA = [
    bigquery.SchemaField("chunk_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("file_id", "STRING"),
    bigquery.SchemaField("file_name", "STRING"),
    bigquery.SchemaField("section", "STRING"),
    bigquery.SchemaField("text", "STRING"),
    bigquery.SchemaField("char_start", "INTEGER"),
    bigquery.SchemaField("char_end", "INTEGER"),
    bigquery.SchemaField("embedding", "FLOAT64", mode="REPEATED"),
]


def ensure_bq_table(client: bigquery.Client, dataset: str, table: str, project: str) -> str:
    """Create the BigQuery table if it does not exist; return fully-qualified table ref."""
    table_ref = f"{project}.{dataset}.{table}"
    try:
        client.get_table(table_ref)
        logger.info("BQ table already exists: %s", table_ref)
    except Exception:
        bq_table = bigquery.Table(table_ref, schema=BQ_SCHEMA)
        client.create_table(bq_table)
        logger.info("Created BQ table: %s", table_ref)
    return table_ref


def write_chunks_to_bq(client: bigquery.Client, table_ref: str,
                       chunks: list[dict]) -> None:
    """Stream insert chunks into BigQuery."""
    rows = [
        {
            "chunk_id": c["chunk_id"],
            "file_id": c["file_id"],
            "file_name": c["file_name"],
            "section": c["section"],
            "text": c["text"],
            "char_start": c["char_start"],
            "char_end": c["char_end"],
            "embedding": c.get("embedding", []),
        }
        for c in chunks
    ]
    errors = client.insert_rows_json(table_ref, rows)
    if errors:
        logger.error("BQ insert errors: %s", errors)
    else:
        logger.info("Inserted %d chunks to %s", len(rows), table_ref)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(args: argparse.Namespace) -> None:
    logger.info("Initialising GCP clients...")
    vertexai.init(project=args.project, location=args.region)
    embedding_model = TextEmbeddingModel.from_pretrained(args.embedding_model)
    bq_client = bigquery.Client(project=args.project)
    drive_service = gdrive_build("drive", "v3")

    table_ref = ensure_bq_table(
        bq_client, args.bq_dataset, args.bq_table, args.project
    )

    total_chunks = 0
    logger.info("Listing Drive files under folder: %s", args.drive_folder_id)

    for drive_file in list_drive_files(drive_service, args.drive_folder_id):
        logger.info("Processing: %s (%s)", drive_file["name"], drive_file["id"])
        try:
            text = export_drive_file(
                drive_service, drive_file["id"], drive_file["mimeType"]
            )
        except Exception as exc:
            logger.warning("Could not export %s: %s", drive_file["name"], exc)
            continue

        if not text.strip():
            logger.debug("Empty content, skipping: %s", drive_file["name"])
            continue

        chunks = chunk_text(text, drive_file["id"], drive_file["name"],
                            chunk_size=args.chunk_size,
                            overlap=args.chunk_overlap)
        chunks = embed_chunks(chunks, embedding_model, batch_size=args.batch_size)
        write_chunks_to_bq(bq_client, table_ref, chunks)
        total_chunks += len(chunks)

    logger.info("Corpus build complete. Total chunks written: %d", total_chunks)
    # TODO: after initial load, run:
    #   CREATE VECTOR INDEX ON <table>(embedding) OPTIONS(distance_type='COSINE', ...
    #   to enable ANN search.


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build RAG corpus from Google Drive")
    parser.add_argument("--project", required=True)
    parser.add_argument("--region", default="us-central1")
    parser.add_argument("--drive_folder_id", required=True)
    parser.add_argument("--gcs_bucket", default="")
    parser.add_argument("--bq_dataset", default="rag_corpus")
    parser.add_argument("--bq_table", default="chunks")
    parser.add_argument("--embedding_model", default="text-embedding-004")
    parser.add_argument("--chunk_size", type=int, default=512)
    parser.add_argument("--chunk_overlap", type=int, default=64)
    parser.add_argument("--batch_size", type=int, default=50,
                        help="Embedding batch size")
    main(parser.parse_args())
