from __future__ import annotations

import hashlib
import inspect
import os
import pickle
from collections.abc import Callable
from functools import wraps
from typing import Any, ParamSpec, TypeVar, cast

from diskcache import Cache # type: ignore[import-untyped]

P = ParamSpec("P")
R = TypeVar("R")

os.makedirs("disk_cache", exist_ok=True)
_cache = Cache("disk_cache")

def _hash_value(
    value: Any,
    hash_funcs: dict[type, Callable[[Any], Any]],
) -> Any:
    for typ, func in hash_funcs.items():
        if isinstance(value, typ):
            return func(value)
    if isinstance(value, (str, int, float, bool, bytes, type(None))):
        return value
    if isinstance(value, tuple):
        return tuple(_hash_value(v, hash_funcs) for v in value)
    if isinstance(value, list):
        return [_hash_value(v, hash_funcs) for v in value]
    if isinstance(value, dict):
        return {
            k: _hash_value(v, hash_funcs)
            for k, v in sorted(value.items())
        }
    return value

def disk_cache_data(
    *,
    ttl: float | None = None,
    ignore_args: set[str] | None = None,
    hash_funcs: dict[type, Callable[[Any], Any]] | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    ignore_args = ignore_args or set()
    hash_funcs = hash_funcs or {}
    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        sig = inspect.signature(func)
        @wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            bound = sig.bind_partial(*args, **kwargs)
            bound.apply_defaults()
            key_dict = {}
            for name, value in bound.arguments.items():
                if name in ignore_args:
                    continue
                key_dict[name] = _hash_value(value, hash_funcs)
            key_bytes = pickle.dumps((func.__module__, func.__qualname__, key_dict), protocol=pickle.HIGHEST_PROTOCOL)
            key = hashlib.sha256(key_bytes).hexdigest()
            result = _cache.get(key, default=None)
            if result is not None:
                return cast(R, result)
            result = func(*args, **kwargs)
            _cache.set(key,result,expire=ttl)
            return cast(R, result)
        def clear() -> None:
            _cache.clear()
        wrapper.clear = clear  # type: ignore[attr-defined]
        return wrapper
    return decorator