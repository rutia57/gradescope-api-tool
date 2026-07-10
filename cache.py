from __future__ import annotations

import csv
import hashlib
import tracemalloc
import inspect
import datetime
from itertools import chain
from tqdm import tqdm
import json
import os
import threading
import smtplib
import pickle
from collections.abc import Callable
from email.message import EmailMessage
from functools import wraps
from typing import Any, ParamSpec, TypeVar, cast

from diskcache import Cache # type: ignore[import-untyped]

P = ParamSpec("P")
R = TypeVar("R")

os.makedirs("disk_cache", exist_ok=True)
_cache = Cache("disk_cache")
_cache_events: dict[str, threading.Event] = {}
_cache_lock = threading.Lock()

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
                if name in ignore_args or name.startswith('_'):
                    continue
                key_dict[name] = _hash_value(value, hash_funcs)
            key_bytes = pickle.dumps((func.__module__, func.__qualname__, key_dict), protocol=pickle.HIGHEST_PROTOCOL)
            key = hashlib.sha256(key_bytes).hexdigest()

            result = _cache.get(key, default=None)
            if result is not None:
                return cast(R, result)

            with _cache_lock:
                event = _cache_events.get(key)
                if event is None:
                    event = threading.Event()
                    _cache_events[key] = event
                    first = True
                else:
                    first = False

            if not first:
                event.wait()
                return cast(R, _cache.get(key))

            try:
                result = func(*args, **kwargs)
                _cache.set(key, result, expire=ttl)
                return cast(R, result)
            finally:
                with _cache_lock:
                    _cache_events.pop(key, None)
                event.set()
        def clear() -> None:
            _cache.clear()
        wrapper.clear = clear  # type: ignore[attr-defined]
        return wrapper
    return decorator


def save_memory_usage(filename: str="memory_report.tsv", limit: int=200) -> None:
    import gc
    import sys
    objects = []
    all_vars = chain(locals().items(), globals().items())
    for obj in gc.get_objects():
        try:
            if sys.getsizeof(obj) > 5000:
                objects.append((sys.getsizeof(obj), obj))
        except Exception as e:
            print(e)
            pass
    objects.sort(reverse=True, key=lambda x: x[0])
    os.makedirs("tmp/data/", exist_ok=True)
    with open(f"tmp/data/{filename}", "w", newline="") as f:
        writer = csv.writer(f, delimiter="\t")
        writer.writerow([
            "Size (MB)",
            "Names",
            "Referrers",
            "Indirect referrers",
            "Type",
            "Example",
        ])
        for size, obj in tqdm(objects[:limit]):
            try:
                seen = set()
                referrers = []
                for r in gc.get_referrers(obj):
                    if isinstance(r, dict):
                        referrer_strs = []
                        for (n,v) in r.items():
                            if str(n) not in seen:
                                seen.add(str(n))
                                if v is obj:
                                    nn = str(n).replace('\n','')[:100]
                                    vv = str(v).replace('\n','')[:100]
                                    referrer_strs.append(f"{nn}:{vv}")
                        referrers.append("\n".join([r for r in referrer_strs if r.strip()]))

                writer.writerow([
                    f"{size / 1024 / 1024:.2f}",
                    ','.join([n for (n,v) in all_vars if v is obj]),
                    '\n'.join([r for r in referrers if r.strip()]),
                    type(obj).__name__,
                    repr(obj)[:200].replace('\n',' '),
                ])
            except Exception as e:
                print(f'Couldnt write object of type {type(obj).__name__} ({size / 1024 / 1024:.2f} MB) because: {e}')

def send_email_with_attachment(
    attachment_path: str,
    subject: str,
    body: str,
    gmail_key_file: str,
) -> None:

    with open(gmail_key_file) as f:
        gmail_keys = json.load(f)
        gmail_username = gmail_keys['GMAIL_USERNAME']
        gmail_addressee = gmail_keys['EMAIL_TO']
        gmail_app_password = gmail_keys['GMAIL_APP_PASSWORD']

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = gmail_username
    msg["To"] = gmail_addressee

    msg.set_content(body)

    with open(attachment_path, "rb") as f:
        msg.add_attachment(
            f.read(),
            maintype="application",
            subtype="octet-stream",
            filename=os.path.basename(attachment_path),
        )

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_username, gmail_app_password)
        server.send_message(msg)


def save_tracemalloc_file(snapshot: tracemalloc.Snapshot, filename: str="tracemalloc_memory_report.tsv", limit: int=200) -> str:
    top_stats = snapshot.statistics("lineno")
    os.makedirs("tmp/data/", exist_ok=True)
    filepath = f"tmp/data/{datetime.datetime.now(datetime.timezone.utc).isoformat()}_{filename}"
    with open(filepath, "w", newline="") as f:
        writer = csv.writer(f, delimiter="\t")
        for stat in top_stats[:200]:
            writer.writerow([
                f"{stat.size / (1024*1024) :.2f} MB",
                stat.traceback,
            ])
    return filepath