import sys,unittest
sys.path.insert(0,'backend')
from research_report import parse_report,to_markdown

class ParseReportTests(unittest.TestCase):
    def test_basic_structure_and_citations(self):
        md="## Summary\nThe sky is blue during the day [S1]. At night it is dark [S2].\n\n## Details\nMore detail here [S1]."
        r=parse_report(md,2)
        self.assertEqual([b['t'] for b in r['blocks']],['h2','p','h2','p'])
        self.assertEqual(r['blocks'][1]['runs'][0]['cites'],[1]);self.assertEqual(r['blocks'][1]['runs'][1]['cites'],[2])
        self.assertEqual(r['stats']['dropped_uncited'],0)
    def test_title_comes_from_the_plan_stage_not_the_body(self):
        """The body always starts with a real heading block ('## Summary'); the report's title is supplied separately
        by the caller (the plan stage), never sniffed from the first heading in the body."""
        r=parse_report("## Summary\nSome text [S1].",1)
        self.assertNotIn('title',r);self.assertEqual([b['t'] for b in r['blocks']],['h2','p'])
    def test_uncited_claim_is_dropped(self):
        r=parse_report("## S\nThis has no citation at all.",3)
        self.assertEqual(r['blocks'][1:],[])  # the paragraph had its only sentence dropped -> no 'p' block emitted
        self.assertEqual(r['stats']['dropped_uncited'],1)
    def test_coverage_gap_statement_survives_without_citation(self):
        r=parse_report("## Q3\nThe gathered sources do not cover this topic in enough detail.",2)
        self.assertEqual(len(r['blocks'][1]['runs']),1);self.assertEqual(r['blocks'][1]['runs'][0]['cites'],[])
        self.assertEqual(r['stats']['dropped_uncited'],0)
    def test_out_of_range_citation_is_dropped_not_kept(self):
        r=parse_report("## S\nA claim citing a source that does not exist [S99].",2)
        self.assertEqual(r['blocks'][1:],[]);self.assertEqual(r['stats']['dropped_out_of_range_only'],1)
    def test_mixed_valid_and_invalid_citations_keeps_only_valid(self):
        r=parse_report("## S\nA claim with one good and one bad citation [S1][S99].",2)
        self.assertEqual(r['blocks'][1]['runs'][0]['cites'],[1])
    def test_table_parsed_with_citations_and_short_table_dropped(self):
        md="## Compare\n| Option | Cost |\n|---|---|\n| A | Cheap [S1] |\n| B | Expensive [S2] |"
        r=parse_report(md,2)
        tbl=[b for b in r['blocks'] if b['t']=='table'];self.assertEqual(len(tbl),1)
        self.assertEqual(tbl[0]['head'],['Option','Cost']);self.assertEqual(len(tbl[0]['rows']),2)
        self.assertEqual(tbl[0]['rows'][0][1]['cites'],[1])
        # a table with only 1 data row is dropped entirely
        r2=parse_report("## C\n| A | B |\n|---|---|\n| x | y [S1] |",1)
        self.assertEqual([b for b in r2['blocks'] if b['t']=='table'],[]);self.assertEqual(r2['stats']['tables_dropped'],1)
    def test_table_row_with_no_citation_anywhere_is_dropped(self):
        md="## C\n| A | B |\n|---|---|\n| x [S1] | y |\n| no cite here | also none |\n| z | w [S1] |"
        r=parse_report(md,1)
        tbl=[b for b in r['blocks'] if b['t']=='table'][0]
        self.assertEqual(len(tbl['rows']),2);self.assertEqual(r['stats']['table_rows_dropped'],1)
    def test_bullet_list_parsed_and_validated_like_paragraphs(self):
        md="## L\n- First point [S1].\n- Second point with no citation.\n- Third point [S2]."
        r=parse_report(md,2)
        ul=[b for b in r['blocks'] if b['t']=='ul'][0]
        self.assertEqual(len(ul['items']),2)  # the uncited bullet is dropped entirely (empty runs -> item omitted)
    def test_html_tags_markdown_links_and_emphasis_are_stripped(self):
        md="## S\n**Bold** claim with a [link](javascript:alert(1)) and <script>alert(1)</script> text [S1]."
        r=parse_report(md,1)
        text=r['blocks'][1]['runs'][0]['text']
        self.assertNotIn('<script',text);self.assertNotIn('javascript:',text);self.assertNotIn('**',text);self.assertIn('link',text)
    def test_citation_variants_normalised(self):
        for variant in ('[1]','[Source 1]','[S 1]'):
            r=parse_report(f"## S\nA claim with a variant citation {variant}.",1)
            self.assertEqual(r['blocks'][1]['runs'][0]['cites'],[1],variant)
    def test_injection_fake_heading_and_fake_table_inside_a_sentence_does_not_forge_structure(self):
        """A citation's source text could contain markdown-looking strings; this module only parses the MODEL's own
        markdown output (sources are embedded as plain delimited text in the prompt, never re-parsed as markdown), but
        verify that stray '##'/'|' characters arriving mid-sentence don't crash or silently create new blocks."""
        md="## Summary\nSome text mentions a ## fake heading and a | fake | table | inline, all in one sentence [S1]."
        r=parse_report(md,1)
        self.assertEqual(r['blocks'][0],{'t':'h2','text':'Summary'})
        self.assertTrue(any(b['t']=='p' for b in r['blocks']))
    def test_drop_rate_threshold_signal(self):
        md="## S\n"+' '.join(f"Claim {i} with no citation." for i in range(8))+" One cited claim [S1]."
        r=parse_report(md,1)
        self.assertGreater(r['drop_rate'],0.3)
    def test_empty_input_has_no_claims_and_zero_drop_rate(self):
        r=parse_report('',5);self.assertEqual(r['blocks'],[]);self.assertEqual(r['drop_rate'],0.0)
    def test_to_markdown_roundtrips_cleanly(self):
        md="## Summary\nA fact [S1].\n\n| A | B |\n|---|---|\n| x [S1] | y [S1] |\n| p [S1] | q [S1] |"
        out=to_markdown(parse_report(md,1),title='My Report')
        self.assertIn('# My Report',out);self.assertIn('## Summary',out);self.assertIn('A fact [S1].',out);self.assertIn('| x [S1] | y [S1] |',out)
    def test_uncited_value_cells_inherit_row_citations_and_are_marked(self):
        r=parse_report("## T\n| Item | A | B |\n|---|---|---|\n| X | one [S1] | two |\n| Y | three [S1] | four [S1] |\n",1)
        cell=[b for b in r['blocks'] if b['t']=='table'][0]['rows'][0][2]
        self.assertEqual(cell['cites'],[1]);self.assertTrue(cell['rowcited'])

if __name__=='__main__':unittest.main()
