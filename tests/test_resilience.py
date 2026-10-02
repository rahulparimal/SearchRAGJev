"""Behaviour tests against a local mock HTTP server (real httpx pools, real threads): Jev wedge/cancel regression, executor starvation,
concurrency without a fallback storm, breaker semantics, ad filtering, overview validation, URL validation, privacy and admin endpoints."""
import asyncio,json,os,re,threading,time,unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from unittest import mock
import test_rag as T   # sets the sqlite test environment before backend modules load
from fastapi import HTTPException
import app as A,jev as J,overview as OV,plan as PL,providers as PR,web_search as ws,urlcheck
from cache import cache
from database import Db,User
from test_search_redesign import FakeJev,FakeGen,local_env,reader,collect,aio

class Mock:
    """In-process Jev + LLM endpoints. Tunable: jev_delay, jev_status, llm_delay; counts calls and peak concurrency."""
    def __init__(s):
        s.jev_delay=0.0;s.jev_status=200;s.llm_delay=0.0;s.jev_calls=0;s.llm_calls=0;s.llm_inflight=0;s.llm_peak=0;s.lock=threading.Lock()
        outer=s
        class H(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def log_message(self,*a):pass
            def send(self,code,obj):
                b=json.dumps(obj).encode();self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))) or b'{}')
                if self.path=='/v1/systemone':
                    with outer.lock:outer.jev_calls+=1
                    time.sleep(outer.jev_delay)
                    if outer.jev_status!=200:return self.send(outer.jev_status,{'error':'x'})
                    ans={}
                    for k,q in body.get('questions',{}).items():
                        if q['type']=='noul':ans[k]={'type':'noul','noul':0.9}
                        elif q['type']=='choice':c=q['criteria'];ans[k]={'type':'choice','choice':list(c)[0] if isinstance(c,dict) else c[0]}
                    return self.send(200,{'answers':ans,'usage':{'input_tokens':1,'output_tokens':1}})
                if self.path=='/v1/chat/completions':
                    with outer.lock:outer.llm_calls+=1;outer.llm_inflight+=1;outer.llm_peak=max(outer.llm_peak,outer.llm_inflight)
                    try:time.sleep(outer.llm_delay)
                    finally:
                        with outer.lock:outer.llm_inflight-=1
                    return self.send(200,{'choices':[{'message':{'content':'Parental leave is sixteen weeks [S1].'}}]})
                self.send(404,{})
            def do_GET(self):self.send(200,{})
        class Srv(ThreadingHTTPServer):
            def handle_error(self,*a):pass  # clients abandon requests on purpose (deadline tests)
        s.srv=Srv(('127.0.0.1',0),H);s.srv.daemon_threads=True;s.url=f'http://127.0.0.1:{s.srv.server_address[1]}'
        threading.Thread(target=s.srv.serve_forever,daemon=True).start()
    def close(s):s.srv.shutdown();s.srv.server_close()

