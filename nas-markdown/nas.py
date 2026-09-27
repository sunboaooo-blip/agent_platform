import json,urllib.request,base64,re,os,sys
c=json.load(open(os.path.expanduser('~/nas-upload/config.json')))
URL=f"http://{c['host']}:{c['port']}"
AUTH=base64.b64encode(f"{c['user']}:{c['password']}".encode()).decode()
def req(method,path,data=None,hdrs=None):
    r=urllib.request.Request(URL+path,method=method,data=data)
    r.add_header('Authorization','Basic '+AUTH)
    for k,v in (hdrs or {}).items(): r.add_header(k,v)
    return urllib.request.urlopen(r,timeout=120)
def propfind(path,depth='1'):
    r=req('PROPFIND',path,None,{'Depth':str(depth),'Content-Type':'application/xml'})
    x=r.read().decode()
    out=[]
    for resp in re.findall(r'<[Dd]:response>(.*?)</[Dd]:response>',x,re.S):
        href=re.search(r'<[Dd]:href>(.*?)</[Dd]:href>',resp,re.S).group(1)
        mt=re.search(r'mtype="([^"]*)"',resp)
        t=re.search(r'<[Dd]:getlastmodified>(.*?)</[Dd]:getlastmodified>',resp,re.S)
        sz=re.search(r'<[Dd]:getcontentlength>(.*?)</[Dd]:getcontentlength>',resp,re.S)
        col='collection' in resp
        out.append(dict(href=href,dir=col,size=int(sz.group(1)) if sz else None,mt=mt.group(1) if mt else None,lm=t.group(1) if t else None))
    return out
def get(path): return req('GET',path).read()
