from dataclasses import dataclass


@dataclass
class JobOutcome:
    state: str
    result: dict
    external_id: str | None = None
    error_code: str | None = None
