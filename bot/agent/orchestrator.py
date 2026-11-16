import re
import json
import logging
from pathlib import Path
from typing import AsyncGenerator, Tuple, List, Dict, Any, Optional

import config
from agent.registry import get_tool_schemas, execute_tool
import agent.tools  # Ensure all tools are registered

logger = logging.getLogger(__name__)

MEDIA_URL_REGEX = re.compile(
    r'(https?://(?:[a-zA-Z0-9-]+\.)?(?:instagram\.com|youtube\.com|youtu\.be|tiktok\.com|twitter\.com|x\.com|facebook\.com|fb\.watch|reddit\.com|pin\.it|pinterest\.com)/[^\s]+)',
    re.IGNORECASE
)


def _parse_failed_generation(failed_gen: str):
    if not failed_gen:
        return None
    # 1. Try JSON tool call structure inside failed_generation
    json_match = re.search(r'\{.*"name"\s*:\s*"([^"]+)".*\}', failed_gen, re.DOTALL)
    if json_match:
        try:
            s = failed_gen[json_match.start():]
            data = json.loads(s[:s.rfind('}')+1])
            fn_name = data.get("name")
            args = data.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}
            if fn_name:
                return fn_name, args
        except Exception:
            pass

    # 2. Try XML function tags
    fn_match = re.search(r'<function=([a-zA-Z0-9_ ]+)>', failed_gen)
    if not fn_match:
        return None
    fn_name = fn_match.group(1).strip().replace(" ", "_")
    param_matches = re.findall(r'<\s*parameter(?:name)?=([a-zA-Z0-9_ ]+)>\s*(.*?)\s*</parameter>', failed_gen, re.DOTALL)
    args = {k.strip().replace(" ", "_"): v.strip() for k, v in param_matches}

    # Handle un-named / bare parameters e.g. < parameter>value</parameter>
    if not args:
        bare_matches = re.findall(r'<\s*parameter\s*>\s*(.*?)\s*</parameter>', failed_gen, re.DOTALL)
        if bare_matches:
            val = bare_matches[0].strip()
            fn_lower = fn_name.lower()
            if "search" in fn_lower or "searx" in fn_lower:
                args["query"] = val
            elif "download" in fn_lower or "video" in fn_lower:
                args["url"] = val
            elif "calc" in fn_lower:
                args["expression"] = val
            else:
                args["query"] = val

    return fn_name, args


