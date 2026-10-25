import json,tempfile,unittest
from datetime import datetime
from unittest.mock import patch
from app.service import Service
from app.calendar_cn import SH

class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.s=Service(self.tmp.name)
        self.t=datetime(2026,10,24,10,tzinfo=SH)
        self.final={s:[dict(date=d,close=p,open=p,high=p,low=p,volume=1) for d,p in [('2026-10-20',3),('2026-10-21',3.1),('2026-10-22',3.2),('2026-10-23',3.3)]] for s in ['399006','159915']}
        for day in ['2026-10-21','2026-10-22','2026-10-23']:
            self.s.db.execute('INSERT INTO monitor_days(day) VALUES(?)',(day,))
        self.s.db.commit()
    def tearDown(self):self.s.db.close();self.tmp.cleanup()
    def test_weekend_catchup_is_idempotent_and_no_orders(self):
        before=self.s.rows('SELECT * FROM settings');self.s.bars={'original':[]}
        with patch('app.market.history',side_effect=lambda s:self.final[s]),patch.object(self.s,'learn') as learn:
            self.s.recover_reports(self.t,force=True);self.s.recover_reports(self.t,force=True)
        self.assertEqual([r['day'] for r in self.s.rows('SELECT day FROM reports ORDER BY day')],['2026-10-21','2026-10-22','2026-10-23'])
        self.assertEqual(len(self.s.rows('SELECT * FROM equity')),3)
        self.assertEqual(self.s.rows('SELECT * FROM fills'),[]);self.assertEqual(self.s.rows('SELECT * FROM decisions'),[])
        self.assertEqual(self.s.rows('SELECT * FROM settings'),before);learn.assert_not_called()
        self.assertEqual(self.s.bars,{'original':[]})
        self.assertTrue(all(r['status']=='仅本机' for r in self.s.rows("SELECT * FROM events WHERE kind='close'")))
    def test_missing_bar_records_symbol_and_date(self):
        self.final['159915']=self.final['159915'][:1]
        with patch('app.market.history',side_effect=lambda s:self.final[s]):self.s.recover_reports(self.t,force=True)
        self.assertFalse(self.s.rows('SELECT * FROM reports'))
        self.assertIn('159915',self.s.rows('SELECT * FROM close_status')[0]['detail'])
        self.assertIn('2026-10-20',self.s.rows('SELECT * FROM close_status')[0]['detail'])
    def test_timeout_retains_cause(self):
        with patch('app.market.history',side_effect=TimeoutError('source timed out')):self.s.recover_reports(self.t,force=True)
        self.assertIn('TimeoutError: source timed out',self.s.rows('SELECT * FROM close_status')[0]['detail'])
    def test_backfill_reverses_later_fills_and_fees(self):
        self.s.put('shares',900);self.s.put('cash',10294)
        self.s.db.execute('INSERT INTO fills(day,side,qty,price,fee) VALUES(?,?,?,?,?)',('2026-10-22','买入',100,3,2))
        self.s.db.execute('INSERT INTO fills(day,side,qty,price,fee) VALUES(?,?,?,?,?)',('2026-10-23','卖出',200,3,4));self.s.db.commit()
        self.assertEqual(self.s.portfolio_at_close('2026-10-21'),(1000,10000))
        self.assertEqual(self.s.portfolio_at_close('2026-10-22'),(1100,9698))
    def test_current_intraday_is_never_backfilled(self):
        with patch('app.market.history',side_effect=lambda s:self.final[s]):self.s.recover_reports(datetime(2026,10,21,10,tzinfo=SH),force=True)
        self.assertEqual(self.s.rows('SELECT * FROM reports'),[])
    def test_success_does_not_rewrite_previous_report(self):
        self.s.db.execute('INSERT INTO reports VALUES(?,?)',('2026-10-21','original'));self.s.db.commit()
        with patch('app.market.history',side_effect=lambda s:self.final[s]):self.s.recover_reports(self.t,force=True)
        self.assertEqual(self.s.rows('SELECT body FROM reports WHERE day=?',('2026-10-21',))[0]['body'],'original')

if __name__=='__main__':unittest.main()
