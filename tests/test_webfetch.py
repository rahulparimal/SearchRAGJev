import asyncio,os,sys,unittest
from unittest import mock
sys.path.insert(0,'backend')
import httpx
import webfetch as WF

def aio(c):return asyncio.run(c)

class IsGlobalHostTests(unittest.TestCase):
    def addrinfo(self,*ips):return [(None,None,None,None,(ip,0)) for ip in ips]
    def test_rejects_loopback(self):
        with mock.patch.object(WF.socket,'getaddrinfo',return_value=self.addrinfo('127.0.0.1')):
            self.assertFalse(WF._is_global_host('localhost'))
    def test_rejects_private_ranges(self):
        for ip in ('10.0.0.5','172.16.0.1','192.168.1.1'):
            with mock.patch.object(WF.socket,'getaddrinfo',return_value=self.addrinfo(ip)):
                self.assertFalse(WF._is_global_host('internal.example'),ip)
    def test_rejects_link_local_and_cloud_metadata(self):
        with mock.patch.object(WF.socket,'getaddrinfo',return_value=self.addrinfo('169.254.169.254')):
            self.assertFalse(WF._is_global_host('metadata.example'))
    def test_rejects_unspecified_and_multicast(self):
        for ip in ('0.0.0.0','224.0.0.1'):
            with mock.patch.object(WF.socket,'getaddrinfo',return_value=self.addrinfo(ip)):
                self.assertFalse(WF._is_global_host('x'),ip)
    def test_accepts_public_ip(self):
        with mock.patch.object(WF.socket,'getaddrinfo',return_value=self.addrinfo('93.184.216.34')):
            self.assertTrue(WF._is_global_host('example.com'))
    def test_one_private_ip_among_multiple_rejects_the_whole_host(self):
        """DNS can return multiple A records; if ANY resolves privately, reject (defends against a host that round-robins
        between a public decoy and an internal address)."""
        with mock.patch.object(WF.socket,'getaddrinfo',return_value=self.addrinfo('93.184.216.34','10.0.0.1')):
            self.assertFalse(WF._is_global_host('mixed.example'))
    def test_dns_failure_rejects(self):
        with mock.patch.object(WF.socket,'getaddrinfo',side_effect=WF.socket.gaierror('no such host')):
            self.assertFalse(WF._is_global_host('nonexistent.invalid'))
    def test_ipv4_mapped_ipv6_private_is_rejected(self):
        with mock.patch.object(WF.socket,'getaddrinfo',return_value=self.addrinfo('::ffff:127.0.0.1')):
            self.assertFalse(WF._is_global_host('mapped.example'))

class AllowlistTests(unittest.TestCase):
    def test_empty_allowlist_allows_everything(self):
        with mock.patch.dict(os.environ,{'DEEP_RESEARCH_FETCH_ALLOWLIST':''}):
            self.assertTrue(WF._allowlisted('anything.example'))
    def test_exact_and_suffix_match(self):
        with mock.patch.dict(os.environ,{'DEEP_RESEARCH_FETCH_ALLOWLIST':'wikipedia.org,.trusted.example'}):
            self.assertTrue(WF._allowlisted('wikipedia.org'))
            self.assertTrue(WF._allowlisted('en.wikipedia.org'))
            self.assertTrue(WF._allowlisted('sub.trusted.example'))
            self.assertFalse(WF._allowlisted('evil.example'))

class TextExtractorTests(unittest.TestCase):
    def test_strips_script_style_nav_footer(self):
        html='<html><head><style>.x{color:red}</style></head><body><nav>Home | About</nav><script>evil()</script><main>Real content here.</main><footer>copyright</footer></body></html>'
        p=WF._TextExtractor();p.feed(html)
        t=p.text()
        self.assertIn('Real content here.',t)
        self.assertNotIn('evil()',t);self.assertNotIn('color:red',t);self.assertNotIn('Home',t);self.assertNotIn('copyright',t)

