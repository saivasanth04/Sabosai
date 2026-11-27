"""Exception hierarchy for the BMAD adapter."""

from __future__ import annotations


class BMadAdapterError(Exception):
    """Base class for every adapter error."""


class BMadRegistryError(BMadAdapterError):
    """Discovery of BMAD skills/agents/manifests failed."""


class BMadLoaderError(BMadAdapterError):
    """Loading a skill or one of its resources failed."""


class BMadResourceNotFoundError(BMadLoaderError):
    """A requested BMAD resource could not be found."""


class BMadResourceSecurityError(BMadLoaderError):
    """A resource attempted to resolve outside its permitted roots."""


class BMadResourceLimitError(BMadLoaderError):
    """A configured resource loading limit was exceeded."""


class BMadConfigError(BMadAdapterError):
    """BMAD configuration could not be resolved."""


class ScriptExecutionError(BMadAdapterError):
    """A BMAD Python / uv script failed or was refused."""


class PermissionDeniedError(BMadAdapterError):
    """The permission manager refused an action."""


class ToolError(BMadAdapterError):
    """A tool call failed (the message is returned to the LLM)."""


class LLMError(BMadAdapterError):
    """The model gateway (GoModel) returned an error."""


class MCPError(BMadAdapterError):
    """The GoModel MCP gateway returned an error."""


class SessionError(BMadAdapterError):
    """A session could not be saved, found, or resumed."""


class WorkflowError(BMadAdapterError):
    """The workflow engine hit an unrecoverable state."""