class JevWedgeRegression(unittest.TestCase):
    @classmethod
    def setUpClass(c):c.m=Mock()
    @classmethod
    def tearDownClass(c):c.m.close()
    def setUp(self):J.reset();self.m.jev_delay=0.0;self.m.jev_status=200
    def test_cancelled_short_deadline_calls_do_not_wedge_the_pool(self):
        base=self.m.url;self.m.jev_delay=0.4
        async def go():
            J.breaker(base);J._st['breakers'][base]=ws.CircuitBreaker(threshold=10_000)  # isolate the pool behaviour from the breaker
            j=J.AsyncJev('tok','m',base,100);rs=await asyncio.gather(*(j.decide({'q':'x'},J.REL_QUESTION) for _ in range(40)))
            self.assertTrue(all(r=={} for r in rs));self.assertEqual(j.stats['failed'],40)   # every one timed out, none raised
            self.m.jev_delay=0.0;await asyncio.sleep(0.6)   # abandoned requests finish in the background
            ok=J.AsyncJev('tok','m',base,3000);a=await ok.decide({'q':'x'},J.REL_QUESTION);return a,ok
        a,ok=aio(go());self.assertIn('rel',a);self.assertEqual(ok.stats['failed'],0)
    def test_repeated_timeouts_recreate_the_client_and_breaker_opens_then_recovers(self):
        base=self.m.url;self.m.jev_delay=0.5
        async def go():
            j=J.AsyncJev('tok','m',base,100);first=J._client()
            for _ in range(6):await j.decide({'q':'x'},J.REL_QUESTION)
            self.assertIsNot(J._client(),first)   # rebuilt after 5 consecutive timeouts
            self.assertEqual(J.breaker(base).state(),'open');calls=self.m.jev_calls
            r=await j.decide({'q':'x'},J.REL_QUESTION);self.assertEqual((r,j.last_error),({},'breaker_open'));self.assertEqual(self.m.jev_calls,calls)   # open: no request is made
            J._st['breakers'][base]=ws.CircuitBreaker(threshold=5,window=10,cooldown=0.1);self.m.jev_delay=0.0
            for _ in range(5):await j.decide({'q':'x'},J.REL_QUESTION)  # (fresh breaker closed) healthy again
            return await J.AsyncJev('tok','m',base,2000).decide({'q':'x'},J.REL_QUESTION)
        self.assertIn('rel',aio(go()))
    def test_cancelled_half_open_probe_does_not_wedge_the_breaker(self):
        base=self.m.url;self.m.jev_delay=0.5
        async def go():
            J._st['breakers'][base]=ws.CircuitBreaker(threshold=1,window=10,cooldown=0.05);j=J.AsyncJev('tok','m',base,100);await j.decide('q',{'x':{}})   # one timeout opens it
            await asyncio.sleep(0.1);self.assertEqual(J.breaker(base).state(),'half_open')
            t=asyncio.ensure_future(J.AsyncJev('tok','m',base,3000).decide('q',{'x':{}}));await asyncio.sleep(0.05);t.cancel()   # the probe's caller goes away
            try:await t
            except asyncio.CancelledError:pass
            self.m.jev_delay=0.0;await asyncio.sleep(0.6)
            return await J.AsyncJev('tok','m',base,2000).decide('q',{'x':{'type':'noul'}})
        self.assertIn('x',aio(go()));self.assertEqual(J.breaker(self.m.url).state(),'closed')   # the next call went through and closed the breaker
    def test_http_errors_are_logged_and_counted_not_raised(self):
        self.m.jev_status=500
        async def go():
            j=J.AsyncJev('tok','m',self.m.url,500)
            with self.assertLogs('rag.jev',level='WARNING') as lg:r=await j.decide('q',{'x':{}})
            return r,j,lg.output
        r,j,out=aio(go());self.assertEqual(r,{});self.assertEqual((j.stats['failed'],j.last_error),(1,'http_500'));self.assertTrue(any('http_500' in l for l in out));self.assertTrue(j.down)
    def test_pool_limits(self):self.assertGreaterEqual(J.POOL_MAX,128);self.assertGreaterEqual(J.POOL_KEEP,32)

class ExecutorStarvation(unittest.TestCase):
    @classmethod
    def setUpClass(c):c.m=Mock()
    @classmethod
    def tearDownClass(c):c.m.close()
    def setUp(self):local_env(self);PR.reset_llm_client();OV._active[0]=0
    def gen(self):
        cfg={'OVERVIEW_LLM_BASE_URL':self.m.url+'/v1','OVERVIEW_LLM_MODEL':'m','OVERVIEW_LLM_API_KEY':'k'};return PR.OverviewGenerator(lambda n,e:cfg.get(n,''))
    def rec(self):return {'query':'how many weeks of parental leave','eligible':True,'route':'local','evidence':[{'title':'hr','text':'Parental leave is sixteen weeks.','source':'local','rel':0.9,'chunk_id':'hr-0','document_id':'hr'}]}
    def test_search_stays_fast_while_25_overviews_stream_and_excess_are_busy(self):
        self.m.llm_delay=1.5
        async def go():
            async def one(i):return ''.join([c async for c in OV.stream_overview(self.rec(),FakeJev(),self.gen(),f'scope{i}')])
            ov=[asyncio.ensure_future(one(i)) for i in range(25)];await asyncio.sleep(0.3)   # overviews are now in the LLM call
            t=time.perf_counter()
            with mock.patch.object(A,'jev_client',return_value=FakeJev(enabled=False)):
                rs=[await A.search(A.SearchBody(query=f'parental leave sixteen weeks {i}',mode='local'),T.Response(),reader(),self.db) for i in range(5)]
            dt=time.perf_counter()-t;outs=await asyncio.gather(*ov);return dt,outs
        dt,outs=aio(go())
        self.assertLess(dt,1.0,'search legs must not queue behind overview LLM calls')
        self.assertLessEqual(self.m.llm_peak,OV.MAX_CONCURRENT)
        self.assertEqual(sum('reason": "busy' in o for o in outs),25-OV.MAX_CONCURRENT);self.assertEqual(sum('event: overview' in o for o in outs),OV.MAX_CONCURRENT)
    def test_llm_key_never_sent_to_a_different_host_and_cache_key_has_endpoint(self):
        cfg={'OVERVIEW_LLM_BASE_URL':'https://other.example/v1','LLM_BASE_URL':'https://llm.example/v1','LLM_API_KEY':'secret-llm','LLM_MODEL':'m1'}
        g=PR.OverviewGenerator(lambda n,e:cfg.get(n,''));self.assertEqual(g._cfg()[1],'');self.assertEqual(g.model,'m1')   # overview host differs: the LLM key is NOT used
        cfg2={'LLM_BASE_URL':'https://llm.example/v1','LLM_API_KEY':'secret-llm','LLM_MODEL':'m1','OVERVIEW_LLM_BASE_URL':'https://llm.example/v1/'}
        self.assertEqual(PR.OverviewGenerator(lambda n,e:cfg2.get(n,''))._cfg()[1],'secret-llm')   # identical base URL: fallback allowed
        ev=[{'chunk_id':'a','source':'local'}];self.assertNotEqual(OV.evidence_key('q',ev,'s','m','e1'),OV.evidence_key('q',ev,'s','m','e2'))
        self.assertNotEqual(PR.OverviewGenerator(lambda n,e:cfg.get(n,'')).endpoint_id,PR.OverviewGenerator(lambda n,e:cfg2.get(n,'')).endpoint_id)
    def test_llm_retries_once_on_5xx_within_deadline_and_reports_provider_unavailable_on_total_failure(self):
        calls=[0]
        class H(BaseHTTPRequestHandler):
            def log_message(s,*a):pass
            def do_POST(s):
                s.rfile.read(int(s.headers.get('Content-Length',0)));calls[0]+=1;ok=calls[0]>1;b=json.dumps({'choices':[{'message':{'content':'Sixteen weeks [S1].'}}]} if ok else {'e':1}).encode()
                s.send_response(200 if ok else 503);s.send_header('Content-Length',str(len(b)));s.end_headers();s.wfile.write(b)
        srv=ThreadingHTTPServer(('127.0.0.1',0),H);threading.Thread(target=srv.serve_forever,daemon=True).start();self.addCleanup(srv.shutdown)
        cfg={'OVERVIEW_LLM_BASE_URL':f'http://127.0.0.1:{srv.server_address[1]}','OVERVIEW_LLM_MODEL':'m'}
        self.assertEqual(aio(PR.OverviewGenerator(lambda n,e:cfg.get(n,'')).agenerate('s','u')),'Sixteen weeks [S1].');self.assertEqual(calls[0],2)

