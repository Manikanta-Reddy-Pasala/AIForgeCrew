"""Sample API module — multiple classes/methods to exercise tree-sitter walker."""
import os
from pathlib import Path

from .helpers import normalize


class PaymentService:
    def __init__(self) -> None:
        self.fees = 0.0

    def process(self, amount: float) -> dict:
        normalized = normalize(amount)
        return {"processed": normalized}

    def refund(self, txn_id: str) -> bool:
        return True


def health() -> dict:
    return {"status": "ok"}


def main() -> None:
    svc = PaymentService()
    print(svc.process(100.0))


if __name__ == "__main__":
    main()
