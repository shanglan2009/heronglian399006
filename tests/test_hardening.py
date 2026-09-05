import unittest,json,tempfile
from datetime import datetime
from unittest.mock import patch
from app.service import Service
from app.api import API
from app.calendar_cn import SH
from app.review import diagnose
from app.market import parse_quotes

class HardeningTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.s=Service(self.temp.name)
    def tearDown(self):self.s.db.close();self.temp.cleanup()
    def test_private_bridge(self):
        public=[k for k in vars(API(self.s)) if not k.startswith('_')]
        self.assertEqual(public,[])
    def test_diagnostic_categories(self):
        self.assertEqual(diagnose('买入',3,3.0001,3000,3001,12)['category'],'涨跌幅不足以覆盖成本')
        self.assertEqual(diagnose('买入',3,2.9,3000,3100,12)['category'],'指数与ETF方向不一致')
        self.assertEqual(diagnose('卖出',3,3.1,3000,3100,12)['category'],'市场方向未兑现')
        self.assertEqual(diagnose('卖出',3,2.9,3000,2900,12)['category'],'方向兑现')
    def test_no_partial_bar_at_close(self):
        t=datetime(2026,9,4,10,tzinfo=SH);self.s.analysis=dict(action='买入',family='panic_rebound',regime='test',allocation=.1,reasons=[])
        self.s.quotes={'159915':dict(price=3,timestamp=t.isoformat()),'399006':dict(price=3000)}
        self.s.record(t,'test','test',True);self.s.bars={'159915':[dict(date='2026-09-07',close=3.3)]}
        self.s.settle(datetime(2026,9,7,16,tzinfo=SH))
        self.assertEqual(self.s.rows('SELECT * FROM outcomes'),[])
        self.s.settle(datetime(2026,9,8,9,tzinfo=SH))
        self.assertEqual(len(self.s.rows('SELECT * FROM outcomes')),1)
    def test_no_token_export_in_state(self):
        with self.s.lock:self.s.put('token','someEncryptedSecret');self.s.db.commit()
        self.assertNotIn('someEncryptedSecret',json.dumps(self.s.state()))
    def test_quote_reject_nan(self):
        fields=['0']*40
        for i,v in {1:'name',3:'nan',4:'3',5:'3',6:'1000',30:'20260904150000',33:'3.1',34:'2.9',38:'1'}.items():fields[i]=v
        with self.assertRaises(ValueError):parse_quotes('v_sz399006="'+'~'.join(fields)+'";')
    def test_backup(self):
        result=API(self.s).export_records();self.assertTrue(result['ok']);self.assertEqual(len(list((self.s.root/'exports').glob('*.sqlite3'))),1)
    def test_rules_are_chronological_source(self):
        r=API(self.s).get_rules()['articles'];self.assertEqual(len(r),45);self.assertEqual(r[-1]['date'],'2026-08-28');self.assertEqual(sorted(a['date'] for a in r),[a['date'] for a in r])
    def test_shadow_separate_from_real_signals(self):
        t=datetime(2026,9,4,10,tzinfo=SH)
        with self.s.lock:self.s.put('shadow_families',['panic_rebound']);self.s.db.commit()
        self.s.analysis=dict(action='买入',family='panic_rebound',regime='test',allocation=.1,reasons=[])
        self.s.quotes={'159915':dict(price=3,timestamp=t.isoformat()),'399006':dict(price=3000)}
        self.s.record(t,'intra-1000','test',True)
        self.assertEqual(self.s.rows('SELECT * FROM events'),[])
        self.s.bars={'159915':[dict(date='2026-09-07',close=3.3)]}
        self.s.settle(datetime(2026,9,8,9,tzinfo=SH))
        self.assertEqual(len(self.s.rows('SELECT * FROM shadow_outcomes')),1)
        self.assertEqual(len(self.s.rows('SELECT * FROM outcomes')),0)
    def test_push_acceptance_not_delivery(self):
        with self.s.lock:
            self.s.put('push_enabled',True);self.s.put('token','encrypted-placeholder');self.s.db.commit()
        self.s.event('test','test','test')
        with patch('app.service.secret_codec',return_value='fake-test-token'),patch('app.market.request',return_value=b'{"code":200,"data":"receipt"}') as request:
            self.s.push_pending()
            self.assertEqual(request.call_count,1)
        self.assertEqual(self.s.rows('SELECT status FROM events')[0]['status'],'服务已受理·送达待确认')
    def test_unknown_push_is_not_retried(self):
        with self.s.lock:self.s.put('push_enabled',True);self.s.put('token','placeholder');self.s.db.commit()
        self.s.event('test','test','test')
        with patch('app.service.secret_codec',return_value='fake'),patch('app.market.request',side_effect=TimeoutError) as request:
            self.s.push_pending();self.s.push_pending();self.assertEqual(request.call_count,1)
    def test_close_report_once_and_daily_equity(self):
        final={'399006':[dict(date='2026-09-03',close=3300),dict(date='2026-09-04',close=3286.55)],
               '159915':[dict(date='2026-09-03',close=3.33),dict(date='2026-09-04',close=3.305)]}
        t=datetime(2026,9,4,15,6,tzinfo=SH)
        with patch('app.market.history',side_effect=lambda symbol:final[symbol]):
            self.s.dispatch_due(t);self.s.dispatch_due(t)
        self.assertEqual(len(self.s.rows('SELECT * FROM reports')),1)
        self.assertEqual(len(self.s.rows("SELECT * FROM events WHERE kind='close'")),1)
        self.assertEqual(self.s.rows('SELECT total FROM equity')[0]['total'],13305)
    def test_windows_task_xml_encoding(self):
        from pathlib import Path
        from types import SimpleNamespace
        from app.startup import task_owned
        exe=Path('dist/和融联/和融联.exe').resolve()
        xml=('<?xml version="1.0" encoding="UTF-16"?><Task><Actions><Exec><Command>'+str(exe)+'</Command></Exec></Actions></Task>').encode('utf-8')
        with patch('app.startup.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout=xml)):
            self.assertTrue(task_owned(exe))

if __name__=='__main__':unittest.main()
