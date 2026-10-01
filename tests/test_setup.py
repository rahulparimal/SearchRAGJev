import contextlib,hashlib,io,os,socket,stat,subprocess,sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import setup_env as S
EX=ROOT/'.env.example'
class SetupEnv(unittest.TestCase):
    def setUp(self):self.d=Path(tempfile.mkdtemp());self.env=self.d/'.env'
    def run_main(self,*a):
        out=io.StringIO()
        with contextlib.redirect_stdout(out):S.main(['--env',str(self.env),'--example',str(EX),*a])
        return out.getvalue()
    def test_generates_env_with_secrets_private_and_not_printed(self):
        before=hashlib.sha256(EX.read_bytes()).hexdigest();out=self.run_main()
        v={k:x for k,(_,x) in S.parse(self.env.read_text()).items()}
        for k,bad in (('APP_SECRET_KEY','replace-with-a-long-random-secret'),('ADMIN_PASSWORD','change-this-on-first-run'),('POSTGRES_PASSWORD','change-me')):self.assertNotIn(v[k],('',bad))
        self.assertGreaterEqual(len(v['APP_SECRET_KEY'].encode()),32);self.assertIn(v['POSTGRES_PASSWORD'],v['DATABASE_URL']);self.assertEqual(stat.S_IMODE(self.env.stat().st_mode),0o600)
        for k in ('APP_SECRET_KEY','ADMIN_PASSWORD','POSTGRES_PASSWORD'):self.assertNotIn(v[k],out)
        self.assertEqual(hashlib.sha256(EX.read_bytes()).hexdigest(),before)   # tracked template untouched
        self.assertIn('postgres:5432',v['DATABASE_URL'])   # docker mode keeps compose service names
    def test_two_runs_generate_different_secrets(self):
        self.run_main();a=self.env.read_text();self.env.unlink();self.run_main();self.assertNotEqual(a,self.env.read_text())
    def test_existing_env_never_overwritten_and_second_run_idempotent(self):
        self.env.write_text('APP_SECRET_KEY=my-own-secret-value-that-is-long-enough-123456\nADMIN_PASSWORD=keepme\nPOSTGRES_PASSWORD=pg-keep\nCUSTOM=1\nWEB_PORT=5173\n')
        with unittest.mock.patch.object(S,'port_free',lambda p,host='127.0.0.1':True):
            self.run_main()
        t=self.env.read_text();v={k:x for k,(_,x) in S.parse(t).items()}
        self.assertEqual((v['APP_SECRET_KEY'],v['ADMIN_PASSWORD'],v['POSTGRES_PASSWORD'],v['CUSTOM']),('my-own-secret-value-that-is-long-enough-123456','keepme','pg-keep','1'))
        self.assertIn('API_PORT',v);self.assertIn('WEB_PORT',v);self.assertEqual(t.splitlines()[:5],['APP_SECRET_KEY=my-own-secret-value-that-is-long-enough-123456','ADMIN_PASSWORD=keepme','POSTGRES_PASSWORD=pg-keep','CUSTOM=1','WEB_PORT=5173'])
        with unittest.mock.patch.object(S,'port_free',lambda p,host='127.0.0.1':True):out=self.run_main()
        self.assertEqual(self.env.read_text(),t);self.assertIn('unchanged',out)
    def test_busy_ports_move_to_next_free_and_cors_follows(self):
        self.env.write_text('CORS_ORIGINS=http://localhost:5173\nWEB_PORT=5173\nAPI_PORT=8000\n')
        busy={5173,5174,8000}
        text,r=S.plan(self.env,EX,'docker',free=lambda p,host='127.0.0.1':p not in busy)
        self.assertEqual((r['ports']['WEB_PORT'],r['ports']['API_PORT']),(5175,8001));v={k:x for k,(_,x) in S.parse(text).items()};self.assertEqual(v['CORS_ORIGINS'],'http://localhost:5175');self.assertEqual(v['WEB_PORT'],'5175')
        self.assertEqual(len(set(r['ports'].values())),5)   # never two services on one port
    def test_real_listening_socket_is_detected(self):
        s=socket.socket();s.bind(('0.0.0.0',0));s.listen(1);p=s.getsockname()[1]
        try:
            self.assertFalse(S.port_free(p));self.assertNotEqual(S.pick_port(p,set()),p);self.assertTrue(S.port_free(S.pick_port(p,set())))
        finally:s.close()
    def test_keep_ports_for_a_running_stack(self):
        self.env.write_text('WEB_PORT=5173\nAPI_PORT=8000\nPG_PORT=5433\nQDRANT_PORT=6335\nREDIS_PORT=6380\n')
        _,r=S.plan(self.env,EX,'docker',keep_ports=True,free=lambda p,host='127.0.0.1':False);self.assertEqual(r['moved'],{});self.assertEqual(r['ports']['API_PORT'],8000)
    def test_check_mode_writes_nothing(self):
        out=self.run_main('--check');self.assertFalse(self.env.exists());self.assertIn('Would create',out)
        self.env.write_text('X=1\n');before=self.env.read_text();self.run_main('--check');self.assertEqual(self.env.read_text(),before)
    def test_dev_mode_points_at_published_ports(self):
        text,r=S.plan(self.env,EX,'dev',free=lambda p,host='127.0.0.1':True);v={k:x for k,(_,x) in S.parse(text).items()}
        self.assertIn(f"localhost:{r['ports']['PG_PORT']}/",v['DATABASE_URL']);self.assertEqual(v['QDRANT_URL'],f"http://localhost:{r['ports']['QDRANT_PORT']}");self.assertTrue(v['REDIS_URL'].startswith(f"redis://localhost:{r['ports']['REDIS_PORT']}"))
    def test_scripts_parse(self):
        for f in ('rag','scripts/run_local.sh','scripts/run_enterprise.sh'):self.assertEqual(subprocess.run(['bash','-n',str(ROOT/f)]).returncode,0,f)
        self.assertEqual(subprocess.run([sys.executable,'-m','py_compile',str(ROOT/'scripts/doctor.py')]).returncode,0)
    def test_missing_database_url_is_derived_from_existing_password_not_the_example(self):
        self.env.write_text('POSTGRES_PASSWORD=pg-real-pass-1\nAPP_SECRET_KEY=my-own-secret-value-that-is-long-enough-123456\n')
        with unittest.mock.patch.object(S,'port_free',lambda p,host='127.0.0.1':True):out=self.run_main('--keep-ports')
        v={k:x for k,(_,x) in S.parse(self.env.read_text()).items()}
        self.assertIn('pg-real-pass-1',v['DATABASE_URL']);self.assertNotIn('change-me',v['DATABASE_URL']);self.assertEqual(v['POSTGRES_PASSWORD'],'pg-real-pass-1')
        self.assertIn('DATABASE_URL',out);self.assertNotIn('pg-real-pass-1',out);self.assertRegex(out,r'Appended \d+ missing key\(s\) \(names only\)')
    def test_fresh_clone_check_says_env_will_be_generated(self):
        d=Path(tempfile.mkdtemp())
        for f in ('rag','.env.example','docker-compose.yml'):(d/f).write_bytes((ROOT/f).read_bytes())
        (d/'rag').chmod(0o755);(d/'scripts').mkdir();(d/'scripts/setup_env.py').write_bytes((ROOT/'scripts/setup_env.py').read_bytes())
        r=subprocess.run([str(d/'rag'),'up','--check'],capture_output=True,text=True,cwd=d,timeout=60);self.assertEqual(r.returncode,0,r.stderr);self.assertIn('will CREATE it',r.stdout);self.assertFalse((d/'.env').exists());self.assertNotIn('env file',r.stderr)
    def test_dockerignore_and_pinned_images(self):
        self.assertIn('node_modules',(ROOT/'frontend/.dockerignore').read_text());self.assertNotIn(':latest',(ROOT/'docker-compose.yml').read_text().replace('never :latest',''))
    def test_rag_up_check_changes_nothing(self):
        r=subprocess.run([str(ROOT/'rag'),'up','--check'],capture_output=True,text=True,cwd=ROOT,timeout=60);self.assertEqual(r.returncode,0,r.stderr);self.assertIn('nothing will be started',r.stdout)
import unittest.mock
if __name__=='__main__':unittest.main()