class ConcurrentSearches(unittest.TestCase):
    @classmethod
    def setUpClass(c):c.m=Mock()
    @classmethod
    def tearDownClass(c):c.m.close()
    def setUp(self):
        local_env(self);J.reset();self.m.jev_delay=0.1;self.m.jev_status=200
        for i in range(12):T.add_doc(self.db,f'd{i}',[f'parental leave sixteen weeks note number {i} with extra words about policy {i}' ])
    def test_16_parallel_searches_no_fallback_storm(self):
        jev=J.AsyncJev('tok','m',self.m.url,900)
        async def go():
            with mock.patch.object(A,'jev_client',return_value=jev),mock.patch.dict(os.environ,{'JEV_RERANK_ENABLED':'true'}):
                t=time.perf_counter();rs=await asyncio.gather(*(A.search(A.SearchBody(query=f'parental leave sixteen weeks policy {i}',mode='local'),T.Response(),reader(f'u{i}'),self.db) for i in range(16)));return rs,time.perf_counter()-t
        rs,dt=aio(go());self.assertEqual({r['reranker'] for r in rs},{'jev_noul'});self.assertFalse([r for r in rs if r['degraded_stages']])
        self.assertLessEqual(self.m.jev_calls,16*5);self.assertEqual(jev.stats['failed'],0);self.assertLess(dt,3.0)   # ~2-3 Jev requests per search (plan + batches), not one per candidate
    def test_rerank_scores_cached_per_query_and_candidate(self):
        jev=J.AsyncJev('tok','m',self.m.url,900)
        def run(user):
            with mock.patch.object(A,'jev_client',return_value=jev),mock.patch.dict(os.environ,{'JEV_RERANK_ENABLED':'true'}):return aio(A.search(A.SearchBody(query='parental leave sixteen weeks policy cached',mode='local'),T.Response(),reader(user),self.db))
        run('a');n=self.m.jev_calls;r=run('b');plan_only=self.m.jev_calls-n;self.assertEqual(r['reranker'],'jev_noul');self.assertLessEqual(plan_only,1)   # second user: scores come from the cache (only the plan call remains)
    def test_partial_answers_are_accepted_with_per_candidate_fallback(self):
        class Half(FakeJev):
            async def decide(s,state,q,t=None):
                a=await super().decide(state,q,t)
                return {k:v for k,v in a.items() if k!='c1'}   # the 2nd candidate of each batch gets no answer
        r=RerankHelper.run(self,Half());self.assertEqual(r['reranker'],'jev_partial');self.assertIn('jev_rerank_partial',r['degraded_stages']);self.assertTrue(r['results'])
