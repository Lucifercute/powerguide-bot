import asyncio
import logging
from dataclasses import dataclass

from tavily import AsyncTavilyClient

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SearchResult:
    url: str
    title: str
    content: str


class WebSearch:
    """Tavily search. Never raises: a failed query just yields no results."""

    def __init__(self, api_key: str, results_per_query: int = 5):
        self.client = AsyncTavilyClient(api_key)
        self.results_per_query = results_per_query

    async def _search_one(self, query: str) -> list[SearchResult]:
        try:
            response = await self.client.search(query, max_results=self.results_per_query, timeout=30)
        except Exception as e:  # the SDK raises a zoo of exception types; none of them should break the step
            log.warning("Tavily search failed for %r: %r", query, e)
            return []
        return [
            SearchResult(url=r["url"], title=r.get("title") or "", content=r.get("content") or "")
            for r in response.get("results", [])
        ]

    async def search(self, queries: list[str]) -> list[SearchResult]:
        """Run all queries concurrently; results are deduplicated by URL, order preserved."""
        batches = await asyncio.gather(*(self._search_one(q) for q in queries))
        seen: set[str] = set()
        results = []
        for batch in batches:
            for r in batch:
                if r.url not in seen and r.content:
                    seen.add(r.url)
                    results.append(r)
        log.info("Search: %d queries → %d unique results", len(queries), len(results))
        return results
