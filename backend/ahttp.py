"""Deadline-bounded async HTTP that never cancels an in-flight httpx request. Cancelling a connecting httpx request (what asyncio.wait_for does)
can leave its pool slot stuck until every slot is wedged; here the deadline is enforced by httpx timeouts plus asyncio.wait, and a request that
outlives the caller finishes in the background (bounded by those same timeouts)."""
import asyncio,httpx
def _drain(t):
    if not t.cancelled():t.exception()  # retrieve so an abandoned request never logs "exception never retrieved"
async def send(client,method,url,left,**kw):
    """Raises asyncio.TimeoutError at the deadline, httpx.HTTPError on transport errors; returns the response otherwise."""
    t=httpx.Timeout(connect=min(left,3.0),read=left,write=min(left,3.0),pool=min(left,1.0))
    task=asyncio.ensure_future(client.request(method,url,timeout=t,**kw))
    try:done,_=await asyncio.wait({task},timeout=left)
    except BaseException:task.add_done_callback(_drain);raise
    if not done:task.add_done_callback(_drain);raise asyncio.TimeoutError()
    return task.result()
