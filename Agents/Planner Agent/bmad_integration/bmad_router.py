from __future__ import annotations

from .bmad_models import RouteDecision
from .bmad_registry import BMadRegistry


class BMadRouter:
    """Uses GoModel to choose only installed BMAD skills relevant to a request."""

    def __init__(self, client, registry: BMadRegistry):
        self.client = client
        self.registry = registry

    def route(self, user_request: str, project_context: str = "") -> RouteDecision:
        if not user_request or not user_request.strip():
            raise ValueError("user_request cannot be empty")

        skills = self.registry.describe()
        allowed_ids = {item["skill_id"] for item in skills}
        system = (
            "You are a BMAD workflow router for a software-planning agent. "
            "Select the smallest useful set of installed skills for the request. "
            "Only use skill IDs from the registry. Do not invent agents, workflows, tools, "
            "or skill names. Existing-codebase requests require repository inspection. "
            "Do not select implementation skills when the task is only planning. "
            "Return only the requested structured output."
        )
        user = (
            f"USER REQUEST:\n{user_request}\n\n"
            f"PROJECT CONTEXT (may be empty):\n{project_context[:12000]}\n\n"
            f"INSTALLED BMAD SKILLS:\n{skills}\n\n"
            "Choose at most four skills. Include their IDs in selected_skill_ids and "
            "execution_order. execution_order must contain only selected IDs."
        )
        raw = self.client.complete(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            schema=RouteDecision.model_json_schema(),
        )
        decision = RouteDecision.model_validate(raw)

        unknown = set(decision.selected_skill_ids) - allowed_ids
        if unknown:
            raise ValueError(f"Router selected unavailable BMAD skills: {sorted(unknown)}")

        selected = set(decision.selected_skill_ids)
        order = [skill for skill in decision.execution_order if skill in selected]
        order.extend(skill for skill in decision.selected_skill_ids if skill not in order)
        decision.execution_order = order
        return decision
