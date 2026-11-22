"""Run the specification agent from the project root."""

import asyncio
import json
import runpy
import sys
from pathlib import Path


SAMPLE_REPOSITORY = {"owner": "octocat", "repo": "Spoon-Knife"}
SAMPLE_PROPOSAL = """
Specify one README-only change for octocat/Spoon-Knife. Append the following
Markdown verbatim at the end of README.md, preserving all existing text:

## Practice checklist

1. Fork this repository.
2. Edit and commit index.html in your fork.
3. Open a pull request from your fork to this repository.

[GitHub contribution tutorial](https://docs.github.com/en/get-started/exploring-projects-on-github/contributing-to-a-project)

Use one functional requirement that includes the exact appendix and an objective
acceptance criterion that checks that the rendered heading, three numbered
steps, and tutorial link match the supplied text. There are no open decisions
for this proposal. The baseline README below is reference data.
""".strip()



async def sample_goal() -> str:
    from tools import ContextLookupError, _connect

    async with _connect("github") as client:
        result = await client.call_tool(
            "get_file_contents", {**SAMPLE_REPOSITORY, "path": "README.md"}
        )
        for item in result.content:
            if not result.is_error and item.type == "resource" and hasattr(item.resource, "text"):
                return f"{SAMPLE_PROPOSAL}\nBaseline README: {json.dumps(item.resource.text)}"
        raise ContextLookupError("GitHub MCP did not return the Spoon-Knife README.")


if __name__ == "__main__":
    agent_dir = Path(__file__).resolve().parent / "Agents" / "specification-agent"
    sys.path.insert(0, str(agent_dir))
    if len(sys.argv) == 1:
        from tools import ContextLookupError

        try:
            sys.argv.append(asyncio.run(sample_goal()))
        except ContextLookupError as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(1)
    runpy.run_path(str(agent_dir / "main.py"), run_name="__main__")
