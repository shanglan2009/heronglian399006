"""Persistent opportunity lifecycle; no change to price-signal calculations."""
import json
from datetime import datetime

def observe(service,t,analysis):
    key=(analysis['family']+'|'+analysis['action']) if analysis and analysis.get('action') in ('买入','卖出') else None
    stamp=t.isoformat()
    with service.lock:
        for row in service.rows('SELECT * FROM opportunities WHERE active=1'):
            if row['key']==key:
                service.db.execute('UPDATE opportunities SET absent_since=NULL,last_seen=? WHERE key=?',(stamp,row['key']))
            else:
                gap=(t-datetime.fromisoformat(row['last_seen'])).total_seconds()
                since=row['absent_since'] if 0<=gap<=120 else None
                since=since or stamp
                closed=(t-datetime.fromisoformat(since)).total_seconds()>=1800
                service.db.execute('UPDATE opportunities SET active=?,absent_since=?,last_seen=? WHERE key=?',
                                   (int(not closed),since,stamp,row['key']))
                if closed:
                    service.db.execute('INSERT INTO opportunity_audit(ts,key,event) VALUES(?,?,?)',(stamp,row['key'],'条件连续30分钟不成立，机会失效'))
        service.db.commit()

def claim(service,t,analysis):
    """Call only inside the decision transaction, after quantity validation."""
    key=analysis['family']+'|'+analysis['action'];day=t.date().isoformat()
    row=service.rows('SELECT * FROM opportunities WHERE key=?',(key,))
    if row and row[0]['active']:return False
    # Limit each direction to one executable prompt a day, across signal families.
    if service.rows('SELECT id FROM decisions WHERE day=? AND action=? LIMIT 1',(day,analysis['action'])):return False
    service.db.execute('INSERT OR REPLACE INTO opportunities VALUES(?,?,?,?,?)',(key,1,None,t.isoformat(),t.isoformat()))
    service.db.execute('INSERT INTO opportunity_audit(ts,key,event) VALUES(?,?,?)',(t.isoformat(),key,'新机会：首次提醒'))
    return True
