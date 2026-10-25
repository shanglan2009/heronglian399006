const closeRecoveryPanel=document.createElement('article');closeRecoveryPanel.className='panel';
closeRecoveryPanel.innerHTML='<h3>收盘复盘重试状态</h3><p class="muted">每15分钟检查缺失复盘，休市和重启后也会补结算。仅使用对应日期的日K与登记账本，不补造买卖信号。</p><button id="retry-reports">重试缺失复盘</button><div id="close-recovery-status"></div>';
document.getElementById('review').appendChild(closeRecoveryPanel);
const renderBeforeRecovery=render;render=function(s){renderBeforeRecovery(s);document.getElementById('close-recovery-status').innerHTML=(s.close_status||[]).map(r=>`<div class="entry"><h4>${esc(r.day)} · ${esc(r.status)}</h4><p>${esc(r.detail)}</p></div>`).join('')||empty('暂无新版本重试记录');};
document.getElementById('retry-reports').onclick=async()=>{await window.pywebview.api.retry_reports();};
