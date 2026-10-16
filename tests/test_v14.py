import unittest,tempfile,json
from pathlib import Path
from datetime import datetime,timedelta
from unittest.mock import patch
from app.service import Service
from app.calendar_cn import SH
from app import risk

class RiskTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.s=Service(self.temp.name)
        self.t=datetime(2026,10,19,10,tzinfo=SH)
        start=datetime(2026,1,1)
        self.rows=[dict(date=(start+timedelta(days=i)).date().isoformat(),close=4000-i*12,open=4000-i*12,high=4010-i*12,low=3990-i*12,volume=100) for i in range(100)]
        self.s.bars={'399006':self.rows};self.s.analysis=dict(action='观望',family='hold',regime='test',reasons=[],allocation=0)
        self.quote(self.t,2750)
    def tearDown(self):self.s.db.close();self.temp.cleanup()
    def quote(self,t,price):
        self.s.quotes={'399006':dict(date=t.date().isoformat(),timestamp=t.isoformat(),price=price),'159915':dict(date=t.date().isoformat(),timestamp=t.isoformat(),price=3)}
    def test_downtrend_risk_even_when_original_holds(self):
        r=self.s.risk_state(self.t)
        self.assertEqual(r['level'],3);self.assertEqual(r['target'],.15);self.assertGreater(r['quantity'],0)
    def test_future_rows_ignored(self):
        a=self.s.risk_state(self.t)
        self.s.bars['399006']+= [dict(date='2026-10-20',close=999999)]
        self.assertEqual(self.s.risk_state(self.t),a)
    def test_data_insufficient_is_not_zero_risk(self):
        self.s.bars={};r=self.s.risk_state(self.t)
        self.assertFalse(r['ready']);self.assertIsNone(r['level']);self.assertIsNone(r['target'])
    def test_t1_zero_sellable_keeps_warning(self):
        self.s.db.execute('INSERT INTO fills(ts,day,side,qty,price,fee,realized,nonce) VALUES(?,?,?,?,?,?,?,?)',(self.t.isoformat(),self.t.date().isoformat(),'买入',1000,3,0,0,'test'));self.s.db.commit()
        r=self.s.risk_state(self.t);self.assertEqual(r['quantity'],0);self.assertEqual(r['level'],3)
    def test_insufficient_data_blocks_buy(self):
        self.s.bars={};self.s.analysis.update(action='买入')
        self.assertEqual(self.s.effective_analysis(self.t)['action'],'观望')
    def test_risk_blocks_buy(self):
        self.s.analysis.update(action='买入')
        self.assertEqual(self.s.effective_analysis(self.t)['action'],'观望')
    def test_risk_confirmation_and_repeat_dedup(self):
        for secs in (0,30,60,90):
            t=self.t+timedelta(seconds=secs);self.quote(t,2750);self.s.check_risk(t)
        alerts=self.s.rows('SELECT * FROM risk_alerts');self.assertEqual(len(alerts),1)
        ds=self.s.rows('SELECT * FROM decisions');self.assertEqual(len(ds),1);self.assertEqual(ds[0]['action'],'卖出')
        self.assertEqual(self.s.rows('SELECT * FROM fills'),[])
    def test_risk_escalation_bypasses_old_daily_sell_cap(self):
        self.s.db.execute('INSERT INTO decisions(ts,day,slot,action,family,price,idx,payload,version) VALUES(?,?,?,?,?,?,?,?,?)',(self.t.isoformat(),self.t.date().isoformat(),'old','卖出','top_divergence',3,2750,'{}','old'));self.s.db.commit()
        for secs in (0,60):
            t=self.t+timedelta(seconds=secs);self.quote(t,2750);self.s.check_risk(t)
        self.assertEqual(len(self.s.rows('SELECT * FROM risk_alerts')),1)
        # Increase observed account drawdown to >20%, forcing a higher risk level.
        self.s.put('risk_peak',100000);self.s.db.commit()
        for secs in (90,150):
            t=self.t+timedelta(seconds=secs);self.quote(t,2750);self.s.check_risk(t)
        self.assertEqual([a['level'] for a in self.s.rows('SELECT * FROM risk_alerts')],[3,4])
        self.assertEqual(len(self.s.rows('SELECT * FROM decisions')),3)
    def test_disconnect_resets_confirmation(self):
        self.s.check_risk(self.t)
        later=self.t+timedelta(minutes=5);self.quote(later,2750);self.s.check_risk(later)
        self.assertEqual(self.s.rows('SELECT * FROM risk_alerts'),[])
    def test_recovery_cannot_buy_before_confirmation(self):
        self.s.put('risk_confirmed',dict(level=3,ts=self.t.isoformat()));self.s.db.commit()
        self.s.analysis.update(action='买入')
        self.quote(self.t,5000)
        self.assertEqual(self.s.risk_state(self.t)['level'],0)
        self.assertEqual(self.s.effective_analysis(self.t)['action'],'观望')
        for secs in (0,60):
            t=self.t+timedelta(seconds=secs);self.quote(t,5000);self.s.check_risk(t)
        self.assertEqual(self.s.effective_analysis(t)['action'],'买入')
    def test_stale_and_closed_cannot_send_risk(self):
        self.s.check_risk(self.t+timedelta(minutes=4))
        t=self.t.replace(hour=16);self.quote(t,2750);self.s.check_risk(t)
        self.assertEqual(self.s.rows('SELECT * FROM risk_alerts'),[])
    def test_account_peak_is_observed_not_cost(self):
        self.s.check_risk(self.t)
        self.assertEqual(self.s.get('risk_peak'),13000)
        self.assertEqual(self.s.risk_state(self.t)['account_drawdown'],0)
    def test_health_missing_open_once_per_day(self):
        self.s.monitor_health(self.t);self.s.monitor_health(self.t)
        self.assertEqual(len(self.s.rows("SELECT * FROM health_alerts WHERE code='open'")),1)
        self.assertEqual(self.s.rows('SELECT ticks FROM monitor_days')[0]['ticks'],2)
    def test_health_gap_and_lunch_not_false_gap(self):
        self.s.monitor_health(self.t)
        self.s.monitor_health(self.t+timedelta(minutes=10))
        self.assertEqual(self.s.rows('SELECT gaps FROM monitor_days')[0]['gaps'],1)
        self.s.monitor_health(self.t.replace(hour=11,minute=29))
        before=self.s.rows('SELECT gaps FROM monitor_days')[0]['gaps']
        self.s.monitor_health(self.t.replace(hour=13,minute=1))
        self.assertEqual(self.s.rows('SELECT gaps FROM monitor_days')[0]['gaps'],before)
    def test_missing_close_is_reported(self):
        self.s.monitor_health(self.t.replace(hour=15,minute=16))
        self.assertEqual(len(self.s.rows("SELECT * FROM health_alerts WHERE code='close'")),1)
    def test_health_outage_sends_only_one_warning(self):
        self.s.error='offline'
        for i in range(3):self.s.monitor_health(self.t+timedelta(seconds=i*30))
        self.assertEqual(len(self.s.rows("SELECT * FROM health_alerts WHERE code='stale'")),1)
    def test_upgrade_preserves_existing_settings(self):
        self.s.put('cash',12345);self.s.put('token','encrypted-test');self.s.db.commit()
        self.s.db.close();self.s=Service(self.temp.name)
        self.assertEqual(self.s.get('cash'),12345);self.assertEqual(self.s.get('token'),'encrypted-test')
    def test_reduction_never_exceeds_shares(self):
        r=risk.assess(self.rows,self.s.quotes['399006'],self.s.quotes['159915'],100,0,100,100000)
        self.assertEqual(r['quantity'],100)
        r=risk.assess(self.rows,self.s.quotes['399006'],self.s.quotes['159915'],0,100000,0,100000)
        self.assertEqual(r['quantity'],0)

if __name__=='__main__':unittest.main()
