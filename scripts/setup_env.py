#!/usr/bin/env python3
"""Create or extend .env and pick free host ports. Stdlib only (runs before any dependency exists).
- Never touches tracked files; the target defaults to ./.env (untracked, written with mode 0600).
- Never overwrites an existing .env: missing keys are appended (names are listed, values are not printed) and only busy *_PORT lines are rewritten.
- Secrets come from `secrets`; they are never printed.
- No sed: BSD and GNU differ, and this file's edits are exact line replacements.
Usage: setup_env.py [--env PATH] [--example PATH] [--mode docker|dev] [--keep-ports] [--check]"""
import argparse,os,re,secrets,socket,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
PORTS={'WEB_PORT':5173,'API_PORT':8000,'PG_PORT':5433,'QDRANT_PORT':6335,'REDIS_PORT':6380}

def parse(text):
    """-> ordered {key: (line_index, value)} for KEY=VALUE lines (comments and blanks ignored)."""
    out={}
    for i,l in enumerate(text.splitlines()):
        m=re.match(r'^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$',l)
        if m:out[m.group(1)]=(i,m.group(2).strip())
    return out
def port_free(p,host='127.0.0.1'):
    """Free = nothing accepts connections on it AND it can be bound (covers Docker-published and wildcard listeners)."""
    if not 1024<=p<=65535:return False
    with socket.socket() as c:
        c.settimeout(0.3)
        if c.connect_ex((host,p))==0:return False
    with socket.socket() as b:
        try:b.bind(('0.0.0.0',p))
        except OSError:return False
    return True
def pick_port(want,taken,free=None):
    free=free or port_free;p=want
    while p<=65535:
        if p not in taken and free(p):return p
        p+=1
    raise SystemExit(f'No free port at or above {want}')
def url_with(url,host,port):return re.sub(r'@[^/:]+(:\d+)?/',f'@{host}:{port}/',url) if '@' in url else re.sub(r'//[^/:]+(:\d+)?',f'//{host}:{port}',url,1)
def generated(example_vals):
    """Fresh secrets for a new .env; DATABASE_URL is rebuilt so it carries the generated database password."""
    g={'APP_SECRET_KEY':secrets.token_urlsafe(48),'POSTGRES_PASSWORD':secrets.token_urlsafe(24),'ADMIN_PASSWORD':secrets.token_urlsafe(18),'SEARXNG_SECRET':secrets.token_hex(24)}
    if 'DATABASE_URL' in example_vals:g['DATABASE_URL']=re.sub(r'(//[^:/]+:)[^@]*@',lambda m:m.group(1)+g['POSTGRES_PASSWORD']+'@',example_vals['DATABASE_URL'])
    return g
