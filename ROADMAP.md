# SwarMesh Development Roadmap

## Phase 1: Real Money ✅ DONE
- [x] Wire mainnet SOL transfers into task lifecycle
- [x] Message signing with Solana keypairs (identity verification)
- [x] Test real payment: treasury → test wallet on mainnet (0.000005 SOL fee)

## Phase 2: Persistence ✅ DONE
- [x] SQLite storage for tasks, agents, reputation
- [x] Survive node restarts without losing state
- [x] Transaction log (every SOL movement recorded)

## Phase 3: Real Worker Agents ✅ DONE
- [x] Web scraper agent (web-scrape, fetch-url, extract-links)
- [x] Data agent (json-transform, text-process, hash-compute, csv-parse)
- [x] Agent runner (starts all agents, systemd service)
- [x] All 3 real tasks tested and passing

## Phase 4: Package & Ship — NEXT
- [ ] Clean up imports, add proper error handling
- [ ] pyproject.toml for pip install
- [ ] Upload to PyPI as `swarmesh`
- [ ] Landing page (when domain is ready)
- [ ] API documentation

## Phase 5: Growth
- [ ] Multi-node mesh (nodes discover each other)
- [ ] Agent auto-scaling (spin up workers based on demand)
- [ ] Revenue: take 1-2% fee on every task payment
- [ ] LLM agent (earns SOL by running inference)
- [ ] More specialized agents
