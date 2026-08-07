"""Report generator skill for GPT Researcher.

This module provides the ReportGenerator class that handles report
writing, including introductions, conclusions, and subtopic management.
"""

import hashlib
import json
import os
from typing import Dict, Optional

from ..actions import (
    generate_draft_section_titles,
    generate_report,
    stream_output,
    write_conclusion,
    write_report_introduction,
)


_DEFAULT_REPORT_CONTEXT_CHARS = 50_000
_MAX_REPORT_CONTEXT_CHARS = 60_000
_MAX_REPORT_CONTEXT_BLOCK_CHARS = 18_000


def compact_report_context(
    context,
    research_sources=None,
    *,
    max_chars: int | None = None,
) -> str:
    """Bound final-writing context while preserving opened source evidence."""

    configured_limit = os.getenv("REPORT_CONTEXT_MAX_CHARS")
    if max_chars is None:
        try:
            max_chars = int(configured_limit) if configured_limit else _DEFAULT_REPORT_CONTEXT_CHARS
        except ValueError:
            max_chars = _DEFAULT_REPORT_CONTEXT_CHARS
    max_chars = min(_MAX_REPORT_CONTEXT_CHARS, max(8_000, max_chars))
    context_text = (
        "\n\n".join(str(item) for item in context)
        if isinstance(context, list)
        else str(context or "")
    )
    if len(context_text) <= max_chars:
        return context_text

    priority_blocks = []
    for source in research_sources or []:
        if not isinstance(source, dict):
            continue
        tool_name = str(source.get("tool_name") or "")
        normalized_tool = tool_name.split("__")[-1].split(".")[-1].split("/")[-1]
        if normalized_tool not in {"read_source", "verify_source_ref"}:
            continue
        content = str(source.get("content") or source.get("body") or "").strip()
        if not content:
            continue
        title = str(source.get("title") or normalized_tool)
        url = str(source.get("url") or source.get("href") or "")
        priority_blocks.append(f"Title: {title}\n{content}\nSource: {url}")

    if isinstance(context, list):
        general_blocks = [str(block).strip() for block in context if str(block).strip()]
    else:
        general_blocks = [
            block.strip()
            for block in context_text.split("\n\n---\n\n")
            if block.strip()
        ]
    selected = []
    seen = set()
    used_chars = 0
    for block in [*priority_blocks, *general_blocks]:
        digest = hashlib.sha256(block.encode("utf-8", errors="ignore")).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        bounded = block[:_MAX_REPORT_CONTEXT_BLOCK_CHARS]
        separator_chars = 7 if selected else 0
        remaining = max_chars - used_chars - separator_chars
        if remaining <= 0:
            break
        selected.append(bounded[:remaining])
        used_chars += len(selected[-1]) + separator_chars
    return "\n\n---\n\n".join(selected)
from ..utils.llm import construct_subtopics


