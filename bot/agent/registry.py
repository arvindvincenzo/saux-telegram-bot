import inspect
import json
import logging
from typing import Callable, Dict, Any, List

logger = logging.getLogger(__name__)

# Registry stores: {name: {"func": callable, "schema": dict}}
_TOOL_REGISTRY: Dict[str, Dict[str, Any]] = {}


def register_tool(name: str, description: str, parameters: dict):
    """
    Decorator to register a function as an agent tool with OpenAI-compatible JSON schema.
    """
    def decorator(func: Callable):
        schema = {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": parameters,
            }
        }
        _TOOL_REGISTRY[name] = {
            "func": func,
            "schema": schema,
        }
        logger.info(f"Registered agent tool: {name}")
        return func
    return decorator


def get_tool_schemas() -> List[dict]:
    """
    Returns list of registered tool schemas formatted for OpenAI/Groq API.
    Filters to canonical names to keep prompt compact and prevent duplicate tools.
    """
    canonical_order = [
        "web_search",
        "download_video",
        "clip_video",
        "extract_audio",
        "convert_file",
        "calculator",
        "compress_image"
    ]
    schemas = []
    seen = set()
    for name in canonical_order:
        if name in _TOOL_REGISTRY and name not in seen:
            schemas.append(_TOOL_REGISTRY[name]["schema"])
            seen.add(name)
    # Append any other registered tools
    for name, t in _TOOL_REGISTRY.items():
        if name not in seen and not name.startswith("reclip_") and name != "searxng_search":
            schemas.append(t["schema"])
            seen.add(name)
    return schemas


async def execute_tool(name: str, arguments: Any, status_callback: Any = None) -> str:
    """
    Executes a registered tool by name with arguments (dict or json string).
    Supports token glitch resilience, alias mapping, and real-time status callbacks.
    """
    norm_name = name.lower().strip().replace(" ", "_").replace("-", "_")
    
    # Prefix and substring fuzzy matching for resilient LLM tool calling
    if norm_name.startswith("download"):
        resolved_name = "download_video"
    elif "searx" in norm_name or norm_name in ("web_search", "search_web", "websearch"):
        resolved_name = "web_search"
    elif "clip" in norm_name:
        resolved_name = "clip_video"
    elif "audio" in norm_name or "extract" in norm_name:
        resolved_name = "extract_audio"
    elif "convert" in norm_name:
        resolved_name = "convert_file"
    elif "calc" in norm_name or "math" in norm_name:
        resolved_name = "calculator"
    elif "compress" in norm_name:
        resolved_name = "compress_image"
    else:
        alias_map = {
            "reclip_inspect": "reclip_inspect",
            "inspect_video": "reclip_inspect",
        }
        resolved_name = alias_map.get(norm_name, norm_name)

    if resolved_name not in _TOOL_REGISTRY:
        if name in _TOOL_REGISTRY:
            resolved_name = name
        else:
            return f"Error: Tool '{name}' is not registered."

    tool = _TOOL_REGISTRY[resolved_name]
    func = tool["func"]

    if isinstance(arguments, str):
        try:
            kwargs = json.loads(arguments) if arguments.strip() else {}
        except Exception as e:
            return f"Error: Invalid arguments JSON for {name}: {e}"
    elif isinstance(arguments, dict):
        kwargs = arguments
    else:
        kwargs = {}

    # Resilient argument fallback for single-argument tools
    if "query" not in kwargs and resolved_name == "web_search":
        for v in kwargs.values():
            if isinstance(v, str) and v:
                kwargs["query"] = v
                break
    elif "url" not in kwargs and resolved_name == "download_video":
        for v in kwargs.values():
            if isinstance(v, str) and ("http://" in v or "https://" in v or v):
                kwargs["url"] = v
                break
    elif "expression" not in kwargs and resolved_name == "calculator":
        for v in kwargs.values():
            if isinstance(v, str) and v:
                kwargs["expression"] = v
                break

    try:
        kwargs_to_pass = dict(kwargs)
        try:
            sig = inspect.signature(func)
            if "status_callback" in sig.parameters:
                kwargs_to_pass["status_callback"] = status_callback
        except Exception:
            pass

        if inspect.iscoroutinefunction(func):
            result = await func(**kwargs_to_pass)
        else:
            # Check if dual-mode tool returns a coroutine inside event loop
            call_res = func(**kwargs_to_pass)
            if inspect.iscoroutine(call_res):
                result = await call_res
            else:
                result = call_res

        if isinstance(result, (dict, list)):
            return json.dumps(result, ensure_ascii=False)
        return str(result)
    except Exception as e:
        logger.error(f"Error executing tool '{name}': {e}", exc_info=True)
        return f"Error executing tool '{name}': {str(e)}"
