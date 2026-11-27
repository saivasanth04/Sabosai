"""bmad_adapter — run the BMad Method (v6.x skills) inside the NTG Planner Agent.

BMAD ships as an IDE plugin: Markdown skills that assume an IDE agent with a shell, file
tools, subagents and a human in the loop. This package is that host, built as a library:

    from bmad_adapter import BMadRuntime

    rt = BMadRuntime.for_project(r"C:\\Projects\\NTG\\Workspace\\my-app")
    result = rt.run_skill("bmad-prd", {"intent": "create", "brief": "…"})    # headless
    session = rt.start(agent="pm", message="Let's write the PRD")             # interactive

Components: Registry, Loader, AgentExecutor, SkillExecutor, InstructionEngine,
WorkflowEngine, ConfigResolver, ScriptExecutor, ToolManager (+ GoModel MCP),
SubagentManager, ContextManager, SessionManager, PermissionManager, ArtifactManager.
"""

from .errors import BMadAdapterError
from .registry import AgentInfo, BMadRegistry, SkillInfo
from .runtime import BMadRuntime, TurnResult
from .settings import AdapterSettings

__all__ = ["BMadRuntime", "TurnResult", "AdapterSettings", "BMadRegistry", "SkillInfo", "AgentInfo",
           "BMadAdapterError"]
__version__ = "1.0.0"
