from pathlib import Path
import subprocess
import sys

import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release_to_production.ps1"
PROMPT_SYNC = ROOT / "scripts" / "sync_system_prompts.py"


def test_release_script_contains_operator_safety_contract() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    required_tokens = [
        "param(",
        "[switch]$DryRun",
        "[switch]$SkipDeploy",
        "[switch]$SkipPr",
        "git status --porcelain",
        "python -m pytest tests/ -q",
        "npm run check",
        "gh pr create",
        "gh pr merge",
        "git archive --format=tar",
        "scp",
        "root@arcol.site",
        "/opt/mutsumi-sync-v3/releases",
        "/opt/mutsumi-sync-v3/shared/config.yaml",
        "/opt/mutsumi-sync-v3/shared/system-prompts.yaml",
        "/opt/mutsumi-sync-v3/shared/data",
        "mutsumi-sync-v3.service",
        "journalctl -u mutsumi-sync-v3.service",
        "logging.stream_store.path",
    ]

    for token in required_tokens:
        assert token in source


def test_release_script_documents_dry_run_usage() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert ".\\scripts\\release_to_production.ps1 -DryRun" in source
    assert "DryRun" in source


def test_release_script_normalizes_git_status_before_counting() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "function Get-LineCount" in source
    assert "if ($null -eq $Lines)" in source
    assert "Get-LineCount $StatusLines" in source
    assert "Get-LineCount $InitialStatus" in source
    assert "Get-LineCount $postCommitStatus" in source
    assert "$StatusLines.Count" not in source
    assert "$InitialStatus.Count" not in source
    assert "$postCommitStatus.Count" not in source


def test_release_script_lists_prs_before_create() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "gh pr list --head $CurrentBranch" in source
    assert "gh pr view $CurrentBranch" not in source


def test_release_script_keeps_command_output_out_of_function_returns() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "$output = & $Command 2>&1" in source
    assert "foreach ($line in @($output))" in source
    assert "Write-Host $line" in source
    assert "$createdPrOutput = (& gh pr create" in source


def test_release_script_synchronizes_prompts_and_migrates_renderer_timeout() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "scripts/sync_system_prompts.py" in source
    assert 'config.set("render.markdown_image.timeout_seconds", 60)' in source
    assert "if current <= 20" in source


def test_prompt_sync_preserves_persona_and_replaces_all_operational_prompts(tmp_path) -> None:
    shared = tmp_path / "shared.yaml"
    release = tmp_path / "release.yaml"
    shared.write_text(
        yaml.safe_dump({
            "persona": "生产人格",
            "runtime": "含 Priority Override 和 bot_state 的旧规则",
            "message_summary": "old-message",
            "summary_merge": "old-merge",
            "episode_summary": "old-episode",
            "operator_note": "keep-me",
        }, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    release_data = {
        "persona": "release-default",
        "runtime": "new-runtime",
        "message_summary": "new-message",
        "summary_merge": "new-merge",
        "episode_summary": "new-episode",
        "heartbeat": "new-heartbeat",
    }
    release.write_text(
        yaml.safe_dump(release_data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    subprocess.run(
        [sys.executable, str(PROMPT_SYNC), str(shared), str(release), "test-release"],
        check=True,
        capture_output=True,
        text=True,
    )

    migrated = yaml.safe_load(shared.read_text(encoding="utf-8"))
    assert migrated["persona"] == "生产人格"
    for key in ("runtime", "message_summary", "summary_merge", "episode_summary", "heartbeat"):
        assert migrated[key] == release_data[key]
    assert migrated["operator_note"] == "keep-me"
    assert (tmp_path / "shared.yaml.bak-test-release").exists()
