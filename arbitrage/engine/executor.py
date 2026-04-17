"""Executor protocol.

Paper and live executors share the same surface so the rest of the engine is
mode-agnostic. The Opportunity -> Basket lifecycle is identical until the
very bottom of the stack, where one simulates and the other signs.
"""
from __future__ import annotations

from typing import Protocol

from ..models import Basket, Opportunity


class Executor(Protocol):
    async def execute(self, opp: Opportunity) -> Basket | None:
        """Try to open a basket from an opportunity.

        Returns the persisted basket on success (even for a `failed` basket —
        that still got persisted for forensics). Returns None if the executor
        rejected the opportunity before touching storage (e.g. risk gate).
        """
        ...
