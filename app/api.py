import math, threading, uuid, sys, winreg, subprocess, webbrowser, json
from pathlib import Path
from app.service import secret_codec
from app.calendar_cn import now


class API:
    def __init__(self,service): self._s=service
    def get_state(self): return self._s.state()
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