class RerankHelper:
    @staticmethod
    def run(t,jev):
        with mock.patch.object(A,'jev_client',return_value=jev),mock.patch.dict(os.environ,{'JEV_RERANK_ENABLED':'true'}):return aio(A.search(A.SearchBody(query='parental leave sixteen weeks policy partial',mode='local'),T.Response(),reader('p'),t.db))

class BreakerAndWeb(unittest.TestCase):
    def test_empty_results_never_trip_the_breaker_but_errors_do(self):
        class Empty:
            name='e';engines=['E']
            def fetch(s,q,n,f=None):return []
        class Boom:
            name='b';engines=['B']
            def fetch(s,q,n,f=None):raise RuntimeError()
        br={}
        for _ in range(8):rows,meta=aio(ws.hedged_fetch([Empty()],br,'q',5));self.assertEqual((rows,meta['empty']),([],True))
        self.assertEqual(br['e'].state(),'closed');self.assertEqual(br['e'].fails,[])
        for _ in range(3):
            with self.assertRaises(ws.WebUnavailable):aio(ws.hedged_fetch([Boom()],br,'q',5))
        self.assertEqual(br['b'].state(),'open')
        with self.assertRaises(ws.WebUnavailable) as c:aio(ws.hedged_fetch([Boom()],br,'q',5))
        self.assertEqual((c.exception.reason,c.exception.sent),('breaker_open',[]))
    def test_ddgs_no_results_exception_is_an_empty_answer(self):
        class D:
            def __init__(s,**k):pass
            def text(s,q,**kw):raise Exception('No results found.')
        with mock.patch.dict(__import__('sys').modules,{'ddgs':mock.Mock(DDGS=D)}):self.assertEqual(ws.DdgsProvider('bing').fetch('q',8),[])
    def test_notice_is_accurate_for_cache_empty_sent_and_open_breaker(self):
        class P:
            def __init__(s,n,e):s.name=n;s.engines=[e]
        chain=[P('a','Alpha'),P('b','Beta')]
        self.assertIn('no new request was made',A.web_notice(chain,{'cache_hit':True},False));self.assertNotIn('Only your query text',A.web_notice(chain,{'cache_hit':True},False))
        n=A.web_notice(chain,{'sent':['b'],'empty':True},False);self.assertIn('Only your query text was sent to: Beta.',n);self.assertNotIn('Alpha',n);self.assertIn('no results',n);self.assertNotIn('unavailable',n)
        n=A.web_notice(chain,{'sent':[],'error':'breaker_open'},True);self.assertIn('No web request was made',n);self.assertNotIn('Only your query text',n);self.assertIn('unavailable',n)
    def test_ad_and_search_engine_results_are_dropped(self):
        rows=[{'title':'Best VPN 2026 ad','url':'https://www.bing.com/aclick?ld=abc&u=x','text':'vpn'},{'title':'serp','url':'https://www.bing.com/search?q=vpn','text':'vpn'},{'title':'maps','url':'https://www.google.com/maps/place/x','text':'vpn'},
              {'title':'ddg','url':'https://duckduckgo.com/y.js?ad_domain=x','text':'vpn'},{'title':'dc','url':'https://ad.doubleclick.net/ddm/clk/1','text':'vpn'},{'title':'yahoo redirect','url':'https://r.search.yahoo.com/_ylt=1/RU=x/RK=2','text':'vpn'},
              {'title':'Real review','url':'https://www.pcmag.com/picks/best-vpn','text':'vpn review'},{'title':'Docs','url':'https://support.google.com/chrome/answer/1','text':'vpn'},{'title':'news','url':'https://finance.yahoo.com/news/x','text':'vpn'}]
        out={r['domain'] for r in ws.rank_hits('best vpn 2026',rows,10)};self.assertEqual(out,{'pcmag.com','support.google.com','finance.yahoo.com'})
    def test_cache_hit_never_calls_jev_even_after_a_failed_rerank(self):
        local_env(self);calls=[]
        class P:
            name='p';engines=['PE']
            def fetch(s,q,n,f=None):calls.append(q);return [{'title':'a page','url':'https://a.example/1','text':'alpha beta'},{'title':'b page','url':'https://b.example/2','text':'alpha gamma'}]
        j=FakeJev(fail=True)
        with mock.patch.object(A,'web_searcher',return_value=ws.WebSearcher(chain=[P()])),mock.patch.object(A,'jev_client',return_value=j),mock.patch.dict(os.environ,{'WEB_SEARCH_ENABLED':'true','WEB_RERANK_ENABLED':'true','JEV_RERANK_ENABLED':'true'}):
            r1=aio(A.search(A.SearchBody(query='alpha beta unique-cachejev',mode='web'),T.Response(),reader(),self.db));n=len(j.calls)
            r2=aio(A.search(A.SearchBody(query='alpha beta unique-cachejev',mode='web'),T.Response(),reader(),self.db))
        self.assertEqual(r1['reranker'],'bm25_fallback');self.assertGreaterEqual(n,1);self.assertEqual(len(j.calls),n);self.assertEqual(len(calls),1);self.assertNotIn('jev_rerank_unavailable',r2['degraded_stages']);self.assertLess(r2['timings']['total'],150)
    def test_web_rerank_is_off_by_default(self):
        local_env(self);j=FakeJev()
        class P:
            name='p';engines=['PE']
            def fetch(s,q,n,f=None):return [{'title':'a page','url':'https://a.example/1','text':'alpha beta'},{'title':'b page','url':'https://b.example/2','text':'alpha gamma'}]
        with mock.patch.object(A,'web_searcher',return_value=ws.WebSearcher(chain=[P()])),mock.patch.object(A,'jev_client',return_value=j),mock.patch.dict(os.environ,{'WEB_SEARCH_ENABLED':'true','JEV_RERANK_ENABLED':'true'}):
            os.environ.pop('WEB_RERANK_ENABLED',None);r=aio(A.search(A.SearchBody(query='alpha beta unique-default-off',mode='web'),T.Response(),reader(),self.db))
        self.assertEqual(r['reranker'],'bm25');self.assertFalse([1 for _,q in j.calls if 'c0' in q or 'rel' in q])

