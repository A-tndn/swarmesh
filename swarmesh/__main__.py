"""Allow running as: python -m swarmesh"""
from .node import main
import asyncio

asyncio.run(main())
