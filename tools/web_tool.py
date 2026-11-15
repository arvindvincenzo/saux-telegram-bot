import logging
import re
import httpx

try:
    from duckduckgo_search import DDGS
except ImportError:
    try:
        from ddgs import DDGS
    except ImportError:
        DDGS = None

logger = logging.getLogger("web_tool")

def get_live_web_context(query: str, max_results: int = 3) -> str:
    """
    Searches DuckDuckGo live web and breaking news to retrieve accurate, real-time facts.
    Prevents the LLM from hallucinating or answering from outdated historical memory.
    """
    snippets = []
    seen = set()

    # Detect if breaking news / headline is explicitly asked
    is_news = any(k in query.lower() for k in ["breaking news", "headline", "headlines", "scandal"])

    # Refine search query for leadership / official roles
    search_queries = [query]
    
    cleaned = re.sub(r"^(who is|who are|who was|what is|what are|what about|tell me about|how is|which is|search for|search)\s+", "", query, flags=re.IGNORECASE).strip()
    if cleaned and cleaned != query:
        search_queries.append(cleaned)

    # 0. High-accuracy Wikipedia summary for named entities, people, movies, and concepts
    # Extract targeted inquiry if wrapped in conversational prefixes (e.g. "can you tell what is babe" -> "babe")
    m_q = re.search(r"\b(who|what|where|when|why|how)\s+(is|are|was|were)\s+([^?.,!]+)", query, flags=re.IGNORECASE)
    if m_q and len(m_q.group(3).strip()) > 1:
        target_term = m_q.group(3).strip()
    else:
        target_term = cleaned or query

    if target_term and len(target_term) > 1 and not is_news:
        try:
            wiki_headers = {"User-Agent": "SAUXBot/2.1 (contact@saux.si; https://saux.si)"}
            sr = httpx.get(
                "https://en.wikipedia.org/w/api.php",
                params={"action": "query", "list": "search", "srsearch": target_term, "format": "json", "srlimit": 1},
                headers=wiki_headers,
                timeout=2.5
            )
            if sr.status_code == 200:
                s_data = sr.json().get("query", {}).get("search", [])
                if s_data:
                    best_title = s_data[0].get("title", "")
                    if best_title:
                        sum_r = httpx.get(
                            f"https://en.wikipedia.org/api/rest_v1/page/summary/{best_title.replace(' ', '_')}",
                            headers=wiki_headers,
                            timeout=2.5
                        )
                        if sum_r.status_code == 200:
                            page_summary = sum_r.json()
                            extract = page_summary.get("extract", "").strip()
                            if extract and len(extract) > 40:
                                return f"**{page_summary.get('title', best_title)}**\n{extract}"
        except Exception as e:
            logger.debug(f"Wikipedia summary lookup note: {e}")

    # Check for political/role queries (e.g. CM, PM, President)
    if any(k in query.lower() for k in ["cm", "chief minister", "pm", "prime minister", "president", "ceo"]):
        role_search = query.lower()
        if "cm" in role_search and "chief minister" not in role_search:
            role_search = role_search.replace("cm", "chief minister")
        search_queries.append(f"current {role_search} 2026")

    if DDGS:
        try:
            with DDGS(timeout=2.0) as ddgs:
                # 1. Search breaking news first if news/trending
                if is_news:
                    try:
                        for n in ddgs.news(search_queries[0], max_results=max_results):
                            title = n.get("title", "").strip()
                            body = n.get("body", "").strip()
                            key = title[:40].lower()
                            if body and key not in seen:
                                seen.add(key)
                                snippets.append(f"• [NEWS] {title}: {body}")
                                if len(snippets) >= 2:
                                    break
                    except Exception as e:
                        logger.debug(f"ddgs news search warning: {e}")

                # 2. Search general web results
                if len(snippets) < 2:
                    for sq in search_queries[:2]:
                        try:
                            for r in ddgs.text(sq, max_results=max_results):
                                title = r.get("title", "").strip()
                                body = r.get("body", "").strip()
                                key = title[:40].lower()
                                if body and key not in seen:
                                    seen.add(key)
                                    snippets.append(f"• {title}: {body}")
                                    if len(snippets) >= 2:
                                        break
                        except Exception as e:
                            logger.debug(f"ddgs text search warning: {e}")
                        if len(snippets) >= 2:
                            break

        except Exception as e:
            logger.warning(f"Error fetching live web context from DDGS: {e}")

    # Fallback to zero-dependency instant answer API / Wikipedia if no snippets yet
    if not snippets:
        try:
            resp = httpx.get("https://api.duckduckgo.com/", params={"q": query, "format": "json"}, timeout=2.0)
            if resp.status_code in (200, 202):
                data = resp.json()
                abstract = data.get("AbstractText") or data.get("Abstract")
                heading = data.get("Heading") or query
                if abstract:
                    snippets.append(f"• {heading}: {abstract}")
        except Exception:
            pass

    if not snippets:
        try:
            r = httpx.get(
                "https://en.wikipedia.org/w/api.php",
                params={"action": "query", "list": "search", "srsearch": query, "format": "json", "srlimit": 2},
                headers={"User-Agent": "SAUXBot/2.0 (contact@saux.si)"},
                timeout=2.0
            )
            if r.status_code == 200:
                for s in r.json().get("query", {}).get("search", []):
                    clean = re.sub(r'<[^>]+>', '', s.get("snippet", "")).strip()
                    clean = clean.replace("&quot;", '"').replace("&#039;", "'").replace("&amp;", "&")
                    if clean:
                        snippets.append(f"• {s.get('title')}: {clean}")
        except Exception:
            pass

    return "\n".join(snippets[:max_results])

def search_web(query: str, max_results: int = 5) -> str:
    """Standard user-facing search tool for 'search <topic>' command."""
    context = get_live_web_context(query, max_results=max_results)
    return context if context else "No web results found for this query."
