import base64, ctypes, json, math, os, queue, re, sqlite3, threading, time
from pathlib import Path
from datetime import datetime, timedelta
from app import market
from app.calendar_cn import now, phase, trading_day, sessions_after, SH
from app.signals import analyse,size_order,wilson,VERSION
from app.review import diagnose
from app import repair,opportunities,risk,capital


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
        self.close_lock=threading.RLock();self.next_recovery=0
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
        CREATE TABLE IF NOT EXISTS opportunities(key TEXT PRIMARY KEY,active INTEGER,absent_since TEXT,last_seen TEXT,created TEXT);
        CREATE TABLE IF NOT EXISTS opportunity_audit(id INTEGER PRIMARY KEY,ts TEXT,key TEXT,event TEXT);
        CREATE TABLE IF NOT EXISTS repair_pairs(day TEXT PRIMARY KEY,ts TEXT,price REAL,baseline TEXT,candidate TEXT,payload TEXT,version TEXT);
        CREATE TABLE IF NOT EXISTS repair_results(day TEXT,horizon INTEGER,due TEXT,move REAL,baseline_net REAL,candidate_net REAL,PRIMARY KEY(day,horizon));
        CREATE TABLE IF NOT EXISTS risk_samples(day TEXT PRIMARY KEY,ts TEXT,payload TEXT);
        CREATE TABLE IF NOT EXISTS risk_alerts(id INTEGER PRIMARY KEY,ts TEXT,day TEXT,level INTEGER,payload TEXT);
        CREATE TABLE IF NOT EXISTS monitor_days(day TEXT PRIMARY KEY,first_seen TEXT,last_seen TEXT,last_fresh TEXT,ticks INTEGER DEFAULT 0,fresh_ticks INTEGER DEFAULT 0,gaps INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS health_alerts(day TEXT,code TEXT,ts TEXT,message TEXT,PRIMARY KEY(day,code));
        CREATE TABLE IF NOT EXISTS close_status(day TEXT PRIMARY KEY,attempted TEXT,status TEXT,detail TEXT);
        ''')
        defaults={'shares':1000,'cost':3.0,'cash':10000.0,'initial_value':13000.0,
                  'push_enabled':False,'channel':'wechat','token':'','poll':30,'roundtrip_bps':12,
                  'monitor_enabled':True,'autostart':False,'startup_task':False,'shadow_families':[], 'last_trade_day':'',
                  'risk_peak':0,'risk_pending':None,'risk_confirmed':None,'health_started':now().date().isoformat()}
        for k,v in defaults.items(): self.db.execute('INSERT OR IGNORE INTO settings VALUES(?,?)',(k,json.dumps(v)))
        self.db.commit()
        self.capital=capital.CapitalMonitor(self)
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
        try:return all(q.get('date')==t.date().isoformat() and -10<=(t-datetime.fromisoformat(q['timestamp'])).total_seconds()<=120 for q in self.quotes.values())
        except (KeyError,ValueError,TypeError):return False

    def risk_state(self,t=None):
        t=t or now()
        with self.lock:
            result=risk.assess(self.bars.get('399006',[]),self.quotes.get('399006'),self.quotes.get('159915'),
                self.get('shares'),self.get('cash'),self.sellable(t.date().isoformat()),self.get('risk_peak'))
            result['live']=not self.error and self.fresh(t) and phase(t) in ['上午交易','下午交易']
            confirmed=self.get('risk_confirmed')
            result['confirmed']=bool(confirmed and confirmed['level']==result['level'] and result['live'] and (t-datetime.fromisoformat(confirmed['ts'])).total_seconds()<=120)
            result['display_note']='实时风险，待连续60秒确认' if result['live'] and not result['confirmed'] else ('实时风险已确认' if result['live'] else '最近行情观察；非实时操作建议')
            return result

    def monitor_health(self,t):
        if not trading_day(t.date()) or not self.get('monitor_enabled'):return
        minute=t.hour*60+t.minute
        if not 565<=minute<1080:return
        day=t.date().isoformat();live=phase(t) in ['上午交易','下午交易'];fresh=self.fresh(t) and not self.error
        with self.lock:
            previous=self.rows('SELECT * FROM monitor_days WHERE day=?',(day,))
            gap=False
            if previous:
                before=datetime.fromisoformat(previous[0]['last_seen'])
                gap=live and phase(before)==phase(t) and (t-before).total_seconds()>180
                self.db.execute('UPDATE monitor_days SET last_seen=?,last_fresh=COALESCE(?,last_fresh),ticks=ticks+1,fresh_ticks=fresh_ticks+?,gaps=gaps+? WHERE day=?',
                    (t.isoformat(),t.isoformat() if live and fresh else None,int(live and fresh),int(gap),day))
            else:self.db.execute('INSERT INTO monitor_days VALUES(?,?,?,?,?,?,?)',(day,t.isoformat(),t.isoformat(),t.isoformat() if live and fresh else None,1,int(live and fresh),0))
            self.db.commit()
        checks=[]
        if live and not fresh:checks.append(('stale','行情失效：当前暂停操作建议，请检查联网与行情时间'))
        if gap:checks.append(('gap','检测到交易时段监控间隔超过3分钟；该时段可能漏报，不能视为全程监控'))
        if minute>=580 and not self.rows("SELECT id FROM decisions WHERE day=? AND slot='open' AND family!='data'",(day,)):
            checks.append(('open','今日未完成有效开盘判断，可能晚启动或行情不可用；不补造当时建议'))
        if minute>=915 and not self.rows('SELECT day FROM reports WHERE day=?',(day,)):
            checks.append(('close','15:15后尚未完成收盘复盘，正在等待完整行情；请确认程序继续运行'))
        prior=t.date()-timedelta(days=1)
        for _ in range(15):
            if trading_day(prior):break
            prior-=timedelta(days=1)
        if prior.isoformat()>=self.get('health_started') and not self.rows('SELECT day FROM reports WHERE day=?',(prior.isoformat(),)):
            checks.append(('previous_close','上一交易日未保存收盘复盘，存在监控缺口：'+prior.isoformat()))
        for code,message in checks:
            with self.lock:
                if self.rows('SELECT code FROM health_alerts WHERE day=? AND code=?',(day,code)):continue
                self.db.execute('INSERT INTO health_alerts VALUES(?,?,?,?)',(day,code,t.isoformat(),message));self.db.commit()
            self.event('health_warning','监控健康提醒',message+'。程序退出、电脑关机或断网期间无法实时发出通知，恢复运行后才能报告缺口。')

    def check_risk(self,t):
        if phase(t) not in ['上午交易','下午交易'] or self.error or not self.fresh(t):return
        with self.lock:
            nav=self.get('shares')*self.quotes['159915']['price']+self.get('cash')
            peak=self.get('risk_peak')
            if not peak:peak=self.rows('SELECT COALESCE(MAX(total),0) n FROM equity')[0]['n']
            self.put('risk_peak',max(peak,nav))
            r=self.risk_state(t)
            if not r['ready']:self.db.commit();return
            last=self.get('risk_pending');stamp=t.isoformat()
            if not last or last['level']!=r['level'] or not 0<=(t-datetime.fromisoformat(last['last'])).total_seconds()<=120:
                last=dict(level=r['level'],since=stamp,last=stamp)
            else:last['last']=stamp
            self.put('risk_pending',last)
            confirmed=(t-datetime.fromisoformat(last['since'])).total_seconds()>=60
            if confirmed:self.put('risk_confirmed',dict(level=r['level'],ts=stamp))
            self.db.execute('INSERT OR REPLACE INTO risk_samples VALUES(?,?,?)',(t.date().isoformat(),stamp,json.dumps(r,ensure_ascii=False)));self.db.commit()
            if not confirmed or r['level']==0:return
            latest=self.rows('SELECT level,day FROM risk_alerts ORDER BY id DESC LIMIT 1')
            # One reminder per day for ongoing risk; any higher level may alert immediately.
            if latest and latest[0]['day']==t.date().isoformat() and latest[0]['level']>=r['level']:return
            self.db.execute('INSERT INTO risk_alerts(ts,day,level,payload) VALUES(?,?,?,?)',(stamp,t.date().isoformat(),r['level'],json.dumps(r,ensure_ascii=False)));self.db.commit()
        self.record(t,'risk-'+stamp[11:19].replace(':',''),'持仓风控 · '+r['label'],live=True,risk_override=r)

    def effective_analysis(self,t):
        a=json.loads(json.dumps(self.analysis)) if self.analysis else None
        r=self.risk_state(t)
        previous=self.get('risk_confirmed')
        recovering=bool(previous and previous['level']>0 and not (r['confirmed'] and r['level']==0))
        if a and a.get('action')=='买入' and r['live'] and (not r['ready'] or r['level']>0 or recovering):
            a.update(action='观望',allocation=0)
            a['reasons'].append('持仓风控优先：风险尚未解除或数据不足，暂停新增买入')
        flow=self.capital.state(t)
        if a and a.get('action')=='买入' and r['live'] and flow['mode']=='filter' and flow['ready'] and flow['outflow']:
            a.update(action='观望',allocation=0)
            a['reasons'].append('资金过滤：创业板ETF样本出现已确认日度异常净赎回，暂停新增买入；交易者身份未确认')
        return a

    def state(self):
        with self.lock:
            t=now(); q=self.quotes.get('159915',{}); p=q.get('price');shares=self.get('shares');cost=self.get('cost');cash=self.get('cash')
            outstanding=self.rows("SELECT COUNT(*) n FROM decisions d WHERE action!='观望' AND NOT EXISTS(SELECT 1 FROM outcomes o WHERE o.decision_id=d.id AND horizon=1)")[0]['n']
            agg=self.rows('SELECT COUNT(*) n,COALESCE(SUM(success),0) wins,AVG(net) avg FROM outcomes WHERE horizon=1')[0]
            events=self.rows('SELECT * FROM events ORDER BY id DESC LIMIT 50')
            ds=self.rows('SELECT d.*,o.net,o.success,o.due FROM decisions d LEFT JOIN outcomes o ON o.decision_id=d.id AND o.horizon=1 ORDER BY d.id DESC LIMIT 80')
            grouped=self.rows('SELECT horizon,COUNT(*) n,SUM(success) wins,AVG(net) avg FROM outcomes GROUP BY horizon')
            realized=self.rows('SELECT COALESCE(SUM(realized),0) n FROM fills')[0]['n']
            display=self.effective_analysis(t)
            if display and display.get('action') in ('买入','卖出'):
                key=display['family']+'|'+display['action']
                if self.rows('SELECT key FROM opportunities WHERE key=? AND active=1',(key,)) or self.rows('SELECT id FROM decisions WHERE day=? AND action=? LIMIT 1',(t.date().isoformat(),display['action'])):
                    display.update(action='观望',allocation=0)
                    display['reasons'].append('原条件仍可成立，但同一机会/今日同向已提醒，不重复操作')
            return {'now':t.isoformat(),'phase':phase(t),'quotes':self.quotes,'error':self.error,'last_fetch':self.last_fetch,
                'fresh':self.fresh(t),'history_date':self.hist_date,'analysis':display,'downside_risk':self.risk_state(t),
                'capital':self.capital.details(t),
                'monitor_health':self.rows('SELECT * FROM monitor_days ORDER BY day DESC LIMIT 7'),
                'health_warnings':self.rows('SELECT * FROM health_alerts ORDER BY ts DESC LIMIT 10'),
                'close_status':self.rows('SELECT * FROM close_status ORDER BY day DESC LIMIT 20'),
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

    def repair_state(self):
        with self.lock:
            current=repair.assess(self.bars.get('399006',[]),self.quotes.get('399006',{}))
            return dict(current=current,pair_count=self.rows('SELECT COUNT(*) n FROM repair_pairs')[0]['n'],pairs=self.rows('SELECT * FROM repair_pairs ORDER BY day DESC LIMIT 20'),
                        results=self.rows('SELECT * FROM repair_results ORDER BY day DESC,horizon LIMIT 60'),
                        opportunities=self.rows('SELECT * FROM opportunity_audit ORDER BY id DESC LIMIT 15'),
                        note='每天首个有效交易时段快照配对；旧规则原始方向 vs 新研究假设。不是两个账户收益，不自动替换正式策略。')

    def capture_repair(self,t):
        if not self.analysis or self.error or not self.fresh(t):return
        candidate=repair.assess(self.bars.get('399006',[]),self.quotes.get('399006',{}))
        if not candidate['eligible']:return
        candidate['roundtrip_bps']=self.get('roundtrip_bps')
        with self.lock:
            self.db.execute('INSERT OR IGNORE INTO repair_pairs VALUES(?,?,?,?,?,?,?)',
                (t.date().isoformat(),t.isoformat(),self.quotes['159915']['price'],self.analysis['action'],
                 candidate['action'],json.dumps(candidate,ensure_ascii=False),repair.VERSION));self.db.commit()

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
        if ph in ['上午交易','下午交易'] and self.fresh(t) and not self.error:
            self.check_risk(t)
            self.capital.observe(t,self.risk_state(t))
            opportunities.observe(self,t,self.analysis)
            self.capture_repair(t)
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

    def record(self,t,slot,title,live,risk_override=None):
        with self.lock:
            day=t.date().isoformat()
            if self.rows('SELECT id FROM decisions WHERE day=? AND slot=?',(day,slot)):return
            a=self.effective_analysis(t) if self.analysis and live else dict(action='观望',family='data',regime='行情待确认',reasons=['没有新鲜完整行情，暂停操作'],allocation=0,version=VERSION)
            if risk_override:
                a=dict(action='卖出' if risk_override['quantity'] else '观望',family='downside_risk',regime=risk_override['label'],
                    allocation=0,reasons=list(risk_override['reasons']),risk=risk_override,version=VERSION)
            if not risk_override and a['family'] in self.get('shadow_families'):
                a['shadow_action']=a['action'];a['action']='观望';a['reasons'].append('同类信号样本外表现不足，进入影子观察')
            eq=self.quotes.get('159915',{}); iq=self.quotes.get('399006',{})
            price=eq.get('price',0)
            qty=size_order(a['action'],price,self.get('shares'),self.get('cash'),self.sellable(day),a.get('allocation',0)) if price else 0
            if risk_override:qty=risk_override['quantity']
            a.update(qty=qty,quote_ts=eq.get('timestamp'),horizons=[1,3,5],roundtrip_bps=self.get('roundtrip_bps'))
            if a['action']!='观望' and qty==0:
                a['signal_action']=a['action'];a['action']='观望';a['reasons'].append('现金或T+1可卖份额不足，本次不建议执行')
            if not risk_override and a['action'] in ('买入','卖出') and not opportunities.claim(self,t,a):
                if slot!='open':return
                a['signal_action']=a['action'];a['action']='观望';qty=0;a['qty']=0
                a['reasons'].append('同一机会已提醒或今日该方向已提醒；保持观察，不重复加减仓')
            self.db.execute('INSERT OR IGNORE INTO decisions(ts,day,slot,action,family,price,idx,payload,version) VALUES(?,?,?,?,?,?,?,?,?)',
                (t.isoformat(),day,slot,a['action'],a['family'],price,iq.get('price',0),json.dumps(a,ensure_ascii=False),VERSION));self.db.commit()
            body=f"{a['action']} · {a['regime']}\n"+'\n'.join(a['reasons'])
            if risk_override:body+=f"\n登记仓位 {risk_override['exposure']:.1%}；目标上限 {risk_override['target']:.0%}。参考减仓按最新登记持仓重新计算，不是追加卖单；请核对已成交记录与T+1可卖份额。"
            if qty: body+=f'\n159915参考 {price:.3f} 元，小批 {qty} 份；请在券商核实可成交价格。'
            flow=self.capital.state(t)
            body+='\n资金观察：'+flow['label']+'（日度数据 '+str(flow['data_day'] or '待取得')+'，交易者身份未确认）。'
            body+='\n建议有效10分钟；仅提示，不自动下单。次一/三/五交易日收盘验证，胜率不是盈利承诺。'
            if a.get('shadow_action') and slot!='open':
                # Shadow records are not executable advice; avoid mobile or desktop trading prompts.
                return
            self.event('decision',title,body)

    def settle(self,t,final=False,bars_override=None):
        snapshot=self.bars if bars_override is None else bars_override
        self.capital.settle(t,snapshot.get('159915',[]),final)
        bars={r['date']:r for r in snapshot.get('159915',[]) if r['date']<t.date().isoformat() or (final and t.hour*60+t.minute>=905 and r['date']==t.date().isoformat())}
        for pair in self.rows('SELECT * FROM repair_pairs'):
            for horizon in (1,3,5):
                due=sessions_after(pair['day'],horizon)
                if due not in bars or pair['price']<=0:continue
                move=bars[due]['close']/pair['price']-1
                bps=json.loads(pair['payload'])['roundtrip_bps']/10000
                def diagnostic(action):return None if action=='观望' else move*(1 if action=='买入' else -1)-bps
                with self.lock:
                    self.db.execute('INSERT OR IGNORE INTO repair_results VALUES(?,?,?,?,?,?)',
                        (pair['day'],horizon,due,move,diagnostic(pair['baseline']),diagnostic(pair['candidate'])));self.db.commit()
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
                    index_close=next((r['close'] for r in snapshot.get('399006',[]) if r['date']==due),None)
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

    def recover_reports(self,t,force=False):
        """Recover observed days only, including after 18:00 and at weekend startup."""
        if not self.get('monitor_enabled') or (not force and time.monotonic()<self.next_recovery):return
        self.next_recovery=time.monotonic()+900
        days=self.rows('SELECT day FROM (SELECT day FROM monitor_days UNION SELECT day FROM decisions) WHERE day<=? AND day NOT IN (SELECT day FROM reports) ORDER BY day', (t.date().isoformat(),))
        days=[r['day'] for r in days if trading_day(r['day']) and (r['day']<t.date().isoformat() or t.hour*60+t.minute>=905)]
        if not days:return
        try:final={s:market.history(s) for s in ['399006','159915']}
        except Exception as ex:
            for day in days:self.close_failure(day,ex)
            return
        for day in days:
            historical=day<t.date().isoformat()
            self.close_report(datetime.fromisoformat(day+'T15:10:00').replace(tzinfo=SH),final_data=final,historical=historical)

    def close_failure(self,day,ex):
        detail=type(ex).__name__+': '+str(ex)[:400]
        with self.lock:
            old=self.rows('SELECT detail FROM close_status WHERE day=?',(day,))
            self.db.execute('INSERT OR REPLACE INTO close_status VALUES(?,?,?,?)',(day,now().isoformat(),'等待重试',detail));self.db.commit()
        if not old or old[0]['detail']!=detail:
            self.event('close_wait','收盘复盘待重试 · '+day,detail+'。将定期重试，重启后也会补结算；不补造买卖建议。',phone=False)

    def portfolio_at_close(self,day):
        # Reverse subsequent actual fills, never value an old day with today's position.
        shares=self.get('shares');cash=self.get('cash')
        for f in self.rows('SELECT * FROM fills WHERE day>? ORDER BY id DESC',(day,)):
            if f['side']=='买入':shares-=f['qty'];cash+=f['qty']*f['price']+f['fee']
            else:shares+=f['qty'];cash-=f['qty']*f['price']-f['fee']
        if shares<0 or cash<-.01:raise ValueError('历史账户无法从成交账本还原，请核对账本')
        return shares,round(cash,4)

    def close_report(self,t,final_data=None,historical=False):
        with self.close_lock:
            try:return self._close_report(t,final_data,historical)
            except Exception as ex:self.close_failure(t.date().isoformat(),ex)

    def _close_report(self,t,final_data=None,historical=False):
        day=t.date().isoformat()
        if self.rows('SELECT day FROM reports WHERE day=?',(day,)):return
        if not trading_day(day) or t.hour*60+t.minute<905:return
        # Re-fetch final unadjusted daily bars; outdated closes never settle an outcome.
        try:
            source=final_data if final_data is not None else {s:market.history(s) for s in ['399006','159915']}
            final={s:[r for r in source[s] if r['date']<=day] for s in ['399006','159915']}
            for symbol,rows in final.items():
                if len(rows)<2 or rows[-1]['date']!=day:
                    raise ValueError(f'{symbol} 目标日期 {day}，最新可用日K {rows[-1]["date"] if rows else "无"}')
        except Exception as ex:
            self.close_failure(day,ex)
            return
        self.settle(t,final=True,bars_override=final)
        if not historical:self.learn(day)
        with self.lock:
            close=final['159915'][-1]['close'];shares,cash=self.portfolio_at_close(day)
            self.db.execute('INSERT OR IGNORE INTO equity VALUES(?,?,?,?,?)',(day,shares*close+cash,shares,cash,close));self.db.commit()
        ds=self.rows('SELECT * FROM decisions WHERE day=?',(day,));agg=self.rows('SELECT COUNT(*) n,SUM(success) wins,AVG(net) avg FROM outcomes WHERE horizon=1 AND due<=?',(day,))[0]
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
        body+=('历史补结算：不追溯修改当日策略，不补造交易或信号。' if historical else '学习结果：'+('部分信号降级为影子观察。' if self.get('shadow_families') else '样本不足或没有充分退化证据，保持当前规则。'))
        pair=self.rows('SELECT baseline,candidate FROM repair_pairs WHERE day=?',(day,))
        body+='\n修复逻辑并行研究：'+(f"首个有效快照旧规则 {pair[0]['baseline']} / 新假设 {pair[0]['candidate']}；仅方向诊断，不执行、不混入正式胜率。" if pair else '今日无有效配对快照，不补造。')
        last_risk=self.rows('SELECT payload FROM risk_samples WHERE day=?',(day,))
        if last_risk:
            r=json.loads(last_risk[0]['payload'])
            body+=f"\n最后有效盘中风险观察：{r['label']}，登记仓位 {r['exposure']:.1%}；目标上限 "+('未触发' if r['target'] is None else f"{r['target']:.0%}")+'。参考建议不等于已成交，实际收益只按登记账本。'
        coverage=self.rows('SELECT fresh_ticks,gaps FROM monitor_days WHERE day=?',(day,))
        if coverage:body+=f"\n监控覆盖：{coverage[0]['fresh_ticks']}次有效行情采样，检测到{coverage[0]['gaps']}次间隔缺口；不代表全天无遗漏。"
        body+='\n'+(f'补生成于 {now().isoformat()}；仅使用该日及此前日K和已登记成交；不以当前资金观察代替历史快照。' if historical else self.capital.summary(t))
        with self.lock:
            self.db.execute('INSERT OR IGNORE INTO reports VALUES(?,?)',(day,body))
            self.db.execute('INSERT OR REPLACE INTO close_status VALUES(?,?,?,?)',(day,now().isoformat(),'已补结算' if historical else '已完成',''))
            self.db.commit()
        self.event('close','收盘复盘 · '+day,body,phone=not historical)

    def push_pending(self):
        if not self.get('push_enabled') or not self.get('token'): return
        for e in self.rows("SELECT * FROM events WHERE status='待发送' AND attempts<3 AND retry_at<=? ORDER BY id LIMIT 3",(time.time(),)):
            if e['kind'] in ('decision','health_warning','capital_signal','capital_news') and (now()-datetime.fromisoformat(e['ts'])).total_seconds()>600:
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
        self.capital.start()
        self.refresh()
        while not self.stop.is_set():
            try:
                t=now();ph=phase(t)
                if self.get('monitor_enabled') and trading_day(t.date()) and 565<=t.hour*60+t.minute<1080:
                    self.refresh();t=now();self.dispatch_due(t)
                    self.monitor_health(t)
                self.recover_reports(t)
                self.push_pending()
            except Exception as ex:self.error='监控轮次异常：'+type(ex).__name__+'；下一轮重试'
            self.wake.wait(self.get('poll'));self.wake.clear()
