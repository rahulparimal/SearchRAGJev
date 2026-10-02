import sys,unittest
import test_rag as T  # sets the test environment before backend modules load
import app as A

class RouteTableTests(unittest.TestCase):
    """Unit tests call handlers directly and bypass FastAPI's route table; a misplaced decorator once turned `db` into a required
    query parameter on /auth/me and broke every login unseen. These checks inspect the real table."""
    def routes(self):return [r for r in A.app.routes if hasattr(r,'dependant')]
    def test_no_injected_dependency_leaks_into_query_params(self):
        for r in self.routes():
            leaked={q.name for q in r.dependant.query_params}&{'db','user','p','snap','gen','jev'}
            self.assertFalse(leaked,f'{r.path} exposes {leaked} as query params (decorator on the wrong function?)')
    def test_auth_me_is_served_by_the_me_handler(self):
        r=[x for x in self.routes() if x.path=='/auth/me'][0];self.assertEqual(r.endpoint.__name__,'me')
    def test_expected_routes_present(self):
        paths={r.path for r in self.routes()}
        for p in ('/search','/answer','/research','/search/overview','/auth/me','/health'):self.assertIn(p,paths)
if __name__=='__main__':unittest.main()
