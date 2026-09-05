"""Exchange sessions, Asia/Shanghai; unknown calendar years fail closed."""
from datetime import datetime, date, timedelta, timezone

SH = timezone(timedelta(hours=8))
HOLIDAYS = [('2026-01-01','2026-01-03'),('2026-02-15','2026-02-23'),
            ('2026-04-04','2026-04-06'),('2026-05-01','2026-05-05'),
            ('2026-06-19','2026-06-21'),('2026-09-25','2026-09-27'),
            ('2026-10-01','2026-10-07')]

def now():
    return datetime.now(SH)

def trading_day(d):
    if isinstance(d, str): d = date.fromisoformat(d)
    return d.year == 2026 and d.weekday() < 5 and not any(a <= d.isoformat() <= b for a,b in HOLIDAYS)

def phase(t):
    if t.year != 2026: return '日历待更新'
    if not trading_day(t.date()): return '休市'
    m=t.hour*60+t.minute
    if m < 570: return '开盘前'
    if m < 690: return '上午交易'
    if m < 780: return '午间休市'
    if m < 900: return '下午交易'
    return '已收盘'

def sessions_after(start, count):
    d=date.fromisoformat(start)
    for _ in range(370):
        d+=timedelta(days=1)
        if d.year != 2026: return None
        if trading_day(d):
            count-=1
            if not count: return d.isoformat()
    return None
