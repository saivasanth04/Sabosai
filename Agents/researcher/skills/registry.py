from pathlib import Path


SKILLS_DIR = Path(__file__).resolve().parent
SKILLS = frozenset({"mcp-duckgo", "crawl4ai"})


class SkillRegistry:
    def load(self, skill_name: str) -> str:
        if skill_name not in SKILLS:
            raise FileNotFoundError(f"Unknown research skill: {skill_name}")
        return (SKILLS_DIR / skill_name / "SKILL.md").read_text(encoding="utf-8")
