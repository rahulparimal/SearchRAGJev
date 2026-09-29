"""Explicit in-process extension points. Extensions receive bounded, non-secret data."""
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import time

HOOKS={'pre_ingest','post_extract','pre_index','pre_query','post_answer'}
_registry=defaultdict(list)
_pool=ThreadPoolExecutor(max_workers=4,thread_name_prefix='rag-extension')

def register(name, callback):
    if name not in HOOKS:raise ValueError(f'Unsupported extension hook: {name}')
    _registry[name].append(callback)

def run(name, payload, timeout_seconds=0.25):
    """Run hook chain with bounded execution; errors are isolated and returned for audit."""
    if name not in HOOKS:raise ValueError(f'Unsupported extension hook: {name}')
    value=payload;events=[]
    for callback in tuple(_registry[name]):
        started=time.perf_counter();future=_pool.submit(callback,value)
        try:value=future.result(timeout=timeout_seconds);events.append({'hook':name,'status':'ok','elapsed_ms':round((time.perf_counter()-started)*1000,2)})
        except FutureTimeout:future.cancel();events.append({'hook':name,'status':'timeout'})
        except Exception as exc:events.append({'hook':name,'status':'error','reason':type(exc).__name__})
    return value,events
