from dataclasses import dataclass
from uuid import UUID

from evidencedesk.providers.base import ModelResult, Prices


@dataclass(frozen=True)
class Attempt:
    id: UUID
    number: int
    reserved: int
    prices: Prices
    execution_state: str
    result: ModelResult | None = None
