import asyncio
import logging
from typing import Optional, List, Dict, Any
import httpx
from ddgs import DDGS

import config
from agent.registry import register_tool

logger = logging.getLogger(__name__)

# Default local/custom SearXNG endpoint or public fallback list
DEFAULT_SEARXNG_URL = getattr(config, "searxng_url", "http://127.0.0.1:8080")


@register_tool(
    name="web_search",
    description="Search the live web and news using SearXNG. MUST be used for questions about current office holders (CM, PM, President), latest news, real-time facts, and today's updates.",
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query keywords"
            },
            "category": {
                "type": "string",
                "description": "Optional search category: general, news, science, or it"
            }
        },
        "required": ["query"]
    }
)
@register_tool(
    name="searxng_search",
    description="Alias for web_search. Searches live web, news, science, or technology information.",
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query keywords"
            },
            "category": {
                "type": "string",
                "description": "Optional search category: general, news, science, or it"
            }
        },
        "required": ["query"]
    }
)
async def web_search(
    query: str,
    category: str = "general",
    time_range: str = "all",
    max_results: int = 5
) -> str:
    """
    Executes SearXNG search with automatic resilient fallback.
    """
    q_to_search = (query or "").strip()
    if not q_to_search:
        return "Please provide a valid search query."

    logger.info(f"Web search requested: '{q_to_search}' [category={category}]")

    # 1. Attempt SearXNG JSON endpoint
    results = await _try_searxng_api(q_to_search, category, time_range, max_results)
    if results:
        return _format_results(results, source="SearXNG")

    # 2. Resilient non-blocking fallback to DDGS via thread pool to prevent event-loop freezing
    fallback_results = await asyncio.to_thread(_fallback_search, q_to_search, category, max_results)
    if fallback_results:
        return _format_results(fallback_results, source="WebSearch")

    return f"No search results found for query: '{query}'."


async def _try_searxng_api(
    query: str,
    category: str,
    time_range: str,
    max_results: int
) -> Optional[List[Dict[str, Any]]]:
    endpoints = [DEFAULT_SEARXNG_URL, "https://searx.be", "https://baresearch.org"]
    params = {
        "q": query,
        "format": "json",
        "categories": category if category != "all" else "general",
    }
    if time_range != "all":
        params["time_range"] = time_range

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json"
    }

    async with httpx.AsyncClient(timeout=4.0) as client:
        for ep in endpoints:
            try:
                url = f"{ep.rstrip('/')}/search"
                r = await client.get(url, params=params, headers=headers)
                if r.status_code == 200:
                    data = r.json()
                    raw_items = data.get("results", [])
                    if raw_items:
                        items = []
                        for item in raw_items[:max_results]:
                            items.append({
                                "title": item.get("title", "No Title"),
                                "url": item.get("url", ""),
                                "content": item.get("content", ""),
                                "published_date": item.get("publishedDate", ""),
                                "engine": item.get("engine", "searxng"),
                            })
                        return items
            except Exception as e:
                logger.debug(f"SearXNG endpoint {ep} error: {e}")
                continue
    return None


def _fallback_search(query: str, category: str, max_results: int) -> List[Dict[str, Any]]:
    ddg = DDGS()
    raw = None
    if category == "news":
        try:
            raw = list(ddg.news(query, max_results=max_results))
        except Exception:
            raw = None

    if not raw:
        try:
            raw = list(ddg.text(query, max_results=max_results))
        except Exception as e:
            logger.error(f"Fallback text search error: {e}")
            raw = []

    items = []
    for r in (raw or []):
        items.append({
            "title": r.get("title", ""),
            "url": r.get("href") or r.get("url", ""),
            "content": r.get("body", ""),
            "published_date": r.get("date", ""),
            "engine": "ddg-web",
        })
    return items


def _format_results(results: List[Dict[str, Any]], source: str) -> str:
    lines = [f"Found {len(results)} relevant results (via {source}):\n"]
    for i, r in enumerate(results, 1):
        title = r.get("title", "Untitled")
        url = r.get("url", "")
        content = r.get("content", "")
        date = r.get("published_date")
        engine = r.get("engine", "")

        header = f"[{i}] {title}"
        if date:
            header += f" ({date})"
        if engine:
            header += f" [source: {engine}]"

        lines.append(header)
        lines.append(f"URL: {url}")
        lines.append(f"Summary: {content}\n")

    return "\n".join(lines)


# Backward compatibility alias
searxng_search = web_search

