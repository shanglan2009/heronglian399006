"""Read-only public quote adapter. No SLA; timestamps must pass consumer gates."""
import json, re, urllib.request, math
from datetime import datetime
from app.calendar_cn import SH

def request(url, payload=None):
    body=None if payload is None else json.dumps(payload).encode('utf-8')
    req=urllib.request.Request(url,data=body,headers={'User-Agent':'Mozilla/5.0','Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=12) as r: return r.read()

def quotes():
    return parse_quotes(request('https://qt.gtimg.cn/q=sz399006,sz159915').decode('gbk'))

def parse_quotes(text):
    result={}
    for symbol,raw in re.findall(r'v_sz(\d+)="([^"]*)"',text):
        f=raw.split('~')
        if len(f)<39: raise ValueError('行情字段不完整')
        stamp=datetime.strptime(f[30],'%Y%m%d%H%M%S').replace(tzinfo=SH)
        q={'symbol':symbol,'name':f[1],'price':float(f[3]),'prev':float(f[4]),
           'open':float(f[5]),'high':float(f[33]),'low':float(f[34]),
           'volume':float(f[6]),'turnover':float(f[38] or 0)/100,
           'timestamp':stamp.isoformat(),'date':stamp.date().isoformat()}
        if not all(math.isfinite(q[k]) for k in ['price','prev','open','high','low','volume','turnover']):raise ValueError('行情包含非有限数值')
        if min(q['price'],q['prev'],q['low'],q['open'])<=0 or q['high']<max(q['price'],q['open']) or q['low']>min(q['price'],q['open']): raise ValueError('行情价格异常')
        q['change']=(q['price']/q['prev']-1)*100
        result[symbol]=q
    if set(result)!={'399006','159915'}: raise ValueError('指数或ETF报价缺失')
    return result

def history(symbol):
    raw=json.loads(request(f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sz{symbol},day,,,640,'))
    series=raw['data']['sz'+symbol].get('day',[])
    rows=[dict(date=r[0],open=float(r[1]),close=float(r[2]),high=float(r[3]),low=float(r[4]),volume=float(r[5])) for r in series]
    if len(rows)<260 or len({r['date'] for r in rows})!=len(rows): raise ValueError('历史行情不足或日期重复')
    if any(not all(math.isfinite(r[k]) for k in ['close','open','low','high','volume']) or min(r['close'],r['open'],r['low'])<=0 or r['high']<max(r['close'],r['open']) or r['low']>min(r['close'],r['open']) or r['volume']<0 for r in rows): raise ValueError('历史行情异常')
    return sorted(rows,key=lambda r:r['date'])
