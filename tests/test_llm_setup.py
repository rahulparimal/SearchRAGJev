import asyncio,json,os,unittest
from unittest import mock
import test_rag as T  # sets the sqlite test environment before backend modules load
import httpx
import app as A
from database import Db,User

def aio(c):return asyncio.run(c)
admin=lambda:User(username='adm',password_hash='x',role='admin')
REAL=httpx.AsyncClient
LLM_ENV=('LLM_BASE_URL','LLM_API_KEY','LLM_MODEL','OVERVIEW_LLM_BASE_URL','OVERVIEW_LLM_MODEL','OVERVIEW_LLM_API_KEY','DIRECT_LLM_BASE_URL','DIRECT_LLM_MODEL','DIRECT_LLM_API_KEY','TYPESAFE_API_KEY')

class SetupStatusTests(unittest.TestCase):
    def setUp(self):T.fresh();self.db=Db()
    def env(self,**kw):
        e={k:'' for k in LLM_ENV};e.update(kw);return mock.patch.dict(os.environ,e)
    def test_nothing_configured_needs_setup(self):
        with self.env():s=A.setup_status(self.db)
        self.assertEqual(s,{'llm_configured':False,'jev_configured':False,'needs_setup':True})
    def test_ollama_needs_no_key(self):
        with self.env(LLM_BASE_URL='http://localhost:11434/v1',LLM_MODEL='llama3.2',TYPESAFE_API_KEY='k'):s=A.setup_status(self.db)
        self.assertFalse(s['needs_setup'])
    def test_llm_without_jev_still_needs_setup(self):
        with self.env(LLM_BASE_URL='http://x/v1',LLM_MODEL='m'):s=A.setup_status(self.db)
        self.assertTrue(s['llm_configured']);self.assertFalse(s['jev_configured']);self.assertTrue(s['needs_setup'])

class ProbeTests(unittest.TestCase):
    def setUp(self):
        T.fresh();self.db=Db();self.seen=[];self.status=200;self.payload={'choices':[{'message':{'content':'OK'}}],'data':[{'id':'b'},{'id':'a'},{'id':'a'}]};self.raise_=None
        def handler(req):
            self.seen.append(req)
            if self.raise_:raise self.raise_
            return httpx.Response(self.status,json=self.payload)
        p=mock.patch.object(A.httpx,'AsyncClient',lambda **kw:REAL(transport=httpx.MockTransport(handler),**kw));p.start();self.addCleanup(p.stop)
    def probe(self,body):return aio(A.llm_test(body,admin(),self.db))
    def models(self,body):return aio(A.llm_models(body,admin(),self.db))
    def test_ok_sends_chat_request_to_the_given_base(self):
        r=self.probe(A.LlmProbe(base_url='http://localhost:11434/v1/',model='llama3.2'))
        self.assertTrue(r['ok']);self.assertEqual(str(self.seen[0].url),'http://localhost:11434/v1/chat/completions');self.assertNotIn('authorization',self.seen[0].headers)
    def test_explicit_key_is_sent(self):
        self.probe(A.LlmProbe(base_url='http://x/v1',model='m',api_key='sk-abc'));self.assertEqual(self.seen[0].headers['authorization'],'Bearer sk-abc')
    def test_saved_key_reused_only_for_the_saved_base(self):
        with mock.patch.dict(os.environ,{'LLM_BASE_URL':'http://saved.example/v1','LLM_API_KEY':'saved-key'}):
            self.probe(A.LlmProbe(base_url='http://saved.example/v1',model='m'))
            self.probe(A.LlmProbe(base_url='http://other.example/v1',model='m'))
        self.assertEqual(self.seen[0].headers['authorization'],'Bearer saved-key');self.assertNotIn('authorization',self.seen[1].headers)
    def test_http_error_and_timeout_and_bad_json_reported_as_short_classes(self):
        self.status=401;self.assertEqual(self.probe(A.LlmProbe(base_url='http://x/v1',model='m'))['error'],'http_401')
        self.status=200;self.payload={'oops':1};self.assertEqual(self.probe(A.LlmProbe(base_url='http://x/v1',model='m'))['error'],'bad_response')
        self.raise_=httpx.ReadTimeout('t');self.assertEqual(self.probe(A.LlmProbe(base_url='http://x/v1',model='m'))['error'],'timeout')
        self.raise_=httpx.ConnectError('refused');self.assertEqual(self.probe(A.LlmProbe(base_url='http://x/v1',model='m'))['error'],'ConnectError')
    def test_rejects_metadata_url_and_missing_model(self):
        with self.assertRaises(A.HTTPException) as c:self.probe(A.LlmProbe(base_url='http://169.254.169.254/v1',model='m'))
        self.assertEqual(c.exception.status_code,422);self.assertEqual(self.seen,[])
        with self.assertRaises(A.HTTPException) as c:self.probe(A.LlmProbe(base_url='http://x/v1',model=' '))
        self.assertEqual(c.exception.status_code,422)
    def test_models_lists_sorted_unique_ids(self):
        r=self.models(A.LlmProbe(base_url='http://localhost:11434/v1'));self.assertEqual(r,{'ok':True,'models':['a','b']});self.assertEqual(str(self.seen[0].url),'http://localhost:11434/v1/models')
    def test_models_failure_is_not_an_exception(self):
        self.raise_=httpx.ConnectError('refused');r=self.models(A.LlmProbe(base_url='http://localhost:11434/v1'));self.assertFalse(r['ok']);self.assertEqual(r['models'],[])
if __name__=='__main__':unittest.main()
