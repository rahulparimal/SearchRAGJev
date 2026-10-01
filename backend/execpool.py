"""Dedicated bounded executors so no workload can starve another: `search` for retrieval legs, `io` for cache/settings/extension I/O.
Overviews use async httpx and never touch either pool, and the default asyncio executor is left to libraries (DNS etc.)."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
_search=ThreadPoolExecutor(max_workers=24,thread_name_prefix='rag-search');_io=ThreadPoolExecutor(max_workers=16,thread_name_prefix='rag-io')
def run_search(fn,*a):return asyncio.get_running_loop().run_in_executor(_search,fn,*a)
def run_io(fn,*a):return asyncio.get_running_loop().run_in_executor(_io,fn,*a)
