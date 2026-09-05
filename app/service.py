import base64, ctypes, json, math, os, queue, re, sqlite3, threading, time
from pathlib import Path
from datetime import datetime, timedelta
from app import market
from app.calendar_cn import now, phase, trading_day, sessions_after, SH
from app.signals import analyse,size_order,wilson,VERSION
from app.review import diagnose


def secret_codec(text, decrypt=False):
    """Windows DPAPI binds tokens to the signed-in Windows user."""
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_=[('size',wintypes.DWORD),('data',ctypes.POINTER(ctypes.c_char))]
    raw=base64.b64decode(text) if decrypt else text.encode()
    buf=ctypes.create_string_buffer(raw); src=Blob(len(raw),ctypes.cast(buf,ctypes.POINTER(ctypes.c_char))); dst=Blob()
    fn=ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not fn(ctypes.byref(src),None,None,None,None,0,ctypes.byref(dst)): raise OSError('令牌保护失败')
    try:
        val=ctypes.string_at(dst.data,dst.size)
        return val.decode() if decrypt else base64.b64encode(val).decode()
    finally: ctypes.windll.kernel32.LocalFree(dst.data)


class Service:
    def __init__(self,directory):
        self.root=Path(directory); self.root.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(self.root/'monitor.sqlite3',check_same_thread=False)
        self.db.row_factory=sqlite3.Row; self.lock=threading.RLock(); self.refresh_lock=threading.Lock()
        self.stop=threading.Event();self.wake=threading.Event();self.notices=queue.Queue()
        self.window=None; self.quotes={};self.bars={};self.analysis=None;self.error='';self.last_fetch=''; self.hist_date=''
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY,v TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,ts TEXT,kind TEXT,title TEXT,body TEXT,status TEXT DEFAULT '仅本机',attempts INTEGER DEFAULT 0,retry_at REAL DEFAULT 0,receipt TEXT);
        CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY,ts TEXT,day TEXT,slot TEXT,action TEXT,family TEXT,price REAL,idx REAL,payload TEXT,version TEXT,UNIQUE(day,slot));
        CREATE TABLE IF NOT EXISTS outcomes(decision_id INTEGER,horizon INTEGER,due TEXT,net REAL,success INTEGER,price REAL,settled TEXT,PRIMARY KEY(decision_id,horizon));
        CREATE TABLE IF NOT EXISTS fills(id INTEGER PRIMARY KEY,ts TEXT,day TEXT,side TEXT,qty INTEGER,price REAL,fee REAL,realized REAL,nonce TEXT UNIQUE);
        CREATE TABLE IF NOT EXISTS reports(day TEXT PRIMARY KEY,body TEXT);
        CREATE TABLE IF NOT EXISTS learning(day TEXT PRIMARY KEY,payload TEXT);
        CREATE TABLE IF NOT EXISTS diagnostics(decision_id INTEGER,horizon INTEGER,payload TEXT,PRIMARY KEY(decision_id,horizon));
        CREATE TABLE IF NOT EXISTS equity(day TEXT PRIMARY KEY,total REAL,shares INTEGER,cash REAL,close REAL);
        CREATE TABLE IF NOT EXISTS shadow_outcomes(decision_id INTEGER,horizon INTEGER,due TEXT,net REAL,success INTEGER,price REAL,settled TEXT,PRIMARY KEY(decision_id,horizon));
        ''')
        defaults={'shares':1000,'cost':3.0,'cash':10000.0,'initial_value':13000.0,
                  'push_enabled':False,'channel':'wechat','token':'','poll':30,'roundtrip_bps':12,
                  'monitor_enabled':True,'autostart':False,'startup_task':False,'shadow_families':[], 'last_trade_day':''}
        for k,v in defaults.items(): self.db.execute('INSERT OR IGNORE INTO settings VALUES(?,?)',(k,json.dumps(v)))
        self.db.commit()
        # A saved snapshot helps offline viewing but cannot itself trigger an order recommendation.
        try:
            cached=json.loads((self.root/'last_market.json').read_text(encoding='utf-8'))
            self.quotes=cached['quotes'];self.bars=cached['bars']
            self.analysis=analyse(self.bars['399006'],self.quotes['399006'],self.quotes['159915'])
            self.error='正在显示上次缓存，联网校验前暂停建议。'
        except (OSError,ValueError,KeyError,TypeError):pass

    def get(self,k):
        with self.lock: return json.loads(self.db.execute('SELECT v FROM settings WHERE k=?',(k,)).fetchone()[0])

    def put(self,k,v): self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(k,json.dumps(v)))

    def rows(self,sql,args=()):
        with self.lock: return [dict(r) for r in self.db.execute(sql,args).fetchall()]

    def event(self,kind,title,body,phone=True):
        with self.lock:
            status='待发送' if phone and self.get('push_enabled') and self.get('token') else '仅本机'
            self.db.execute('INSERT INTO events(ts,kind,title,body,status) VALUES(?,?,?,?,?)',(now().isoformat(),kind,title,body,status));self.db.commit()
        self.notices.put((title,body))

    def sellable(self,today):
        bought=self.rows('SELECT COALESCE(SUM(qty),0) AS n FROM fills WHERE day=? AND side=\'买入\'',(today,))[0]['n']
        return max(0,self.get('shares')-bought)

    def fresh(self,t):
        if len(self.quotes)!=2: return False
        return all(q['date']==t.date().isoformat() and -10<=(t-datetime.fromisoformat(q['timestamp'])).total_seconds()<=120 for q in self.quotes.values())

    def state(self):
        with self.lock:
            t=now(); q=self.quotes.get('159915',{}); p=q.get('price');shares=self.get('shares');cost=self.get('cost');cash=self.get('cash')
            outstanding=self.rows("SELECT COUNT(*) n FROM decisions d WHERE action!='观望' AND NOT EXISTS(SELECT 1 FROM outcomes o WHERE o.decision_id=d.id AND horizon=1)")[0]['n']
            agg=self.rows('SELECT COUNT(*) n,COALESCE(SUM(success),0) wins,AVG(net) avg FROM outcomes WHERE horizon=1')[0]
            events=self.rows('SELECT * FROM events ORDER BY id DESC LIMIT 50')
            ds=self.rows('SELECT d.*,o.net,o.success,o.due FROM decisions d LEFT JOIN outcomes o ON o.decision_id=d.id AND o.horizon=1 ORDER BY d.id DESC LIMIT 80')
            grouped=self.rows('SELECT horizon,COUNT(*) n,SUM(success) wins,AVG(net) avg FROM outcomes GROUP BY horizon')
            realized=self.rows('SELECT COALESCE(SUM(realized),0) n FROM fills')[0]['n']
            return {'now':t.isoformat(),'phase':phase(t),'quotes':self.quotes,'error':self.error,'last_fetch':self.last_fetch,
                'fresh':self.fresh(t),'history_date':self.hist_date,'analysis':self.analysis,
                'portfolio':dict(shares=shares,cost=cost,cash=cash,sellable=self.sellable(t.date().isoformat()),
                    value=None if p is None else p*shares,profit=None if p is None else (p-cost)*shares,
                    total=None if p is None else p*shares+cash,realized=realized),
                'stats':dict(n=agg['n'],wins=agg['wins'],avg=agg['avg'],pending=outstanding,interval=wilson(agg['wins'],agg['n']),horizons=grouped),
                'events':events,'decisions':ds,'reports':self.rows('SELECT * FROM reports ORDER BY day DESC LIMIT 20'),
                'learning':self.rows('SELECT * FROM learning ORDER BY day DESC LIMIT 10'),
                'fills':self.rows('SELECT * FROM fills ORDER BY id DESC LIMIT 30'),
                'diagnostics':self.rows('SELECT x.*,d.ts,d.action FROM diagnostics x JOIN decisions d ON d.id=x.decision_id ORDER BY d.id DESC,x.horizon LIMIT 100'),
                'equity':self.rows('SELECT * FROM equity ORDER BY day'),
                'shadow_stats':self.rows('SELECT d.family,COUNT(*) n,AVG(o.net) avg,SUM(o.success) wins FROM shadow_outcomes o JOIN decisions d ON d.id=o.decision_id WHERE o.horizon=1 GROUP BY d.family'),
                'settings':{k:self.get(k) for k in ['push_enabled','channel','poll','roundtrip_bps','monitor_enabled','autostart','startup_task']},
                'token_saved':bool(self.get('token')),'chart':self.bars.get('399006',[])[-100:],
                'shadow_families':self.get('shadow_families'),'version':VERSION,'data_dir':str(self.root)}

    def refresh(self):
        if not self.refresh_lock.acquire(False): return
        try:
            t=now(); qs=market.quotes();previous_error=self.error
            # Load histories in full before publishing a new coherent snapshot.
            bars=self.bars
            if self.hist_date!=t.date().isoformat() or not bars:
                bars={s:market.history(s) for s in ['399006','159915']}
            a=analyse(bars['399006'],qs['399006'],qs['159915'])
            with self.lock:
                self.quotes=qs;self.bars=bars;self.hist_date=t.date().isoformat();self.analysis=a
                self.error='';self.last_fetch=t.isoformat()
            # Cache is visibly stale; never replay it as a live trigger.
            (self.root/'last_market.json').write_text(json.dumps({'quotes':qs,'bars':bars},ensure_ascii=False),encoding='utf-8')
            # Catch up only outcomes whose future observation dates really passed; never invent missed signals.
            self.settle(t)
            if previous_error and not previous_error.startswith('正在显示'):
                self.event('health','行情连接已恢复','已重新取得行情；交易建议仍需通过时间与数据完整性检查。',phone=False)
        except Exception as e:
            was_error=bool(self.error)
            with self.lock:self.error='行情暂不可用：'+type(e).__name__+'。等待重试；暂停生成买卖提醒。'
            if not was_error:self.event('health','行情连接中断',self.error,phone=False)
        finally:self.refresh_lock.release()

    def dispatch_due(self,t):
        if not trading_day(t.date()) or not self.get('monitor_enabled'): return
        day=t.date().isoformat(); ph=phase(t);m=t.hour*60+t.minute
        # Open brief is only issued in the opening window; missed sessions are not fabricated.
        if 570<=m<580:
            self.record(t,'open','开盘建议',live=self.fresh(t) and not self.error)
        elif ph in ['上午交易','下午交易'] and self.fresh(t) and not self.error:
            a=self.analysis
            if a and a['action']!='观望':
                last=self.rows("SELECT * FROM decisions WHERE day=? AND (action!='观望' OR slot LIKE 'intra-%') ORDER BY id DESC LIMIT 1",(day,))
                if not last or (t-datetime.fromisoformat(last[0]['ts'])).total_seconds()>=1800:
                    self.record(t,'intra-'+t.strftime('%H%M'),'盘中观察点',live=True)
        if 905<=m<1080:
            if not self.rows('SELECT day FROM reports WHERE day=?',(day,)):
                self.close_report(t)

    def record(self,t,slot,title,live):
        with self.lock:
            day=t.date().isoformat()
            if self.rows('SELECT id FROM decisions WHERE day=? AND slot=?',(day,slot)):return
            a=json.loads(json.dumps(self.analysis)) if self.analysis and live else dict(action='观望',family='data',regime='行情待确认',reasons=['没有新鲜完整行情，暂停操作'],allocation=0,version=VERSION)
            if a['family'] in self.get('shadow_families'):
                a['shadow_action']=a['action'];a['action']='观望';a['reasons'].append('同类信号样本外表现不足，进入影子观察')
            eq=self.quotes.get('159915',{}); iq=self.quotes.get('399006',{})
            price=eq.get('price',0)
            qty=size_order(a['action'],price,self.get('shares'),self.get('cash'),self.sellable(day),a.get('allocation',0)) if price else 0
            a.update(qty=qty,quote_ts=eq.get('timestamp'),horizons=[1,3,5],roundtrip_bps=self.get('roundtrip_bps'))
            if a['action']!='观望' and qty==0:
                a['signal_action']=a['action'];a['action']='观望';a['reasons'].append('现金或T+1可卖份额不足，本次不建议执行')
            self.db.execute('INSERT OR IGNORE INTO decisions(ts,day,slot,action,family,price,idx,payload,version) VALUES(?,?,?,?,?,?,?,?,?)',
                (t.isoformat(),day,slot,a['action'],a['family'],price,iq.get('price',0),json.dumps(a,ensure_ascii=False),VERSION));self.db.commit()
            body=f"{a['action']} · {a['regime']}\n"+'\n'.join(a['reasons'])
            if qty: body+=f'\n159915参考 {price:.3f} 元，小批 {qty} 份；请在券商核实可成交价格。'
            body+='\n建议有效10分钟；仅提示，不自动下单。次一/三/五交易日收盘验证，胜率不是盈利承诺。'
            if a.get('shadow_action') and slot!='open':
                # Shadow records are not executable advice; avoid mobile or desktop trading prompts.
                return
            self.event('decision',title,body)

    def settle(self,t,final=False):
        bars={r['date']:r for r in self.bars.get('159915',[]) if r['date']<t.date().isoformat() or (final and t.hour*60+t.minute>=905)}
        for d in self.rows("SELECT * FROM decisions WHERE action IN ('买入','卖出') OR payload LIKE '%shadow_action%'"):
            payload=json.loads(d['payload'])
            shadow=payload.get('shadow_action')
            direction=shadow or d['action'];table='shadow_outcomes' if shadow else 'outcomes'
            if direction not in ['买入','卖出'] or d['price']<=0:continue
            for h in [1,3,5]:
                due=sessions_after(d['day'],h)
                if due not in bars or due>t.date().isoformat():continue
                ret=bars[due]['close']/d['price']-1
                net=(ret if direction=='买入' else -ret)-payload['roundtrip_bps']/10000
                with self.lock:
                    self.db.execute(f'INSERT OR IGNORE INTO {table} VALUES(?,?,?,?,?,?,?)',(d['id'],h,due,net,int(net>0),bars[due]['close'],t.isoformat()));self.db.commit()
                    if shadow:continue
                    frozen=self.rows('SELECT price FROM outcomes WHERE decision_id=? AND horizon=?',(d['id'],h))[0]['price']
                    index_close=next((r['close'] for r in self.bars.get('399006',[]) if r['date']==due),None)
                    details=diagnose(d['action'],d['price'],frozen,d['idx'],index_close,payload['roundtrip_bps'])
                    self.db.execute('INSERT OR IGNORE INTO diagnostics VALUES(?,?,?)',(d['id'],h,json.dumps(details,ensure_ascii=False)));self.db.commit()

    def learn(self,day):
        result=[];shadows=[]
        for family in ['top_divergence','panic_rebound','rebound_resistance','support_contraction']:
            # One observation per family/day: repeated intraday signals must not inflate sample size.
            rows=self.rows('SELECT d.day,AVG(o.net) net FROM decisions d JOIN outcomes o ON d.id=o.decision_id WHERE o.horizon=1 AND d.family=? AND d.version=? GROUP BY d.day ORDER BY d.day',(family,VERSION))[-90:]
            n=len(rows); status='积累样本';detail=f'{n}/45个独立交易日；尚不改变策略'
            if n>=45:
                train=rows[:-15];valid=rows[-15:]
                tr=sum(r['net'] for r in train)/len(train);va=sum(r['net'] for r in valid)/15
                if tr<0 and va<0:
                    shadows.append(family);status='次日起影子观察';detail='训练段与最近15日验证段平均方向收益均为负，停止实盘建议，保留审计'
                else: status='维持规则';detail='未触发保守降级条件；不自动放大仓位'
            result.append(dict(family=family,n=n,status=status,detail=detail))
        with self.lock:
            # Newly restricted families remain shadowed until manual review, avoiding reactivation without new evidence.
            self.put('shadow_families',sorted(set(shadows+self.get('shadow_families'))))
            self.db.execute('INSERT OR REPLACE INTO learning VALUES(?,?)',(day,json.dumps(result,ensure_ascii=False)));self.db.commit()
        return result

    def close_report(self,t):
        day=t.date().isoformat()
        # Re-fetch final unadjusted daily bars; outdated closes never settle an outcome.
        try:
            final={s:market.history(s) for s in ['399006','159915']}
            if any(v[-1]['date']!=day for v in final.values()):raise ValueError('收盘日K未齐')
            self.bars=final
        except Exception:
            if not self.rows("SELECT id FROM events WHERE kind='close_wait' AND ts LIKE ?",(day+'%',)):
                self.event('close_wait','收盘数据待确认','收盘日K尚未齐全，将继续尝试；胜率与复盘暂不结算。')
            return
        self.settle(t,final=True);learning=self.learn(day)
        with self.lock:
            close=final['159915'][-1]['close'];shares=self.get('shares');cash=self.get('cash')
            self.db.execute('INSERT OR IGNORE INTO equity VALUES(?,?,?,?,?)',(day,shares*close+cash,shares,cash,close));self.db.commit()
        ds=self.rows('SELECT * FROM decisions WHERE day=?',(day,));agg=self.rows('SELECT COUNT(*) n,SUM(success) wins,AVG(net) avg FROM outcomes WHERE horizon=1')[0]
        before=final['399006'][-2]['close'];last=final['399006'][-1]['close'];change=(last/before-1)*100
        directional=[d for d in ds if d['action']!='观望']
        diagnostic=[]
        close=final['159915'][-1]['close']
        for d in directional:
            r=(close/d['price']-1)*(1 if d['action']=='买入' else -1)
            diagnostic.append(f"{d['ts'][11:16]} {d['action']}：至当日收盘方向变化 {r*100:+.2f}%。"+
                ('方向未兑现；可能与趋势延续、反弹受阻或信号过早有关，原因待验证。' if r<=0 else '方向暂符合预期。'))
        rate='暂无到期样本' if not agg['n'] else f"{agg['wins']}/{agg['n']} = {agg['wins']/agg['n']*100:.1f}%"
        body=f'创业板收盘 {last:.2f} 点，当日 {change:+.2f}%。\n今日记录 {len(ds)} 个决策，其中 {len(directional)} 个买卖观察点。\n'
        body+='\n'.join(diagnostic) if diagnostic else '今日未记录有效买卖信号；未运行的时段不补造建议。'
        body+=f'\n次一交易日到期方向胜率：{rate}。今日新信号仍待后续交易日验证。\n卖出成功指避免后续下跌，并非卖出后的实际盈利；实际收益仅据已登记成交。\n'
        body+='学习结果：'+('部分信号降级为影子观察。' if self.get('shadow_families') else '样本不足或没有充分退化证据，保持当前规则。')
        with self.lock:self.db.execute('INSERT OR IGNORE INTO reports VALUES(?,?)',(day,body));self.db.commit()
        self.event('close','收盘复盘 · '+day,body)

    def push_pending(self):
        if not self.get('push_enabled') or not self.get('token'): return
        for e in self.rows("SELECT * FROM events WHERE status='待发送' AND attempts<3 AND retry_at<=? ORDER BY id LIMIT 3",(time.time(),)):
            if e['kind']=='decision' and (now()-datetime.fromisoformat(e['ts'])).total_seconds()>600:
                with self.lock:
                    self.db.execute("UPDATE events SET status='建议过期·未发送' WHERE id=?",(e['id'],));self.db.commit()
                continue
            try:
                token=secret_codec(self.get('token'),True)
                reply=json.loads(market.request('https://www.pushplus.plus/send',dict(token=token,title=e['title'],content=e['body'],template='txt',channel=self.get('channel'))))
                if reply.get('code')!=200: raise ValueError('服务拒绝请求')
                status='服务已受理·送达待确认';receipt=str(reply.get('data',''))
            except Exception as ex:
                # Timeouts may mean accepted remotely; don't duplicate an ambiguous delivery.
                status='发送结果未知·请检查服务端' if isinstance(ex,(TimeoutError,OSError)) else ('发送失败' if e['attempts']>=2 else '待发送');receipt=''
            with self.lock:
                self.db.execute('UPDATE events SET status=?,attempts=attempts+1,retry_at=?,receipt=? WHERE id=?',(status,time.time()+60*(e['attempts']+1),receipt,e['id']));self.db.commit()

    def run(self):
        self.refresh()
        while not self.stop.is_set():
            try:
                t=now();ph=phase(t)
                if self.get('monitor_enabled') and trading_day(t.date()) and 565<=t.hour*60+t.minute<1080:
                    self.refresh();self.dispatch_due(t)
                self.push_pending()
            except Exception as ex:self.error='监控轮次异常：'+type(ex).__name__+'；下一轮重试'
            self.wake.wait(self.get('poll'));self.wake.clear()
