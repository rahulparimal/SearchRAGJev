import os,unittest
from unittest import mock
import test_rag as T  # sets the sqlite test environment before backend modules load
import app as A
from database import Db,User

admin=lambda:User(username='adm',password_hash='x',role='admin')
class ResearchGeneratorTests(unittest.TestCase):
    def reader(self,vals):return lambda a,b:vals.get(a,'')
    def test_admin_deep_research_llm_settings_are_actually_used(self):
        """Regression: the generator used prefix DEEP_RESEARCH, so DEEP_RESEARCH_LLM_* saved by the admin tab were ignored."""
        g=self.reader({'DEEP_RESEARCH_LLM_BASE_URL':'http://dr.example/v1','DEEP_RESEARCH_LLM_MODEL':'dr-model','LLM_BASE_URL':'http://base.example/v1','LLM_MODEL':'base-model'})
        gen,gen2,role=A.research_generators(g);self.assertEqual(gen.model,'dr-model');self.assertIsNone(gen2);self.assertEqual(role,'failover')
    def test_falls_back_to_base_llm_when_blank(self):
        gen,_,_=A.research_generators(self.reader({'LLM_BASE_URL':'http://base.example/v1','LLM_MODEL':'base-model'}));self.assertEqual(gen.model,'base-model')
    def test_secondary_only_when_configured_and_never_inherits_base(self):
        g=self.reader({'LLM_BASE_URL':'http://base.example/v1','LLM_MODEL':'base-model'});self.assertIsNone(A.research_generators(g)[1])
        g=self.reader({'DEEP_RESEARCH_2_LLM_BASE_URL':'http://fast.example/v1','DEEP_RESEARCH_2_LLM_MODEL':'fast','DEEP_RESEARCH_SECONDARY_ROLE':'planner_and_failover'})
        _,gen2,role=A.research_generators(g);self.assertEqual(gen2.model,'fast');self.assertEqual(role,'planner_and_failover')
    def test_invalid_role_defaults_to_failover(self):
        self.assertEqual(A.research_generators(self.reader({'DEEP_RESEARCH_SECONDARY_ROLE':'weird'}))[2],'failover')

class AdminConfigTests(unittest.TestCase):
    def setUp(self):T.fresh();A.config_store.invalidate();self.db=Db();A._web[0]=None;A._web[1]=None
    def tearDown(self):A.config_store.invalidate();A._web[0]=None;A._web[1]=None
    def test_secondary_model_and_web_provider_settings_round_trip_without_leaking_keys(self):
        cfg=A.save_config(A.ConfigBody(deep_research2_llm_base_url='http://fast.example/v1',deep_research2_llm_model='fast-1',deep_research2_llm_api_key='sk-secret',deep_research_secondary_role='planner_and_failover',web_providers='brave_api,ddgs',brave_api_key='brave-secret',searxng_url='http://searx.example:8080'),admin(),self.db)
        self.assertEqual(cfg['deep_research2_llm_model'],'fast-1');self.assertTrue(cfg['deep_research2_llm_key_configured']);self.assertEqual(cfg['deep_research_secondary_role'],'planner_and_failover')
        self.assertEqual(cfg['web_providers_setting'],'brave_api,ddgs');self.assertTrue(cfg['brave_configured']);self.assertEqual(cfg['searxng_url'],'http://searx.example:8080')
        self.assertNotIn('sk-secret',str(cfg));self.assertNotIn('brave-secret',str(cfg))
    def test_rejects_unknown_provider_choice_bad_role_and_metadata_urls(self):
        for body in (A.ConfigBody(web_providers='evil'),A.ConfigBody(deep_research_secondary_role='x'),A.ConfigBody(searxng_url='http://169.254.169.254/'),A.ConfigBody(deep_research2_llm_base_url='http://169.254.169.254/v1')):
            with self.assertRaises(A.HTTPException) as c:A.save_config(body,admin(),self.db)
            self.assertEqual(c.exception.status_code,422)
    def test_changing_secondary_host_requires_retyping_its_key(self):
        A.save_config(A.ConfigBody(deep_research2_llm_base_url='http://a.example/v1',deep_research2_llm_model='m',deep_research2_llm_api_key='k'),admin(),self.db)
        with self.assertRaises(A.HTTPException) as c:A.save_config(A.ConfigBody(deep_research2_llm_base_url='http://b.example/v1'),admin(),self.db)
        self.assertEqual(c.exception.status_code,422)
    def test_web_searcher_rebuilds_chain_when_settings_change(self):
        with mock.patch.dict(os.environ,{'BRAVE_API_KEY':'','SEARXNG_URL':'','WEB_PROVIDERS':''}):
            first=A.web_searcher();self.assertEqual([p.name for p in first.chain],['ddgs'])
            A.save_config(A.ConfigBody(web_providers='brave_api,ddgs',brave_api_key='bk'),admin(),self.db)
            second=A.web_searcher();self.assertEqual([p.name for p in second.chain],['brave_api','ddgs']);self.assertIsNot(first,second)
            self.assertIs(A.web_searcher(),second)  # unchanged settings keep the same instance (and its breakers)
if __name__=='__main__':unittest.main()
