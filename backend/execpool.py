"""Dedicated bounded executors so no workload can starve another: `search` for retrieval legs, `io` for cache/settings/extension I/O,
`web` for hedged web-provider fetches (keeps a slow/hedged ddgs call off the default executor, which also runs getaddrinfo for every
httpx connect including Jev/LLM), `ov_io` for overview/quick-answer I/O (settings/cache/evidence) so a Redis hiccup under search load
cannot delay an overview or vice versa. Overviews/quick-answers use async httpx for the LLM call itself and never touch any pool for
that; the default asyncio executor is left to libraries (DNS etc.)."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
_search=ThreadPoolExecutor(max_workers=24,thread_name_prefix='rag-search');_io=ThreadPoolExecutor(max_workers=16,thread_name_prefix='rag-io')
_web=ThreadPoolExecutor(max_workers=16,thread_name_prefix='rag-web');_ov_io=ThreadPoolExecutor(max_workers=8,thread_name_prefix='rag-ov')
_web_inflight=0
WEB_MAX=16
def run_search(fn,*a):return asyncio.get_running_loop().run_in_executor(_search,fn,*a)
def run_io(fn,*a):return asyncio.get_running_loop().run_in_executor(_io,fn,*a)
def run_ov(fn,*a):return asyncio.get_running_loop().run_in_executor(_ov_io,fn,*a)
def web_busy():return _web_inflight>=WEB_MAX
def web_headroom():return max(0,WEB_MAX-_web_inflight)
def run_web(fn,*a):
    """Fail fast with RuntimeError('web_busy') instead of queuing onto an already-saturated pool; callers should catch and degrade.
    Submits straight to the executor and decrements _web_inflight from the raw concurrent.futures.Future's own completion
    callback -- NOT from a `finally` on the awaited coroutine -- because a cancelled awaiter (hedged_fetch cancels its losers)
    does not stop the underlying thread: cancelling the asyncio-side wrapper only stops *waiting* for it, so decrementing there
    would undercount real thread occupancy and let web_busy() report the pool free while it is actually saturated."""
    global _web_inflight
    if web_busy():
        fut=asyncio.get_running_loop().create_future();fut.set_exception(RuntimeError('web_busy'));return fut
    _web_inflight+=1
    loop=asyncio.get_running_loop();cf=_web.submit(fn,*a)
    def _done(_):
        global _web_inflight
        _web_inflight-=1  # runs in the worker thread itself (simple int decrement, GIL-atomic); NOT scheduled back onto the
        # loop via call_soon_threadsafe, which would raise if this future outlives a short-lived loop (e.g. per-test asyncio.run())
    cf.add_done_callback(_done)
    return asyncio.wrap_future(cf,loop=loop)