def plan(env_path,example_path,mode,keep_ports=False,free=None):
    """-> (new_text, report). Pure given `free`; writes nothing."""
    example=example_path.read_text();ex=parse(example);created=not env_path.exists()
    if created:
        vals={k:v for k,(_,v) in ex.items()};vals.update(generated(vals));lines=example.splitlines()
        for k in ('APP_SECRET_KEY','POSTGRES_PASSWORD','ADMIN_PASSWORD','DATABASE_URL','SEARXNG_SECRET'):
            if k in ex:lines[ex[k][0]]=f'{k}={vals[k]}'
        text='\n'.join(lines)+'\n';added=['(new file)']
    else:
        text=env_path.read_text();cur=parse(text);missing=[k for k in ex if k not in cur];added=missing[:]
        if missing:
            g=generated({k:v for k,(_,v) in ex.items()});g.pop('DATABASE_URL',None);add=['','# added by setup_env.py (keys missing from this file)']
            if 'DATABASE_URL' in missing:  # never append the example URL (it carries the example password): derive it from the password this file really has
                pw=cur['POSTGRES_PASSWORD'][1] if cur.get('POSTGRES_PASSWORD',(0,''))[1] else g['POSTGRES_PASSWORD']
                g['DATABASE_URL']=re.sub(r'(//[^:/]+:)[^@]*@',lambda m:m.group(1)+pw+'@',ex['DATABASE_URL'][1])
            add+=[f'{k}={g.get(k,ex[k][1])}' for k in missing]
            text=text.rstrip('\n')+'\n'+'\n'.join(add)+'\n'
    cur=parse(text);lines=text.splitlines();chosen={};moved={};taken=set()
    for k,default in PORTS.items():
        want=int(cur[k][1]) if k in cur and cur[k][1].isdigit() else default
        p=want if keep_ports and k in cur else pick_port(want,taken,free)
        taken.add(p);chosen[k]=p
        if p!=want or k not in cur:
            moved[k]=(want,p)
            if k in cur:lines[cur[k][0]]=f'{k}={p}'
            else:lines.append(f'{k}={p}')
    text='\n'.join(lines)+'\n';cur=parse(text)
    def setv(k,v):
        nonlocal text,cur
        if k in cur and cur[k][1]!=v:
            ls=text.splitlines();ls[cur[k][0]]=f'{k}={v}';text='\n'.join(ls)+'\n';cur=parse(text)
    if 'WEB_PORT' in moved and 'CORS_ORIGINS' in cur:  # keep CORS aligned with the (possibly moved) web port
        setv('CORS_ORIGINS',','.join(re.sub(r'(localhost|127\.0\.0\.1):\d+',lambda m:f"{m.group(1)}:{chosen['WEB_PORT']}",o) for o in cur['CORS_ORIGINS'][1].split(',')))
    if mode=='dev' and created:  # host-run API/worker talk to published ports, not compose service names
        for k,h,pk in (('DATABASE_URL','localhost','PG_PORT'),('QDRANT_URL','localhost','QDRANT_PORT'),('REDIS_URL','localhost','REDIS_PORT')):
            if k in cur:setv(k,url_with(cur[k][1],h,chosen[pk]))
    return text,{'created':created,'added_keys':added,'ports':chosen,'moved':moved}
def write_private(path,text):
    """Atomic, mode 0600 from the first byte (no world-readable window)."""
    tmp=path.with_name(path.name+'.tmp');fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as f:f.write(text)
    os.replace(tmp,path);os.chmod(path,0o600)
def main(argv=None):
    ap=argparse.ArgumentParser();ap.add_argument('--env',type=Path,default=ROOT/'.env');ap.add_argument('--example',type=Path,default=ROOT/'.env.example');ap.add_argument('--mode',choices=['docker','dev'],default='docker')
    ap.add_argument('--keep-ports',action='store_true',help='do not move ports already in .env (the stack is running and owns them)');ap.add_argument('--check',action='store_true',help='report what would change; write nothing');a=ap.parse_args(argv)
    if not a.example.exists():sys.exit(f'Missing {a.example}')
    text,r=plan(a.env,a.example,a.mode,a.keep_ports)
    gen=[k for k in ('APP_SECRET_KEY','POSTGRES_PASSWORD','ADMIN_PASSWORD','SEARXNG_SECRET') if k in parse(a.example.read_text())]
    if r['created']:print(('Would create' if a.check else 'Created')+f' {a.env.name} with generated secrets for {", ".join(gen)} (names only; values are never printed). Admin password: see ADMIN_PASSWORD in {a.env.name}')
    else:
        print(('Would keep' if a.check else 'Kept')+f' existing {a.env.name}'+('' if r['added_keys'] else ' unchanged'))
        if r['added_keys']:print(f"{'Would append' if a.check else 'Appended'} {len(r['added_keys'])} missing key(s) (names only): {', '.join(r['added_keys'])}")
    for k,(was,now) in r['moved'].items():print(f'port {k}: {was} -> {now}'+(' (was busy)' if was!=now else ' (new key)'))
    print('ports: '+' '.join(f'{k}={v}' for k,v in r['ports'].items()))
    if not a.check and (r['created'] or r['added_keys'] or r['moved'] or text!=(a.env.read_text() if a.env.exists() else '')):write_private(a.env,text)
    return 0
if __name__=='__main__':sys.exit(main())
