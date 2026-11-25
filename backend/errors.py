"""Shared exception types (kept in their own module to avoid circular imports)."""


class PipelineError(RuntimeError):
    """An expected, user-explainable pipeline failure.

    The message is safe to show in the UI and in job logs. Never put secrets
    in it.
    """

    def __init__(self, message: str, *, code: str = "pipeline_error") -> None:
        super().__init__(message)
        self.code = code


class ArchiveError(PipelineError):
    """The uploaded archive was rejected by the intake policy."""

    def __init__(self, message: str, *, code: str = "archive_rejected") -> None:
        super().__init__(message, code=code)


class DockerUnavailable(PipelineError):
    """Docker could not be used. `code` says why (see docker_client.py)."""
