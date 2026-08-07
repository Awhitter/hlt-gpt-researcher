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
                    name.split("__")[-1].split(".")[-1].split("/")[-1]
                    for name in round_tool_names
                }
                if (
                    "search_source" in normalized_round_tools
                    and "read_source" not in normalized_round_tools
                ):
                    messages.append(HumanMessage(content=(
                        "You now have real repository search results. Open the most "
                        "relevant returned file paths with read_source before doing "
                        "more broad searching. Use bounded line windows around the "
                        "matches and cover distinct systems when the question spans "
                        "more than one repository."
                    )))
            
            logger.info(f"Research completed with {len(research_results)} total results")
            return research_results
            
        except Exception as e:
            logger.error(f"Error in LLM research with tools: {e}")
            return []

    def _process_tool_result(self, tool_name: str, result: Any) -> List[Dict[str, str]]:
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
                        return self._process_tool_result(tool_name, json.loads(candidate))
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
                            return self._process_tool_result(tool_name, nested)
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
