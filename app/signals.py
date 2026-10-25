"""Conservative, versioned operational translation of the article rules.

This module deliberately has no daily full-volume comparison with partial-day
volume and no inference of unobserved weekly/monthly bars.
"""
from statistics import mean
from datetime import date
import math

VERSION='HRL-1.5-etf-capital-watch'

def ema(values,n):
    out=[]; v=values[0]; a=2/(n+1)
    for x in values:
        v=a*x+(1-a)*v; out.append(v)
    return out

def macd(values):
    fast=ema(values,12); slow=ema(values,26)
    dif=[a-b for a,b in zip(fast,slow)]; dea=ema(dif,9)
    return dif,[2*(a-b) for a,b in zip(dif,dea)]

def grouped(rows,asof,period):
    # Exclude current calendar week/month even during the close report.
    d=date.fromisoformat(asof)
    current=(d.isocalendar().year,d.isocalendar().week) if period=='week' else (d.year,d.month)
    groups={}
    for r in rows:
        x=date.fromisoformat(r['date'])
        key=(x.isocalendar().year,x.isocalendar().week) if period=='week' else (x.year,x.month)
        if key<current: groups[key]=r['close']
    return list(groups.values())

def analyse(rows,q,etf):
    past=[r for r in rows if r['date']<q['date']]
    if len(past)<260: return {'action':'观望','regime':'数据不足','family':'data','reasons':['需要至少260根完整日K线'],'version':VERSION}
    c=[r['close'] for r in past]; p=q['price']
    ma={n:mean(c[-n:]) for n in (5,10,20,30,40,60,90,120,160,250)}
    slope250=ma[250]>mean(c[-255:-5])
    weeks=grouped(past,q['date'],'week'); months=grouped(past,q['date'],'month')
    week_up=len(weeks)>=26 and weeks[-1]>mean(weeks[-13:]) and mean(weeks[-13:])>mean(weeks[-14:-1])
    month_up=len(months)>=6 and months[-1]>mean(months[-6:]) and months[-1]>months[-2]
    month_ready=len(months)>=35
    month_positive=month_ready and macd(months)[0][-1]>0
    long=sum([slope250,week_up,month_up,month_positive])
    dif,hist=macd(c+[p]); wdiv=False
    if len(weeks)>=35:
        wd,wh=macd(weeks)
        wdiv=weeks[-1]>=max(weeks[-9:-1]) and wd[-1]<max(wd[-9:-1])
    high=p>=max(c[-60:]); divergence=high and dif[-1]<max(dif[-21:-1])
    # A distinct day closing above its prior 60 closes counts once; intraday ticks do not accumulate highs.
    high_count=sum(c[i]>max(c[i-60:i]) for i in range(len(c)-19,len(c)))+int(high)
    atr=mean([max(r['high']-r['low'],abs(r['high']-past[i-1]['close']),abs(r['low']-past[i-1]['close'])) for i,r in enumerate(past) if i>=len(past)-20])
    prev_month=[r for r in past if r['date'][:7]<q['date'][:7]]
    mret=p/prev_month[-1]['close']-1 if prev_month else 0
    support=max([v for n,v in ma.items() if n>=60 and v<=p*1.015]+[min(c[-20:])])
    resistance=min([v for n,v in ma.items() if n>=20 and v>=p*0.99]+[max(c[-60:])])
    near_support=abs(p-support)<=atr*0.6
    near_resistance=abs(p-resistance)<=atr*0.6
    bias=p/mean(weeks[-6:])-1 if len(weeks)>=6 else 0
    risk=sum([divergence,wdiv,bias>=0.15,q['turnover']>=0.07])
    # Confirmation from the prior completed day: high-volume drop then contraction.
    capitulation=any(r['close']<r['open'] and r['volume']>mean([x['volume'] for x in past[-22:-2]])*1.3 for r in past[-6:-1])
    contraction=past[-1]['volume']<mean([x['volume'] for x in past[-21:-1]])*0.75
    # A one-sided lower bound: already-exceeded full-day volume confirms a burst.
    # Never call a partial trading day's volume 'contracted' by comparison with a full day.
    volume_burst=q.get('volume',0)>=mean([r['volume'] for r in past[-20:]])*1.2
    sharp_episodes=0;in_episode=False
    for i in range(len(c)-20,len(c)):
        if c[i]-c[i-1]<=-2*atr and not in_episode:sharp_episodes+=1;in_episode=True
        elif c[i]>c[i-1]:in_episode=False
    reasons=[f'长期条件满足 {long}/'+('4' if month_ready else '3')+' 项',f'参考支撑 {support:.0f} 点，压力 {resistance:.0f} 点']
    gaps=['20日累计换手率未接入','60/120分钟背离尚未实现','时间周期锚点需审定']
    if not month_ready: gaps.append('月线MACD预热不足，未参与计分')
    action='观望'; family='hold'; regime='等待位置'; allocation=0
    if high and (divergence or wdiv) and (risk>=2 or high_count>=3):
        action='卖出'; family='top_divergence'; regime='冲高背离'; allocation=0.15
        reasons.append('价格接近阶段新高，动能未同步；优先锁定部分收益')
    elif long>=3 and mret<=-0.20 and near_support:
        action='买入'; family='panic_rebound'; regime='长期有撑的极端回撤'; allocation=0.15
        reasons.append('月内跌幅超过20%，长期支撑仍在，出现小批低接观察点')
    elif long>=2 and near_resistance and p<ma[60] and q['change']>0.8 and volume_burst:
        action='卖出'; family='rebound_resistance'; regime='反弹遇阻'; allocation=0.10
        reasons.append('短线反弹接近均线压力，累计量已超近20个完整交易日均量1.2倍；先减一小批')
    elif sharp_episodes>=2 and risk>=2:
        regime='二次急杀风险';family='second_selloff'
        reasons.append('近20日检测到多轮急跌且风险未消退；未达较新文章的极端低接条件，不再次抄底')
    elif long>=3 and near_support and q['change']<-1 and capitulation and contraction:
        action='买入'; family='support_contraction'; regime='缩量回测'; allocation=0.10
        reasons.append('前期放量下跌后完成缩量，当前重新接近长期支撑')
    elif long>=2 and p>ma[20]:
        regime='持仓观察'; reasons.append('趋势仍有支撑；现价没有同时满足低接条件，耐心持有')
    else: reasons.append('趋势与位置尚未共同确认，暂不增加操作')
    return dict(action=action,family=family,regime=regime,reasons=reasons,version=VERSION,
                support=round(support,2),resistance=round(resistance,2),long_score=long,risk=risk,
                features=dict(ma20=ma[20],ma60=ma[60],ma250=ma[250],weekly_bias=bias,monthly_return=mret,long_score=long,risk_score=risk,new_high_days=high_count,volume_burst=volume_burst,sharp_episodes=sharp_episodes),
                monthly_return=mret,ma=ma,atr=atr,allocation=allocation,gaps=gaps,
                etf_support=round(etf['price']*support/p,3),etf_resistance=round(etf['price']*resistance/p,3),
                level_note='ETF区间按当前指数/ETF价格比例估算，未含折溢价变化',rule_ids={'top_divergence':['A08','A37'], 'panic_rebound':['A40','A41'],'rebound_resistance':['A43','A45'],'support_contraction':['A16','A29']}.get(family,['A45']))

def size_order(action,price,shares,cash,sellable,allocation=0.10):
    if action=='观望': return 0
    total=shares*price+cash
    budget=total*allocation
    if action=='买入': return max(0,int(min(budget,cash/(1.001))*1/price/100)*100)
    return max(0,min(sellable,int(budget/price/100)*100))

def wilson(wins,n):
    if not n: return None
    z=1.96;p=wins/n; d=1+z*z/n
    mid=(p+z*z/(2*n))/d; half=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/d
    return [mid-half,mid+half]
