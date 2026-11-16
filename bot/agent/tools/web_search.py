import logging
from ddgs import DDGS
from agent.registry import register_tool

logger = logging.getLogger(__name__)


@register_tool(
    name="ddgs_direct_search",
    description="Fallback direct DuckDuckGo search if primary SearXNG search is unavailable.",
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query keywords"
            },
            "max_results": {
                "type": "integer",
                "description": "Number of results to retrieve (default: 4)",
                "default": 4
            }
        },
        "required": ["query"]
    }
)
def web_search(query: str, max_results: int = 4) -> str:
    """
    Executes a web search via DDGS and returns summarized search snippets.
    """
    try:
        results = DDGS().text(query, max_results=max_results)
        if not results:
            return f"No search results found for query: '{query}'."

        formatted = []
        for i, r in enumerate(results, 1):
            title = r.get("title", "No Title")
            snippet = r.get("body", "No Snippet")
            url = r.get("href", "")
            formatted.append(f"[{i}] {title}\nURL: {url}\nSummary: {snippet}\n")

        return "\n".join(formatted)
    except Exception as e:
        logger.error(f"Web search error: {e}")
        return f"Web search encountered an error: {e}"
