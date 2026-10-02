import sys,unittest
from datetime import date
sys.path.insert(0,'backend')
from research_sections import classify_source,build_sections,assemble,published_year

def src(n,domain,title='T',url=None,published='',source='web',text='x'):return {'n':n,'domain':domain,'title':title,'url':url or f'https://{domain}/p','published':published,'source':source,'text':text}
def run(text,cites):return {'text':text,'cites':cites}
LLM=[{'t':'h2','text':'Executive summary'},{'t':'p','runs':[run('A [S1][S2].',[1,2]),run('B [S3].',[3])]},{'t':'h2','text':'Findings'},{'t':'ul','items':[[run('C [S1][S3].',[1,3])]]}]
BASE=dict(question='Q?',subqs=[{'question':'a'}],gaps=[],providers=['Brave Search API'],stats_in={'queries':['q1','q2'],'pages_fetched':0,'dropped_uncited':2,'models':{'plan':'m1','report':'m2'}},today=date(2026,10,2))
def build(numbered,verification,mode='synthesised',**kw):
    a=dict(BASE);a.update(kw);return build_sections(a['question'],a['subqs'],numbered,LLM,verification,a['gaps'],a['providers'],mode,a['stats_in'],a['today'])

class ClassifyTests(unittest.TestCase):
    def test_domain_types(self):
        for dom,want in (('www.nih.gov','Government'),('mit.edu','Academic'),('en.wikipedia.org','Encyclopedia'),('arxiv.org','Research'),('reddit.com','Community'),('medium.com','Blog'),('ibm.com','Vendor'),('github.com','Code'),('docs.python.org','Documentation'),('randomsite.xyz','unclassified')):
            self.assertIn(want,classify_source(dom)[0],dom)
    def test_empty_domain_is_internal_document(self):self.assertEqual(classify_source('','')[0],'Internal document')
    def test_year_extraction(self):self.assertEqual(published_year('2025-01-02T00:00:00'),2025);self.assertIsNone(published_year(''))

class SectionTests(unittest.TestCase):
    def test_register_has_one_row_per_source_and_counts_citations(self):
        sec=build([src(1,'nih.gov'),src(2,'medium.com'),src(3,'x.org'),src(4,'unused.com')],{'checked':4,'supported':4})
        rows=sec['register'][1]['rows'];self.assertEqual(len(rows),4);self.assertEqual(rows[0][5]['text'],'Cited in 2 statements');self.assertEqual(rows[3][5]['text'],'Not cited')
    def test_confidence_high_needs_verified_agreement_and_diversity(self):
        sec=build([src(1,'nih.gov'),src(2,'mit.edu'),src(3,'who.int')],{'checked':10,'supported':9});self.assertEqual(sec['confidence']['level'],'High')
    def test_confidence_low_when_few_sources_or_poor_verification(self):
        self.assertEqual(build([src(1,'a.com')],{'checked':3,'supported':3})['confidence']['level'],'Low')
        self.assertEqual(build([src(1,'a.com'),src(2,'b.com'),src(3,'c.com')],{'checked':10,'supported':2})['confidence']['level'],'Low')
    def test_extractive_mode_is_always_low(self):self.assertEqual(build([src(1,'nih.gov'),src(2,'mit.edu'),src(3,'who.int')],None,mode='extractive')['confidence']['level'],'Low')
    def test_assessment_says_not_assessed_instead_of_inventing(self):
        rows={r[0]['text']:r for r in build([src(1,'a.com'),src(2,'b.com')],None)['assessment'][1]['rows']}
        self.assertEqual(rows['Recency'][2]['text'],'Not assessed');self.assertEqual(rows['Data quality (claim checking)'][2]['text'],'Not assessed');self.assertEqual(rows['Applicability'][2]['text'],'Not assessed')
    def test_recency_uses_provider_dates(self):
        rows={r[0]['text']:r for r in build([src(1,'a.com',published='2026-01-01'),src(2,'b.com',published='2025-06-01')],None)['assessment'][1]['rows']}
        self.assertEqual(rows['Recency'][2]['text'],'Strong')
    def test_method_lists_queries_and_removed_statement_count_and_limitations(self):
        sec=build([src(1,'a.com')],None);text=str(sec['method'])
        self.assertIn('q1; q2',text);self.assertIn('2 statement(s) without a valid citation were removed',text);self.assertIn('snippets',text)
    def test_assemble_places_confidence_before_findings_and_appends_computed_sections(self):
        sec=build([src(1,'a.com'),src(2,'b.com'),src(3,'c.com')],{'checked':3,'supported':3});blocks=assemble(LLM,sec)
        heads=[b['text'] for b in blocks if b['t']=='h2'];self.assertEqual(heads,['Research brief','Executive summary','Findings','Evidence assessment','Research method','Source register','Appendix'])
        self.assertLess(next(i for i,b in enumerate(blocks) if b['t']=='confidence'),next(i for i,b in enumerate(blocks) if b['t']=='h2' and b['text']=='Findings'))
    def test_gaps_appear_in_the_appendix(self):
        sec=build([src(1,'a.com')],None,gaps=[{'id':'Q2','question':'Missing topic','reason':'no_results'}]);self.assertIn('Missing topic',str(sec['appendix']))
if __name__=='__main__':unittest.main()
