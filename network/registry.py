"""
SwarMesh Agent Registry — Tracks known agents and their capabilities.

Every agent that joins the mesh announces what skills it has.
The registry lets you find agents by skill, reputation, or address.
"""

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set


@dataclass
class AgentInfo:
    address: str                          # Solana pubkey
    skills: List[str] = field(default_factory=list)  # What this agent can do
    description: str = ""                 # Human-readable description
    endpoint: str = ""                    # WebSocket endpoint to reach this agent
    last_seen: float = field(default_factory=time.time)
    tasks_completed: int = 0
    tasks_failed: int = 0
    total_earned_lamports: int = 0
    total_spent_lamports: int = 0
    reputation_score: float = 0.5         # 0.0 to 1.0, starts neutral

    @property
    def success_rate(self) -> float:
        total = self.tasks_completed + self.tasks_failed
        if total == 0:
            return 0.0
        return self.tasks_completed / total

    @property
    def is_online(self) -> bool:
        return (time.time() - self.last_seen) < 300  # 5 min timeout

    def to_dict(self) -> dict:
        return {
            "address": self.address,
            "skills": self.skills,
            "description": self.description,
            "endpoint": self.endpoint,
            "last_seen": self.last_seen,
            "tasks_completed": self.tasks_completed,
            "tasks_failed": self.tasks_failed,
            "total_earned_lamports": self.total_earned_lamports,
            "reputation_score": self.reputation_score,
            "success_rate": self.success_rate,
            "is_online": self.is_online,
        }


class AgentRegistry:
    def __init__(self):
        self._agents: Dict[str, AgentInfo] = {}
        self._skill_index: Dict[str, Set[str]] = {}  # skill -> set of addresses

    def register(self, agent: AgentInfo):
        """Register or update an agent."""
        self._agents[agent.address] = agent
        for skill in agent.skills:
            if skill not in self._skill_index:
                self._skill_index[skill] = set()
            self._skill_index[skill].add(agent.address)

    def unregister(self, address: str):
        agent = self._agents.pop(address, None)
        if agent:
            for skill in agent.skills:
                self._skill_index.get(skill, set()).discard(address)

    def get(self, address: str) -> Optional[AgentInfo]:
        return self._agents.get(address)

    def find_by_skill(self, skill: str, online_only: bool = True,
                      min_reputation: float = 0.0) -> List[AgentInfo]:
        """Find agents that have a specific skill."""
        addresses = self._skill_index.get(skill, set())
        agents = [self._agents[a] for a in addresses if a in self._agents]
        if online_only:
            agents = [a for a in agents if a.is_online]
        if min_reputation > 0:
            agents = [a for a in agents if a.reputation_score >= min_reputation]
        return sorted(agents, key=lambda a: a.reputation_score, reverse=True)

    def heartbeat(self, address: str):
        """Update last_seen for an agent."""
        agent = self._agents.get(address)
        if agent:
            agent.last_seen = time.time()

    def record_completion(self, address: str, earned_lamports: int):
        agent = self._agents.get(address)
        if agent:
            agent.tasks_completed += 1
            agent.total_earned_lamports += earned_lamports

    def record_failure(self, address: str):
        agent = self._agents.get(address)
        if agent:
            agent.tasks_failed += 1

    @property
    def all_agents(self) -> List[AgentInfo]:
        return list(self._agents.values())

    @property
    def online_agents(self) -> List[AgentInfo]:
        return [a for a in self._agents.values() if a.is_online]

    @property
    def all_skills(self) -> List[str]:
        return list(self._skill_index.keys())
