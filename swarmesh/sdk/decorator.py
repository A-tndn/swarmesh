"""
SwarMesh Decorators — Syntactic sugar for defining task handlers.

Usage:
    from swarmesh.sdk import task_handler

    @task_handler("summarize")
    async def summarize(input_data):
        text = input_data["text"]
        return {"summary": text[:100] + "..."}
"""

import functools
from typing import Any, Callable, Coroutine, Dict

TaskFunc = Callable[[Dict[str, Any]], Coroutine[Any, Any, Dict[str, Any]]]

# Global registry of decorated handlers (used by SwarMeshServer.auto_discover)
_registered_handlers: Dict[str, TaskFunc] = {}


def task_handler(skill: str):
    """Decorator that registers a function as a handler for a specific skill."""
    def decorator(func: TaskFunc) -> TaskFunc:
        _registered_handlers[skill] = func

        @functools.wraps(func)
        async def wrapper(input_data: Dict[str, Any]) -> Dict[str, Any]:
            return await func(input_data)

        wrapper._swarmesh_skill = skill
        return wrapper

    return decorator


def get_registered_handlers() -> Dict[str, TaskFunc]:
    """Get all handlers registered via @task_handler decorator."""
    return _registered_handlers.copy()
