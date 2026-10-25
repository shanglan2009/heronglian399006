"""ETF flow research and an opt-in buy filter. No investor identity inference."""
import json
import math
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from statistics import mean
from app import capital_data as data
from app.calendar_cn import now, phase, trading_day, sessions_after

VERSION = 'HRL-capital-1'


def flow_series(rows):
    result = []
    for before, current in zip(rows, rows[1:]):
        reason = ''
        if data.previous_session(current['day']) != before['day']:
            reason = '相邻交易日缺失'
        elif not before.get('nav') or not current.get('nav'):
            reason = '同日净值缺失'
        elif current.get('corporate_action'):
            reason = '分红或份额调整待核验'
        elif abs(current['shares'] / before['shares'] - 1) > .30 or abs(current['nav'] / before['nav'] - 1) > .25:
            reason = '份额或净值异常跳变，待核验'
        result.append(dict(day=current['day'], shares=current['shares'], nav=current.get('nav'),
            amount=None if reason else (current['shares'] - before['shares']) * current['nav'],
            base=None if reason else before['shares'] * before['nav'], issue=reason))
    return result


def group_series(series, codes):
    lookup = {c: {r['day']: r for r in series.get(c, [])} for c in codes}
    days = sorted(set().union(*(set(x) for x in lookup.values())))
    out = []
    for day in days:
        points = [lookup[c].get(day) for c in codes]
        valid = [p for p in points if p and p['amount'] is not None]
        full = len(valid) == len(codes)
        out.append(dict(day=day, amount=sum(p['amount'] for p in valid) if full else None,
                        base=sum(p['base'] for p in valid) if full else None,
                        covered=len(valid), total=len(codes),
                        negative=sum(p['amount'] < 0 for p in valid), positive=sum(p['amount'] > 0 for p in valid)))
    return out


def evaluate(series, statuses, t):
    flows = {c: flow_series(rows) for c, rows in series.items()}
    core = group_series(flows, data.CORE)
    expected = data.previous_session(t.date())
    result = dict(version=VERSION, ready=False, expected=expected, data_day=None,
                  direction='观望', label='资金数据待确认', reasons=[], inflow=False, outflow=False,
                  amount=None, three=None, five=None, ratio=None, threshold=None,
                  history=core[-25:], funds=[], identity='交易者身份未确认', background=[])
    for c, (name, group) in data.FUNDS.items():
        rows = flows.get(c, [])
        last = rows[-1] if rows else {}
        result['funds'].append(dict(code=c, name=name, group=group, **last,
                                   source='深交所' if c.startswith('1') else '上交所',
                                   checked=statuses.get(c, {}).get('checked'),
                                   error=statuses.get(c, {}).get('error', '')))
    for name in ('沪深300', '上证50', '中证500'):
        codes = [c for c, v in data.FUNDS.items() if v[1] == name]
        points = group_series(flows, codes)
        result['background'].append(dict(name=name, **(points[-1] if points else {})))
    if not core:
        result['reasons'] = ['等待交易所日度份额和同日净值；不把缺失填成零']
        return result
    last = core[-1]
    result.update(data_day=last['day'], amount=last['amount'])
    if last['day'] != expected or last['amount'] is None:
        result['reasons'] = ['需要上一交易日完整的3只创业板ETF样本；当前存在滞后、缺失或异常值']
        return result
    for c in data.CORE:
        status = statuses.get(c, {})
        try:
            age = (t - datetime.fromisoformat(status['checked'])).total_seconds()
        except (KeyError, ValueError, TypeError):
            age = math.inf
        if status.get('error') or not 0 <= age <= 7200:
            result['reasons'] = ['创业板ETF来源校验失败或超过2小时未成功检查；资金过滤暂停']
            return result
    def window(count):
        selected = core[-count:]
        if len(selected) != count or any(x['amount'] is None for x in selected):
            return None
        if any(data.previous_session(b['day']) != a['day'] for a, b in zip(selected, selected[1:])):
            return None
        return sum(x['amount'] for x in selected)
    history = sorted(abs(r['amount']) for r in core[:-1][-60:] if r['amount'] is not None)
    threshold = max(100_000_000, last['base'] * .01,
                    history[min(len(history)-1, math.ceil(len(history)*.90)-1)] if len(history) >= 20 else 0)
    three, five = window(3), window(5)
    sustained_out = three is not None and three < -max(300_000_000, last['base']*.025) and all(x['amount'] < 0 for x in core[-3:])
    sustained_in = three is not None and three > max(300_000_000, last['base']*.025) and all(x['amount'] > 0 for x in core[-3:])
    outflow = last['negative'] >= 2 and (last['amount'] < -threshold or sustained_out)
    inflow = last['positive'] >= 2 and (last['amount'] > threshold or sustained_in)
    direction = '卖出' if outflow else '买入' if inflow else '观望'
    label = '异常净赎回' if outflow else '资金回流候选' if inflow else '未触发异常资金条件'
    result.update(ready=True, direction=direction, label=label, inflow=inflow, outflow=outflow,
                  three=three, five=five, ratio=last['amount']/last['base'], threshold=threshold,
                  reasons=[f"{last['day']} 创业板3只样本净申赎估算 {last['amount']/1e8:+.2f}亿元，占前日规模 {last['amount']/last['base']:+.2%}",
                           '份额为日终口径；金额按份额变化乘同日单位净值估算，交易者身份未确认',
                           '历史异常阈值已纳入' if len(history)>=20 else '历史不足20日，暂用绝对金额与规模占比阈值'])
    return result


