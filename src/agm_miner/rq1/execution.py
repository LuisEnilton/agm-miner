"""Run many LLM requests through Message Batches, resumably.

``execute_batched`` answers each request from the cache when it can, finishes the batches an
interrupted run left behind, and submits only what is still missing. A batch id is saved before
waiting for it, so Ctrl+C at any moment loses no paid work: the next run picks the batch up.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from agm_miner.progress import Progress
from agm_miner.rq1.llm import BatchProgress, LLMClient, LLMRequest, LLMResult
from agm_miner.rq1.store import BatchState, ResponseCache

# Requests per submitted batch (the API allows 100,000 or 256 MB; smaller batches finish and
# are cached sooner).
MAX_BATCH_REQUESTS = 5_000


def execute_batched(
    client: LLMClient,
    requests: Sequence[LLMRequest],
    cache: ResponseCache,
    state: BatchState,
    progress: Progress | None = None,
    *,
    max_batch_requests: int = MAX_BATCH_REQUESTS,
) -> dict[str, LLMResult]:
    """Result of every request (cached or new), keyed by ``custom_id``.

    Failed requests (refused, truncated, errored, expired...) are returned but not cached, so
    the next run asks them again.
    """
    progress = progress or Progress()
    wanted = {request.custom_id: request for request in requests}
    progress.set_total("requests", len(wanted))
    results: dict[str, LLMResult] = {}
    for key in wanted:
        cached = cache.get(key)
        if cached is not None:
            results[key] = cached
    progress.advance("requests", len(results))
    progress.set_done("cached", len(results))

    for batch_id in state.pending():
        _collect(client, batch_id, wanted, results, cache, state, progress)

    missing = [request for key, request in wanted.items() if key not in results]
    for start in range(0, len(missing), max_batch_requests):
        chunk = missing[start : start + max_batch_requests]
        progress.set_phase(f"submitting {len(chunk)} requests")
        batch_id = client.submit_batch(chunk)
        state.add(batch_id, [request.custom_id for request in chunk])
        _collect(client, batch_id, wanted, results, cache, state, progress)
    return results


def _collect(
    client: LLMClient,
    batch_id: str,
    wanted: Mapping[str, LLMRequest],
    results: dict[str, LLMResult],
    cache: ResponseCache,
    state: BatchState,
    progress: Progress,
) -> None:
    progress.set_phase(f"waiting for Claude batch {batch_id}")

    def show(batch: BatchProgress) -> None:
        done = sum(count for name, count in batch.counts.items() if name != "processing")
        progress.set_done("batch requests answered", done)
        progress.set_total("batch requests answered", done + batch.counts.get("processing", 0))

    client.wait_for_batch(batch_id, show)
    progress.set_phase(f"collecting Claude batch {batch_id}")
    for result in client.batch_results(batch_id):
        cache.put(result)  # also answers left by an interrupted run with other settings
        if result.custom_id in wanted and result.custom_id not in results:
            results[result.custom_id] = result
            progress.advance("requests")
            progress.advance(result.status)
    state.remove(batch_id)


def usage_totals(results: Iterable[LLMResult]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for result in results:
        for name, value in result.usage.items():
            totals[name] = totals.get(name, 0) + value
    return totals
