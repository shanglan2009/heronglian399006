"""Independent downside guardrails. Versioned engineering defaults, not fitted returns."""
from math import ceil
from statistics import mean

VERSION='HRL-risk-1'
NAMES={0:'未触发下跌风控',1:'趋势转弱',2:'破位防守',3:'严重回撤',4:'账户回撤超限'}
TARGETS={0:None,1:.60,2:.35,3:.15,4:0.0}

def assess(rows,quote,etf,shares,cash,sellable,peak=0):
    result=dict(version=VERSION,ready=False,level=None,label='数据不足，无法判断持仓风险',
        target=None,exposure=None,quantity=0,reasons=[],account_drawdown=None,
        market_drawdown=None,action='暂停判断')
    if not quote or not quote.get('date') or not etf:return result
    past=[r for r in rows if r['date']<quote['date']]
    if len(past)<65 or etf['price']<=0:return result
    c=[r['close'] for r in past];p=quote['price'];price=etf['price']
    nav=shares*price+cash
    if nav<=0:return result
    ma20=mean(c[-20:]);ma60=mean(c[-60:])
    falling20=ma20<mean(c[-25:-5])
    weak20=p<ma20 and c[-1]<mean(c[-21:-1]) and falling20
    weak60=p<ma60 and all(c[-i]<mean(c[-i-60:-i]) for i in (1,2))
    atr=mean(max(r['high']-r['low'],abs(r['high']-past[i-1]['close']),abs(r['low']-past[i-1]['close'])) for i,r in enumerate(past) if i>=len(past)-20)
    support=min(r['low'] for r in past[-20:]);broken=p<support-.5*atr
    market_dd=max(0,1-p/max(c[-60:]+[p]));account_dd=max(0,1-nav/max(peak,nav))
    level=0;reasons=[]
    if weak20:level=1;reasons.append('价格低于20日均线，前一完整日也偏弱，20日均线向下')
    if weak60:level=max(level,2);reasons.append('前两个完整交易日分别低于此前60日均线，当前仍未收复')
    if broken:level=max(level,2);reasons.append('跌破此前20日最低价，幅度超过0.5倍ATR')
    if market_dd>=.12 and p<ma60:level=max(level,3);reasons.append('相对此前60日最高收盘回撤达到12%，且价格低于60日均线')
    if account_dd>=.12:level=max(level,3);reasons.append('登记账户相对已观测资产峰值回撤达到12%')
    if account_dd>=.20:level=4;reasons.append('登记账户相对已观测资产峰值回撤达到20%')
    target=TARGETS[level];qty=0
    if target is not None:
        excess=max(0,shares-target*nav/price)
        qty=min(sellable,shares,ceil(max(0,excess-1e-8)/100)*100)
        qty=qty//100*100
    action='持仓观察' if level==0 else ('减仓至目标上限' if qty else '暂停加仓；核对持仓与可卖份额')
    result.update(ready=True,level=level,label=NAMES[level],target=target,exposure=shares*price/nav,
        quantity=qty,reasons=reasons or ['当前未触发本版本下跌风控条件，不代表没有风险'],
        account_drawdown=account_dd,market_drawdown=market_dd,action=action,nav=nav,
        support=support,ma20=ma20,ma60=ma60,atr=atr,
        peak_note='登记账户按已记录收盘/实时估值采样的峰值；不是持仓成本，也不是历史真实最大资产')
    return result
