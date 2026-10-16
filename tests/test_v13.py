import unittest,tempfile,json
from datetime import datetime,timedelta
from app.service import Service
from app.calendar_cn import SH
from app import opportunities,repair

class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.s=Service(self.temp.name)
        self.t=datetime(2026,10,8,10,tzinfo=SH)
        self.a=dict(action='买入',family='support_contraction',regime='test',allocation=.1,reasons=[])
        self.s.analysis=self.a
        self.s.bars={'399006':[dict(date=(datetime(2026,1,1)+timedelta(days=i)).date().isoformat(),close=2000+i*10,high=2010+i*10,low=1990+i*10) for i in range(100)]}
        self.s.quotes={'399006':dict(price=3300,date='2026-10-08',timestamp=self.t.isoformat()),'159915':dict(price=3,date='2026-10-08',timestamp=self.t.isoformat())}
    def tearDown(self):self.s.db.close();self.temp.cleanup()
    def test_duplicate_across_restart(self):
        self.s.record(self.t,'intra-1000','test',True)
        self.s.db.close();self.s=Service(self.temp.name);self.s.analysis=self.a
        self.s.quotes={'399006':dict(price=3300),'159915':dict(price=3,timestamp=self.t.isoformat())}
        self.s.record(self.t+timedelta(days=1),'intra-1000','test',True)
        self.assertEqual(len(self.s.rows('SELECT * FROM decisions')),1)
    def test_daily_open_is_observation_not_repeat_buy(self):
        self.s.record(self.t,'intra-1000','test',True)
        self.s.record(self.t+timedelta(days=1),'open','test',True)
        row=self.s.rows("SELECT * FROM decisions WHERE slot='open'")[0]
        self.assertEqual(row['action'],'观望');self.assertEqual(json.loads(row['payload'])['qty'],0)
    def test_invalidity_requires_continuous_data(self):
        self.s.record(self.t,'first','test',True)
        no=dict(action='观望',family='hold')
        opportunities.observe(self.s,self.t+timedelta(hours=2),no)
        opportunities.observe(self.s,self.t+timedelta(hours=3),no)
        self.assertEqual(self.s.rows('SELECT active FROM opportunities')[0]['active'],1)
        for i in range(1,32):opportunities.observe(self.s,self.t+timedelta(hours=3,minutes=i),no)
        self.assertEqual(self.s.rows('SELECT active FROM opportunities')[0]['active'],0)
        self.s.record(self.t+timedelta(hours=4),'second','test',True)
        self.assertEqual(len(self.s.rows('SELECT * FROM decisions')),1)
        self.s.record(self.t+timedelta(days=1),'third','test',True)
        self.assertEqual(len(self.s.rows('SELECT * FROM decisions')),2)
    def test_cross_family_daily_cap(self):
        self.s.record(self.t,'first','test',True)
        self.s.analysis=dict(self.a,family='panic_rebound')
        self.s.record(self.t+timedelta(hours=1),'second','test',True)
        self.assertEqual(len(self.s.rows('SELECT * FROM decisions')),1)
    def test_opposite_risk_direction_not_blocked(self):
        self.s.record(self.t,'first','test',True)
        self.s.analysis=dict(self.a,action='卖出',family='top_divergence')
        self.s.record(self.t+timedelta(hours=1),'second','test',True)
        self.assertEqual(len(self.s.rows('SELECT * FROM decisions')),2)
    def test_zero_qty_does_not_consume_opportunity(self):
        self.s.put('cash',0);self.s.db.commit()
        self.s.record(self.t,'first','test',True)
        self.assertEqual(self.s.rows('SELECT * FROM opportunities'),[])
    def bars(self):
        start=datetime(2025,1,1)
        return [dict(date=(start+timedelta(days=i)).date().isoformat(),close=3600 if i<240 else 3300,open=3300,low=3290,high=3320,volume=50 if i>=255 else 100) for i in range(260)]
    def test_no_future_bars_and_no_prepublication(self):
        rows=self.bars();q=dict(date='2026-10-08',price=3300)
        result=repair.assess(rows,q)
        self.assertEqual(result,repair.assess(rows+[dict(date='2026-10-08',close=999999)],q))
        self.assertFalse(repair.assess(rows,dict(q,date='2026-09-24'))['eligible'])
    def test_structural_failure_not_buy(self):
        r=repair.assess(self.bars(),dict(date='2026-10-08',price=3000))
        self.assertEqual(r['action'],'观望');self.assertIn('失效',r['state'])
        self.assertIn('未接入',r['minute120'])
    def test_positive_candidate_and_publication_time(self):
        rows=self.bars()
        for i in range(205,260):rows[i]['close']=3300
        q=dict(date='2026-10-08',price=3300)
        result=repair.assess(rows,q)
        self.assertEqual(result['action'],'买入');self.assertEqual(result['mode'],'仅影子研究')
        self.assertFalse(repair.assess(rows,dict(q,date='2026-09-28',timestamp='2026-09-28T09:00:00+08:00'))['eligible'])
    def test_shadow_pair_separate_frozen_and_once(self):
        self.s.bars={'399006':self.bars()}
        self.s.capture_repair(self.t);self.s.capture_repair(self.t)
        self.assertEqual(len(self.s.rows('SELECT * FROM repair_pairs')),1)
        self.assertEqual(self.s.rows('SELECT * FROM decisions'),[])
        self.assertEqual(self.s.rows('SELECT * FROM events'),[])
        self.s.bars['159915']=[dict(date='2026-10-09',close=3.3)]
        self.s.settle(datetime(2026,10,10,10,tzinfo=SH))
        before=self.s.rows('SELECT * FROM repair_results')
        self.assertEqual(len(before),1);self.assertEqual(self.s.rows('SELECT * FROM outcomes'),[])
        self.s.bars['159915'][0]['close']=9
        self.s.settle(datetime(2026,10,10,10,tzinfo=SH))
        self.assertEqual(before,self.s.rows('SELECT * FROM repair_results'))
    def test_stale_shadow_snapshot_not_recorded(self):
        self.s.bars={'399006':self.bars()}
        self.s.capture_repair(self.t+timedelta(hours=1))
        self.assertEqual(self.s.rows('SELECT * FROM repair_pairs'),[])
    def test_account_untouched_by_new_tables(self):
        self.s.put('shares',1234);self.s.put('cash',5678);self.s.db.commit()
        self.s.db.close();self.s=Service(self.temp.name)
        self.assertEqual(self.s.get('shares'),1234);self.assertEqual(self.s.get('cash'),5678)

if __name__=='__main__':unittest.main()