class AgentOrchestrator:
    """
    Autonomous ReAct Agent Orchestrator:
    Coordinates multi-step tool execution, reasoning, and final synthesis.
    """
    def __init__(self, client, model: Optional[str] = None):
        self.client = client
        self.model = model or config.models["available_text_models"][0]
        self.max_steps = 4

    async def run(
        self,
        messages: List[Dict[str, Any]],
        enable_tools: bool = True
    ) -> AsyncGenerator[Tuple[str, str, Dict[str, Any]], None]:
        """
        Executes autonomous tool loop.
        Yields: (step_type, text_content, metadata)
          - ("status", "🔍 Searching web...", {"tool": "web_search"})
          - ("chunk", " partial text ", {})
          - ("finished", "Final answer text", {"n_input_tokens": x, "n_output_tokens": y})
        """
        tools = get_tool_schemas() if enable_tools else None
        current_messages = list(messages)
        total_input_tokens = 0
        total_output_tokens = 0
        generated_files = []
        file_titles = {}
        file_creators = {}
        latest_video_metadata = None
        executed_tool_count = 0

        # Proactive Intent Recognition for Media URLs
        user_text = ""
        for m in reversed(current_messages):
            if m.get("role") == "user":
                c = m.get("content", "")
                if isinstance(c, str):
                    user_text = c
                elif isinstance(c, list):
                    for part in c:
                        if isinstance(part, dict) and part.get("type") == "text":
                            user_text = part.get("text", "")
                            break
                if user_text:
                    break

        media_match = MEDIA_URL_REGEX.search(user_text) if enable_tools else None
        if media_match and executed_tool_count == 0:
            media_url = media_match.group(1).rstrip(",.!?")
            u_lower = user_text.lower()
            if any(w in u_lower for w in ["audio", "mp3", "m4a", "soundtrack"]):
                fn_name = "extract_audio"
                fn_args = {"source": media_url}
            elif any(w in u_lower for w in ["clip", "trim", "cut"]):
                fn_name = "clip_video"
                fn_args = {"source": media_url, "start_time": "00:00:00", "end_time": "00:00:30"}
            else:
                fn_name = "download_video"
                fn_args = {"url": media_url}

            action_msg = self._format_action_status(fn_name, json.dumps(fn_args))
            yield ("status", action_msg, {"tool": fn_name})

            tool_output = await execute_tool(fn_name, fn_args)
            executed_tool_count += 1

            title_matches = re.findall(r'\[TITLE:\s*([^\]]+)\]', tool_output)
            t_val = title_matches[0].strip() if title_matches else ""
            creator_matches = re.findall(r'\[CREATOR:\s*([^\]]+)\]', tool_output)
            c_val = creator_matches[0].strip() if creator_matches else ""
            meta_matches = re.findall(r'\[META:\s*({.+?})\]', tool_output)
            m_val = {}
            if meta_matches:
                try:
                    m_val = json.loads(meta_matches[0])
                except Exception:
                    pass
            if not m_val and (t_val or c_val):
                m_val = {
                    "source_url": media_url,
                    "video_title": t_val or "Video",
                    "creator_name": c_val or "Unknown Creator",
                    "duration": None,
                    "thumbnail": None,
                    "description": None,
                    "transcript": None,
                    "direct_url": None
                }
            if m_val:
                latest_video_metadata = m_val

            for m in re.findall(r'\[FILE:\s*([^\]]+)\]', tool_output):
                p = Path(m.strip())
                if p.exists() and p.is_file() and str(p) not in generated_files:
                    generated_files.append(str(p))
                    if t_val:
                        file_titles[str(p)] = t_val
                        file_titles[p.name] = t_val
                    if c_val:
                        file_creators[str(p)] = c_val
                        file_creators[p.name] = c_val

            clean_tool_output = re.sub(r'\[FILE:\s*[^\]]+\]', '', tool_output)
            clean_tool_output = re.sub(r'\[TITLE:\s*[^\]]+\]', '', clean_tool_output)
            clean_tool_output = re.sub(r'\[CREATOR:\s*[^\]]+\]', '', clean_tool_output)
            clean_tool_output = re.sub(r'\[DIRECT_URL:\s*[^\]]*\]', '', clean_tool_output)
            clean_tool_output = re.sub(r'\[META:\s*({.+?})\]', '', clean_tool_output).strip()

            if generated_files or (latest_video_metadata and latest_video_metadata.get("direct_url")):
                # Media resolved/downloaded: yield directly with zero extra Groq round-trip!
                logger.info(f"[PERF] Media proactive stream/download succeeded, yielding finished directly")
                size_str = ""
                if generated_files:
                    try:
                        s_mb = Path(generated_files[0]).stat().st_size / (1024 * 1024)
                        size_str = f" ({s_mb:.2f} MB)"
                    except Exception:
                        pass
                final_answer = f"Here is your video: **{t_val or 'Video'}**{size_str}.\nIt has been delivered directly to this chat."
                yield ("finished", final_answer, {
                    "generated_files": generated_files,
                    "file_titles": file_titles,
                    "file_creators": file_creators,
                    "video_metadata": latest_video_metadata,
                    "n_input_tokens": 0,
                    "n_output_tokens": 0
                })
                return

            current_messages.append({
                "role": "user",
                "content": f"[System Context: The user provided a media URL ({media_url}). The backend downloader has already fetched the video:\n{clean_tool_output}\n\nPlease confirm to the user that their video has been downloaded and delivered to this Telegram chat. Do NOT mention local file paths or server storage.]"
            })

        for step in range(self.max_steps):
            kwargs = {
                "model": self.model,
                "messages": current_messages,
                "temperature": 0.7,
            }
            if tools:
                kwargs["tools"] = tools
                # If a tool has already run, force immediate answer synthesis with tool_choice="none"
                kwargs["tool_choice"] = "none" if executed_tool_count >= 1 else "auto"

            FALLBACK_MODELS = ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b"]
            candidate_models = [kwargs["model"]] + [m for m in FALLBACK_MODELS if m != kwargs["model"]]
            response = None
            e = None

            for try_idx, cand_model in enumerate(candidate_models):
                kwargs["model"] = cand_model
                try:
                    response = await self.client.chat.completions.create(**kwargs)
                    self.model = cand_model
                    e = None
                    break
                except Exception as call_err:
                    e = call_err
                    err_str = str(call_err).lower()
                    if ("rate_limit" in err_str or "429" in err_str) and try_idx < len(candidate_models) - 1:
                        logger.warning(f"Rate limit reached on {cand_model}. Seamlessly failing over to {candidate_models[try_idx+1]}...")
                        continue
                    break

            if response is None and e is not None:

                # Check for Groq XML failed_generation recovery
                failed_gen = ""
                if hasattr(e, "body") and isinstance(e.body, dict):
                    failed_gen = e.body.get("failed_generation", "")
                elif hasattr(e, "response"):
                    try:
                        failed_gen = e.response.json().get("error", {}).get("failed_generation", "")
                    except Exception:
                        pass

                recovered = _parse_failed_generation(failed_gen) if failed_gen else None
                if recovered:
                    fn_name, fn_args = recovered
                    action_msg = self._format_action_status(fn_name, json.dumps(fn_args))
                    yield ("status", action_msg, {"tool": fn_name})

                    tool_output = ""
                    async for p_type, p_val, p_meta in self._execute_tool_with_progress(fn_name, fn_args):
                        if p_type == "status":
                            yield ("status", p_val, p_meta)
                        elif p_type == "result":
                            tool_output = p_val
                    executed_tool_count += 1

                    # Parse generated files and titles
                    title_matches = re.findall(r'\[TITLE:\s*([^\]]+)\]', tool_output)
                    t_val = title_matches[0].strip() if title_matches else ""
                    creator_matches = re.findall(r'\[CREATOR:\s*([^\]]+)\]', tool_output)
                    c_val = creator_matches[0].strip() if creator_matches else ""
                    meta_matches = re.findall(r'\[META:\s*({.+?})\]', tool_output)
                    m_val = {}
                    if meta_matches:
                        try:
                            m_val = json.loads(meta_matches[0])
                        except Exception:
                            pass
                    if m_val:
                        latest_video_metadata = m_val

                    for m in re.findall(r'\[FILE:\s*([^\]]+)\]', tool_output):
                        p = Path(m.strip())
                        if p.exists() and p.is_file() and str(p) not in generated_files:
                            generated_files.append(str(p))
                            if t_val:
                                file_titles[str(p)] = t_val
                                file_titles[p.name] = t_val
                            if c_val:
                                file_creators[str(p)] = c_val
                                file_creators[p.name] = c_val

                    clean_tool_output = re.sub(r'\[FILE:\s*[^\]]+\]', '', tool_output)
                    clean_tool_output = re.sub(r'\[TITLE:\s*[^\]]+\]', '', clean_tool_output)
                    clean_tool_output = re.sub(r'\[CREATOR:\s*[^\]]+\]', '', clean_tool_output)
                    clean_tool_output = re.sub(r'\[DIRECT_URL:\s*[^\]]*\]', '', clean_tool_output)
                    clean_tool_output = re.sub(r'\[META:\s*({.+?})\]', '', clean_tool_output).strip()

                    current_messages.append({"role": "assistant", "content": f"I executed {fn_name}."})
                    current_messages.append({
                        "role": "user",
                        "content": f"[Tool {fn_name} returned]:\n{clean_tool_output}\n\nPlease synthesize the final direct answer for the user based on these results."
                    })
                    continue

                if "response" not in locals() or response is None:
                    logger.error(f"Agent completion error: {e}", exc_info=True)
                    yield ("error", f"AI generation error: {e}", {})
                    return

            choice = response.choices[0]
            message = choice.message

            if response.usage:
                total_input_tokens += response.usage.prompt_tokens or 0
                total_output_tokens += response.usage.completion_tokens or 0

            # If the model requested tool executions
            if message.tool_calls:
                current_messages.append(message)

                for tool_call in message.tool_calls:
                    fn_name = tool_call.function.name
                    fn_args = tool_call.function.arguments

                    # Notify user of agent action
                    action_msg = self._format_action_status(fn_name, fn_args)
                    yield ("status", action_msg, {"tool": fn_name})

                    # Execute the tool with live progress
                    logger.info(f"Agent executing tool: {fn_name} with args: {fn_args}")
                    tool_output = ""
                    async for p_type, p_val, p_meta in self._execute_tool_with_progress(fn_name, fn_args):
                        if p_type == "status":
                            yield ("status", p_val, p_meta)
                        elif p_type == "result":
                            tool_output = p_val
                    executed_tool_count += 1

                    # Parse generated files and titles
                    title_matches = re.findall(r'\[TITLE:\s*([^\]]+)\]', tool_output)
                    t_val = title_matches[0].strip() if title_matches else ""
                    creator_matches = re.findall(r'\[CREATOR:\s*([^\]]+)\]', tool_output)
                    c_val = creator_matches[0].strip() if creator_matches else ""
                    meta_matches = re.findall(r'\[META:\s*({.+?})\]', tool_output)
                    m_val = {}
                    if meta_matches:
                        try:
                            m_val = json.loads(meta_matches[0])
                        except Exception:
                            pass
                    if m_val:
                        latest_video_metadata = m_val

                    for m in re.findall(r'\[FILE:\s*([^\]]+)\]', tool_output):
                        p = Path(m.strip())
                        if p.exists() and p.is_file() and str(p) not in generated_files:
                            generated_files.append(str(p))
                            if t_val:
                                file_titles[str(p)] = t_val
                                file_titles[p.name] = t_val
                            if c_val:
                                file_creators[str(p)] = c_val
                                file_creators[p.name] = c_val

                    clean_tool_output = re.sub(r'\[FILE:\s*[^\]]+\]', '', tool_output)
                    clean_tool_output = re.sub(r'\[TITLE:\s*[^\]]+\]', '', clean_tool_output)
                    clean_tool_output = re.sub(r'\[CREATOR:\s*[^\]]+\]', '', clean_tool_output)
                    clean_tool_output = re.sub(r'\[DIRECT_URL:\s*[^\]]*\]', '', clean_tool_output)
                    clean_tool_output = re.sub(r'\[META:\s*({.+?})\]', '', clean_tool_output).strip()

                    current_messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": fn_name,
                        "content": clean_tool_output
                    })
                continue

            # If no tool calls, this is the final answer
            final_content = message.content or ""
            # Safety net: If user provided a media URL but model refused, override and download directly!
            if media_match and not generated_files:
                refusal_markers = ["cannot download", "cannot access", "terms of service", "technical restrictions", "don't have the ability", "unable to download", "cannot save"]
                if any(rm in final_content.lower() for rm in refusal_markers):
                    logger.warning("Detected hallucinated refusal for media URL. Intercepting and downloading directly...")
                    media_url = media_match.group(1).rstrip(",.!?")
                    fn_name = "download_video"
                    fn_args = {"url": media_url}
                    yield ("status", "⚡ Fetching video in seconds...", {"tool": fn_name})
                    tool_output = ""
                    async for p_type, p_val, p_meta in self._execute_tool_with_progress(fn_name, fn_args):
                        if p_type == "status":
                            yield ("status", p_val, p_meta)
                        elif p_type == "result":
                            tool_output = p_val
                    title_matches = re.findall(r'\[TITLE:\s*([^\]]+)\]', tool_output)
                    t_val = title_matches[0].strip() if title_matches else ""
                    for m in re.findall(r'\[FILE:\s*([^\]]+)\]', tool_output):
                        p = Path(m.strip())
                        if p.exists() and p.is_file() and str(p) not in generated_files:
                            generated_files.append(str(p))
                            if t_val:
                                file_titles[str(p)] = t_val
                                file_titles[p.name] = t_val
                    final_content = f"Here is your video: **{t_val or 'Video'}**! It has been downloaded and delivered to your chat."

            # Sanitize internal paths from user-facing text
            final_content = re.sub(r'\[FILE:\s*[^\]]+\]', '', final_content, flags=re.IGNORECASE)
            final_content = re.sub(r'\[TITLE:\s*[^\]]+\]', '', final_content, flags=re.IGNORECASE)
            final_content = re.sub(r'[A-Za-z]:\\[^\s\n*`<>]+', '', final_content)

            yield ("finished", final_content.strip(), {
                "n_input_tokens": total_input_tokens,
                "n_output_tokens": total_output_tokens,
                "generated_files": generated_files,
                "file_titles": file_titles,
                "file_creators": file_creators,
                "video_metadata": latest_video_metadata
            })
            return

        # Fallback if max steps reached
        fallback_msg = "Here are the results I found."
        yield ("finished", fallback_msg, {
            "n_input_tokens": total_input_tokens,
            "n_output_tokens": total_output_tokens,
            "generated_files": generated_files,
            "file_titles": file_titles,
            "file_creators": file_creators,
            "video_metadata": latest_video_metadata
        })

    def _format_action_status(self, tool_name: str, args_str: str) -> str:
        try:
            args = json.loads(args_str) if args_str else {}
        except Exception:
            args = {}

        norm_name = tool_name.lower().strip().replace(" ", "_").replace("-", "_")
        if norm_name.startswith("download"):
            t_name = "download_video"
        elif "searx" in norm_name or norm_name in ("web_search", "search_web", "websearch"):
            t_name = "web_search"
        elif "clip" in norm_name:
            t_name = "clip_video"
        elif "audio" in norm_name or "extract" in norm_name:
            t_name = "extract_audio"
        elif "convert" in norm_name:
            t_name = "convert_file"
        elif "calc" in norm_name or "math" in norm_name:
            t_name = "calculator"
        elif "compress" in norm_name:
            t_name = "compress_image"
        else:
            t_name = norm_name

        if t_name in ("searxng_search", "web_search", "searxngollar", "search_web", "searxng"):
            q = args.get("query", "")
            cat = args.get("category", "")
            prefix = f" [{cat}]" if cat and cat != "general" else ""
            return f"🔍 Searching live web{prefix} for: <i>{q}</i>"
        elif t_name in ("reclip_inspect", "inspect_video"):
            return "🎬 Inspecting media details..."
        elif t_name in ("reclip_download_video", "download_video", "reclip_downloadvideo"):
            return "⚡ Fetching video in seconds..."
        elif t_name in ("reclip_clip_video", "clip_video"):
            st = args.get("start_time", "00:00")
            et = args.get("end_time", "")
            return f"✂️ Trimming video clip ({st} to {et})..."
        elif t_name in ("reclip_extract_audio", "extract_audio"):
            fmt = args.get("audio_format", "mp3").upper()
            return f"🎵 Extracting {fmt} audio track..."
        elif t_name == "convert_file":
            tgt = args.get("target_format", "").upper()
            return f"🔄 Converting file to {tgt}..."
        elif t_name == "calculator":
            expr = args.get("expression", "")
            return f"🧮 Calculating: <code>{expr}</code>"
        elif t_name == "compress_image":
            kb = args.get("target_kb", 100)
            return f"🖼️ Compressing image to {kb} KB..."
        return f"⚙️ Running tool: {tool_name}..."

    async def _execute_tool_with_progress(self, fn_name: str, fn_args: Any):
        import asyncio
        status_queue = asyncio.Queue()

        async def status_cb(msg: str):
            await status_queue.put(msg)

        task = asyncio.create_task(execute_tool(fn_name, fn_args, status_callback=status_cb))

        while not task.done():
            try:
                msg = await asyncio.wait_for(status_queue.get(), timeout=0.25)
                if msg:
                    yield ("status", msg, {"tool": fn_name})
            except asyncio.TimeoutError:
                pass

        tool_output = await task
        while not status_queue.empty():
            msg = status_queue.get_nowait()
            if msg:
                yield ("status", msg, {"tool": fn_name})

        yield ("result", tool_output, {})
