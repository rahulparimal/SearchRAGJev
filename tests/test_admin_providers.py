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
    def test_changing_secondary_host_clears_its_saved_key_and_warns(self):
        A.save_config(A.ConfigBody(deep_research2_llm_base_url='http://a.example/v1',deep_research2_llm_model='m',deep_research2_llm_api_key='k'),admin(),self.db)
        out=A.save_config(A.ConfigBody(deep_research2_llm_base_url='http://b.example/v1'),admin(),self.db)
        self.assertFalse(out['deep_research2_llm_key_configured']);self.assertTrue(any('key was cleared' in w for w in out['warnings']))
    def test_values_are_trimmed_and_urls_stored_normalised(self):
        out=A.save_config(A.ConfigBody(llm_base_url=' http://x.example/v1/ ',llm_model='  model-a  '),admin(),self.db)
        self.assertEqual(out['llm_model'],'model-a');self.assertEqual(out['llm_base_url'],'http://x.example/v1')
    def test_clear_beats_environment_and_can_empty_a_field(self):
        with mock.patch.dict(os.environ,{'OVERVIEW_LLM_MODEL':'env-model'}):
            self.assertEqual(A.get_config(admin(),self.db)['overview_llm_model'],'env-model')
            out=A.save_config(A.ConfigBody(clear=['overview_llm_model']),admin(),self.db)
            self.assertEqual(out['overview_llm_model'],'')
    def test_clear_rejects_unknown_fields_and_set_plus_clear(self):
        for body in (A.ConfigBody(clear=['nope']),A.ConfigBody(llm_model='x',clear=['llm_model'])):
            with self.assertRaises(A.HTTPException) as c:A.save_config(body,admin(),self.db)
            self.assertEqual(c.exception.status_code,422)
    def test_model_only_override_inherits_next_tier_url_and_key(self):
        from providers import resolve_llm
        g={'OVERVIEW_LLM_MODEL':'gemma','LLM_BASE_URL':'http://base.example/v1','LLM_API_KEY':'k','LLM_MODEL':'big'}.get
        b,k,m=resolve_llm(lambda a,e:g(a,''),[('DEEP_RESEARCH_LLM_BASE_URL','DEEP_RESEARCH_LLM_API_KEY','DEEP_RESEARCH_LLM_MODEL'),('OVERVIEW_LLM_BASE_URL','OVERVIEW_LLM_API_KEY','OVERVIEW_LLM_MODEL'),('LLM_BASE_URL','LLM_API_KEY','LLM_MODEL')])
        self.assertEqual((b,k,m),('http://base.example/v1','k','gemma'))
    def test_effective_table_names_the_tier_that_supplies_each_feature(self):
        with mock.patch.dict(os.environ,{'LLM_BASE_URL':'http://base.example/v1','LLM_MODEL':'big','OVERVIEW_LLM_MODEL':'gemma','DIRECT_LLM_BASE_URL':'','DIRECT_LLM_MODEL':'','DEEP_RESEARCH_LLM_BASE_URL':'','DEEP_RESEARCH_LLM_MODEL':'','OVERVIEW_LLM_BASE_URL':'','DEEP_RESEARCH_2_LLM_BASE_URL':'','DEEP_RESEARCH_2_LLM_MODEL':''}):
            e=A.effective_llms(self.db)
        self.assertEqual(e['overview']['model'],'gemma');self.assertEqual(e['quick']['model'],'gemma');self.assertEqual(e['quick']['model_tier'],'AI Overview');self.assertEqual(e['quick']['base_tier'],'Base LLM')
        self.assertEqual(e['research_secondary']['base'],'')
    def test_web_searcher_rebuilds_chain_when_settings_change(self):
        with mock.patch.dict(os.environ,{'BRAVE_API_KEY':'','SEARXNG_URL':'','WEB_PROVIDERS':''}):
            first=A.web_searcher();self.assertEqual([p.name for p in first.chain],['ddgs'])
            A.save_config(A.ConfigBody(web_providers='brave_api,ddgs',brave_api_key='bk'),admin(),self.db)
            second=A.web_searcher();self.assertEqual([p.name for p in second.chain],['brave_api','ddgs']);self.assertIsNot(first,second)
            self.assertIs(A.web_searcher(),second)  # unchanged settings keep the same instance (and its breakers)
if __name__=='__main__':unittest.main()
