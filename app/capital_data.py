"""Public ETF data adapters. Shares are units, NAV is CNY/unit, never market size.

Exchange JSON metadata is part of the data contract. Reject unknown units and
incomplete pagination rather than silently treating a sample as the whole series.
"""
import hashlib
import html
import json
import math
import re
import urllib.parse
import urllib.request
from datetime import date, timedelta
from app.calendar_cn import trading_day

FUNDS = {
    '159915': ('创业板ETF易方达', '创业板'),
    '159952': ('创业板ETF广发', '创业板'),
    '159958': ('创业板ETF工银', '创业板'),
    '159919': ('沪深300ETF嘉实', '沪深300'),
    '510300': ('沪深300ETF华泰柏瑞', '沪深300'),
    '510310': ('沪深300ETF易方达', '沪深300'),
    '510330': ('沪深300ETF华夏', '沪深300'),
    '510050': ('上证50ETF华夏', '上证50'),
    '510500': ('中证500ETF南方', '中证500'),
}
CORE = tuple(k for k, v in FUNDS.items() if v[1] == '创业板')
SZ_URL = 'https://www.szse.cn/api/report/ShowReport/data'
SSE_URL = 'https://query.sse.com.cn/commonQuery.do'
NAV_URL = 'https://api.fund.eastmoney.com/f10/lsjz'
HUIJIN = 'https://www.huijin-inv.cn/huijin-inv/Corporate_History/index.shtml'


def fetch(url, params=None, referer=None):
    if params:
        url += '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0', 'Referer': referer or url})
    with urllib.request.urlopen(req, timeout=12) as r:
        return r.read(8_000_000).decode('utf-8-sig')


def number(value):
    n = float(str(value).replace(',', ''))
    if not math.isfinite(n) or n <= 0:
        raise ValueError('份额/净值必须为有限正数')
    return n


def previous_session(day):
    d = date.fromisoformat(day) if isinstance(day, str) else day
    for _ in range(20):
        d -= timedelta(days=1)
        if trading_day(d):
            return d.isoformat()
    return None


def parse_sz_page(payload, code):
    block = next(x for x in payload if x['metadata']['tabkey'] == 'tab1')
    if block.get('error'):
        raise ValueError('深交所返回错误')
    meta = block['metadata']
    unit = re.sub('<[^>]*>', '', meta['cols']['current_size'])
    multiplier = 10000 if '万份' in unit else 1 if '(份)' in unit or '（份）' in unit else None
    if multiplier is None:
        raise ValueError('深交所份额单位变更，暂停使用')
    rows = []
    for r in block['data']:
        if r['fund_code'] != code:
            raise ValueError('深交所代码筛选失效')
        day = date.fromisoformat(r['size_date']).isoformat()
        rows.append(dict(day=day, shares=number(r['current_size']) * multiplier))
    return rows, int(meta['pagecount']), int(meta['recordcount'])


def sz_shares(code, start, end):
    args = dict(SHOWTYPE='JSON', CATALOGID='scsj_fund_jjgm', TABKEY='tab1',
                txtStart=start, txtEnd=end, jjlb='ETF', txtDm=code)
    rows = []
    pages = 1
    for page in range(1, 12):
        args['PAGENO'] = page
        data, pages, count = parse_sz_page(json.loads(fetch(SZ_URL, args)), code)
        rows.extend(data)
        if page >= pages:
            break
    if len(rows) != count or len({x['day'] for x in rows}) != len(rows):
        raise ValueError('深交所分页缺失或重复')
    if not rows:
        raise ValueError('深交所份额为空')
    return rows


def sse_shares(day):
    args = {'isPagination': 'true', 'pageHelp.pageSize': 10000,
            'pageHelp.pageNo': 1, 'sqlId': 'COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L',
            'STAT_DATE': day}
    data = json.loads(fetch(SSE_URL, args, 'https://www.sse.com.cn/'))
    result = {}
    for r in data['result']:
        code = r['SEC_CODE']
        if code not in FUNDS:
            continue
        if r['STAT_DATE'][:10] != day or code in result:
            raise ValueError('上交所日期不符或代码重复')
        result[code] = dict(day=day, shares=number(r['TOT_VOL']) * 10000)
    if len(result) != len([c for c in FUNDS if c.startswith('5')]):
        raise ValueError('上交所观察池份额缺失')
    return result


