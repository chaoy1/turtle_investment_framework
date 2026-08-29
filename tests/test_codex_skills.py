"""Contract tests for the repository-level Codex/GPT skills."""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / ".agents" / "skills"


EXPECTED_SKILLS = {
    "turtle-analysis": "strategies/turtle/coordinator.md",
    "business-analysis": "shared/qualitative/coordinator.md",
    "valuation": "strategies/valuation/coordinator.md",
    "download-annual-report": "scripts/download_report.py",
}


def test_all_codex_skills_have_frontmatter_and_entrypoint():
    for name, entrypoint in EXPECTED_SKILLS.items():
        path = SKILLS / name / "SKILL.md"
        assert path.is_file(), f"Missing Codex skill: {name}"
        text = path.read_text(encoding="utf-8")
        assert text.startswith("---\n")
        assert f"name: {name}" in text
        assert "description:" in text
        assert entrypoint in text


def test_active_runtime_config_uses_openai_names():
    for relative in ("AGENTS.md", ".env.sample", "init.sh", "init.ps1"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "DEEPSEEK_API_KEY" not in text
        assert "OPENAI_API_KEY" in text


def test_codex_skill_directory_is_not_gitignored():
    text = (ROOT / ".gitignore").read_text(encoding="utf-8")
    active_rules = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert ".agents/" not in active_rules

