import logging
import re
from ddgs import DDGS

logger = logging.getLogger("web_tool")

def get_live_web_context(query: str, max_results: int = 4) -> str:
    """
    Searches DuckDuckGo live web and breaking news to retrieve accurate, real-time facts.
    Prevents the LLM from hallucinating or answering from outdated historical memory.
    """
    snippets = []
    seen = set()

    # Detect if news/trending is asked
    is_news = any(k in query.lower() for k in ["news", "trending", "latest", "today", "update", "current", "scandal", "headline"])

    # Refine search query for leadership / official roles
    search_queries = [query]
    
    cleaned = re.sub(r"^(who is|who are|what is|what about|tell me about|how is|which is)\s+", "", query, flags=re.IGNORECASE).strip()
    if cleaned and cleaned != query:
        search_queries.append(cleaned)

    # Check for political/role queries (e.g. CM, PM, President)
    if any(k in query.lower() for k in ["cm", "chief minister", "pm", "prime minister", "president", "ceo"]):
        role_search = query.lower()
        if "cm" in role_search and "chief minister" not in role_search:
            role_search = role_search.replace("cm", "chief minister")
        search_queries.append(f"current {role_search} 2026")
        search_queries.append(f"{role_search} today")

    try:
        with DDGS() as ddgs:
            # 1. Search breaking news first if news/trending
            if is_news:
                for sq in search_queries[:2]:
                    try:
                        for n in ddgs.news(sq, max_results=max_results):
                            title = n.get("title", "").strip()
                            body = n.get("body", "").strip()
                            key = title[:40].lower()
                            if body and key not in seen:
                                seen.add(key)
                                snippets.append(f"• [NEWS] {title}: {body}")
                                if len(snippets) >= max_results:
                                    break
                    except Exception as e:
                        logger.debug(f"ddgs news search warning: {e}")
                    if len(snippets) >= max_results:
                        break

            # 2. Search general web results
            if len(snippets) < max_results:
                for sq in search_queries:
                    try:
                        for r in ddgs.text(sq, max_results=max_results):
                            title = r.get("title", "").strip()
                            body = r.get("body", "").strip()
                            key = title[:40].lower()
                            if body and key not in seen:
                                seen.add(key)
                                snippets.append(f"• {title}: {body}")
                                if len(snippets) >= max_results:
                                    break
                    except Exception as e:
                        logger.debug(f"ddgs text search warning: {e}")
                    if len(snippets) >= max_results:
                        break

    except Exception as e:
        logger.warning(f"Error fetching live web context: {e}")

    return "\n".join(snippets[:max_results])

def search_web(query: str, max_results: int = 5) -> str:
    """Standard user-facing search tool for 'search <topic>' command."""
    context = get_live_web_context(query, max_results=max_results)
    return context if context else "No web results found for this query."
