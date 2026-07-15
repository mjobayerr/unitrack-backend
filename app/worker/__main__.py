"""UniTrack worker process (spec §4.1 worker layer).

Jobs:
  1. GPS ES indexer — read gps_ingest stream, index to Elasticsearch   [wired]
                      (ES is the sole GPS store; Postgres holds no GPS.)
  2. ETA engine    — Mapbox Directions per live trip every 2-3 min   [later]
  3. bKash reconciler (nightly)                                      [later]
  4. Fraud sweep + auto-alerts + report aggregation                  [later]
"""

import asyncio
import logging

from app.worker.gps_es_indexer import run as run_gps_es_indexer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("unitrack.worker")


async def main() -> None:
    logger.info("UniTrack worker starting.")
    await asyncio.gather(
        run_gps_es_indexer(),
    )


if __name__ == "__main__":
    asyncio.run(main())
