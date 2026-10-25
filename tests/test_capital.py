import json
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from app import capital, capital_data as data
from app.api import API
from app.calendar_cn import SH, trading_day, sessions_after
from app.service import Service


def fixture(t, sign=-1):
    day=t.date()-timedelta(days=80)
    dates=[]
    while day<t.date():
        if trading_day(day):dates.append(day.isoformat())
        day+=timedelta(days=1)
    dates=dates[-31:]
    series={}
    for code in data.CORE:
        shares=1e9
        rows=[]
        for i,d in enumerate(dates):
            if i>=len(dates)-3:shares*=1+sign*.02
            rows.append(dict(day=d,shares=shares,nav=3,corporate_action=False))
        series[code]=rows
    status={c:dict(checked=t.isoformat(),error='') for c in series}
    return series,status


class DataTests(unittest.TestCase):
    def setUp(self):self.t=datetime(2026,10,21,10,tzinfo=SH)
    def test_sz_unit_contract(self):
        def payload(unit):return [dict(metadata=dict(tabkey='tab1',cols={'current_size':unit},pagecount=1,recordcount=1),data=[dict(fund_code='159915',size_date='2026-10-20',current_size='2,000.5')])]
        self.assertEqual(data.parse_sz_page(payload('规模<br>(万份)'),'159915')[0][0]['shares'],20005000)
        self.assertEqual(data.parse_sz_page(payload('规模(份)'),'159915')[0][0]['shares'],2000.5)
        with self.assertRaises(ValueError):data.parse_sz_page(payload('规模(亿元)'),'159915')
    def test_nav_missing_is_not_zero(self):
        joined=data.join_series([dict(day='2026-10-19',shares=100),dict(day='2026-10-20',shares=101)],[dict(day='2026-10-19',nav=3)],'2026-10-20')
        flow=capital.flow_series(joined)[0]
        self.assertIsNone(flow['amount']);self.assertIn('净值',flow['issue'])
    def test_scale_drop_is_not_redemption(self):
        points=[dict(day='2026-10-19',shares=1000,nav=3),dict(day='2026-10-20',shares=1000,nav=2.9)]
        self.assertEqual(capital.flow_series(points)[0]['amount'],0)
    def test_gaps_and_corporate_actions_are_rejected(self):
        for before,action in [('2026-10-16',False),('2026-10-19',True)]:
            points=[dict(day=before,shares=1000,nav=3),dict(day='2026-10-20',shares=1000,nav=3,corporate_action=action)]
            self.assertIsNone(capital.flow_series(points)[0]['amount'])
    def test_future_rows_rejected(self):
        with self.assertRaises(ValueError):data.join_series([dict(day='2026-10-22',shares=10)],[],'2026-10-20')
    def test_non_finite_and_negative_values_rejected(self):
        for x in ('NaN','inf',0,-1):
            with self.assertRaises(ValueError):data.number(x)
    def test_pagination_duplicate_fails_closed(self):
        p=[dict(metadata=dict(tabkey='tab1',cols={'current_size':'(万份)'},pagecount=2,recordcount=2),data=[dict(fund_code='159915',size_date='2026-10-20',current_size='100')])]
        with patch.object(data,'fetch',return_value=json.dumps(p)):
            with self.assertRaises(ValueError):data.sz_shares('159915','2026-10-01','2026-10-20')
    def test_stale_missing_and_future_checks_block(self):
        series,status=fixture(self.t)
        for bad in ('stale','missing','future','error'):
            modified={c:dict(v) for c,v in status.items()}
            if bad=='missing':modified.pop('159915')
            elif bad=='stale':modified['159915']['checked']=(self.t-timedelta(hours=3)).isoformat()
            elif bad=='future':modified['159915']['checked']=(self.t+timedelta(minutes=1)).isoformat()
            else:modified['159915']['error']='offline'
            self.assertFalse(capital.evaluate(series,modified,self.t)['ready'],bad)
    def test_sustained_flow_and_cross_fund_confirmation(self):
        series,status=fixture(self.t)
        r=capital.evaluate(series,status,self.t)
        self.assertTrue(r['ready']);self.assertTrue(r['outflow']);self.assertFalse(r['inflow'])
        self.assertEqual(r['identity'],'交易者身份未确认')
        for c in data.CORE[1:]:
            for row in series[c]:row['shares']=1e9
        self.assertFalse(capital.evaluate(series,status,self.t)['outflow'])
    def test_partial_core_never_claims_full_basket(self):
        series,status=fixture(self.t);series.pop('159952')
        r=capital.evaluate(series,status,self.t)
        self.assertFalse(r['ready']);self.assertIsNone(r['amount'])
    def test_wrong_date_not_current(self):
        series,status=fixture(self.t)
        for c in series:series[c].pop()
        self.assertFalse(capital.evaluate(series,status,self.t)['ready'])
    def test_news_is_not_classified_as_confirmed_trade(self):
        text='<div class="infor-list-con"><div class="infor-list-item clearfix"><span class="day">20</span><span class="year">2026.10</span><h1>公告</h1><p>已增持ETF，金额未披露</p><a href="/notice.shtml">查看更多</a>'
        r=data.parse_announcements(text,data.HUIJIN,'2026-10-21')[0]
        self.assertNotIn('action',r);self.assertIn('待核验',r['evidence'])
        with self.assertRaises(ValueError):data.parse_announcements(text,data.HUIJIN,'2026-10-19')


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.s=Service(self.tmp.name);self.t=datetime(2026,10,21,10,tzinfo=SH)
        self.s.analysis=dict(action='买入',allocation=.1,family='support_contraction',reasons=[],regime='test')
        self.risk=dict(live=True,ready=True,level=0,confirmed=True,ma20=3000,label='未触发下跌风控')
        series,status=fixture(self.t)
        for c in series:self.s.db.execute('INSERT INTO capital_data VALUES(?,?,?,?)',(c,json.dumps(series[c]),status[c]['checked'],''))
        self.s.db.commit()
        self.s.bars={'399006':[dict(date=r['day'],close=3000+i*10) for i,r in enumerate(series['159915'])]}
        self.quote(self.t)
    def quote(self,t):
        self.s.quotes={c:dict(price=p,date=t.date().isoformat(),timestamp=t.isoformat()) for c,p in [('399006',3400),('159915',3)]}
    def tearDown(self):self.s.db.close();self.tmp.cleanup()
    def test_default_shadow_preserves_buy_and_filter_vetoes(self):
        with patch.object(self.s,'risk_state',return_value=self.risk):
            self.assertEqual(self.s.effective_analysis(self.t)['action'],'买入')
            API(self.s).set_capital_mode('filter')
            self.assertEqual(self.s.effective_analysis(self.t)['action'],'观望')
            self.s.db.execute("UPDATE capital_data SET error='offline'");self.s.db.commit()
            self.assertEqual(self.s.effective_analysis(self.t)['action'],'买入')
    def test_new_data_does_not_mutate_ledger(self):
        before={k:self.s.get(k) for k in ['cash','shares','cost','token']}
        self.s.capital.details(self.t)
        self.assertEqual(before,{k:self.s.get(k) for k in before})
        self.assertEqual(self.s.rows('SELECT * FROM fills'),[])
    def test_flow_in_cannot_buy_in_downtrend(self):
        series,status=fixture(self.t,1)
        self.s.db.execute('DELETE FROM capital_data')
        for c in series:self.s.db.execute('INSERT INTO capital_data VALUES(?,?,?,?)',(c,json.dumps(series[c]),status[c]['checked'],''))
        self.s.db.commit();risk=dict(self.risk,level=2)
        for secs in [0,60]:
            t=self.t+timedelta(seconds=secs);self.quote(t);self.s.capital.observe(t,risk)
        self.assertEqual(self.s.rows('SELECT * FROM capital_samples'),[])
    def test_candidates_confirm_dedup_and_stay_out_of_real_decisions(self):
        risk=dict(self.risk,level=2)
        for secs in [0,30,60,90]:
            t=self.t+timedelta(seconds=secs);self.quote(t);self.s.capital.observe(t,risk)
        self.assertEqual(len(self.s.rows('SELECT * FROM capital_samples')),1)
        self.assertEqual(len(self.s.rows("SELECT * FROM events WHERE kind='capital_signal'")),1)
        self.assertEqual(self.s.rows('SELECT * FROM decisions'),[])
        self.assertEqual(self.s.rows('SELECT * FROM fills'),[])
    def test_gap_resets_confirmation(self):
        risk=dict(self.risk,level=2)
        for secs in [0,300]:
            t=self.t+timedelta(seconds=secs);self.quote(t);self.s.capital.observe(t,risk)
        self.assertEqual(self.s.rows('SELECT * FROM capital_samples'),[])
    def test_outcomes_wait_for_complete_next_sessions_and_freeze(self):
        risk=dict(self.risk,level=2)
        for secs in [0,60]:
            t=self.t+timedelta(seconds=secs);self.quote(t);self.s.capital.observe(t,risk)
        due=sessions_after('2026-10-21',1)
        bar=dict(date=due,close=2.9,high=3.05,low=2.85)
        self.s.capital.settle(datetime(2026,10,22,10,tzinfo=SH),[bar])
        self.assertEqual(self.s.rows('SELECT * FROM capital_results'),[])
        self.s.capital.settle(datetime(2026,10,22,16,tzinfo=SH),[bar],True)
        first=self.s.rows('SELECT * FROM capital_results')[0]
        self.assertGreater(first['net'],0);self.assertLess(first['baseline_net'],0)
        bar['close']=4
        self.s.capital.settle(datetime(2026,10,22,16,tzinfo=SH),[bar],True)
        self.assertEqual(first,self.s.rows('SELECT * FROM capital_results')[0])
    def test_evidence_entry_rejects_future_date_and_duplicates(self):
        value=dict(subject='测试主体',code='159915',asof='2026-06-30',published='2026-08-31',shares=100,url='https://www.efunds.com.cn/report.pdf')
        api=API(self.s)
        self.assertTrue(api.add_capital_holding(value)['ok'])
        self.assertFalse(api.add_capital_holding(value)['ok'])
        value['published']='2099-01-01';self.assertFalse(api.add_capital_holding(value)['ok'])
        self.assertEqual(self.s.rows('SELECT * FROM fills'),[])


if __name__=='__main__':unittest.main()
