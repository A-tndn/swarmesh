"""Basic tests for SwarMesh core modules."""

import json
import sys
import os
# Add the parent of swarmesh package to path
swarmesh_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
package_parent = os.path.dirname(swarmesh_root)
sys.path.insert(0, package_parent)

from swarmesh.core.wallet import Wallet
from swarmesh.core.task import Task, TaskStatus
from swarmesh.core.escrow import Escrow, EscrowState
from swarmesh.core.protocol import Protocol, Message, MessageType


def test_wallet_create():
    w = Wallet()
    assert len(w.address) > 30
    assert w.keypair is not None
    print(f"  Wallet: {w}")


def test_wallet_save_load(tmp_path="/tmp/swarmesh_test_wallets"):
    os.makedirs(tmp_path, exist_ok=True)
    w1 = Wallet(wallet_dir=tmp_path)
    w1.save("test_agent")
    w2 = Wallet.load("test_agent", wallet_dir=tmp_path)
    assert w1.address == w2.address
    print(f"  Save/Load: {w1.address[:12]}... matches")


def test_wallet_export_import():
    w1 = Wallet()
    secret = w1.export_secret_key()
    w2 = Wallet.from_secret_key(secret)
    assert w1.address == w2.address
    print(f"  Export/Import: OK")


def test_task_lifecycle():
    t = Task(buyer="buyer123", skill="echo", description="Test task")
    t.bounty_sol = 0.01
    assert t.status == TaskStatus.OPEN
    assert t.bounty_lamports == 10_000_000

    t.claim("worker456")
    assert t.status == TaskStatus.CLAIMED
    assert t.worker == "worker456"

    t.submit({"result": "done"})
    assert t.status == TaskStatus.SUBMITTED

    t.verify()
    assert t.status == TaskStatus.VERIFIED
    print(f"  Task lifecycle: {t}")


def test_task_serialization():
    t = Task(buyer="buyer123", skill="summarize")
    t.bounty_sol = 0.05
    d = t.to_dict()
    t2 = Task.from_dict(d)
    assert t2.task_id == t.task_id
    assert t2.bounty_lamports == t.bounty_lamports
    print(f"  Serialization: OK")


def test_escrow():
    escrow = Escrow()
    rec = escrow.create("task1", "buyer123", 10_000_000)
    assert rec.state == EscrowState.FUNDED
    assert escrow.total_locked == 10_000_000

    escrow.assign_worker(rec.escrow_id, "worker456")
    escrow.release(rec.escrow_id, tx_signature="fake_tx_sig")
    assert rec.state == EscrowState.RELEASED
    assert escrow.total_locked == 0
    print(f"  Escrow: fund → release OK")


def test_escrow_refund():
    escrow = Escrow()
    rec = escrow.create("task2", "buyer789", 5_000_000)
    escrow.refund(rec.escrow_id)
    assert rec.state == EscrowState.REFUNDED
    print(f"  Escrow: fund → refund OK")


def test_protocol_message():
    msg = Message(
        msg_type=MessageType.TASK_POST,
        sender="agent123",
        payload={"skill": "echo"},
    )
    raw = msg.to_json()
    msg2 = Message.from_json(raw)
    assert msg2.msg_type == MessageType.TASK_POST
    assert msg2.sender == "agent123"
    assert msg2.payload["skill"] == "echo"
    print(f"  Protocol message: serialize/deserialize OK")


if __name__ == "__main__":
    print("\n=== SwarMesh Core Tests ===\n")
    tests = [
        test_wallet_create,
        test_wallet_save_load,
        test_wallet_export_import,
        test_task_lifecycle,
        test_task_serialization,
        test_escrow,
        test_escrow_refund,
        test_protocol_message,
    ]
    passed = 0
    for test in tests:
        try:
            test()
            passed += 1
            print(f"  PASS: {test.__name__}")
        except Exception as e:
            print(f"  FAIL: {test.__name__}: {e}")

    print(f"\n{passed}/{len(tests)} tests passed\n")
