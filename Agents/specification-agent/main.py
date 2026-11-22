"""Command-line entry point for the specification agent."""

import argparse
import json
import sys

from openai import APIConnectionError, APIStatusError
from pydantic import ValidationError

from gateway import GATEWAY_URL
from specification_agent import specification_agent
from tools import ContextLookupError

DEFAULT_GOAL = (
    "Specify one capability for an existing employee directory. A signed-in "
    "administrator can change an existing employee's display name. The administrator "
    "selects the employee from the existing directory, enters a new display name, "
    "and saves it. Trim surrounding whitespace. If the name is empty after trimming, "
    "show 'Name is required' and keep the previous name. Otherwise save the new "
    "name and show it in the directory. Users without administrator access cannot "
    "change names. Existing sign-in, permissions, and directory navigation are "
    "outside this change. Write only the requirements needed for this capability."
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a reviewed requirements specification.")
    parser.add_argument("goal", nargs="?", default=DEFAULT_GOAL)
    parser.add_argument("--workspace", help="Absolute path within GoModel's filesystem MCP root")
    parser.add_argument("--repository-url", help="HTTPS GitHub repository URL")
    parser.add_argument("--documentation-library", help="Context7 library name or exact ID")
    parser.add_argument("--documentation-question", help="Question for Context7 (defaults to the goal)")
    args = parser.parse_args()
    if args.documentation_question and not args.documentation_library:
        parser.error("--documentation-question requires --documentation-library")

    try:
        result = specification_agent.invoke({
            key: value for key, value in vars(args).items() if value is not None
        })
    except APIConnectionError:
        print(f"Could not reach GoModel at {GATEWAY_URL}. Start it from the project root.", file=sys.stderr)
        return 1
    except APIStatusError as exc:
        print(f"GoModel returned HTTP {exc.status_code}. Check the gateway key and model routing.", file=sys.stderr)
        return 1
    except ValidationError:
        print("GoModel returned data that did not match the expected specification schema.", file=sys.stderr)
        return 1
    except (ContextLookupError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    output = {key: result[key] for key in ("status", "spec", "questions", "errors")}
    print(json.dumps(output, indent=2))
    return 1 if result["status"] == "invalid" else 0


if __name__ == "__main__":
    raise SystemExit(main())
