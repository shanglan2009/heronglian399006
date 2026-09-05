import unittest,tempfile,json
from datetime import datetime
from unittest.mock import patch
from app.calendar_cn import SH,trading_day,sessions_after
from app.service import Service,secret_codec
from app.api import API
from app.signals import size_order,wilson

class AppTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.s=Service(self.tmp.name);self.api=API(self.s)
    def tearDown(self):self.s.db.close();self.tmp.cleanup()
    def test_calendar(self):
        self.assertFalse(trading_day('2026-10-01'));self.assertFalse(trading_day('2026-09-05'));self.assertFalse(trading_day('2027-01-04'))
        self.assertEqual(sessions_after('2026-09-24',1),'2026-09-28')
    def test_defaults(self):
        p=self.s.state()['portfolio'];self.assertEqual(p['cash'],10000);self.assertEqual(p['shares'],1000);self.assertEqual(p['cost'],3.0)
    def test_encryption(self):
        text=secret_codec('secretForTest');self.assertNotIn('secretForTest',text);self.assertEqual(secret_codec(text,True),'secretForTest')
    def test_trade_t1_idempotent(self):
        trade=dict(side='买入',qty='1000',price='3',fee='2',nonce='one')
        self.assertTrue(self.api.record_trade(trade)['ok']);self.assertTrue(self.api.record_trade(trade)['ok'])
        self.assertEqual(self.s.get('cash'),6998);self.assertEqual(self.s.get('shares'),2000)
        self.assertEqual(self.s.state()['portfolio']['sellable'],1000)
        self.assertFalse(self.api.record_trade(dict(side='卖出',qty='1100',price='3',fee='0',nonce='two'))['ok'])
    def test_trade_invalid(self):
        for value in ['nan','inf','-1','0']:
            self.assertFalse(self.api.record_trade(dict(side='买入',qty='100',price=value,fee='0',nonce=value))['ok'])
        self.assertEqual(self.s.get('shares'),1000)
    def test_buy_capped(self):
        self.assertLessEqual(size_order('买入',3,1000,10000,1000,1)*3*1.001,10000)
        self.assertEqual(size_order('观望',3,1000,10000,1000),0)
    def test_stale_open_no_trade_duplicate(self):
        t=datetime(2026,9,7,9,31,tzinfo=SH)
        self.s.analysis=dict(action='买入',family='panic_rebound',regime='test',allocation=.1,reasons=[])
        self.s.dispatch_due(t);self.s.dispatch_due(t)
        d=self.s.rows('SELECT * FROM decisions');self.assertEqual(len(d),1);self.assertEqual(d[0]['action'],'观望')
    def test_future_close_and_immutable(self):
        t=datetime(2026,9,4,10,tzinfo=SH)
        self.s.analysis=dict(action='买入',family='panic_rebound',regime='test',allocation=.1,reasons=[])
        self.s.quotes={'159915':dict(price=3,timestamp=t.isoformat()),'399006':dict(price=3000)}
        self.s.record(t,'test','test',True)
        self.s.bars={'159915':[dict(date='2026-09-04',close=3.1),dict(date='2026-09-07',close=3.3)]}
        self.s.settle(datetime(2026,9,4,16,tzinfo=SH));self.assertEqual(len(self.s.rows('SELECT * FROM outcomes')),0)
        self.s.settle(datetime(2026,9,7,14,tzinfo=SH));self.assertEqual(len(self.s.rows('SELECT * FROM outcomes')),0)
        self.s.settle(datetime(2026,9,7,16,tzinfo=SH),final=True);r=self.s.rows('SELECT * FROM outcomes')[0];self.assertAlmostEqual(r['net'],.0988)
        self.s.bars['159915'][-1]['close']=2;self.s.settle(datetime(2026,9,7,16,tzinfo=SH),final=True);self.assertEqual(r,self.s.rows('SELECT * FROM outcomes')[0])
    def test_empty_stats(self):self.assertIsNone(wilson(0,0));self.assertTrue(0<=wilson(1,2)[0]<=wilson(1,2)[1]<=100)
    def test_no_learning_without_samples(self):
        self.s.learn('2026-09-04');self.assertEqual(self.s.get('shadow_families'),[])

if __name__=='__main__':unittest.main()