class CapitalMonitor:
    def __init__(self, service):
        self.s = service
        self.thread = None
        self.last_attempt = 0
        self.last_news = 0
        self.wake = threading.Event()
        self.pending = None
        self.cached = None
        self.cache_key = None
        service.db.executescript('''
        CREATE TABLE IF NOT EXISTS capital_data(code TEXT PRIMARY KEY,payload TEXT,checked TEXT,error TEXT);
        CREATE TABLE IF NOT EXISTS capital_revisions(id INTEGER PRIMARY KEY,code TEXT,observed TEXT,payload TEXT);
        CREATE TABLE IF NOT EXISTS capital_news(id TEXT PRIMARY KEY,published TEXT,payload TEXT,seen TEXT);
        CREATE TABLE IF NOT EXISTS capital_status(name TEXT PRIMARY KEY,checked TEXT,error TEXT);
        CREATE TABLE IF NOT EXISTS capital_samples(id INTEGER PRIMARY KEY,day TEXT,ts TEXT,action TEXT,baseline TEXT,price REAL,payload TEXT,version TEXT,UNIQUE(day,action,version));
        CREATE TABLE IF NOT EXISTS capital_results(sample_id INTEGER,horizon INTEGER,due TEXT,net REAL,baseline_net REAL,adverse REAL,favorable REAL,reason TEXT,PRIMARY KEY(sample_id,horizon));
        CREATE TABLE IF NOT EXISTS capital_alerts(day TEXT,kind TEXT,PRIMARY KEY(day,kind));
        CREATE TABLE IF NOT EXISTS capital_holdings(id INTEGER PRIMARY KEY,subject TEXT,code TEXT,asof TEXT,published TEXT,shares REAL,url TEXT,entered TEXT,UNIQUE(subject,code,asof));
        ''')
        service.db.execute('INSERT OR IGNORE INTO settings VALUES(?,?)', ('capital_mode', '"shadow"'))
        service.db.commit()

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self.run, daemon=True, name='ETF-public-data')
        self.thread.start()

    def run(self):
        while not self.s.stop.is_set():
            try:
                t = now()
                if self.s.get('monitor_enabled') and t.year == 2026 and (not self.last_attempt or (trading_day(t.date()) and 540 <= t.hour*60+t.minute < 1080)):
                    self.refresh(t)
            except Exception as exc:
                with self.s.lock:
                    self.s.db.execute('INSERT OR REPLACE INTO capital_status VALUES(?,?,?)',
                        ('worker', now().isoformat(), '资金采集失败：'+type(exc).__name__))
                    self.s.db.commit()
            self.wake.wait(60)
            self.wake.clear()

    def request_refresh(self):
        self.last_attempt = 0
        self.last_news = 0
        self.wake.set()

    def refresh(self, t):
        # Separate worker: slow disclosures cannot delay price risk monitoring.
        if t.timestamp() - self.last_attempt >= 1800:
            self.last_attempt = t.timestamp()
            end = data.previous_session(t.date())
            start = (t.date()-timedelta(days=100)).isoformat()
            if not end:
                return
            stored = {r['code']: json.loads(r['payload']) for r in self.s.rows('SELECT code,payload FROM capital_data')}
            def fetch_one(code):
                if code.startswith('1'):
                    # Bootstrap a short history; thereafter recheck recent rows for revisions.
                    begin = (t.date()-timedelta(days=12)).isoformat() if len(stored.get(code, [])) >= 25 else start
                    shares = data.sz_shares(code, begin, end)
                else:
                    shares = [r for r in stored.get(code, []) if r['day'] < end]
                    for day, result in sse_days.items():
                        if isinstance(result, Exception):
                            raise result
                        shares = [r for r in shares if r['day'] != day] + [result[code]]
                navs = data.nav_history(code, start, end)
                if code.startswith('1'):
                    latest = {r['day']: r for r in stored.get(code, [])}
                    latest.update({r['day']: r for r in shares})
                    shares = [r for r in latest.values() if start <= r['day'] <= end]
                return data.join_series(shares, navs, end)
            # SSE exposes one date at a time. Bootstrap seven sessions, then refresh two.
            sse_days = {}
            count = 2 if all(len(stored.get(c, [])) >= 7 for c in data.FUNDS if c.startswith('5')) else 7
            day = end
            days = []
            for _ in range(count):
                if day:
                    days.append(day)
                    day = data.previous_session(day)
            with ThreadPoolExecutor(max_workers=3) as pool:
                jobs = {pool.submit(data.sse_shares, d): d for d in days}
                for f in as_completed(jobs):
                    try:
                        sse_days[jobs[f]] = f.result()
                    except Exception as exc:
                        sse_days[jobs[f]] = exc
            with ThreadPoolExecutor(max_workers=4) as pool:
                jobs = {pool.submit(fetch_one, c): c for c in data.FUNDS}
                for future in as_completed(jobs):
                    if self.s.stop.is_set():
                        return
                    code = jobs[future]
                    stamp = now().isoformat()
                    error = ''
                    try:
                        rows = future.result()
                    except Exception as exc:
                        rows = stored.get(code, [])
                        error = type(exc).__name__ + '：'+str(exc)[:100]
                    payload = json.dumps(rows, ensure_ascii=False)
                    with self.s.lock:
                        old = self.s.rows('SELECT payload FROM capital_data WHERE code=?', (code,))
                        if not error and (not old or json.loads(old[0]['payload']) != rows):
                            self.s.db.execute('INSERT INTO capital_revisions(code,observed,payload) VALUES(?,?,?)', (code, stamp, payload))
                        self.s.db.execute('INSERT OR REPLACE INTO capital_data VALUES(?,?,?,?)', (code, payload, stamp, error))
                        self.s.db.commit()
                        self.cache_key = None
        if t.timestamp() - self.last_news >= 900:
            self.last_news = t.timestamp()
            try:
                items, source = data.announcements(t.date().isoformat())
                for item in items:
                    with self.s.lock:
                        known = self.s.rows('SELECT id FROM capital_news WHERE id=?', (item['id'],))
                        self.s.db.execute('INSERT OR IGNORE INTO capital_news VALUES(?,?,?,?)',
                            (item['id'], item['published'], json.dumps(item, ensure_ascii=False), now().isoformat()))
                        self.s.db.commit()
                    if not known and item['published'] == t.date().isoformat():
                        self.s.event('capital_news', '汇金官网新增ETF相关资讯', item['title']+'\n'+item['excerpt']+'\n'+item['url']+'\n发布时间仅精确到日期；资讯摘要待核验，不能据此推定金额或即时买卖。')
                error = ''
            except Exception as exc:
                error = type(exc).__name__+'：'+str(exc)[:100]
            with self.s.lock:
                self.s.db.execute('INSERT OR REPLACE INTO capital_status VALUES(?,?,?)', ('汇金官网', now().isoformat(), error))
                self.s.db.commit()

    def state(self, t):
        with self.s.lock:
            raw = self.s.rows('SELECT * FROM capital_data')
            key = (t.strftime('%Y-%m-%dT%H:%M'), tuple((r['code'], r['checked'], r['error']) for r in raw))
            if self.cache_key != key:
                self.cached = evaluate({r['code']: json.loads(r['payload']) for r in raw}, {r['code']: r for r in raw}, t)
                self.cache_key = key
            result = dict(self.cached)
            result['mode'] = self.s.get('capital_mode')
            return result

    def details(self, t):
        result = self.state(t)
        result.update(news=[json.loads(x['payload']) for x in self.s.rows('SELECT payload FROM capital_news ORDER BY published DESC LIMIT 12')],
            news_status=self.s.rows('SELECT * FROM capital_status'),
            holdings=self.s.rows('SELECT * FROM capital_holdings ORDER BY asof DESC,id DESC LIMIT 40'),
            samples=self.s.rows('SELECT * FROM capital_samples ORDER BY id DESC LIMIT 15'),
            stats=self.s.rows('SELECT horizon,COUNT(*) n,SUM(net>0) wins,AVG(net) avg,AVG(baseline_net) baseline,AVG(adverse) adverse FROM capital_results r JOIN capital_samples s ON s.id=r.sample_id WHERE s.version=? GROUP BY horizon', (VERSION,)),
            results=self.s.rows('SELECT r.*,s.action,s.day FROM capital_results r JOIN capital_samples s ON s.id=r.sample_id ORDER BY s.id DESC,r.horizon LIMIT 50'))
        daily=self.s.rows('SELECT s.day,AVG(r.net) net FROM capital_results r JOIN capital_samples s ON s.id=r.sample_id WHERE r.horizon=1 AND s.version=? GROUP BY s.day ORDER BY s.day',(VERSION,))[-90:]
        result['learning']=f'已积累 {len(daily)} 个独立交易日；45日后检查训练段与最近15日验证段，暂不自动调参。'
        if len(daily)>=45:
            train=mean(x['net'] for x in daily[:-15]);valid=mean(x['net'] for x in daily[-15:])
            result['learning']=f'训练段平均净方向变化 {train:+.2%}，最近15日验证段 {valid:+.2%}。'+('两段均为负，建议停用资金过滤并人工复核。' if train<0 and valid<0 else '未达到双段退化条件；仍不证明增益，不自动扩大仓位。')
        return result

    def observe(self, t, risk):
        if not self.s.fresh(t) or self.s.error or phase(t) not in ('上午交易', '下午交易'):
            self.pending = None
            return
        result = self.state(t)
        if not result['ready'] or not risk['ready']:
            self.pending = None
            return
        rows = [r for r in self.s.bars.get('399006', []) if r['date'] < t.date().isoformat()]
        rising = len(rows) >= 25 and mean(r['close'] for r in rows[-20:]) > mean(r['close'] for r in rows[-25:-5])
        repair = risk['level'] == 0 and self.s.quotes['399006']['price'] > risk['ma20'] and rising
        action = '卖出' if result['outflow'] and risk['level'] > 0 else '买入' if result['inflow'] and repair else '观望'
        signature = (result['data_day'], result['direction'], action)
        if not self.pending or self.pending['key'] != signature or not 0 <= (t-self.pending['last']).total_seconds() <= 120:
            self.pending = dict(key=signature, since=t, last=t)
            return
        self.pending['last'] = t
        if (t-self.pending['since']).total_seconds() < 60:
            return
        if action != '观望':
            with self.s.lock:
                snapshot = dict(result, price_trend='修复确认' if action=='买入' else risk['label'], roundtrip_bps=self.s.get('roundtrip_bps'))
                self.s.db.execute('INSERT OR IGNORE INTO capital_samples(day,ts,action,baseline,price,payload,version) VALUES(?,?,?,?,?,?,?)',
                    (t.date().isoformat(), t.isoformat(), action, self.s.analysis['action'] if self.s.analysis else '观望', self.s.quotes['159915']['price'], json.dumps(snapshot, ensure_ascii=False), VERSION))
                self.s.db.commit()
        if result['direction'] == '观望':
            return
        kind = result['direction']+'-'+action
        with self.s.lock:
            if self.s.rows('SELECT kind FROM capital_alerts WHERE day=? AND kind=?', (t.date().isoformat(), kind)):
                return
            self.s.db.execute('INSERT INTO capital_alerts VALUES(?,?)', (t.date().isoformat(), kind))
            self.s.db.commit()
        body = '\n'.join(result['reasons'])
        if action == '观望':
            body += '\n资金与价格尚未形成同向确认，暂不形成买卖候选。'
        else:
            body += '\n并行研究候选：'+action+'。此候选单独验证，不作为新增下单指令。'
        body += '\n原有持仓减仓风控继续独立运行；官方交易者身份未确认。'
        self.s.event('capital_signal', '资金观察 · '+result['label'], body)

    def settle(self, t, bars, final=False):
        complete = {r['date']: r for r in bars if r['date'] < t.date().isoformat() or (final and t.hour*60+t.minute >= 905 and r['date']==t.date().isoformat())}
        for sample in self.s.rows('SELECT * FROM capital_samples'):
            for horizon in (1, 3, 5, 20):
                due = sessions_after(sample['day'], horizon)
                if due not in complete or sample['price'] <= 0:
                    continue
                interval = [r for d, r in complete.items() if sample['day'] < d <= due]
                if len(interval) != horizon:
                    continue
                move = complete[due]['close']/sample['price']-1
                payload = json.loads(sample['payload'])
                cost = payload['roundtrip_bps']/10000
                sign = 1 if sample['action']=='买入' else -1
                net = move*sign-cost
                base = None if sample['baseline']=='观望' else move*(1 if sample['baseline']=='买入' else -1)-cost
                extremes = [(r[k]/sample['price']-1)*sign-cost for r in interval for k in ('high','low')]
                reason = '方向暂符合' if net>0 else ('资金流入后趋势未延续或候选过早，原因待验证' if sign==1 else '减仓候选后价格反弹，存在错过上涨的代价')
                with self.s.lock:
                    self.s.db.execute('INSERT OR IGNORE INTO capital_results VALUES(?,?,?,?,?,?,?,?)',
                        (sample['id'], horizon, due, net, base, min(extremes), max(extremes), reason))
                    self.s.db.commit()

    def summary(self, t):
        r = self.state(t)
        text = '资金观察：'+r['label']+'；数据日 '+str(r['data_day'] or '待取得')+'。'+('；'.join(r['reasons']))
        agg = self.s.rows('SELECT COUNT(*) n,SUM(net>0) wins FROM capital_results r JOIN capital_samples s ON s.id=r.sample_id WHERE horizon=1 AND s.version=?', (VERSION,))[0]
        text += f"\n资金候选独立验证：次日到期 {agg['n']} 个，命中 {agg['wins'] or 0} 个；与正式策略胜率分开。"
        text += '\n日终份额可能次日才确认；不将最新日度数据描述成盘中实时资金。阈值尚未经过收益优化，不自动放大仓位。'
        return text
