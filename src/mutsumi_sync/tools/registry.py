from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger("mutsumi.tools.registry")


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict  # JSON Schema
    handler: Callable[..., Awaitable[str]]
    source: str = "builtin"


@dataclass
class ToolRegistry:
    _tools: dict[str, Tool] = field(default_factory=dict)

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool
        logger.info("Registered tool: %s", tool.name)

    async def execute(self, name: str, args: dict | None = None, **deps) -> str:
        args = args or {}
        tool = self._tools.get(name)
        if tool is None:
            return f"[Error: unknown tool: {name}]"
        try:
            sig = inspect.signature(tool.handler)
            accepts_deps = any(
                p.kind == inspect.Parameter.VAR_KEYWORD
                for p in sig.parameters.values()
            )
            if accepts_deps:
                result = await tool.handler(args, **deps)
            else:
                result = await tool.handler(args)
            return str(result)
        except Exception as e:
            logger.exception("Tool %s failed", name)
            return f"[Error: {e}]"

    def to_openai_schema(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in self._tools.values()
        ]

    def snapshot(self) -> tuple[list[Tool], int]:
        return list(self._tools.values()), 0
