import asyncio,json,os,sys,unittest
from unittest import mock
sys.path.insert(0,'backend')
import httpx
import direct as D
import tokencache as TC
from cache import cache

def collect(gen):
    async def go():return [e async for e in gen]
    raw=asyncio.run(go());return [(e.split('\n')[0][7:],json.loads(e.split('\n')[1][6:])) for e in raw]
def sse_body(parts):return ''.join('data: '+json.dumps({'choices':[{'delta':{'content':p}}]})+'\n\n' for p in parts)+'data: [DONE]\n\n'
G=lambda a,b:{'DIRECT_LLM_BASE_URL':'http://llm.example/v1','DIRECT_LLM_MODEL':'m1'}.get(a,'')

class DirectTests(unittest.TestCase):
    def setUp(self):
        TC.reset();TC.ENABLED=True;cache.lru.clear();cache.r=None
        p=mock.patch.dict(os.environ);p.start();os.environ.pop('REDIS_URL',None);self.addCleanup(p.stop)
        self.calls=[];self.status=200
        def handler(req):
            self.calls.append(json.loads(req.content))
            return httpx.Response(self.status,content=sse_body(['Hello',' world']) if self.status==200 else b'err',headers={'content-type':'text/event-stream'})
        self.client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        pc=mock.patch.object(D,'_client',lambda:self.client);pc.start();self.addCleanup(pc.stop)
        self.addCleanup(cache.lru.clear)
    def run_q(self,q,history=None):return collect(D.stream_answer(G,q,history))
    def test_streams_tokens(self):
        ev=self.run_q('Explain recursion');self.assertEqual(''.join(d['t'] for n,d in ev if n=='token'),'Hello world');self.assertEqual(ev[-1][0],'done')
    def test_cache_off_by_default_calls_provider_every_time(self):
        self.run_q('Explain recursion');self.run_q('Explain recursion');self.assertEqual(len(self.calls),2)
    def test_cache_hit_serves_without_calling_provider(self):
        with mock.patch.object(D,'CACHE_ENABLED',True):
            self.run_q('Explain recursion');ev=self.run_q('explain  recursion?')
        self.assertEqual(len(self.calls),1);self.assertTrue(ev[-1][1]['cached']);self.assertEqual(''.join(d['t'] for n,d in ev if n=='token'),'Hello world')
    def test_history_bypasses_cache(self):
        with mock.patch.object(D,'CACHE_ENABLED',True):
            self.run_q('Explain recursion');self.run_q('Explain recursion',[{'role':'user','content':'hi'}])
        self.assertEqual(len(self.calls),2)
    def test_time_sensitive_and_personal_queries_are_never_cached(self):
        with mock.patch.object(D,'CACHE_ENABLED',True):
            for q in ('what is the latest python version','weather today','what is my name','who won in 2026','price of bitcoin'):
                self.run_q(q);self.run_q(q)
        self.assertEqual(len(self.calls),10)
    def test_errors_are_not_cached(self):
        with mock.patch.object(D,'CACHE_ENABLED',True):
            self.status=500;ev=self.run_q('Explain recursion');self.assertEqual(ev[-1][0],'error')
            self.status=200;ev2=self.run_q('Explain recursion')
        self.assertEqual(ev2[-1][0],'done');self.assertFalse(ev2[-1][1].get('cached'))
    def test_different_model_does_not_share_entries(self):
        with mock.patch.object(D,'CACHE_ENABLED',True):
            self.run_q('Explain recursion')
            g2=lambda a,b:{'DIRECT_LLM_BASE_URL':'http://llm.example/v1','DIRECT_LLM_MODEL':'m2'}.get(a,'')
            collect(D.stream_answer(g2,'Explain recursion'))
        self.assertEqual(len(self.calls),2)
if __name__=='__main__':unittest.main()
