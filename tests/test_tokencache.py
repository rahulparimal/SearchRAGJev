import asyncio,sys,unittest
sys.path.insert(0,'backend')
import tokencache as TC
def aio(c):return asyncio.run(c)
class TokenCacheTests(unittest.TestCase):
    def setUp(self):TC.reset()
    def test_norm_query_folds_case_space_and_trailing_punct(self):
        self.assertEqual(TC.norm_query('  What is   Dharma? '),'what is dharma')
    def test_keys_differ_by_any_part(self):
        self.assertNotEqual(TC.make_key('r','q','m1'),TC.make_key('r','q','m2'))
        self.assertNotEqual(TC.make_key('r','q'),TC.make_key('p','q'))
    def test_put_get_roundtrip_counts_hit_and_tokens(self):
        async def go():
            k=TC.make_key('answer','roundtrip');await TC.put('answer',k,{'a':1},tokens=100)
            return await TC.get('answer',k)
        self.assertEqual(aio(go()),{'a':1});s=TC.snapshot();self.assertEqual(s['est_tokens_saved'],100);self.assertEqual(s['hit_rate'],1.0)
    def test_none_is_never_cached(self):
        async def go():
            k=TC.make_key('answer','none');await TC.put('answer',k,None);return await TC.get('answer',k)
        self.assertIsNone(aio(go()))
    def test_single_flight_runs_compute_once_for_concurrent_callers(self):
        n=[0]
        async def compute():
            n[0]+=1;await asyncio.sleep(0.05);return 'v'
        async def go():
            k=TC.make_key('answer','sf')
            return await asyncio.gather(*(TC.get_or_compute('answer',k,compute,tokens=10) for _ in range(5)))
        self.assertEqual(aio(go()),['v']*5);self.assertEqual(n[0],1);self.assertGreaterEqual(TC.snapshot()['namespaces']['answer']['coalesced'],1)
    def test_leader_failure_does_not_poison_followers(self):
        calls=[0]
        async def compute():
            calls[0]+=1;await asyncio.sleep(0.02)
            if calls[0]==1:raise RuntimeError('boom')
            return 'ok'
        async def go():
            k=TC.make_key('answer','fail')
            return await asyncio.gather(TC.get_or_compute('answer',k,compute),TC.get_or_compute('answer',k,compute),return_exceptions=True)
        r=aio(go());self.assertIn('ok',r);self.assertTrue(any(isinstance(x,RuntimeError) for x in r))
if __name__=='__main__':unittest.main()
