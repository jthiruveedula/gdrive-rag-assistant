"""
index_health.py - Corpus health reporting for gdrive-rag-assistant.

Produces a structured health report for the BigQuery vector index:
  - Indexed file count and breakdown by MIME type
  - Sync lag (time since last successful ingestion)
  - Parse failure rate
  - Average source freshness across the corpus
  - Dead-letter queue backlog size

Can be run as a scheduled Cloud Run job or called from the admin API endpoint.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

from google.cloud import bigquery
from google.cloud import pubsub_v1

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

PROJECT_ID = os.environ.get("GCP_PROJECT", "")
BQ_DATASET = os.environ.get("BQ_DATASET", "gdrive_rag")
BQ_CHUNKS_TABLE = f"{PROJECT_ID}.{BQ_DATASET}.chunks"
BQ_INGESTION_LOG_TABLE = f"{PROJECT_ID}.{BQ_DATASET}.ingestion_log"
DLQ_SUBSCRIPTION = os.environ.get("DLQ_SUBSCRIPTION", "")


@dataclass
class CorpusHealthReport:
    generated_at: str  # ISO8601
    total_chunks: int
    total_documents: int
    chunks_by_mime_type: Dict[str, int]
    last_ingestion_ts: Optional[str]  # ISO8601
    sync_lag_minutes: Optional[float]
    parse_failure_rate: float  # 0.0 - 1.0
    avg_source_age_hours: Optional[float]
    dlq_backlog_count: int
    status: str  # "healthy" | "degraded" | "stale"


def get_corpus_stats(client: bigquery.Client) -> Dict:
    """Query BigQuery for chunk and document counts, breakdown by MIME type."""
    query = f"""
        SELECT
            COUNT(*) AS total_chunks,
            COUNT(DISTINCT file_id) AS total_documents,
            mime_type,
            AVG(TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), modified_time, HOUR)) AS avg_age_hours
        FROM `{BQ_CHUNKS_TABLE}`
        GROUP BY mime_type
    """
    results = list(client.query(query).result())
    if not results:
        return {"total_chunks": 0, "total_documents": 0, "by_mime": {}, "avg_age_hours": None}

    total_chunks = sum(r["total_chunks"] for r in results)
    total_documents = sum(r["total_documents"] for r in results)
    by_mime = {r["mime_type"]: r["total_chunks"] for r in results}
    avg_age = sum(r["avg_age_hours"] or 0 for r in results) / len(results)

    return {
        "total_chunks": total_chunks,
        "total_documents": total_documents,
        "by_mime": by_mime,
        "avg_age_hours": round(avg_age, 2),
    }


def get_last_ingestion(client: bigquery.Client) -> Optional[datetime]:
    """Return the timestamp of the most recent successful ingestion."""
    query = f"""
        SELECT MAX(ingested_at) AS last_ts
        FROM `{BQ_INGESTION_LOG_TABLE}`
        WHERE status = 'success'
    """
    rows = list(client.query(query).result())
    if rows and rows[0]["last_ts"]:
        return rows[0]["last_ts"]
    return None


def get_parse_failure_rate(client: bigquery.Client) -> float:
    """Return the fraction of ingestion attempts that ended in parse failure."""
    query = f"""
        SELECT
            COUNTIF(status = 'parse_error') AS failures,
            COUNT(*) AS total
        FROM `{BQ_INGESTION_LOG_TABLE}`
        WHERE ingested_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)
    """
    rows = list(client.query(query).result())
    if not rows or rows[0]["total"] == 0:
        return 0.0
    return round(rows[0]["failures"] / rows[0]["total"], 4)


def get_dlq_backlog(subscription_path: str) -> int:
    """Return the approximate number of messages in the dead-letter queue."""
    if not subscription_path:
        return 0
    try:
        subscriber = pubsub_v1.SubscriberClient()
        response = subscriber.get_subscription(request={"subscription": subscription_path})
        # Pull up to 10 messages to estimate backlog (non-destructive peek)
        pull_response = subscriber.pull(
            request={"subscription": subscription_path, "max_messages": 10},
            timeout=5.0,
        )
        return len(pull_response.received_messages)
    except Exception as exc:
        logger.warning("Could not read DLQ backlog: %s", exc)
        return -1


def classify_status(
    sync_lag_minutes: Optional[float],
    parse_failure_rate: float,
    dlq_backlog: int,
) -> str:
    """Classify overall corpus health as healthy, degraded, or stale."""
    if sync_lag_minutes is None or sync_lag_minutes > 120:
        return "stale"
    if parse_failure_rate > 0.05 or dlq_backlog > 50:
        return "degraded"
    return "healthy"


def build_health_report() -> CorpusHealthReport:
    """Build a full CorpusHealthReport from BigQuery and Pub/Sub."""
    client = bigquery.Client(project=PROJECT_ID)

    stats = get_corpus_stats(client)
    last_ingestion = get_last_ingestion(client)
    failure_rate = get_parse_failure_rate(client)
    dlq_count = get_dlq_backlog(DLQ_SUBSCRIPTION)

    now = datetime.now(timezone.utc)
    sync_lag: Optional[float] = None
    last_ts_str: Optional[str] = None

    if last_ingestion:
        last_ts_str = last_ingestion.isoformat()
        sync_lag = (now - last_ingestion).total_seconds() / 60

    status = classify_status(sync_lag, failure_rate, dlq_count)

    report = CorpusHealthReport(
        generated_at=now.isoformat(),
        total_chunks=stats["total_chunks"],
        total_documents=stats["total_documents"],
        chunks_by_mime_type=stats["by_mime"],
        last_ingestion_ts=last_ts_str,
        sync_lag_minutes=round(sync_lag, 2) if sync_lag is not None else None,
        parse_failure_rate=failure_rate,
        avg_source_age_hours=stats["avg_age_hours"],
        dlq_backlog_count=dlq_count,
        status=status,
    )
    logger.info("Corpus health: %s | lag=%.1f min | failures=%.1%%",
                status, sync_lag or 0, failure_rate * 100)
    return report


if __name__ == "__main__":
    import json
    report = build_health_report()
    print(json.dumps(asdict(report), indent=2, default=str))
