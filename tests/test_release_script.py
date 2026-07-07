from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release_to_production.ps1"


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
