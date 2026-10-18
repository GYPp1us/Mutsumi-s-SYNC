import pytest
from src.mutsumi_sync.tools.registry import Tool, ToolRegistry


class TestToolRegistry:
    async def _echo(self, args: dict) -> str:
        return args.get("text", "no text")

    def test_register(self):
        r = ToolRegistry()
        r.register(Tool(
            name="echo",
            description="echo tool",
            parameters={"type": "object", "properties": {}},
            handler=self._echo,
        ))
        assert "echo" in r._tools

    async def test_execute(self):
        r = ToolRegistry()
        r.register(Tool(
            name="echo",
            description="echo tool",
            parameters={},
            handler=self._echo,
        ))
        result = await r.execute("echo", {"text": "hello"})
        assert result == "hello"

    async def test_execute_unknown(self):
        r = ToolRegistry()
        result = await r.execute("nonexistent")
        assert result.startswith("[Error: unknown tool")

    async def test_execute_default_args(self):
        r = ToolRegistry()
        r.register(Tool(
            name="echo",
            description="echo tool",
            parameters={},
            handler=self._echo,
        ))
        result = await r.execute("echo")
        assert result == "no text"

    def test_to_openai_schema(self):
        r = ToolRegistry()
        r.register(Tool(
            name="echo",
            description="echo test",
            parameters={"type": "object", "properties": {"text": {"type": "string"}}},
            handler=self._echo,
        ))
        schema = r.to_openai_schema()
        assert len(schema) == 1
        assert schema[0]["type"] == "function"
        assert schema[0]["function"]["name"] == "echo"
        assert schema[0]["function"]["description"] == "echo test"

    def test_snapshot(self):
        r = ToolRegistry()
        r.register(Tool(
            name="t1",
            description="",
            parameters={},
            handler=self._echo,
        ))
        tools, version = r.snapshot()
        assert len(tools) == 1
        assert version == 1

    def test_version_increments_on_mutation(self):
        r = ToolRegistry()
        assert r.version == 0

        r.register(Tool(
            name="t1",
            description="",
            parameters={},
            handler=self._echo,
        ))
        first_version = r.version
        assert first_version == 1

        r.register(Tool(
            name="t2",
            description="",
            parameters={},
            handler=self._echo,
        ))
        assert r.version == 2

        r.remove("t1")
        assert r.version == 3

    async def test_execute_error(self):
        async def bad_handler(args: dict) -> str:
            raise ValueError("test error")

        r = ToolRegistry()
        r.register(Tool(
            name="bad",
            description="",
            parameters={},
            handler=bad_handler,
        ))
        result = await r.execute("bad", {})
        assert "[Error:" in result
