import math, threading, uuid, sys, winreg, subprocess, webbrowser, json
from pathlib import Path
from app.service import secret_codec
from app.calendar_cn import now


class API:
    def __init__(self,service): self._s=service
    def get_state(self): return self._s.state()
    def retry_reports(self):
        self._s.next_recovery=0;self._s.wake.set()
        return {'ok':True}
    def get_repair(self): return self._s.repair_state()
    def set_capital_mode(self,mode):
        if mode not in ['shadow','filter']:return {'ok':False,'message':'请选择有效资金观察模式'}
        with self._s.lock:
            self._s.put('capital_mode',mode);self._s.db.commit()
        return {'ok':True,'message':'已保存：'+('并行观察，单独记录资金候选' if mode=='shadow' else '启用异常净赎回买入过滤；仍由原策略与持仓风控决定买卖')}
    def add_capital_holding(self,v):
        from urllib.parse import urlparse
        from datetime import date
        from app.capital_data import FUNDS
        try:
            subject=str(v['subject']).strip();code=str(v['code']);url=str(v['url']).strip()
            shares=float(v['shares']);asof=date.fromisoformat(v['asof']);published=date.fromisoformat(v['published'])
            host=urlparse(url).hostname or ''
            allowed=['sse.com.cn','szse.cn','cninfo.com.cn','huijin-inv.cn','efunds.com.cn','gffunds.com.cn','icbccs.com.cn','jsfund.cn','huatai-pb.com','chinaamc.com','nffund.com']
            if not any(host==d or host.endswith('.'+d) for d in allowed) or urlparse(url).scheme!='https' or len(url)>1000:raise ValueError('请输入交易所、巨潮或观察池基金管理人的HTTPS公告链接')
            if not subject or len(subject)>80 or code not in FUNDS:raise ValueError('请填写主体名称并选择观察池ETF')
            if not math.isfinite(shares) or shares<0 or shares>1e13:raise ValueError('持有份额须为有效的份数，不能填写亿元金额')
            if not asof<=published<=now().date():raise ValueError('必须满足：持仓截止日不晚于发布日期，发布日期不在未来')
            with self._s.lock:
                if self._s.rows('SELECT id FROM capital_holdings WHERE subject=? AND code=? AND asof=?',(subject,code,asof.isoformat())):raise ValueError('同主体、同ETF、同截止日已有记录，不覆盖原证据')
                self._s.db.execute('INSERT INTO capital_holdings(subject,code,asof,published,shares,url,entered) VALUES(?,?,?,?,?,?,?)',(subject,code,asof.isoformat(),published.isoformat(),shares,url,now().isoformat()));self._s.db.commit()
            return {'ok':True,'message':'已登记实名持仓证据；属于人工录入待核验，不作为实时买卖信号'}
        except Exception as e:return {'ok':False,'message':str(e)}
    def get_rules(self):
        root=Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parents[1]))
        return json.loads((root/'data/article_rule_registry.json').read_text(encoding='utf-8'))
    def export_records(self):
        # Backup uses SQLite's consistent snapshot, not a live copy of a WAL database.
        import sqlite3
        from contextlib import closing
        stamp=now().strftime('%Y%m%d-%H%M%S-%f')
        folder=self._s.root/'exports';folder.mkdir(exist_ok=True)
        target=folder/('记录备份-'+stamp+'.sqlite3')
        with self._s.lock,closing(sqlite3.connect(target)) as dest:self._s.db.backup(dest)
        return {'ok':True,'message':'已备份至 '+str(target)+'（含当前用户加密令牌，请妥善保管）'}
    def refresh(self):
        threading.Thread(target=self._s.refresh,daemon=True).start()
        self._s.capital.request_refresh()
        self._s.wake.set()
        return {'ok':True}
    def save_settings(self,values):
        try:
            channel=values.get('channel','wechat')
            if channel not in ['wechat','qq','app']:raise ValueError('请选择有效推送渠道')
            token=str(values.get('token','')).strip()
            if token and (len(token)>256 or not token.isalnum()):raise ValueError('Token 格式不正确')
            encrypted=secret_codec(token) if token else None
            with self._s.lock:
                for k in ['push_enabled','monitor_enabled']:self._s.put(k,bool(values.get(k,False)))
                self._s.put('channel',channel)
                if encrypted:self._s.put('token',encrypted)
                self._s.db.commit()
            self._s.wake.set()
            return {'ok':True,'message':'设置已保存；手机推送请再发送测试消息。'}
        except Exception as e:return {'ok':False,'message':str(e)}
    def test_push(self):
        if not self._s.get('push_enabled') or not self._s.get('token'):return {'ok':False,'message':'请先保存 Token 并开启手机推送'}
        self._s.event('test','和融联 · 手机连接测试','这是一条连接测试，不是交易建议。请在手机确认收到。')
        self._s.wake.set()
        return {'ok':True,'message':'测试已加入发送队列，请在通知记录查看状态，并在手机确认收到。'}
    def open_pushplus(self):
        webbrowser.open('https://www.pushplus.plus/')
    def autostart(self,enabled):
        try:
            if not getattr(sys,'frozen',False):raise ValueError('请在打包后的应用中设置自动启动')
            from app.startup import configure
            result=configure(sys.executable,enabled)
            with self._s.lock:
                for k in ['autostart','startup_task']:self._s.put(k,result[k])
                self._s.db.commit()
            return {'ok':True,'message':result['message']}
        except Exception as e:return {'ok':False,'message':str(e)}
    def record_trade(self,v):
        try:
            side=v['side'];qty=int(v['qty']);price=float(v['price']);fee=float(v.get('fee',0));nonce=str(v['nonce'])
            if side not in ['买入','卖出'] or qty<=0 or qty%100 or not all(math.isfinite(x) for x in [price,fee]) or price<=0 or fee<0:raise ValueError('请填写有效成交：份数为100的整数倍，价格大于0，费用非负')
            if str(qty)!=str(v['qty']).strip():raise ValueError('份数必须为整数')
            if len(nonce)>100:raise ValueError('请求标识不正确')
            t=now();day=t.date().isoformat()
            with self._s.lock:
                if self._s.rows('SELECT id FROM fills WHERE nonce=?',(nonce,)):return {'ok':True,'message':'此笔成交已经登记，无重复入账'}
                shares=self._s.get('shares');cash=self._s.get('cash');cost=self._s.get('cost');realized=0
                if side=='买入':
                    if qty*price+fee>cash:raise ValueError('可用现金不足，请核对真实成交与账户资金')
                    cost=(shares*cost+qty*price+fee)/(shares+qty);shares+=qty;cash-=qty*price+fee
                else:
                    if qty>self._s.sellable(day):raise ValueError('超过今日可卖份额（已排除今日新买入份额）')
                    realized=qty*(price-cost)-fee;shares-=qty;cash+=qty*price-fee
                self._s.db.execute('INSERT INTO fills(ts,day,side,qty,price,fee,realized,nonce) VALUES(?,?,?,?,?,?,?,?)',(t.isoformat(),day,side,qty,price,fee,realized,nonce))
                for k,val in dict(shares=shares,cash=round(cash,4),cost=cost).items():self._s.put(k,val)
                self._s.db.commit()
            return {'ok':True,'message':'已登记实际成交；本应用没有向券商下单。'}
        except Exception as e:return {'ok':False,'message':str(e)}
