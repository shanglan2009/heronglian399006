// Additive research view. Never calls trading, push or account mutation APIs.
(()=>{
  const panel=document.createElement('article');panel.className='panel';panel.id='repair-panel';
  panel.innerHTML='<h3>9月观点更新 · 并行研究</h3><div class="soft">正式提醒先执行机会去重；低位修复新逻辑仅观察，不下单、不自动升级。120分钟数据未接入，不能宣称底背离已确认。3300 / 3740仅为文章当时参考点。</div><div id="repair-current">等待数据</div><h4>新旧方向配对</h4><p class="muted">每日首个有效交易时段快照；观望显示“—”，不当作零收益。到期方向变化扣除成本假设，不是账户利润，不与正式胜率合并。</p><div id="repair-results"></div><h4>机会生命周期</h4><p class="muted">同一机会仅提醒一次，每天每个方向最多一次。条件连续30分钟未成立且行情采样连续，才允许重新识别；断线、午休、跨日间隔不会自动重置机会。开盘仍发送每日摘要。</p><div id="opportunity-events"></div>';
  $('review').prepend(panel);
  const overview=document.createElement('div');overview.className='soft';overview.textContent='1.3版：同一机会不重复操作；9月低位修复逻辑正在独立观察。详情见“复盘与学习”。';
  const first=document.querySelector('.page');first.prepend(overview);
  async function update(){
    try{
      const r=await window.pywebview.api.get_repair();const c=r.current,f=c.features;
      $('repair-current').innerHTML=`<p><b>${esc(c.state)}</b> · ${esc(c.mode)}</p><p class="muted">均线离散度 ${f.compression!=null?pct(f.compression):'—'} · 粘合 ${f.compression_days??'—'} 个完整交易日 · 5日均量/此前20日均量 ${f.volume_ratio5!=null?num(f.volume_ratio5):'—'}<br>参数为待验证工程假设；结构支撑来自滚动行情，失效不无限延期。</p>`;
      $('repair-results').innerHTML=r.results.length?'<table><tr><th>观察日/窗口</th><th>旧规则净方向</th><th>新假设净方向</th></tr>'+r.results.map(x=>`<tr><td>${esc(x.day)} / ${x.horizon}日</td><td>${x.baseline_net==null?'—':pct(x.baseline_net)}</td><td>${x.candidate_net==null?'—':pct(x.candidate_net)}</td></tr>`).join('')+'</table>':empty(`已记录 ${r.pairs.length} 个配对快照，暂无到期结果；不回填历史。`);
      $('opportunity-events').innerHTML=r.opportunities.length?r.opportunities.map(x=>`<div class="entry"><b>${esc(x.event)}</b><small>${esc(date(x.ts))} · ${esc(x.key)}</small></div>`).join(''):empty('尚无新版本机会记录');
    }catch(e){$('repair-current').textContent='研究数据暂不可用，不改变正式策略';}
  }
  window.addEventListener('pywebviewready',()=>{update();setInterval(update,5000)});
})();