class FetchPageTests(unittest.TestCase):
    def test_rejects_non_http_scheme(self):
        self.assertIsNone(aio(WF.fetch_page('file:///etc/passwd')))
    def test_rejects_host_without_hostname(self):
        self.assertIsNone(aio(WF.fetch_page('http://')))
    def test_falls_back_to_none_when_not_globally_routable(self):
        with mock.patch.object(WF,'_is_global_host',return_value=False):
            self.assertIsNone(aio(WF.fetch_page('http://internal.example/page')))
    def test_rejects_disallowed_content_type(self):
        with mock.patch.object(WF,'_is_global_host',return_value=True):
            resp=httpx.Response(200,headers={'content-type':'application/pdf'},content=b'%PDF-1.4')
            with mock.patch.object(WF,'send',return_value=resp):
                self.assertIsNone(aio(WF.fetch_page('http://example.com/doc.pdf')))
    def test_extracts_text_from_html_response(self):
        with mock.patch.object(WF,'_is_global_host',return_value=True):
            resp=httpx.Response(200,headers={'content-type':'text/html; charset=utf-8'},content=b'<html><body><main>The Siva Purana discusses the nature of the self.</main></body></html>')
            with mock.patch.object(WF,'send',return_value=resp):
                text=aio(WF.fetch_page('http://example.com/page'))
        self.assertIn('Siva Purana',text)
    def test_follows_redirect_then_fetches_final_url(self):
        with mock.patch.object(WF,'_is_global_host',return_value=True):
            redirect=httpx.Response(302,headers={'location':'http://example.com/final'})
            final=httpx.Response(200,headers={'content-type':'text/html'},content=b'<html><body>Final content.</body></html>')
            calls=[]
            async def fake_send(client,method,url,left,**kw):
                calls.append(url);return redirect if url=='http://example.com/start' else final
            with mock.patch.object(WF,'send',side_effect=fake_send):
                text=aio(WF.fetch_page('http://example.com/start'))
        self.assertIn('Final content.',text);self.assertEqual(calls,['http://example.com/start','http://example.com/final'])
    def test_too_many_redirects_gives_up(self):
        with mock.patch.object(WF,'_is_global_host',return_value=True):
            n=[0]
            async def fake_send(client,method,url,left,**kw):
                n[0]+=1;return httpx.Response(302,headers={'location':f'http://example.com/{n[0]}'})
            with mock.patch.object(WF,'send',side_effect=fake_send):
                self.assertIsNone(aio(WF.fetch_page('http://example.com/0')))
            self.assertLessEqual(n[0],WF.MAX_REDIRECTS+1)
    def test_byte_cap_truncates_body_without_crashing(self):
        big=b'<html><body><main>'+b'x'*50+b'</main></body></html>'
        with mock.patch.object(WF,'_is_global_host',return_value=True),mock.patch.object(WF,'PAGE_MAX_BYTES',10):
            resp=httpx.Response(200,headers={'content-type':'text/html'},content=big)
            with mock.patch.object(WF,'send',return_value=resp):
                text=aio(WF.fetch_page('http://example.com/huge'))
        self.assertIsNotNone(text)  # truncated but still processed, not a crash
    def test_network_error_returns_none_not_raises(self):
        with mock.patch.object(WF,'_is_global_host',return_value=True),mock.patch.object(WF,'send',side_effect=httpx.ConnectError('boom')):
            self.assertIsNone(aio(WF.fetch_page('http://example.com/down')))
    def test_not_allowlisted_rejects_before_dns(self):
        dns_called=[]
        with mock.patch.dict(os.environ,{'DEEP_RESEARCH_FETCH_ALLOWLIST':'trusted.example'}),mock.patch.object(WF.socket,'getaddrinfo',side_effect=lambda *a,**k:(dns_called.append(1),[])[1]):
            self.assertIsNone(aio(WF.fetch_page('http://untrusted.example/page')))
        self.assertEqual(dns_called,[])

class FetchPagesTests(unittest.TestCase):
    def test_only_successful_fetches_are_returned(self):
        async def fake_fetch(url,deadline_s=None):return 'content' if 'good' in url else None
        with mock.patch.object(WF,'fetch_page',side_effect=fake_fetch):
            out=aio(WF.fetch_pages(['http://good.example','http://bad.example'],10.0))
        self.assertEqual(out,{'http://good.example':'content'})
    def test_respects_max_pages_cap(self):
        calls=[]
        async def fake_fetch(url,deadline_s=None):calls.append(url);return 'x'
        with mock.patch.object(WF,'fetch_page',side_effect=fake_fetch),mock.patch.object(WF,'MAX_PAGES',2):
            aio(WF.fetch_pages([f'http://e{i}.example' for i in range(5)],10.0))
        self.assertEqual(len(calls),2)

class RelevantExcerptTests(unittest.TestCase):
    def test_short_text_is_returned_unchanged(self):
        self.assertEqual(WF.relevant_excerpt('short text',"anything",100),'short text')
    def test_picks_the_passage_that_matches_not_the_intro(self):
        text=('Home About Contact navigation menu. '*40)+'Elasticsearch scales horizontally with sharding and replication across many nodes. '+('Footer legal notice. '*40)
        out=WF.relevant_excerpt(text,'elasticsearch horizontal scaling sharding',600)
        self.assertIn('sharding and replication',out);self.assertLessEqual(len(out),2000)
    def test_no_query_terms_falls_back_to_the_start(self):
        self.assertEqual(WF.relevant_excerpt('x '*2000,'the of and',300),('x '*2000)[:300])
class FetchPagesKeepsLongTextTests(unittest.TestCase):
    def test_extract_cap_is_larger_than_prompt_cap(self):
        self.assertGreater(WF.PAGE_EXTRACT_CHARS,WF.PAGE_TEXT_CHARS)

if __name__=='__main__':unittest.main()