class Privacy(unittest.TestCase):
    def setUp(self):
        local_env(self);self.calls=[];outer=self
        class P:
            name='count';engines=['Counter']
            def fetch(s,q,n,f=None):outer.calls.append(q);return [{'title':'x','url':'https://x.example/1','text':'parental leave'}]
        self.searcher=ws.WebSearcher(chain=[P()]);e=mock.patch.dict(os.environ,{'WEB_SEARCH_ENABLED':'true'});e.start();self.addCleanup(e.stop)
    def go(self,**kw):
        with mock.patch.object(A,'web_searcher',return_value=self.searcher),mock.patch.object(A,'jev_client',return_value=FakeJev(enabled=False)):
            return aio(A.search(A.SearchBody(query=f"parental leave sixteen weeks {kw.pop('tag','')}",**kw),T.Response(),reader(),self.db))
    def test_local_mode_with_web_true_never_fetches(self):
        r=self.go(mode='local',web=True,tag='p1');self.assertEqual(self.calls,[]);self.assertEqual(r['route'],'local');self.assertNotIn('web_fetch',[k for k,v in r['timings'].items() if v is not None])
    def test_web_and_agent_modes_do_fetch_only_with_opt_in(self):
        self.go(mode='agent',web=False,tag='p2');self.assertEqual(self.calls,[])
        self.go(mode='agent',web=True,tag='p3');self.assertEqual(len(self.calls),1)
        self.go(mode='web',tag='p4');self.assertEqual(len(self.calls),2)
    def test_overrides_are_admin_only(self):
        with self.assertRaises(HTTPException) as c:aio(A.search(A.SearchBody(query='parental leave',rerank=False),T.Response(),reader(),self.db))
        self.assertEqual(c.exception.status_code,403)
        with self.assertRaises(HTTPException) as c:aio(A.search(A.SearchBody(query='parental leave',embed_cache=False),T.Response(),reader(),self.db))
        self.assertEqual(c.exception.status_code,403)
        with mock.patch.object(A,'jev_client',return_value=FakeJev()):
            adm=User(username='adm',password_hash='x',role='admin');r=aio(A.search(A.SearchBody(query='parental leave sixteen weeks',mode='local',rerank=False),T.Response(),adm,self.db));self.assertEqual(r['reranker'],'rrf')

class LocalRelevanceFloors(unittest.TestCase):
    class H:
        def __init__(s,cid,doc,score):s.payload={'chunk_id':cid,'doc_id':doc,'page':1};s.score=score
    def setUp(self):
        local_env(self);T.add_doc(self.db,'usb',['Removable storage such as USB flash drives must be encrypted before use on company laptops, and any device found unencrypted will be reported to the security team for review within two working days.']);T.add_doc(self.db,'deploy',['Deploys go out on weekdays after the change review has been completed and the release manager has signed off the checklist for the planned production window and rollback plan.'])
    def run_q(self,q,hits):
        A.vectors.search.return_value=hits
        with mock.patch.object(A,'jev_client',return_value=FakeJev(enabled=False)):return aio(A.search(A.SearchBody(query=q,mode='local'),T.Response(),reader(),self.db))
    def test_semantic_match_survives_content_term_bm25(self):
        r=self.run_q('can I plug in a thumb drive',[self.H('usb-0','usb',0.52),self.H('deploy-0','deploy',0.15)])
        self.assertEqual([x['document_id'] for x in r['results']],['usb'])   # no lexical overlap: dense neighbour above the floor is returned, the 0.15 one is dropped
    def test_no_relevant_results_floor(self):
        r=self.run_q('kubernetes autoscaling',[self.H('deploy-0','deploy',0.29),self.H('usb-0','usb',0.24)])
        self.assertEqual(r['results'],[]);self.assertEqual(r['empty_reason'],'no_relevant_results')
    def test_lexical_hit_is_kept_even_with_weak_dense(self):
        r=self.run_q('deploys weekdays',[self.H('deploy-0','deploy',0.1)]);self.assertEqual([x['document_id'] for x in r['results']],['deploy'])
    def test_bm25_ignores_stop_words(self):
        from bm25 import Bm25Index
        idx=Bm25Index(['how can i do the thing at all in the office','usb drive encryption policy for laptops']);sc=idx.scores('how can i plug in a usb drive');self.assertGreater(sc[1],sc[0])

