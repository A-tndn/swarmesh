"""
SwarMesh Reputation System — Trust scoring for agents.

Score is 0.0 to 1.0. Starts at 0.5 (neutral).
Successful task completions increase score, failures decrease it.
Recent activity is weighted more heavily (exponential decay).
"""

import math
import time
from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class ReputationEvent:
    timestamp: float
    event_type: str   # "success", "failure", "dispute_won", "dispute_lost"
    task_id: str
    counterparty: str  # The other agent involved
    amount_lamports: int = 0


class ReputationSystem:
    DECAY_HALFLIFE = 7 * 24 * 3600  # 7 days — old events matter less

    def __init__(self):
        self._history: Dict[str, List[ReputationEvent]] = {}  # address -> events

    def record(self, address: str, event: ReputationEvent):
        if address not in self._history:
            self._history[address] = []
        self._history[address].append(event)

    def score(self, address: str) -> float:
        """Calculate weighted reputation score for an agent."""
        events = self._history.get(address, [])
        if not events:
            return 0.5  # Neutral — no history

        now = time.time()
        weighted_sum = 0.0
        weight_total = 0.0

        for event in events:
            age = now - event.timestamp
            weight = math.exp(-age * math.log(2) / self.DECAY_HALFLIFE)

            if event.event_type == "success":
                weighted_sum += weight * 1.0
            elif event.event_type == "failure":
                weighted_sum += weight * 0.0
            elif event.event_type == "dispute_won":
                weighted_sum += weight * 1.2  # Bonus for winning dispute
            elif event.event_type == "dispute_lost":
                weighted_sum += weight * -0.2  # Penalty

            weight_total += weight

        if weight_total == 0:
            return 0.5

        raw_score = weighted_sum / weight_total
        return max(0.0, min(1.0, raw_score))

    def get_history(self, address: str) -> List[ReputationEvent]:
        return self._history.get(address, [])

    def summary(self, address: str) -> dict:
        events = self._history.get(address, [])
        successes = sum(1 for e in events if e.event_type == "success")
        failures = sum(1 for e in events if e.event_type == "failure")
        return {
            "address": address,
            "score": round(self.score(address), 4),
            "total_events": len(events),
            "successes": successes,
            "failures": failures,
            "total_earned": sum(e.amount_lamports for e in events if e.event_type == "success"),
        }
