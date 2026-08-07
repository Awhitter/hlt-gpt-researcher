"""
MCP Research Execution Skill

Handles research execution using selected MCP tools as a skill component.
"""
import asyncio
import json
import logging
from typing import List, Dict, Any

from langchain_core.messages import HumanMessage, ToolMessage

logger = logging.getLogger(__name__)
MAX_TOOL_RESULT_NESTING_DEPTH = 4
MAX_AUTO_OPENED_SOURCES = 2


class MCPResearchSkill:
    """
    Handles research execution using selected MCP tools.
    
    Responsible for:
    - Executing research with LLM and bound tools
    - Processing tool results into standard format
    - Managing tool execution and error handling
    """

    def __init__(self, cfg, researcher=None):
        """
        Initialize the MCP research skill.
        
        Args:
            cfg: Configuration object with LLM settings
            researcher: Researcher instance for cost tracking
        """
        self.cfg = cfg
        self.researcher = researcher

    async def conduct_research_with_tools(self, query: str, selected_tools: List) -> List[Dict[str, str]]:
        """
        Use LLM with bound tools to conduct intelligent research.
        
        Args:
            query: Research query
            selected_tools: List of selected MCP tools
            
        Returns:
            List[Dict[str, str]]: Research results in standard format
        """
        if not selected_tools:
            logger.warning("No tools available for research")
            return []
            
        logger.info(f"Conducting research using {len(selected_tools)} selected tools")
        
        try:
            from ..llm_provider.generic.base import GenericLLMProvider
            
            # Create LLM provider using the config
            provider_kwargs = {
                'model': self.cfg.strategic_llm_model,
                **self.cfg.llm_kwargs
            }
            
            llm_provider = GenericLLMProvider.from_provider(
                self.cfg.strategic_llm_provider, 
                **provider_kwargs
            )
            
            # Bind tools to LLM
            llm_with_tools = llm_provider.llm.bind_tools(selected_tools)
            
            # Import here to avoid circular imports
            from ..prompts import PromptFamily
            
            # Create research prompt
            research_prompt = PromptFamily.generate_mcp_research_prompt(query, selected_tools)

            messages = [HumanMessage(content=research_prompt)]
            research_results = []
            search_read_guidance_sent = False
            discovered_source_matches: list[dict[str, Any]] = []
            auto_opened_source_keys: set[tuple[str, str]] = set()

            # Tool results have to go back to the model. A single tool-calling
            # turn cannot discover a path and then inspect that discovered path;
            # it can only guess the second call's arguments. Keep the loop
            # deliberately bounded while allowing search -> read -> verify.
            max_tool_rounds = 4
            for round_number in range(1, max_tool_rounds + 1):
                logger.info(
                    "LLM researching with bound tools (round %s/%s)...",
                    round_number,
                    max_tool_rounds,
                )
                response = await llm_with_tools.ainvoke(messages)
                tool_calls = list(getattr(response, "tool_calls", []) or [])
                if not tool_calls:
                    content = getattr(response, "content", "")
                    if content:
                        analysis_text = content if isinstance(content, str) else str(content)
                        research_results.append({
                            "title": f"LLM Analysis: {query}",
                            "href": "mcp://llm_analysis",
                            "body": analysis_text,
                        })
                        logger.info("Added final LLM analysis to results")
                    break

                logger.info(
                    "LLM made %s tool calls in round %s",
                    len(tool_calls),
                    round_number,
                )
                messages.append(response)
                round_tool_names = []
                for i, tool_call in enumerate(tool_calls, 1):
                    tool_name = tool_call.get("name", "unknown")
                    round_tool_names.append(tool_name)
                    tool_args = tool_call.get("args", {})
                    tool_call_id = tool_call.get("id") or f"mcp-{round_number}-{i}"
                    
                    logger.info(f"Executing tool {i}/{len(response.tool_calls)}: {tool_name}")
                    
                    # Log the tool arguments for transparency
                    if tool_args:
                        args_str = ", ".join([f"{k}={v}" for k, v in tool_args.items()])
                        logger.debug(f"Tool arguments: {args_str}")
                    
                    tool_result: Any = f"Tool {tool_name} was unavailable."
                    try:
                        # Find the tool by name
                        tool = next((t for t in selected_tools if t.name == tool_name), None)
                        if not tool:
                            logger.warning(f"Tool {tool_name} not found in selected tools")
                            messages.append(
                                ToolMessage(
                                    content=str(tool_result),
                                    tool_call_id=tool_call_id,
                                )
                            )
                            continue
                        
                        # Execute the tool
                        if hasattr(tool, 'ainvoke'):
                            result = await tool.ainvoke(tool_args)
                        elif hasattr(tool, 'invoke'):
                            result = tool.invoke(tool_args)
                        else:
                            result = await tool(tool_args) if asyncio.iscoroutinefunction(tool) else tool(tool_args)
                        tool_result = result
                        
                        # Log the actual tool response for debugging
                        if result:
                            result_preview = str(result)[:500] + "..." if len(str(result)) > 500 else str(result)
                            logger.debug(f"Tool {tool_name} response preview: {result_preview}")
                            
                            # Process the result
                            formatted_results = self._process_tool_result(tool_name, result)
                            research_results.extend(formatted_results)
                            if self._normalized_tool_name(tool_name) == "search_source":
                                discovered_source_matches.extend(
                                    self._extract_source_matches(result)
                                )
                            logger.info(f"Tool {tool_name} returned {len(formatted_results)} formatted results")
                            
                            # Log details of each formatted result
                            for j, formatted_result in enumerate(formatted_results):
                                title = formatted_result.get("title", "No title")
                                content_preview = formatted_result.get("body", "")[:200] + "..." if len(formatted_result.get("body", "")) > 200 else formatted_result.get("body", "")
                                logger.debug(f"Result {j+1}: '{title}' - Content: {content_preview}")
                        else:
                            logger.warning(f"Tool {tool_name} returned empty result")
                        messages.append(
                            ToolMessage(
                                content=str(tool_result),
                                tool_call_id=tool_call_id,
                            )
                        )
                    except Exception as e:
                        logger.error(f"Error executing tool {tool_name}: {e}")
                        messages.append(
                            ToolMessage(
                                content=f"Tool {tool_name} failed: {e}",
                                tool_call_id=tool_call_id,
                            )
                        )

                normalized_round_tools = {
                    self._normalized_tool_name(name)
                    for name in round_tool_names
                }
                if (
                    "search_source" in normalized_round_tools
                    and "read_source" not in normalized_round_tools
                ):
                    if search_read_guidance_sent and bool(
                        getattr(self.researcher, "mcp_only", False)
                    ):
                        auto_opened = await self._auto_open_discovered_sources(
                            discovered_source_matches,
                            selected_tools,
                            auto_opened_source_keys,
                        )
                        if auto_opened:
                            research_results.extend(auto_opened)
                            excerpts = []
                            for opened in auto_opened:
                                excerpts.append(
                                    f"{opened.get('href', '')}\n"
                                    f"{str(opened.get('body', ''))[:4000]}"
                                )
                            messages.append(HumanMessage(content=(
                                "You repeated repository search without opening a "
                                "returned path, so the runtime opened the strongest "
                                "distinct matches for you. Use this exact file "
                                "evidence now; only search again for a genuinely "
                                "different missing workflow stage.\n\n"
                                + "\n\n".join(excerpts)
                            )))
                    else:
                        messages.append(HumanMessage(content=(
                            "You now have real repository search results. Open the most "
                            "relevant returned file paths with read_source before doing "
                            "more broad searching. Use bounded line windows around the "
                            "matches and cover distinct systems when the question spans "
                            "more than one repository."
                        )))
                        search_read_guidance_sent = True
            
            logger.info(f"Research completed with {len(research_results)} total results")
            return research_results
            
        except Exception as e:
            logger.error(f"Error in LLM research with tools: {e}")
            return []

    @staticmethod
    def _normalized_tool_name(tool_name: Any) -> str:
        return str(tool_name or "").split("__")[-1].split(".")[-1].split("/")[-1]

    @classmethod
    def _extract_source_matches(
        cls,
        value: Any,
        *,
        depth: int = 0,
    ) -> list[dict[str, Any]]:
        """Recover concrete repo/path/line matches from search_source payloads."""

        if depth > MAX_TOOL_RESULT_NESTING_DEPTH:
            return []
        if isinstance(value, str):
            candidate = value.strip()
            if not candidate.startswith(("{", "[")):
                return []
            try:
                return cls._extract_source_matches(
                    json.loads(candidate),
                    depth=depth + 1,
                )
            except (json.JSONDecodeError, TypeError, ValueError):
                return []
        if isinstance(value, list):
            matches = []
            for item in value:
                matches.extend(cls._extract_source_matches(item, depth=depth + 1))
            return matches
        if not isinstance(value, dict):
            return []

        matches = []
        if value.get("repo") and value.get("path"):
            matches.append(value)
        for key in (
            "matches",
            "results",
            "structured_content",
            "content",
            "text",
            "body",
        ):
            if key in value:
                matches.extend(
                    cls._extract_source_matches(value[key], depth=depth + 1)
                )
        return matches

    async def _auto_open_discovered_sources(
        self,
        matches: list[dict[str, Any]],
        selected_tools: List,
        opened_keys: set[tuple[str, str]],
    ) -> list[dict[str, str]]:
        """Open high-value search matches when the model stalls at discovery."""

        read_tool = next(
            (
                tool
                for tool in selected_tools
                if self._normalized_tool_name(getattr(tool, "name", ""))
                == "read_source"
            ),
            None,
        )
        if read_tool is None or len(opened_keys) >= MAX_AUTO_OPENED_SOURCES:
            return []

        ranked = sorted(
            matches,
            key=lambda item: (
                -int(item.get("authority") or 0),
                -int(item.get("score") or 0),
                str(item.get("repo") or ""),
                str(item.get("path") or ""),
                int(item.get("line") or 1),
            ),
        )
        opened_results: list[dict[str, str]] = []
        for match in ranked:
            repo = str(match.get("repo") or "").strip()
            path = str(match.get("path") or "").strip()
            if not repo or not path:
                continue
            key = (repo.lower(), path)
            if key in opened_keys:
                continue
            line = max(1, int(match.get("line") or 1))
            args = {
                "repo": repo,
                "path": path,
                "start_line": max(1, line - 30),
                "end_line": line + 60,
            }
            try:
                if hasattr(read_tool, "ainvoke"):
                    result = await read_tool.ainvoke(args)
                elif hasattr(read_tool, "invoke"):
                    result = read_tool.invoke(args)
                else:
                    result = (
                        await read_tool(args)
                        if asyncio.iscoroutinefunction(read_tool)
                        else read_tool(args)
                    )
            except Exception as error:
                logger.error("Automatic read_source failed for %s/%s: %s", repo, path, error)
                continue
            opened_keys.add(key)
            opened_results.extend(
                self._process_tool_result(getattr(read_tool, "name", "read_source"), result)
            )
            if len(opened_keys) >= MAX_AUTO_OPENED_SOURCES:
                break
        if opened_results:
            logger.info(
                "Automatically opened %s repository source(s) after repeated search",
                len(opened_results),
            )
        return opened_results

    def _process_tool_result(
        self,
        tool_name: str,
        result: Any,
        *,
        depth: int = 0,
    ) -> List[Dict[str, str]]:
        """
        Process tool result into search result format.
        
        Args:
            tool_name: Name of the tool that produced the result
            result: The tool result
            
        Returns:
            List[Dict[str, str]]: Formatted search results
        """
        search_results = []

        def formatted_result(
            *,
            title: Any,
            href: Any,
            body: Any,
        ) -> Dict[str, str]:
            if not isinstance(body, str):
                body = json.dumps(body, default=str)
            return {
                "title": str(title or f"Result from {tool_name}"),
                "href": str(href or f"mcp://{tool_name}"),
                "body": body,
                # Preserve provenance so delivery grounding can distinguish a
                # file that was actually opened from a broad search result.
                "tool_name": tool_name,
            }
        
        try:
            # Some MCP adapters return their structured payload as JSON text.
            # Decode that shape before formatting it so exact read_source URLs
            # and file metadata are not buried in an opaque body string.
            if isinstance(result, str):
                candidate = result.strip()
                if candidate.startswith(("{", "[")):
                    try:
                        parsed = json.loads(candidate)
                        if depth < MAX_TOOL_RESULT_NESTING_DEPTH:
                            return self._process_tool_result(
                                tool_name,
                                parsed,
                                depth=depth + 1,
                            )
                    except (json.JSONDecodeError, TypeError):
                        pass

            # 1) First: handle MCP result wrapper with structured_content/content
            if isinstance(result, dict) and (
                "structured_content" in result
                or isinstance(result.get("content"), list)
            ):
                search_results = []
                # Prefer structured_content when present
                structured = result.get("structured_content")
                if isinstance(structured, dict):
                    items = structured.get("results")
                    if isinstance(items, list):
                        for i, item in enumerate(items):
                            if isinstance(item, dict):
                                search_results.append(formatted_result(
                                    title=item.get("title", f"Result from {tool_name} #{i+1}"),
                                    href=item.get("href", item.get("url", f"mcp://{tool_name}/{i}")),
                                    body=item.get("body", item.get("content", item)),
                                ))
                    # If no items array but structured is dict, treat as single
                    elif isinstance(structured, dict):
                        search_results.append(formatted_result(
                            title=structured.get("title", f"Result from {tool_name}"),
                            href=structured.get("href", structured.get("url", f"mcp://{tool_name}")),
                            body=structured.get("body", structured.get("content", structured)),
                        ))
                # Fallback to content if provided (MCP spec: list of {type: text, text: ...})
                if not search_results:
                    content_field = result.get("content")
                    if isinstance(content_field, list):
                        texts = []
                        for part in content_field:
                            if isinstance(part, dict):
                                if part.get("type") == "text" and isinstance(part.get("text"), str):
                                    texts.append(part["text"])
                                elif "text" in part:
                                    texts.append(str(part.get("text")))
                                else:
                                    # unknown piece; stringify
                                    texts.append(str(part))
                            else:
                                texts.append(str(part))
                        body_text = "\n\n".join([t for t in texts if t])
                    elif isinstance(content_field, str):
                        body_text = content_field
                    else:
                        body_text = str(result)
                    nested_candidate = body_text.strip()
                    if nested_candidate.startswith(("{", "[")):
                        try:
                            nested = json.loads(nested_candidate)
                            if (
                                isinstance(nested, dict)
                                and nested.get("type") == "text"
                                and isinstance(nested.get("text"), str)
                            ):
                                nested = json.loads(nested["text"])
                            if depth < MAX_TOOL_RESULT_NESTING_DEPTH:
                                return self._process_tool_result(
                                    tool_name,
                                    nested,
                                    depth=depth + 1,
                                )
                        except (json.JSONDecodeError, TypeError, ValueError):
                            pass
                    search_results.append(formatted_result(
                        title=f"Result from {tool_name}",
                        href=f"mcp://{tool_name}",
                        body=body_text,
                    ))
                return search_results

            # 2) If the result is already a list, process each item normally
            if isinstance(result, list):
                # If the result is already a list, process each item
                for i, item in enumerate(result):
                    if isinstance(item, dict):
                        # Use the item as is if it has required fields
                        if "title" in item and ("content" in item or "body" in item):
                            search_result = formatted_result(
                                title=item.get("title", ""),
                                href=item.get("href", item.get("url", f"mcp://{tool_name}/{i}")),
                                body=item.get("body", item.get("content", item)),
                            )
                            search_results.append(search_result)
                        else:
                            # Create a search result with a generic title
                            search_result = formatted_result(
                                title=f"Result from {tool_name}",
                                href=f"mcp://{tool_name}/{i}",
                                body=item,
                            )
                            search_results.append(search_result)
            # 3) If the result is a dict (non-MCP wrapper), use it as a single search result
            elif isinstance(result, dict):
                items = result.get("results")
                if isinstance(items, list):
                    for i, item in enumerate(items):
                        if not isinstance(item, dict):
                            continue
                        search_results.append(formatted_result(
                            title=item.get("title", f"Result from {tool_name} #{i+1}"),
                            href=item.get("href", item.get("url", f"mcp://{tool_name}/{i}")),
                            body=item.get("body", item.get("content", item)),
                        ))
                else:
                    search_results.append(formatted_result(
                        title=result.get("title", f"Result from {tool_name}"),
                        href=result.get("href", result.get("url", f"mcp://{tool_name}")),
                        body=result.get("body", result.get("content", result)),
                    ))
            else:
                # For any other type, convert to string and use as a single search result
                search_result = formatted_result(
                    title=f"Result from {tool_name}",
                    href=f"mcp://{tool_name}",
                    body=result,
                )
                search_results.append(search_result)
                
        except Exception as e:
            logger.error(f"Error processing tool result from {tool_name}: {e}")
            # Fallback: create a basic result
            search_result = formatted_result(
                title=f"Result from {tool_name}",
                href=f"mcp://{tool_name}",
                body=result,
            )
            search_results.append(search_result)
        
        return search_results