class OverviewEdgeCases(unittest.TestCase):
    def test_headings_allowed_but_every_factual_sentence_needs_a_citation(self):
        st,c=OV.validate('Key details:\nParental leave is sixteen weeks [S1].\n- Applies to both parents [S2].',2);self.assertEqual(st,'ok');self.assertEqual(len(c),2)
        self.assertEqual(OV.validate('Key details:\nParental leave is sixteen weeks.',2)[0],'invalid');self.assertEqual(OV.validate('Key details:',2)[0],'invalid')
        self.assertEqual(OV.validate('## Summary\nLeave is long [S1].',1)[0],'ok')
    def test_markdown_emphasis_removed_consistently(self):
        st,lines=OV.parse('**Parental leave** is __sixteen__ weeks [S1].',1);self.assertEqual(st,'ok');self.assertEqual(OV.assemble(lines,{0}),'Parental leave is sixteen weeks [S1].')
    def test_assemble_keeps_heading_only_with_surviving_claims_and_bullets(self):
        st,lines=OV.parse('Lead sentence [S1].\nDetails:\n- First bullet [S1].\n- Second bullet [S2].',2);self.assertEqual(OV.assemble(lines,{0,1,2}),'Lead sentence [S1].\nDetails:\n- First bullet [S1].\n- Second bullet [S2].')
        self.assertEqual(OV.assemble(lines,{0}),'Lead sentence [S1].');self.assertEqual(OV.assemble(lines,{0,2}),'Lead sentence [S1].\nDetails:\n- Second bullet [S2].')
    def test_insufficient_and_out_of_range(self):
        self.assertEqual(OV.validate('insufficient',2)[0],'insufficient');self.assertEqual(OV.validate('A [S3].',2)[0],'invalid');self.assertEqual(OV.validate('',2)[0],'insufficient')
    def test_grounding_gets_query_and_lead_context(self):
        seen=[]
        class Rec:
            enabled=True
            async def decide(s,state,q,t=None):seen.append(state);return {'supported':{'noul':0.8}}
        aio(J.ground_sentences(Rec(),[('Install Acme Secure Client [S3].',['src'])],query='how do I set up the vpn',lead='To use the VPN you must install the client [S3].'))
        self.assertEqual(seen[0]['query'],'how do I set up the vpn');self.assertIn('install the client',seen[0]['context']);self.assertLessEqual(len(json.dumps(seen[0])),6000)
    def test_jev_outage_is_not_a_verdict(self):
        rec={'query':'how many weeks of parental leave','eligible':True,'route':'local','evidence':[{'title':'t','text':'Sixteen weeks','source':'local','rel':0.9,'chunk_id':'c1'}]}
        async def run(jev,rec=rec):return ''.join([c async for c in OV.stream_overview(rec,jev,FakeGen('Parental leave is sixteen weeks [S1].'),'sx')])
        out=aio(run(FakeJev(fail=True)));self.assertIn('jev_unavailable',out);self.assertNotIn('unsupported_claim',out)   # grounding calls all failed: not scored 0.0
        rec2=dict(rec,evidence=[dict(rec['evidence'][0],rel=None)]);out=aio(run(FakeJev(fail=True),rec2));self.assertIn('jev_unavailable',out);self.assertNotIn('insufficient_evidence',out)   # sufficiency call failed
    def test_evidence_at_rest_is_ids_only_and_encrypted(self):
        cache.lru.clear();OV.save_evidence('sidsidsidsid1','alice','private question about bonus',True,'local',[{'title':'Restricted Matrix','text':'CEO bonus is 5 million','source':'local','chunk_id':'c1','document_id':'d1','version':1,'page':1,'rel':0.9}])
        raw=json.dumps(cache.lru['sid:v1:sidsidsidsid1'][1]);self.assertNotIn('bonus',raw);self.assertNotIn('Restricted',raw);rec=OV.load_evidence('sidsidsidsid1','alice');self.assertNotIn('text',rec['evidence'][0]);self.assertNotIn('title',rec['evidence'][0]);self.assertEqual(rec['query'],'private question about bonus')
        self.assertIsNone(OV.load_evidence('sidsidsidsid1','bob'))
    def test_overview_cache_value_is_encrypted(self):
        cache.lru.clear();T.fresh();db=Db();T.add_doc(db,'hr',['Parental leave is sixteen weeks of paid time off for new parents at the company.']);gen=FakeGen('Parental leave is sixteen weeks [S1].')
        rec={'query':'how many weeks of parental leave','eligible':True,'route':'local','evidence':[{'title':'hr','text':'Parental leave is sixteen weeks','source':'local','rel':0.9,'chunk_id':'hr-0'}]}
        async def run():return [c async for c in OV.stream_overview(rec,FakeJev(),gen,'sx')]
        aio(run());keys=[k for k in cache.lru if k.startswith('ov:v1:')];self.assertTrue(keys);self.assertNotIn('sixteen',json.dumps(cache.lru[keys[0]][1]));db.close()
    def test_rebuild_rechecks_current_acl(self):
        local_env(self);T.add_doc(self.db,'sec',['bob only restricted content'],acl='bob')
        ev=[{'source':'local','chunk_id':'sec-0','document_id':'sec','rel':0.9},{'source':'web','title':'w','text':'t','url':'https://a.example'}]
        self.assertEqual([e['source'] for e in A.rebuild_evidence(A.P('alice','reader'),ev)],['web']);self.assertEqual([e['source'] for e in A.rebuild_evidence(A.P('bob','reader'),ev)],['local','web'])

