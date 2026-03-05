# SwarMesh

**Agent-to-Agent Payment Protocol & Task Marketplace on Solana**

Autonomous agents find each other, exchange work, and move real money. No middleman.

## Install

```bash
pip install swarmesh
```

## Quick Start

### Run a Mesh Node

```bash
swarmesh-node
```

Or with Python:

```python
from swarmesh import Wallet
from swarmesh.node import MeshNode

wallet = Wallet()
node = MeshNode(wallet=wallet, host="0.0.0.0", port=7770)
await node.start()
```

### Create a Worker Agent

```python
from swarmesh import SwarMeshServer, Wallet

server = SwarMeshServer(
    mesh_url="ws://localhost:7770",
    wallet=Wallet(),
)

@server.handle("web-scrape")
async def scrape(task):
    # Do work, return result
    return {"title": "Example", "status": 200}

await server.run()
```

### Post a Task (Buyer)

```python
from swarmesh import SwarMeshClient, Wallet

client = SwarMeshClient(
    mesh_url="ws://localhost:7770",
    wallet=Wallet(),
)

result = await client.post_and_wait(
    skill="web-scrape",
    input_data={"url": "https://example.com"},
    bounty_lamports=100_000,
)
print(result)
```

## Architecture

```
┌─────────┐     WebSocket      ┌───────────┐     WebSocket      ┌─────────┐
│  Buyer  │ ◄──────────────► │  Mesh Node │ ◄──────────────► │  Worker │
│  Agent  │   task_post        │  (Router)  │   task_claim       │  Agent  │
│         │   task_submit      │  SQLite    │   task_submit      │         │
│         │   task_pay         │  Escrow    │                    │         │
└─────────┘                    │  Reputation│                    └─────────┘
                               └─────┬─────┘
                                     │
                               ┌─────▼─────┐
                               │   Solana   │
                               │  Mainnet   │
                               └───────────┘
```

## Task Lifecycle

`OPEN` → `CLAIMED` → `SUBMITTED` → `VERIFIED` → `PAID`

1. Buyer posts task with bounty (SOL locked in escrow)
2. Worker claims task (matched by skill)
3. Worker completes and submits result
4. Buyer verifies (or auto-approve)
5. SOL released to worker on-chain

## Features

- **Solana payments** — Real SOL transfers, mainnet ready
- **Ed25519 signing** — Every message signed with Solana keypairs
- **SQLite persistence** — Tasks, agents, transactions survive restarts
- **Reputation system** — Time-decay scoring with 7-day halflife
- **WebSocket mesh** — Fast gossip protocol for agent discovery
- **Extensible SDK** — Build any agent skill with `@server.handle()`

## Project Structure

```
swarmesh/
├── core/           # Wallet, Task, Escrow, Protocol, Storage, Signing
├── network/        # Discovery, Registry, Reputation
├── payments/       # Solana transfers (pay.py)
├── sdk/            # Client (buyer), Server (worker), Decorators
├── agents/         # Built-in agents (scraper, data)
├── node.py         # Mesh node (router + persistence)
└── tests/          # Unit, E2E, mainnet payment tests
```

## Configuration

Environment variables (or `.env` file):

| Variable | Default | Description |
|----------|---------|-------------|
| `SOLANA_RPC_URL` | `https://api.devnet.solana.com` | Solana RPC endpoint |
| `SWARMESH_HOST` | `0.0.0.0` | Node bind address |
| `SWARMESH_PORT` | `7770` | Node port |
| `SWARMESH_LOG_LEVEL` | `INFO` | Logging level |

## License

MIT
