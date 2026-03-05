"""
SwarMesh Storage — SQLite persistence for tasks, agents, and transactions.

Everything survives node restarts. Lightweight, no external dependencies.
"""

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional

from .task import Task, TaskStatus
from ..network.registry import AgentInfo


class Storage:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or os.getenv(
            "SWARMESH_DB_PATH",
            str(Path("~/.swarmesh/swarmesh.db").expanduser())
        )
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._init_tables()

    def _init_tables(self):
        c = self._conn.cursor()
        c.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                buyer TEXT NOT NULL,
                worker TEXT,
                skill TEXT NOT NULL,
                description TEXT,
                input_data TEXT,
                output_data TEXT,
                bounty_lamports INTEGER DEFAULT 0,
                escrow_address TEXT,
                status TEXT DEFAULT 'open',
                verification_method TEXT DEFAULT 'buyer_approve',
                created_at REAL,
                expires_at REAL,
                claimed_at REAL,
                submitted_at REAL,
                completed_at REAL
            );

            CREATE TABLE IF NOT EXISTS agents (
                address TEXT PRIMARY KEY,
                skills TEXT,
                description TEXT,
                endpoint TEXT,
                last_seen REAL,
                tasks_completed INTEGER DEFAULT 0,
                tasks_failed INTEGER DEFAULT 0,
                total_earned_lamports INTEGER DEFAULT 0,
                total_spent_lamports INTEGER DEFAULT 0,
                reputation_score REAL DEFAULT 0.5
            );

            CREATE TABLE IF NOT EXISTS transactions (
                tx_id INTEGER PRIMARY KEY AUTOINCREMENT,
                tx_signature TEXT,
                task_id TEXT,
                from_address TEXT,
                to_address TEXT,
                amount_lamports INTEGER,
                tx_type TEXT,
                timestamp REAL,
                status TEXT DEFAULT 'confirmed',
                FOREIGN KEY (task_id) REFERENCES tasks(task_id)
            );

            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
            CREATE INDEX IF NOT EXISTS idx_tasks_skill ON tasks(skill);
            CREATE INDEX IF NOT EXISTS idx_agents_skills ON agents(skills);
            CREATE INDEX IF NOT EXISTS idx_tx_task ON transactions(task_id);
        """)
        self._conn.commit()

    # --- Tasks ---

    def save_task(self, task: Task):
        c = self._conn.cursor()
        c.execute("""
            INSERT OR REPLACE INTO tasks
            (task_id, buyer, worker, skill, description, input_data, output_data,
             bounty_lamports, escrow_address, status, verification_method,
             created_at, expires_at, claimed_at, submitted_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            task.task_id, task.buyer, task.worker, task.skill, task.description,
            json.dumps(task.input_data), json.dumps(task.output_data) if task.output_data else None,
            task.bounty_lamports, task.escrow_address, task.status.value,
            task.verification_method, task.created_at, task.expires_at,
            task.claimed_at, task.submitted_at, task.completed_at,
        ))
        self._conn.commit()

    def get_task(self, task_id: str) -> Optional[Task]:
        c = self._conn.cursor()
        c.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        row = c.fetchone()
        if not row:
            return None
        return self._row_to_task(row)

    def get_tasks_by_status(self, status: str) -> List[Task]:
        c = self._conn.cursor()
        c.execute("SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC", (status,))
        return [self._row_to_task(r) for r in c.fetchall()]

    def get_tasks_by_skill(self, skill: str) -> List[Task]:
        c = self._conn.cursor()
        c.execute("SELECT * FROM tasks WHERE skill = ? AND status = 'open' ORDER BY bounty_lamports DESC", (skill,))
        return [self._row_to_task(r) for r in c.fetchall()]

    def get_all_tasks(self, limit: int = 100) -> List[Task]:
        c = self._conn.cursor()
        c.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,))
        return [self._row_to_task(r) for r in c.fetchall()]

    def _row_to_task(self, row) -> Task:
        return Task(
            task_id=row["task_id"],
            buyer=row["buyer"],
            worker=row["worker"],
            skill=row["skill"],
            description=row["description"] or "",
            input_data=json.loads(row["input_data"]) if row["input_data"] else {},
            output_data=json.loads(row["output_data"]) if row["output_data"] else None,
            bounty_lamports=row["bounty_lamports"],
            escrow_address=row["escrow_address"],
            status=TaskStatus(row["status"]),
            verification_method=row["verification_method"] or "buyer_approve",
            created_at=row["created_at"] or 0,
            expires_at=row["expires_at"],
            claimed_at=row["claimed_at"],
            submitted_at=row["submitted_at"],
            completed_at=row["completed_at"],
        )

    # --- Agents ---

    def save_agent(self, agent: AgentInfo):
        c = self._conn.cursor()
        c.execute("""
            INSERT OR REPLACE INTO agents
            (address, skills, description, endpoint, last_seen,
             tasks_completed, tasks_failed, total_earned_lamports,
             total_spent_lamports, reputation_score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            agent.address, json.dumps(agent.skills), agent.description,
            agent.endpoint, agent.last_seen, agent.tasks_completed,
            agent.tasks_failed, agent.total_earned_lamports,
            agent.total_spent_lamports, agent.reputation_score,
        ))
        self._conn.commit()

    def get_agent(self, address: str) -> Optional[AgentInfo]:
        c = self._conn.cursor()
        c.execute("SELECT * FROM agents WHERE address = ?", (address,))
        row = c.fetchone()
        if not row:
            return None
        return self._row_to_agent(row)

    def get_all_agents(self) -> List[AgentInfo]:
        c = self._conn.cursor()
        c.execute("SELECT * FROM agents ORDER BY reputation_score DESC")
        return [self._row_to_agent(r) for r in c.fetchall()]

    def _row_to_agent(self, row) -> AgentInfo:
        return AgentInfo(
            address=row["address"],
            skills=json.loads(row["skills"]) if row["skills"] else [],
            description=row["description"] or "",
            endpoint=row["endpoint"] or "",
            last_seen=row["last_seen"] or 0,
            tasks_completed=row["tasks_completed"],
            tasks_failed=row["tasks_failed"],
            total_earned_lamports=row["total_earned_lamports"],
            total_spent_lamports=row["total_spent_lamports"],
            reputation_score=row["reputation_score"],
        )

    # --- Transactions ---

    def log_transaction(self, tx_signature: str, task_id: str,
                        from_address: str, to_address: str,
                        amount_lamports: int, tx_type: str):
        c = self._conn.cursor()
        c.execute("""
            INSERT INTO transactions
            (tx_signature, task_id, from_address, to_address, amount_lamports, tx_type, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (tx_signature, task_id, from_address, to_address, amount_lamports, tx_type, time.time()))
        self._conn.commit()

    def get_transactions(self, limit: int = 50) -> List[dict]:
        c = self._conn.cursor()
        c.execute("SELECT * FROM transactions ORDER BY timestamp DESC LIMIT ?", (limit,))
        return [dict(r) for r in c.fetchall()]

    def get_transactions_for_task(self, task_id: str) -> List[dict]:
        c = self._conn.cursor()
        c.execute("SELECT * FROM transactions WHERE task_id = ? ORDER BY timestamp", (task_id,))
        return [dict(r) for r in c.fetchall()]

    # --- Stats ---

    def stats(self) -> dict:
        c = self._conn.cursor()
        c.execute("SELECT COUNT(*) as total, status FROM tasks GROUP BY status")
        task_stats = {row["status"]: row["total"] for row in c.fetchall()}

        c.execute("SELECT COUNT(*) as total FROM agents")
        agent_count = c.fetchone()["total"]

        c.execute("SELECT COUNT(*) as total, SUM(amount_lamports) as volume FROM transactions")
        tx_row = c.fetchone()

        return {
            "tasks": task_stats,
            "agents": agent_count,
            "transactions": tx_row["total"] or 0,
            "total_volume_sol": (tx_row["volume"] or 0) / 1_000_000_000,
        }

    def close(self):
        self._conn.close()