class ReportGenerator:
    """Generates reports based on research data.

    This class handles all aspects of report generation including
    writing introductions, conclusions, and managing report structure.

    Attributes:
        researcher: The parent GPTResearcher instance.
        research_params: Dictionary of parameters for report generation.
    """

    def __init__(self, researcher):
        """Initialize the ReportGenerator.

        Args:
            researcher: The GPTResearcher instance that owns this generator.
        """
        self.researcher = researcher
        self.research_params = {
            "query": self.researcher.query,
            "agent_role_prompt": self.researcher.cfg.agent_role or self.researcher.role,
            "report_type": self.researcher.report_type,
            "report_source": self.researcher.report_source,
            "tone": self.researcher.tone,
            "websocket": self.researcher.websocket,
            "cfg": self.researcher.cfg,
            "headers": self.researcher.headers,
        }

    async def write_report(self, existing_headers: list = [], relevant_written_contents: list = [], ext_context=None, custom_prompt="", available_images: list = None) -> str:
        """
        Write a report based on existing headers and relevant contents.

        Args:
            existing_headers (list): List of existing headers.
            relevant_written_contents (list): List of relevant written contents.
            ext_context (Optional): External context, if any.
            custom_prompt (str): Custom prompt for the report.
            available_images (list): Pre-generated images available for embedding.

        Returns:
            str: The generated report.
        """
        available_images = available_images or []
        
        # send the selected images prior to writing report
        research_images = self.researcher.get_research_images()
        if research_images:
            await stream_output(
                "images",
                "selected_images",
                json.dumps(research_images),
                self.researcher.websocket,
                True,
                research_images
            )

        context = ext_context or self.researcher.context
        context = compact_report_context(
            context,
            self.researcher.get_research_sources(),
        )
        
        # Log image availability
        if available_images and self.researcher.verbose:
            await stream_output(
                "logs",
                "images_available",
                f"🖼️ {len(available_images)} pre-generated images available for embedding",
                self.researcher.websocket,
            )
        
        if self.researcher.verbose:
            await stream_output(
                "logs",
                "writing_report",
                f"✍️ Writing report for '{self.researcher.query}'...",
                self.researcher.websocket,
            )

        report_params = self.research_params.copy()
        if not report_params["agent_role_prompt"]:
            report_params["agent_role_prompt"] = self.researcher.cfg.agent_role or self.researcher.role
        report_params["context"] = context
        report_params["custom_prompt"] = custom_prompt
        report_params["available_images"] = available_images  # Pass pre-generated images

        if self.researcher.report_type == "subtopic_report":
            report_params.update({
                "main_topic": self.researcher.parent_query,
                "existing_headers": existing_headers,
                "relevant_written_contents": relevant_written_contents,
                "cost_callback": self.researcher.add_costs,
            })
        else:
            report_params["cost_callback"] = self.researcher.add_costs

        report = await generate_report(**report_params, **self.researcher.kwargs)

        if self.researcher.verbose:
            await stream_output(
                "logs",
                "report_written",
                f"📝 Report written for '{self.researcher.query}'",
                self.researcher.websocket,
            )

        return report

    async def write_report_conclusion(self, report_content: str) -> str:
        """
        Write the conclusion for the report.

        Args:
            report_content (str): The content of the report.

        Returns:
            str: The generated conclusion.
        """
        if self.researcher.verbose:
            await stream_output(
                "logs",
                "writing_conclusion",
                f"✍️ Writing conclusion for '{self.researcher.query}'...",
                self.researcher.websocket,
            )

        conclusion = await write_conclusion(
            query=self.researcher.query,
            context=report_content,
            config=self.researcher.cfg,
            agent_role_prompt=self.researcher.cfg.agent_role or self.researcher.role,
            cost_callback=self.researcher.add_costs,
            websocket=self.researcher.websocket,
            prompt_family=self.researcher.prompt_family,
            **self.researcher.kwargs
        )

        if self.researcher.verbose:
            await stream_output(
                "logs",
                "conclusion_written",
                f"📝 Conclusion written for '{self.researcher.query}'",
                self.researcher.websocket,
            )

        return conclusion

    async def write_introduction(self):
        """Write the introduction section of the report."""
        if self.researcher.verbose:
            await stream_output(
                "logs",
                "writing_introduction",
                f"✍️ Writing introduction for '{self.researcher.query}'...",
                self.researcher.websocket,
            )

        introduction = await write_report_introduction(
            query=self.researcher.query,
            context=self.researcher.context,
            agent_role_prompt=self.researcher.cfg.agent_role or self.researcher.role,
            config=self.researcher.cfg,
            websocket=self.researcher.websocket,
            cost_callback=self.researcher.add_costs,
            prompt_family=self.researcher.prompt_family,
            **self.researcher.kwargs
        )

        if self.researcher.verbose:
            await stream_output(
                "logs",
                "introduction_written",
                f"📝 Introduction written for '{self.researcher.query}'",
                self.researcher.websocket,
            )

        return introduction

    async def get_subtopics(self):
        """Retrieve subtopics for the research."""
        if self.researcher.verbose:
            await stream_output(
                "logs",
                "generating_subtopics",
                f"🌳 Generating subtopics for '{self.researcher.query}'...",
                self.researcher.websocket,
            )

        subtopics = await construct_subtopics(
            task=self.researcher.query,
            data=self.researcher.context,
            config=self.researcher.cfg,
            subtopics=self.researcher.subtopics,
            prompt_family=self.researcher.prompt_family,
            **self.researcher.kwargs
        )

        if self.researcher.verbose:
            await stream_output(
                "logs",
                "subtopics_generated",
                f"📊 Subtopics generated for '{self.researcher.query}'",
                self.researcher.websocket,
            )

        return subtopics

    async def get_draft_section_titles(self, current_subtopic: str):
        """Generate draft section titles for the report."""
        if self.researcher.verbose:
            await stream_output(
                "logs",
                "generating_draft_sections",
                f"📑 Generating draft section titles for '{self.researcher.query}'...",
                self.researcher.websocket,
            )

        draft_section_titles = await generate_draft_section_titles(
            query=self.researcher.query,
            current_subtopic=current_subtopic,
            context=self.researcher.context,
            role=self.researcher.cfg.agent_role or self.researcher.role,
            websocket=self.researcher.websocket,
            config=self.researcher.cfg,
            cost_callback=self.researcher.add_costs,
            prompt_family=self.researcher.prompt_family,
            **self.researcher.kwargs
        )

        if self.researcher.verbose:
            await stream_output(
                "logs",
                "draft_sections_generated",
                f"🗂️ Draft section titles generated for '{self.researcher.query}'",
                self.researcher.websocket,
            )

        return draft_section_titles