class EmbedBatching(unittest.TestCase):
    def test_concurrent_query_embeddings_are_coalesced_and_cached(self):
        calls=[]
        class Arr(list):
            def tolist(s):return list(s)
        class M:
            def encode(s,texts,**k):calls.append(len(texts));time.sleep(0.02);return Arr([[float(len(t))] for t in texts])
        e=PR.LocalEmbedder('x',1);e.model=M();out={}
        ts=[threading.Thread(target=lambda i=i:out.__setitem__(i,e.encode_query(f'query number {i}'))) for i in range(12)]
        [t.start() for t in ts];[t.join() for t in ts]
        self.assertEqual(sorted(out),list(range(12)));self.assertTrue(all(out[i]==[float(len(f'query number {i}'))] for i in range(12)))   # each caller got ITS vector
        self.assertLess(len(calls),12);self.assertEqual(sum(calls),12)
        n=len(calls);e.encode_query('query number 3');self.assertEqual(len(calls),n)   # cache hit: no model call
        e.encode_query('query number 3',use_cache=False);self.assertEqual(len(calls),n+1)   # bypass for cold measurements

class WordGate(unittest.TestCase):
    def test_counts(self):
        wc=PL.word_count;self.assertEqual(wc("don't forget e-mail setup now"),5);self.assertEqual(wc('what is vpn'),3);self.assertEqual(wc('a - b'),2);self.assertEqual(wc('one two three four'),4)
    def test_cjk_and_thai_use_a_character_heuristic(self):
        self.assertGreater(PL.word_count('我想知道如何重置密码'),3);self.assertLessEqual(PL.word_count('重置密码吗'),3);self.assertGreater(PL.word_count('ภาษาไทยคำถามยาวมาก'),3)
        self.assertEqual(OV.eligibility('我想知道如何重置密码吗',True,{'overview_worthy':0.9},2),(True,'eligible'));self.assertEqual(OV.eligibility('重置密码',True,{'overview_worthy':0.9},2),(False,'too_short'))

