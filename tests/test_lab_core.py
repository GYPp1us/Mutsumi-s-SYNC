from src.mutsumi_sync.lab.core import LabSession, ScenarioStep, load_lab_config, load_scenario, resolve_config_path


def test_load_scenario_parses_steps(tmp_path):
    path = tmp_path / "scenario.yaml"
    path.write_text(
        """
name: demo
prompt: hello
steps:
  - text: hi
  - text: bye
""",
        encoding="utf-8",
    )

    scenario = load_scenario(str(path))
    assert scenario.name == "demo"
    assert scenario.prompt == "hello"
    assert scenario.steps == [ScenarioStep(text="hi"), ScenarioStep(text="bye")]


def test_lab_session_builds_fresh_paths(tmp_path):
    session = LabSession.create(seed_root=tmp_path / "seed", sessions_root=tmp_path / "sessions")
    assert session.session_root.parent.name == "sessions"
    assert session.session_root.exists()


def test_resolve_config_path_finds_existing_file(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("model:\n  reasoning_effort: max\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert resolve_config_path("config.yaml") == str(path)


def test_load_lab_config_overrides_reasoning_effort(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("model:\n  reasoning_effort: max\n", encoding="utf-8")

    config = load_lab_config(str(path), reasoning_effort="high")

    assert config.model.reasoning_effort == "high"
