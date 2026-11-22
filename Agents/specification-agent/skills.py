from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent / ".agents" / "skills"


def load_skill(name: str) -> str:
    if not name or Path(name).parts != (name,) or name in {".", ".."}:
        raise ValueError("Invalid skill path")
    for directory in (SKILLS_DIR / name, SKILLS_DIR / name / name):
        path = (directory / "SKILL.md").resolve()
        if not path.is_relative_to(SKILLS_DIR.resolve()):
            raise ValueError("Invalid skill path")
        if path.is_file():
            return path.read_text(encoding="utf-8-sig")
    raise FileNotFoundError(f"Missing skill: {name}")