class UrlValidation(unittest.TestCase):
    def test_accepts_http_https_and_private_hosts(self):
        for u in ('https://api.typesafe.ai','http://localhost:11434/v1','http://llm:8000/v1/','https://integrate.api.nvidia.com/v1','http://10.0.0.5:9000'):self.assertTrue(urlcheck.validate_provider_url(u))
        self.assertEqual(urlcheck.validate_provider_url('https://a.example/v1/'),'https://a.example/v1')
    def test_rejects_bad_schemes_credentials_and_metadata_hosts(self):
        for u in ('ftp://x.example','file:///etc/passwd','javascript:alert(1)','https://user:pw@a.example','https://user@a.example','http://169.254.169.254/latest','http://[fe80::1]/','http://metadata.google.internal/x','http://100.100.100.200/','https:///nohost','','http://0.0.0.0:80'):
            with self.assertRaises(ValueError,msg=u):urlcheck.validate_provider_url(u)
    def test_allowlist(self):
        self.assertTrue(urlcheck.validate_provider_url('https://api.typesafe.ai',allowlist='api.typesafe.ai,.nvidia.com'));self.assertTrue(urlcheck.validate_provider_url('https://integrate.api.nvidia.com/v1',allowlist='.nvidia.com'))
        with self.assertRaises(ValueError):urlcheck.validate_provider_url('https://evil.example',allowlist='api.typesafe.ai,.nvidia.com')
        with self.assertRaises(ValueError):urlcheck.validate_provider_url('https://notnvidia.com',allowlist='.nvidia.com')
    def test_invalid_url_disables_the_provider_instead_of_sending_a_key(self):
        self.assertFalse(J.AsyncJev('tok','m','http://169.254.169.254').enabled);self.assertEqual(urlcheck.safe_provider_url('ftp://x'),'')
        self.assertEqual([p.name for p in ws.build_chain({'SEARXNG_URL':'http://169.254.169.254','WEB_PROVIDERS':'searxng,ddgs'}.get)],['ddgs'])
    def test_admin_config_rejects_bad_urls_and_host_change_needs_key_reentry(self):
        T.fresh();db=Db();admin=User(username='a',password_hash='x',role='admin')
        with self.assertRaises(HTTPException) as c:A.save_config(A.ConfigBody(llm_base_url='http://169.254.169.254/v1'),admin,db)
        self.assertEqual(c.exception.status_code,422)
        with mock.patch.dict(os.environ,{'TYPESAFE_API_KEY':'stored-key'}):
            out=A.save_config(A.ConfigBody(jev_base_url='https://other.example'),admin,db)  # host change: the saved key is cleared (never sent elsewhere) and the response says so
            self.assertTrue(any('key was cleared' in w for w in out['warnings']));self.assertFalse(out['typesafe_configured'])
        db.close()

class AdminUsers(unittest.TestCase):
    def setUp(self):
        T.fresh();self.db=Db();self.admin=User(username='root',password_hash='x',role='admin');self.db.add_all([self.admin,User(username='bob',password_hash='x',role='reader'),User(username='eve',password_hash='x',role='admin')]);self.db.commit()
    def tearDown(self):self.db.close()
    def test_delete_user_rules(self):
        self.assertEqual(A.delete_user('bob',self.admin,self.db),{'deleted':'bob'});self.assertEqual([u['username'] for u in A.list_users(self.admin,self.db)],['eve','root'])
        with self.assertRaises(HTTPException) as c:A.delete_user('root',self.admin,self.db)
        self.assertEqual(c.exception.status_code,400)
        with self.assertRaises(HTTPException) as c:A.delete_user('nobody',self.admin,self.db)
        self.assertEqual(c.exception.status_code,404)
        self.assertEqual(A.delete_user('eve',self.admin,self.db),{'deleted':'eve'})
        with self.assertRaises(HTTPException) as c:A.delete_user('root',self.admin,self.db)   # now the only admin and also self
        self.assertEqual(c.exception.status_code,400)
    def test_endpoints_require_admin_dependency(self):
        for fn in ('delete_user','list_users'):
            deps=[d.call for d in next(r for r in A.app.routes if getattr(r,'endpoint',None) is getattr(A,fn)).dependant.dependencies]
            self.assertIn(A.admin_user,deps)
    def test_last_admin_cannot_be_deleted(self):
        self.db.delete(self.db.query(User).filter_by(username='eve').one());self.db.commit();other=User(username='x',password_hash='x',role='admin')
        with self.assertRaises(HTTPException) as c:A.delete_user('root',other,self.db)
        self.assertEqual(c.exception.status_code,400)

class DenseWithPlan(unittest.TestCase):
    def setUp(self):local_env(self);e=mock.patch.dict(os.environ,{'WEB_SEARCH_ENABLED':'true'});e.start();self.addCleanup(e.stop)
    def test_dense_runs_concurrently_with_plan_and_is_skipped_for_web_route(self):
        class P:
            name='p';engines=['PE']
            def fetch(s,q,n,f=None):return [{'title':'x','url':'https://x.example/1','text':'alpha'}]
        with mock.patch.object(A,'web_searcher',return_value=ws.WebSearcher(chain=[P()])):
            order=[]
            A.embedder.encode_query.side_effect=lambda *a,**k:(order.append('dense'),[0.0])[1]
            j=FakeJev(delay=0.5,plan={'route':'local'})
            t=time.time()
            with mock.patch.object(A,'jev_client',return_value=j):r=aio(A.search(A.SearchBody(query='parental leave sixteen weeks',web=True),T.Response(),reader(),self.db))
            self.assertEqual(r['route'],'local');self.assertEqual(order,['dense']);self.assertIsNotNone(r['timings']['dense'])
            order.clear();A.vectors.search.reset_mock()
            with mock.patch.object(A,'jev_client',return_value=FakeJev(plan={'route':'web'})):r=aio(A.search(A.SearchBody(query='capital of atlantis unique-w',web=True),T.Response(),reader(),self.db))
            self.assertEqual(r['route'],'web');self.assertEqual(r['results'][0]['source'],'web')   # dense result (if it ran speculatively) is dropped; the web leg never sees vector data

if __name__=='__main__':unittest.main()
