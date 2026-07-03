import pytest
from src.mutsumi_sync.config import Config, NapcatConfig, ModelConfig, ContextConfig, SessionConfig, MemoryConfig, SummarizerConfig


class TestConfig:
    def test_defaults(self):
        c = Config()
        assert c.napcat.ws_url == "ws://localhost:3000"
        assert c.model.model == "deepseek-chat"
        assert c.context.window_max_tokens == 100000
        assert c.session.timeout == 300
        assert c.render.markdown_image.enabled is False
        assert c.render.markdown_image.node_path == "node"
        assert c.render.markdown_image.output_dir == "data/generated/markdown"
        assert c.dirty is False

    def test_load_missing_file(self):
        c = Config.load("nonexistent.yaml")
        assert isinstance(c, Config)
        assert c._config_path is not None

    def test_load_render_config(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(
            "render:\n"
            "  markdown_image:\n"
            "    enabled: true\n"
            "    node_path: node-custom\n"
            "    output_dir: out/markdown\n",
            encoding="utf-8",
        )

        c = Config.load(str(path))

        assert c.render.markdown_image.enabled is True
        assert c.render.markdown_image.node_path == "node-custom"
        assert c.render.markdown_image.output_dir == "out/markdown"

    def test_set_simple(self):
        c = Config()
        result = c.set("session.timeout", 60)
        assert result.startswith("[OK]")
        assert c.session.timeout == 60
        assert c.dirty is True

    def test_set_dot_path(self):
        c = Config()
        c.set("model.temperature", 0.5)
        assert c.model.temperature == 0.5

    def test_set_nested_model(self):
        c = Config()
        c.set("napcat.ws_url", "ws://example.com:3001")
        assert c.napcat.ws_url == "ws://example.com:3001"

    def test_set_invalid_key(self):
        c = Config()
        result = c.set("invalid.key", 1)
        assert result.startswith("[Error: unknown config key")

    def test_set_type_coercion_int(self):
        c = Config()
        c.set("context.window_max_tokens", "30000")
        assert c.context.window_max_tokens == 30000

    def test_set_type_coercion_float(self):
        c = Config()
        c.set("model.temperature", "0.3")
        assert c.model.temperature == 0.3

    def test_get_value(self):
        c = Config()
        assert c.get("model.temperature") == 0.7
        assert c.get("context.window_max_tokens") == 100000

    def test_get_nonexistent(self):
        c = Config()
        result = c.get("nonexistent")
        assert isinstance(result, str) and "Error" in result

    def test_reload_no_file(self):
        c = Config()
        result = c.reload()
        assert "no config file" in result.lower()


class TestModelDefaults:
    def test_napcat_defaults(self):
        n = NapcatConfig()
        assert n.ws_url == "ws://localhost:3000"
        assert n.http_url == "http://localhost:3000"

    def test_model_defaults(self):
        m = ModelConfig()
        assert m.provider == "deepseek"
        assert m.model == "deepseek-chat"

    def test_context_defaults(self):
        c = ContextConfig()
        assert c.window_max_tokens == 100000
        assert c.window_min_tokens == 50000
        assert c.max_tokens == 4096
        assert c.summaries_max_count == 180
        assert c.summaries_min_count == 90
        assert c.debounce_timeout == 1.5

    def test_session_defaults(self):
        s = SessionConfig()
        assert s.timeout == 300