def nav_history(code, start, end):
    rows = []
    for page in range(1, 12):
        p = json.loads(fetch(NAV_URL, dict(fundCode=code, pageIndex=page,
                       pageSize=20, startDate=start, endDate=end), 'https://fundf10.eastmoney.com/'))
        if p.get('ErrCode') != 0:
            raise ValueError('净值来源返回错误')
        batch = p['Data']['LSJZList']
        for r in batch:
            rows.append(dict(day=date.fromisoformat(r['FSRQ']).isoformat(), nav=number(r['DWJZ']),
                             corporate_action=bool(r.get('FHFCZ') or r.get('FHSP'))))
        if len(rows) >= int(p['TotalCount']):
            break
        if not batch:
            raise ValueError('净值分页缺失')
    if not rows or len(rows) != int(p['TotalCount']) or len({r['day'] for r in rows}) != len(rows):
        raise ValueError('净值数据为空、分页缺失或重复')
    return rows


def join_series(shares, navs, end):
    n = {r['day']: r for r in navs}
    result = []
    for r in sorted(shares, key=lambda x: x['day']):
        if r['day'] > end or not trading_day(r['day']):
            raise ValueError('份额出现未来日期或非交易日')
        # A missing matching NAV is missing data, never a zero flow.
        result.append(dict(r, nav=n.get(r['day'], {}).get('nav'),
                           corporate_action=n.get(r['day'], {}).get('corporate_action', False)))
    return result


def plain(text):
    return html.unescape(re.sub('<[^>]+>', ' ', text)).strip()


def parse_announcements(text, base, today):
    if 'infor-list-con' not in text:
        raise ValueError('汇金页面结构变化')
    items = []
    for block in re.split(r'<div class="infor-list-item clearfix">', text)[1:]:
        day = re.search(r'class="day">(\d+)', block)
        month = re.search(r'class="year">(\d{4})\.(\d{2})', block)
        title = re.search(r'<h1>(.*?)</h1>', block, re.S)
        content = re.search(r'<p>(.*?)</p>', block, re.S)
        link = re.search(r'<a href="([^"]+)"[^>]*>查看更多', block)
        if not all([day, month, title, content, link]):
            raise ValueError('汇金公告缺少日期或来源')
        published = date(int(month[1]), int(month[2]), int(day[1])).isoformat()
        if published > today:
            raise ValueError('汇金公告日期在未来')
        excerpt = plain(content[1])
        if not re.search(r'ETF|交易型开放式|交易型开放式指数', excerpt + plain(title[1]), re.I):
            continue
        url = urllib.parse.urljoin(base, link[1])
        if urllib.parse.urlparse(url).hostname != 'www.huijin-inv.cn':
            continue
        # Preserve announcement text for review; keyword matching is not a trade classifier.
        items.append(dict(id=hashlib.sha256(url.encode()).hexdigest()[:24],
                          published=published, title=plain(title[1]), excerpt=excerpt,
                          url=url, subject='中央汇金', evidence='官方资讯摘要·交易细节待核验'))
    return items


def announcements(today):
    text = fetch(HUIJIN)
    redirect = re.search(r'window.location.href\s*=\s*"([^"]+)"', text)
    base = urllib.parse.urljoin(HUIJIN, redirect[1]) if redirect else HUIJIN
    if urllib.parse.urlparse(base).hostname != 'www.huijin-inv.cn':
        raise ValueError('汇金入口跳转异常')
    if redirect:
        text = fetch(base)
    items = parse_announcements(text, base, today)
    # Previous-year archive is historical context, never a new real-time buy signal.
    year = int(today[:4]) - 1
    match = re.search(r'<a href="([^"]+)"[^>]*title=[\'"]' + str(year) + r'[\'"]', text)
    if match:
        url = urllib.parse.urljoin(base, match[1])
        if urllib.parse.urlparse(url).hostname == 'www.huijin-inv.cn':
            items += parse_announcements(fetch(url), url, today)
    return items, base
