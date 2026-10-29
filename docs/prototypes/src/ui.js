(() => {
"use strict";
const paths={
grid:'<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
flow:'<rect x="3" y="3" width="6" height="5" rx="1"/><rect x="15" y="16" width="6" height="5" rx="1"/><rect x="3" y="16" width="6" height="5" rx="1"/><path d="M6 8v4h12v4M6 12v4"/>',
folder:'<path d="M3 6a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1z"/>',
image:'<rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8" cy="8" r="1.5"/><path d="m3 17 6-6 4 4 3-3 5 5"/>',
play:'<path d="m8 4 12 8-12 8z"/>',
pause:'<path d="M8 5v14M16 5v14" stroke-width="4"/>',
stop:'<rect x="5" y="5" width="14" height="14" rx="2"/>',
record:'<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3" fill="currentColor" stroke="none"/>',
settings:'<path d="m9 3 1-1h4l1 3 3 1 3-1 2 4-2 2v3l1 2-2 4-3-1-3 1-1 2h-4l-1-3-3-1-2 1-2-4 2-2v-3L1 9l2-4 3 1z" transform="translate(0 1) scale(.95)"/><circle cx="12" cy="12" r="3"/>',
search:'<circle cx="10" cy="10" r="6"/><path d="m15 15 6 6"/>',
plus:'<path d="M12 4v16M4 12h16"/>',
chevron:'<path d="m8 4 8 8-8 8"/>',
down:'<path d="m6 9 6 6 6-6"/>',
check:'<path d="m5 12 4 4L20 5"/>',
close:'<path d="m6 6 12 12M6 18 18 6"/>',
more:'<circle cx="5" cy="12" r="1" fill="currentColor"/><circle cx="12" cy="12" r="1" fill="currentColor"/><circle cx="19" cy="12" r="1" fill="currentColor"/>',
target:'<rect x="3" y="4" width="18" height="15" rx="2"/><path d="M3 8h18M9 22h6"/>',
clock:'<circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/>',
branch:'<path d="M6 3v18M6 9h7a5 5 0 0 0 5-5V3"/><circle cx="6" cy="4" r="2" fill="var(--surface,#fff)"/><circle cx="6" cy="20" r="2" fill="var(--surface,#fff)"/><circle cx="18" cy="3" r="2" fill="var(--surface,#fff)"/>',
code:'<path d="m8 6-6 6 6 6m8-12 6 6-6 6m-3-15-2 18"/>',
list:'<path d="M9 6h12M9 12h12M9 18h12"/><circle cx="4" cy="6" r="1"/><circle cx="4" cy="12" r="1"/><circle cx="4" cy="18" r="1"/>',
save:'<path d="M4 3h13l4 4v14H3V3zM7 3v6h9V3M7 21v-8h10v8"/>',
arrow:'<path d="M4 12h16m-6-6 6 6-6 6"/>',
back:'<path d="M20 12H4m6-6-6 6 6 6"/>',
upload:'<path d="M12 16V3m-5 5 5-5 5 5M4 15v6h16v-6"/>',
download:'<path d="M12 3v13m-5-5 5 5 5-5M4 16v5h16v-5"/>',
shield:'<path d="m12 2 9 4v6c0 5-5 8-9 10-4-2-9-5-9-10V6z"/><path d="m8 11 3 3 5-5"/>',
alert:'<path d="m12 3 10 18H2zM12 9v5M12 17v1"/>',
info:'<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7v1"/>',
mouse:'<rect x="6" y="2" width="12" height="20" rx="6"/><path d="M12 2v7"/>',
keyboard:'<rect x="2" y="5" width="20" height="14" rx="2"/><path d="M5 9h1m3 0h1m3 0h1m3 0h1M5 12h1m3 0h1m3 0h1m3 0h1M6 16h12"/>',
eye:'<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
history:'<path d="M3 11a9 9 0 1 1 2 7M3 4v7h7M12 7v6l3 2"/>',
link:'<path d="m9 15 6-6M8 16l-2 2a4 4 0 0 1-6-6l5-5a4 4 0 0 1 6 0m2 10a4 4 0 0 0 6 0l5-5a4 4 0 0 0-6-6l-2 2" transform="translate(2 0) scale(.85)"/>',
trash:'<path d="M3 6h18M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7M14 10v7"/>',
file:'<path d="M5 2h9l5 5v15H5zM14 2v6h5M8 12h8M8 16h8"/>',
bolt:'<path d="m13 2-9 12h7l-1 8 10-13h-8z"/>',
help:'<circle cx="12" cy="12" r="9"/><path d="M9 8a3 3 0 0 1 6 0c0 2-3 2-3 5M12 16v1"/>',
sun:'<circle cx="12" cy="12" r="4"/><path d="M12 1v3M12 20v3M1 12h3M20 12h3M4 4l2 2M18 18l2 2M20 4l-2 2M6 18l-2 2"/>',
moon:'<path d="M20 15A9 9 0 0 1 9 3a9 9 0 1 0 11 12z"/>',
database:'<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v7c0 4 18 4 18 0V5M3 12v7c0 4 18 4 18 0v-7"/>',
connect:'<path d="M8 3v5m8-5v5M5 8h14v4a7 7 0 0 1-14 0zM12 19v4"/>',
crop:'<path d="M6 2v16h16M2 6h16v16"/>',
refresh:'<path d="M20 8a9 9 0 0 0-15-4L2 7m0-5v5h5m-3 9a9 9 0 0 0 15 4l3-3m0 5v-5h-5"/>',
step:'<path d="m5 5 10 7L5 19zM19 4v16"/>',
lock:'<rect x="4" y="10" width="16" height="12" rx="2"/><path d="M8 10V6a4 4 0 0 1 8 0v4M12 14v4"/>',
globe:'<circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="4" ry="9"/><path d="M3 12h18M5 6h14M5 18h14"/>'
};
const esc=v=>String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const icon=(name,size=17)=>'<svg width="'+size+'" height="'+size+'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+(paths[name]||paths.file)+'</svg>';
const btn=(label,to="",kind="",ico="")=>'<button type="button" class="btn '+kind+'" '+(to?'data-go="'+esc(to)+'"':'data-local="feedback"')+'>'+(ico?icon(ico,15):"")+esc(label)+'</button>';
const iconBtn=(ico,label,to="")=>'<button type="button" class="icon-btn" aria-label="'+esc(label)+'" '+(to?'data-go="'+esc(to)+'"':'data-local="feedback"')+'>'+icon(ico,16)+'</button>';
const pill=(label,tone="")=>'<span class="pill '+tone+'">'+esc(label)+'</span>';
const field=(label,value,help="",options={})=>'<label class="field"><span class="field-label">'+esc(label)+'</span>'+(options.area?'<textarea '+(options.invalid?'class="invalid"':"")+'>'+esc(value)+'</textarea>':options.select?'<select '+(options.invalid?'class="invalid"':"")+'>'+options.select.map(x=>'<option'+(x===value?' selected':"")+'>'+esc(x)+'</option>').join("")+'</select>':'<input type="'+(options.password?'password':options.type||'text')+'" value="'+esc(value)+'" '+(options.invalid?'class="invalid"':"")+'>')+(help?'<small'+(options.invalid?' class="error"':"")+'>'+esc(help)+'</small>':"")+'</label>';
const check=(label,checked=true,description="")=>'<label class="check"><input type="checkbox"'+(checked?' checked':"")+'><span>'+esc(label)+(description?'<small>'+esc(description)+'</small>':"")+'</span></label>';
const notice=(title,description="",tone="blue")=>'<div class="notice '+tone+'">'+icon(tone==='red'||tone==='amber'?'alert':tone==='green'?'check':'info',17)+'<div><strong>'+esc(title)+'</strong>'+(description?'<p>'+esc(description)+'</p>':"")+'</div></div>';
const table=(heads,rows,cl="")=>'<table class="table '+cl+'"><thead><tr>'+heads.map(x=>'<th>'+x+'</th>').join("")+'</tr></thead><tbody>'+rows.map(row=>'<tr>'+row.map(x=>'<td>'+x+'</td>').join("")+'</tr>').join("")+'</tbody></table>';
const tabs=(items,active)=>'<div class="tabs">'+items.map(([label,to])=>'<button type="button" class="tab '+(to===active?'active':"")+'" data-go="'+to+'">'+esc(label)+'</button>').join("")+'</div>';
const section=(title,sub="",actions="")=>'<div class="page-header"><div><h2>'+esc(title)+'</h2>'+(sub?'<p>'+esc(sub)+'</p>':"")+'</div><div class="row">'+actions+'</div></div>';
const search=(placeholder="搜索",value="")=>'<label class="searchbox">'+icon('search',15)+'<input aria-label="'+esc(placeholder)+'" placeholder="'+esc(placeholder)+'" value="'+esc(value)+'"><kbd>⌕</kbd></label>';
const progress=(value=62)=>'<div class="progress-track"><div class="progress-fill" style="width:'+value+'%"></div></div>';
const square=(name)=>'<span class="square-icon">'+icon(name,17)+'</span>';
function rail(active){return'<nav class="navrail" aria-label="主导航">'+[['workspace','folder','工作空间'],['assets','image','资产'],['observations-center','eye','观察'],['runs','play','运行']].map(([to,ico,label])=>'<button type="button" class="rail-item '+((active===to||(active==='observations'&&to==='observations-center'))?'active':"")+'" data-go="'+to+'">'+icon(ico,21)+'<span>'+label+'</span></button>').join("")+'<div class="spacer"></div><button type="button" class="rail-item" data-go="help-docs">'+icon('help',20)+'<span>帮助</span></button><button type="button" class="rail-item '+(active==='settings'?'active':"")+'" data-go="settings-general">'+icon('settings',20)+'<span>设置</span></button></nav>'}
function titlebar(name="个人自动化"){return'<div class="titlebar"><span class="dw-mark title-mark"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><path d="m4 6 6 12 4-8 3 6 3-10M4 6h4m8 0h4M9 12h6"/></svg></span><span class="app-name">DeskWeaveStudio</span><span class="muted">'+esc(name)+'</span><div class="window-controls"><span>—</span><span>□</span><span>×</span></div></div>'}
function status(s){const disconnected=s.id==='run-disconnected';return'<footer class="statusbar"><span><i class="status-dot '+(disconnected?'warn':"")+'"></i>'+(disconnected?'连接已中断':'本地工作空间')+'</span><span class="hide-narrow">'+(s.id==='storage-error'?'草稿未保存':'最近保存 09:42')+'</span><span class="spacer"></span><span class="hide-narrow">目标：报表中心</span><span>急停 <kbd>Ctrl</kbd> + <kbd>Alt</kbd> + <kbd>Pause</kbd></span><button type="button" class="icon-btn" aria-label="快捷键帮助" data-go="shortcuts-help">'+icon('help',13)+'</button></footer>'}
function shell(s,content,{active="workspace",editor=false,title="个人自动化",noToolbar=false}={}){
 return titlebar(title)+(noToolbar?"":'<header class="workbar"><button class="btn ghost workspace-name" type="button" data-go="workspace">'+icon('folder',16)+'个人自动化 '+icon('down',12)+'</button><span class="crumb">/</span><span style="font-size:13px">'+(editor?'月度报表下载':esc(s.title))+'</span>'+(editor?pill('已保存 · 版本 12'):"")+'<div class="spacer"></div>'+(editor?'<span class="hide-narrow">'+btn('报表中心','target-select','','target')+'</span>'+btn('录制','record-config','','record')+btn('运行','run-config','primary','play')+btn('停止','run-stopping','','stop'):btn('命令','command-palette','ghost','search'))+'</header>')+'<div class="app-body">'+rail(active)+content+'</div>'+status(s);
}
function targetScene(mode=""){return'<div class="target-scene '+mode+'"><div class="target-title"><span>'+icon('grid',15)+' 报表中心</span><span>个人工作区</span><span class="spacer"></span><span>—　□　×</span></div><div class="target-body"><aside class="target-sidebar"><strong>报表中心</strong><span>总览</span><span class="active">月度报表</span><span>下载记录</span><span>设置</span></aside><main class="target-page"><div class="between"><div><div class="eyebrow">REPORT CENTER</div><h2>月度报表</h2></div><span class="target-avatar">林</span></div><p class="muted">查看和下载已生成的月度经营报表。</p><div class="target-filter"><span>2026 年 10 月　⌄</span><span>全部类型　⌄</span><span class="spacer"></span><button type="button" class="target-download" data-local="target-feedback">下载所选</button></div><div class="target-table"><div class="target-table-head"><span>报表名称</span><span>生成时间</span><span>大小</span><span>状态</span></div>'+[['月度经营汇总','10-29 09:30','1.8 MB'],['收入明细','10-29 09:28','640 KB'],['费用分析','10-29 09:26','920 KB'],['业务趋势','10-29 09:25','420 KB']].map((r,i)=>'<div class="target-table-row '+(i===0?'selected':"")+'"><span><i class="target-checkbox">'+(i===0?'✓':"")+'</i>'+r[0]+'</span><span>'+r[1]+'</span><span>'+r[2]+'</span><span class="target-ready">可下载</span></div>').join("")+'</div><div class="target-footer">共 4 份报表 <span class="spacer"></span>‹　1　›</div></main></div></div>'}
window.DW_UI={esc,icon,btn,iconBtn,pill,field,check,notice,table,tabs,section,search,progress,square,rail,titlebar,status,shell,targetScene};
window.DW_VIEWS={};
})();
