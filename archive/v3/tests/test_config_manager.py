import pytest
import yaml
from src.mutsumi_sync.config import Config
from src.mutsumi_sync.tools.config_manager import config_manager


class TestConfigManager:
    def make_config(self, path: str = "/tmp/test_config.yaml") -> Config:
        c = Config()
        object.__setattr__(c, "_config_path", path)
        return c

    async def test_get(self):
        c = self.make_config()
        result = await config_manager({"action": "get", "key": "model.temperature"}, config=c)
        assert "0.7" in result

    async def test_get_missing_key(self):
        c = self.make_config()
        result = await config_manager({"action": "get", "key": ""}, config=c)
        assert result.startswith("[Error:")

    async def test_set(self):
        c = self.make_config()
        orig_save_key = c.save_key
        called = False

        def fake_save_key(key: str):
            nonlocal called
            called = True
            assert key == "session.timeout"

        object.__setattr__(c, "save_key", fake_save_key)
        try:
            result = await config_manager(
                {"action": "set", "key": "session.timeout", "value": 120},
                config=c,
            )
        finally:
            object.__setattr__(c, "save_key", orig_save_key)

        assert result.startswith("[OK]")
        assert c.session.timeout == 120
        assert c.dirty is True
        assert called is True

    async def test_set_missing_value(self):
        c = self.make_config()
        result = await config_manager({"action": "set", "key": "foo"}, config=c)
        assert result.startswith("[Error:")

    async def test_list(self):
        c = self.make_config()
        result = await config_manager({"action": "list"}, config=c)
        assert "napcat" in result
        assert "model" in result
        assert "dirty" not in result

    async def test_reload_no_file(self):
        c = self.make_config(path="")
        result = await config_manager({"action": "reload"}, config=c)
        assert "Error" in result

    async def test_reload_preserves_deep_model_types(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(
            "render:\n"
            "  markdown_image:\n"
            "    enabled: true\n"
            "    node_path: custom-node\n",
            encoding="utf-8",
        )
        c = Config.load(str(path))
        c.render.markdown_image.enabled = False

        result = await config_manager({"action": "reload"}, config=c)

        assert result.startswith("[OK]")
        assert c.render.markdown_image.enabled is True
        assert c.render.markdown_image.node_path == "custom-node"
        assert c.render.markdown_image.__class__.__name__ == "MarkdownImageRenderConfig"

    async def test_unknown_action(self):
        c = self.make_config()
        result = await config_manager({"action": "delete"}, config=c)
        assert result.startswith("[Error:")

    async def test_set_updates_only_target_yaml_line(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(
            "# keep this comment\n"
            "model:\n"
            "  provider: deepseek\n"
            "  temperature: 0.7\n"
            "napcat:\n"
            "  ws_url: ws://example\n",
            encoding="utf-8",
        )
        c = Config.load(str(path))

        result = await config_manager(
            {"action": "set", "key": "model.temperature", "value": "0.2"},
            config=c,
        )

        assert result.startswith("[OK]")
        saved = path.read_text(encoding="utf-8")
        assert saved == (
            "# keep this comment\n"
            "model:\n"
            "  provider: deepseek\n"
            "  temperature: 0.2\n"
            "napcat:\n"
            "  ws_url: ws://example\n"
        )
        assert yaml.safe_load(saved)["model"]["temperature"] == 0.2

    async def test_set_updates_arbitrary_depth_without_reordering(self, tmp_path):
        path = tmp_path / "config.yaml"
        original = (
            "# renderer settings\n"
            "render:\n"
            "  markdown_image:\n"
            "    enabled: false  # keep inline comment\n"
            "    node_path: node\n"
            "model:\n"
            "  model: deepseek-chat\n"
        )
        path.write_text(original, encoding="utf-8")
        c = Config.load(str(path))

        result = await config_manager(
            {"action": "set", "key": "render.markdown_image.enabled", "value": "true"},
            config=c,
        )

        assert result.startswith("[OK]")
        saved = path.read_text(encoding="utf-8")
        assert saved == original.replace("enabled: false", "enabled: true")
        assert yaml.safe_load(saved)["render"]["markdown_image"]["enabled"] is True
