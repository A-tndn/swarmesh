"""
SwarMesh Data Agent — Earns SOL by processing and transforming data.

Skills: json-transform, text-process, csv-parse, hash-compute
"""

import asyncio
import csv
import hashlib
import io
import json
import logging
import re
from typing import Any, Dict

from ..core.wallet import Wallet
from ..sdk.server import SwarMeshServer

logger = logging.getLogger("swarmesh.agent.data")


def create_data_agent(mesh_url: str = "ws://localhost:7770",
                      wallet: Wallet = None) -> SwarMeshServer:
    wallet = wallet or Wallet()
    server = SwarMeshServer(mesh_url=mesh_url, wallet=wallet)

    @server.handle("json-transform")
    async def json_transform(input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Transform JSON data: filter, map, sort, aggregate."""
        data = input_data.get("data", [])
        operations = input_data.get("operations", [])

        result = data
        for op in operations:
            op_type = op.get("type")

            if op_type == "filter" and isinstance(result, list):
                key = op.get("key")
                value = op.get("value")
                operator = op.get("operator", "eq")
                if operator == "eq":
                    result = [item for item in result if item.get(key) == value]
                elif operator == "contains":
                    result = [item for item in result if str(value) in str(item.get(key, ""))]
                elif operator == "gt":
                    result = [item for item in result if item.get(key, 0) > value]
                elif operator == "lt":
                    result = [item for item in result if item.get(key, 0) < value]

            elif op_type == "sort" and isinstance(result, list):
                key = op.get("key")
                reverse = op.get("reverse", False)
                result = sorted(result, key=lambda x: x.get(key, ""), reverse=reverse)

            elif op_type == "limit" and isinstance(result, list):
                result = result[:op.get("count", 10)]

            elif op_type == "pluck" and isinstance(result, list):
                keys = op.get("keys", [])
                result = [{k: item.get(k) for k in keys} for item in result]

            elif op_type == "count":
                result = {"count": len(result) if isinstance(result, list) else 1}

        return {"result": result, "operations_applied": len(operations)}

    @server.handle("text-process")
    async def text_process(input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Process text: word count, frequency, extract patterns, clean."""
        text = input_data.get("text", "")
        operation = input_data.get("operation", "analyze")

        if operation == "analyze":
            words = text.split()
            sentences = re.split(r'[.!?]+', text)
            return {
                "word_count": len(words),
                "char_count": len(text),
                "sentence_count": len([s for s in sentences if s.strip()]),
                "avg_word_length": sum(len(w) for w in words) / max(len(words), 1),
                "unique_words": len(set(w.lower() for w in words)),
            }

        elif operation == "frequency":
            words = re.findall(r'\w+', text.lower())
            freq = {}
            for w in words:
                freq[w] = freq.get(w, 0) + 1
            top = sorted(freq.items(), key=lambda x: x[1], reverse=True)[:50]
            return {"frequency": dict(top), "total_words": len(words)}

        elif operation == "extract":
            pattern = input_data.get("pattern", r'\b\w+\b')
            matches = re.findall(pattern, text)
            return {"matches": matches[:100], "count": len(matches)}

        elif operation == "clean":
            cleaned = re.sub(r'<[^>]+>', '', text)  # Strip HTML
            cleaned = re.sub(r'\s+', ' ', cleaned).strip()
            cleaned = re.sub(r'[^\w\s.,!?;:\-\'\"()]', '', cleaned)
            return {"cleaned": cleaned, "original_length": len(text), "cleaned_length": len(cleaned)}

        return {"error": f"Unknown operation: {operation}"}

    @server.handle("hash-compute")
    async def hash_compute(input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Compute hashes of data."""
        data = input_data.get("data", "")
        algorithms = input_data.get("algorithms", ["sha256", "md5"])
        data_bytes = data.encode("utf-8") if isinstance(data, str) else str(data).encode("utf-8")

        hashes = {}
        for algo in algorithms:
            if algo in hashlib.algorithms_available:
                h = hashlib.new(algo)
                h.update(data_bytes)
                hashes[algo] = h.hexdigest()

        return {"hashes": hashes, "input_length": len(data_bytes)}

    @server.handle("csv-parse")
    async def csv_parse(input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Parse CSV text into structured data."""
        csv_text = input_data.get("csv", "")
        delimiter = input_data.get("delimiter", ",")
        has_header = input_data.get("has_header", True)

        reader = csv.reader(io.StringIO(csv_text), delimiter=delimiter)
        rows = list(reader)

        if has_header and rows:
            headers = rows[0]
            data = [dict(zip(headers, row)) for row in rows[1:]]
            return {"headers": headers, "data": data[:500], "row_count": len(data)}
        else:
            return {"data": rows[:500], "row_count": len(rows)}

    return server


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    wallet = Wallet()
    logger.info(f"Data agent wallet: {wallet.address}")
    agent = create_data_agent(wallet=wallet)
    await agent.connect()
    logger.info("Data agent online — accepting json-transform, text-process, hash-compute, csv-parse tasks")
    await agent.listen()


if __name__ == "__main__":
    asyncio.run(main())
