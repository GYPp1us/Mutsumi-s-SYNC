import pytest
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
        orig_save = c.save
        called = False

        def fake_save():
            nonlocal called
            called = True

        object.__setattr__(c, "save", fake_save)
        try:
            result = await config_manager(
                {"action": "set", "key": "session.timeout", "value": 120},
                config=c,
            )
        finally:
            object.__setattr__(c, "save", orig_save)

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

    async def test_unknown_action(self):
        c = self.make_config()
        result = await config_manager({"action": "delete"}, config=c)
        assert result.startswith("[Error:")
