(()=>{
  const panel=document.createElement('article');panel.className='panel';panel.id='downside-panel';
  panel.innerHTML='<h3>持仓风险与减仓计划</h3><div id="downside-state">正在核验风险数据</div><p class="muted">仓位上限为当前版本风控默认值，尚未经收益优化验证。参考数量基于已登记持仓；实际成交后请及时登记。低风险并不等于没有风险，恢复买入仍需原策略确认。</p>';
  document.querySelector('.page').prepend(panel);
  const health=document.createElement('article');health.className='panel';health.innerHTML='<h3>运行健康与监控覆盖</h3><div id="monitor-coverage"></div><div id="health-history"></div><p class="muted">采样次数只证明当时程序取得过数据，不代表全天无遗漏。关机、退出或断网期间无法实时推送；恢复后才可发现部分缺口。请保持开机、联网和不休眠。</p>';$('settings').prepend(health);
  const original=render;
  render=function(s){original(s);const r=s.downside_risk;
    if(!r)return;
    $('downside-state').innerHTML=`<h3>${esc(r.label)} <span class="tag">${esc(r.display_note)}</span></h3><p>${esc(r.reasons.join('；'))}</p><div class="two"><div class="feature"><span>登记股票仓位</span><b>${r.exposure==null?'—':pct(r.exposure)}</b></div><div class="feature"><span>风险目标仓位上限</span><b>${!r.ready?'无法判断':r.target==null?'未触发上限':pct(r.target)}</b></div><div class="feature"><span>参考减仓份额${r.live&&r.confirmed?'':'（观察值）'}</span><b>${r.ready?num(r.quantity,0):'—'}</b></div><div class="feature"><span>登记账户观测峰值回撤</span><b>${r.account_drawdown==null?'—':r.account_drawdown>0?pct(-r.account_drawdown):'0.00%'}</b></div></div><small>${esc(r.peak_note||'数据不足时不作减仓计算')} · ${esc(r.action)}</small>`;
    if(r.live&&r.ready&&r.level>0){txt('action',r.confirmed?(r.quantity?'风险减仓':'暂停加仓'):'风险预警 · 确认中');txt('regime',r.label+' · '+r.display_note);$('reasons').innerHTML='<ul>'+r.reasons.map(x=>`<li>${esc(x)}</li>`).join('')+'</ul>';}
    const rows=s.monitor_health||[];
    $('monitor-coverage').innerHTML=rows.length?'<table><tr><th>交易日</th><th>首次/最后轮询</th><th>有效行情采样</th><th>检测到的间隔缺口</th></tr>'+rows.map(x=>`<tr><td>${esc(x.day)}</td><td>${esc(x.first_seen.slice(11,19))} / ${esc(x.last_seen.slice(11,19))}</td><td>${x.fresh_ticks}</td><td>${x.gaps}</td></tr>`).join('')+'</table>':empty('新版尚未记录交易时段监控采样');
    $('health-history').innerHTML=(s.health_warnings||[]).map(x=>`<div class="entry"><small>${esc(date(x.ts))}</small><p>${esc(x.message)}</p></div>`).join('')||empty('暂无已检测到的监控缺口');
  };
})();
