"""ingestion/pipeline_main.py

Apache Beam / Dataflow streaming pipeline:
  Pub/Sub (Drive change notifications) -> chunk -> embed -> BigQuery Vector Search

Usage (local DirectRunner):
  python pipeline_main.py --runner=DirectRunner --project=my-proj --region=us-central1

Usage (Dataflow):
  python pipeline_main.py --runner=DataflowRunner --project=my-proj --region=us-central1 \\
      --temp_location=gs://my-bucket/tmp --staging_location=gs://my-bucket/staging
"""
from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from typing import Iterator

import apache_beam as beam
from apache_beam.options.pipeline_options import PipelineOptions, StandardOptions
from apache_beam.io.gcp.pubsub import ReadFromPubSub

from ingestion.transforms.drive_reader import DriveReader
from ingestion.transforms.chunker import SemanticChunker
from ingestion.transforms.embedder import VertexEmbedder
from ingestion.transforms.bq_writer import BigQueryChunkWriter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pipeline options
# ---------------------------------------------------------------------------

def build_pipeline_options(argv: list[str] | None = None) -> PipelineOptions:
    """Parse CLI flags and return Beam PipelineOptions."""
    parser = argparse.ArgumentParser(description="GDrive RAG Ingestion Pipeline")
    parser.add_argument("--project", required=True, help="GCP project ID")
    parser.add_argument("--region", default="us-central1")
    parser.add_argument("--pubsub_subscription", required=True,
                        help="Full Pub/Sub subscription path")
    parser.add_argument("--bq_dataset", default="rag_corpus")
    parser.add_argument("--bq_table", default="chunks")
    parser.add_argument("--embedding_model", default="text-embedding-004")
    parser.add_argument("--chunk_size_tokens", type=int, default=512)
    parser.add_argument("--chunk_overlap_tokens", type=int, default=64)
    known, pipeline_args = parser.parse_known_args(argv)

    options = PipelineOptions(pipeline_args)
    options.view_as(StandardOptions).streaming = True
    options.view_as(StandardOptions).runner = options.view_as(StandardOptions).runner or "DataflowRunner"
    return options, known


# ---------------------------------------------------------------------------
# Helper DoFns
# ---------------------------------------------------------------------------

class ParsePubSubMessage(beam.DoFn):
    """Deserialise a Pub/Sub message payload into a dict.

    Expected message schema (Drive Change API v3 watch notification):
    {
        "file_id": "1BxiMVs0XRA...",
        "mime_type": "application/vnd.google-apps.document",
        "change_type": "modified"   # modified | deleted
    }
    """

    def process(self, message: bytes) -> Iterator[dict]:
        try:
            payload = json.loads(message.decode("utf-8"))
            if payload.get("change_type") == "deleted":
                # TODO: emit a delete event to a separate side-output for tombstone processing
                logger.info("Skipping deleted file: %s", payload.get("file_id"))
                return
            yield payload
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.error("Failed to parse Pub/Sub message: %s", exc)


class FilterSupportedMimeTypes(beam.DoFn):
    """Drop unsupported Drive MIME types before fetching content."""

    SUPPORTED = {
        "application/vnd.google-apps.document",
        "application/vnd.google-apps.spreadsheet",
        "application/vnd.google-apps.presentation",
        "application/pdf",
        "text/plain",
        "text/markdown",
    }

    def process(self, element: dict) -> Iterator[dict]:
        if element.get("mime_type") in self.SUPPORTED:
            yield element
        else:
            logger.debug("Unsupported mime type: %s", element.get("mime_type"))


# ---------------------------------------------------------------------------
# Pipeline definition
# ---------------------------------------------------------------------------

def build_pipeline(
    pipeline: beam.Pipeline,
    subscription: str,
    bq_dataset: str,
    bq_table: str,
    embedding_model: str,
    chunk_size: int,
    chunk_overlap: int,
) -> None:
    """Wire all transforms together into a streaming pipeline."""

    # TODO: add windowing strategy (e.g., FixedWindows(60)) if you need
    #       batched embedding calls rather than per-element.

    _ = (
        pipeline
        | "ReadPubSub" >> ReadFromPubSub(subscription=subscription)
        | "ParseMessage" >> beam.ParDo(ParsePubSubMessage())
        | "FilterMimeType" >> beam.ParDo(FilterSupportedMimeTypes())
        | "FetchDriveContent" >> beam.ParDo(DriveReader())
        | "ChunkDocument" >> beam.ParDo(
            SemanticChunker(chunk_size=chunk_size, overlap=chunk_overlap)
        )
        | "EmbedChunks" >> beam.ParDo(VertexEmbedder(model_name=embedding_model))
        | "WriteToBigQuery" >> beam.ParDo(
            BigQueryChunkWriter(dataset=bq_dataset, table=bq_table)
        )
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    options, known_args = build_pipeline_options()

    logger.info(
        "Starting pipeline | subscription=%s bq=%s.%s model=%s",
        known_args.pubsub_subscription,
        known_args.bq_dataset,
        known_args.bq_table,
        known_args.embedding_model,
    )

    with beam.Pipeline(options=options) as p:
        build_pipeline(
            pipeline=p,
            subscription=known_args.pubsub_subscription,
            bq_dataset=known_args.bq_dataset,
            bq_table=known_args.bq_table,
            embedding_model=known_args.embedding_model,
            chunk_size=known_args.chunk_size_tokens,
            chunk_overlap=known_args.chunk_overlap_tokens,
        )

    logger.info("Pipeline finished.")
