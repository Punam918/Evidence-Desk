"""Public-safe failures. Never put raw vendor or SQL exceptions in these details."""

from dataclasses import dataclass


@dataclass
class Problem(Exception):
    status: int
    code: str
    detail: str
    retryable: bool = False


class LeaseLost(Problem):
    def __init__(self) -> None:
        super().__init__(409, "lease_lost", "The worker no longer owns this job.")
