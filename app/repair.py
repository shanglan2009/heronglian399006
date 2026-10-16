"""September article hypothesis: research-only, never an execution signal.

All numerical thresholds below are engineering hypotheses, not quoted rules.
Only completed daily bars enter compression/volume/support measurements.
"""
from statistics import mean

VERSION = 'HRL-repair-shadow-1'
AVAILABLE_FROM = '2026-09-28'

def assess(rows, quote):
    result = dict(version=VERSION, mode='仅影子研究', action='观望',
                  state='数据不足', eligible=False, rule_ids=['A46','A47'],
                  minute120='未接入，底背离未确认', thresholds='工程假设，未验证',
                  reference_levels={'support':3300,'resistance':3740,'asof':'2026-09-24',
                                    'usage':'文章情境参考，不参与自动交易'}, features={})
    day=quote.get('date','')
    if day < AVAILABLE_FROM:
        result['state']='尚未发表，不可用于此时点';return result
    if day==AVAILABLE_FROM and quote.get('timestamp','')[11:16]<'09:19':
        result['state']='发表时间尚未到达或未知';return result
    past=[r for r in rows if r['date']<day]
    if len(past)<260:return result
    closes=[r['close'] for r in past];volumes=[r['volume'] for r in past]
    if min(closes[-60:])<=0 or mean(volumes[-25:-5])<=0:return result
    def spread(end):
        vals=[mean(closes[end-n:end]) for n in (5,10,20,40)]
        return (max(vals)-min(vals))/mean(vals)
    n=len(closes);days=0
    for end in range(n,max(40,n-20),-1):
        if spread(end)>.025:break
        days+=1
    quiet=mean(volumes[-5:])/mean(volumes[-25:-5])
    lower=min(r['low'] for r in past[-20:]);upper=max(r['high'] for r in past[-20:])
    p=quote['price'];position=(p-lower)/max(upper-lower,.001)
    support_intact=p>=lower*.985
    low_zone=p<=max(closes[-60:])*.92
    cluster=days>=3
    base=cluster and quiet<=.8 and low_zone
    # Completed close confirmation, not an intraday poke above resistance.
    prior_upper=max(r['high'] for r in past[-21:-1])
    breakout=closes[-1]>prior_upper and p>=prior_upper and volumes[-1]>=mean(volumes[-21:-1])*1.2
    result['features']=dict(compression=spread(n),compression_days=days,volume_ratio5=quiet,
        support=lower,range_high=upper,range_position=position,support_intact=support_intact,
        low_zone=low_zone,breakout_confirmed=breakout)
    result['eligible']=True
    if not support_intact:result['state']='结构失效：跌破滚动支撑'
    elif base and -.1<=position<=.45:
        result.update(state='低位整理：修复候选（120分钟待确认）',action='买入')
    elif breakout:result['state']='突破观察：不能仅因放量判定派发'
    elif base:result['state']='整理等待：不追涨'
    else:result['state']='未满足低位整理组合条件'
    return result
