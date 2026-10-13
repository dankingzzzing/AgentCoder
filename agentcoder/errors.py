"""Errors that interfaces can display without a Python traceback."""


class AgentError(Exception):
    """Base application error."""


class ConfigError(AgentError):
    """Invalid or missing configuration."""


class Cancelled(AgentError):
    """The user stopped an operation; it is not a provider failure."""


class ProviderError(AgentError):
    """An upstream request or response failed."""

    def __init__(self, message: str, *, http_status: int | None = None,
                 code: str = "provider_error", retryable: bool = False):
        super().__init__(message)
        self.http_status = http_status
        self.code, self.retryable = code, retryable


class ToolError(AgentError):
    """A tool request failed validation or execution."""


class MemoryError(AgentError):
    """A persisted session is invalid or cannot be accessed."""
