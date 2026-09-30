import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from bm25 import Bm25Index
from agent import route_query
from web_search import web_search

class Fake:
    def fetch(self,q,n):return [{'title':'Cats','url':'u1','text':'felines'},{'title':'Paris','url':'u2','text':'capital of France is Paris'}]

class AgentTests(unittest.TestCase):
    def test_bm25_ranks_exact_terms(self):
        idx=Bm25Index(['parental leave is sixteen weeks','vpn reconnect every eight hours','annual leave twenty four days'])
        self.assertEqual(idx.search('vpn hours',3)[0][0],1);self.assertEqual(idx.search('zzz',3),[])
    def test_route_without_jev_uses_bm25(self):
        none=lambda s,q:{}
        self.assertEqual(route_query('capital of France',none,0.0)[0],'web')
        self.assertEqual(route_query('parental leave',none,1.0)[0],'local')
    def test_route_jev_and_override(self):
        web=lambda s,q:{'route':{'choice':'web'}};local=lambda s,q:{'route':{'choice':'local'}}
        self.assertEqual(route_query('x',web,0.0)[0],'web');self.assertEqual(route_query('x',local,0.0)[0],'local')
        self.assertEqual(route_query('x',web,0.8)[0],'local')
    def test_web_disabled_forces_local(self):self.assertEqual(route_query('x',lambda s,q:{},0.0,web_enabled=False)[0],'local')
    def test_web_results_bm25_ranked(self):
        r=web_search('capital of France',2,provider=Fake());self.assertEqual(r[0]['url'],'u2')
if __name__=='__main__':unittest.main()
