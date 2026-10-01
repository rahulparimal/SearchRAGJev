"""Structured (key=value / JSON-lines) logging configured once at app start. Never logs query text, secrets or document text."""
import json,logging,os,sys
class JsonFmt(logging.Formatter):
    def format(self,r):
        d={'ts':self.formatTime(r,'%Y-%m-%dT%H:%M:%S'),'level':r.levelname,'logger':r.name,'msg':r.getMessage()}
        if r.exc_info:d['exc']=r.exc_info[0].__name__+': '+str(r.exc_info[1])[:200]
        return json.dumps(d,ensure_ascii=False)
def configure():
    root=logging.getLogger()
    if getattr(root,'_rag_configured',False):return
    h=logging.StreamHandler(sys.stderr);h.setFormatter(JsonFmt() if os.getenv('LOG_FORMAT','json')=='json' else logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
    root.addHandler(h);root.setLevel(os.getenv('LOG_LEVEL','INFO').upper());root._rag_configured=True
    logging.getLogger('httpx').setLevel(logging.WARNING);logging.getLogger('httpcore').setLevel(logging.WARNING)
