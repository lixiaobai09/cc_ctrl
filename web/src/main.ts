import { Terminal } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import '@xterm/xterm/css/xterm.css';
import './style.css';

type Workspace = {workspace_id:string;run_id:string;name:string;cwd:string;comment:string;engine:string;status:string;session_id:string};
type Pane = {id:string;index:number;title:string;command:string;active:boolean};
type Window = {id:string;index:number;name:string;active:boolean;width:number;height:number;panes:Pane[]};
const root = document.querySelector<HTMLElement>('#app')!;
let csrf = '', sessionId = '', ws:WebSocket|null = null, term:Terminal|null = null;
let writer = false, adapted = false, windows:Window[] = [], selected:Workspace|null = null;
let reconnectTimer:number|undefined, pollTimer:number|undefined, generation=0;
let resizeTimer:number|undefined, fit:FitAddon|null=null, statusRows=1;
let frameObserver:ResizeObserver|null=null;
let autoFitEnabled=false;
let remoteScrollSupported=false;
const DEFAULT_FONT_SIZE=8, MIN_FONT_SIZE=8, MAX_FONT_SIZE=20;
function savedFontSize(){
  try{const value=Number(localStorage.getItem('cct.terminal.fontSize'));return Number.isInteger(value)&&value>=MIN_FONT_SIZE&&value<=MAX_FONT_SIZE?value:DEFAULT_FONT_SIZE;}
  catch{return DEFAULT_FONT_SIZE;}
}
let fontSize=savedFontSize();
const get = <T extends HTMLElement>(id:string) => document.getElementById(id) as T;
const notice = (text:string) => {const node=document.getElementById('notice');if(node)node.textContent=text;};

async function api(path:string, method='GET', data?:unknown):Promise<any> {
  const response = await fetch(path,{method,credentials:'same-origin',headers:{'Content-Type':'application/json',...(method==='GET'?{}:{'X-CSRF-Token':csrf})},body:data===undefined?undefined:JSON.stringify(data)});
  const result = await response.json();
  if(!response.ok){
    if(response.status===401 && path!=='/api/auth/login') showLogin();
    throw new Error(result.error || `请求失败 (${response.status})`);
  }
  return result;
}
function button(text:string, action:()=>void, className=''):HTMLButtonElement {
  const b=document.createElement('button');b.textContent=text;b.className=className;b.addEventListener('click',action);return b;
}
function clean(){
  generation++; window.clearTimeout(reconnectTimer);window.clearInterval(pollTimer);window.clearTimeout(resizeTimer);
  frameObserver?.disconnect();frameObserver=null;
  if(ws){ws.onclose=null;ws.close();ws=null;} term?.dispose();term=null;fit=null;
  writer=false;adapted=false;autoFitEnabled=false;windows=[];selected=null;
}
function shell(content:string){
  root.classList.remove("terminal-page");
  root.innerHTML=`<header><a class="brand" href="/">cct<span>REMOTE WORKSPACE</span></a><nav id="nav"></nav></header><p id="notice" role="status" aria-live="polite"></p>${content}`;
}
function showLogin(){
  clean();csrf='';remoteScrollSupported=false;
  shell(`<section class="login card"><div class="eyebrow">你的工作，继续进行</div><h1>连接工作区</h1><p class="muted">登录这台主机，查看进度或接着操作。</p><form id="login"><label>管理员账号<input id="username" name="username" autocomplete="username" required maxlength="100"></label><label>密码<input id="password" name="password" type="password" autocomplete="current-password" required maxlength="1024"></label><label class="check"><input id="remember" type="checkbox">记住登录，最多 30 天</label><button type="submit" class="primary">登录 →</button></form></section>`);
  get<HTMLFormElement>('login').onsubmit=async event=>{
    event.preventDefault();const b=get<HTMLFormElement>('login').querySelector('button')!;b.disabled=true;
    try{await api('/api/auth/login','POST',{username:get<HTMLInputElement>('username').value,password:get<HTMLInputElement>('password').value,remember:get<HTMLInputElement>('remember').checked});get<HTMLInputElement>('password').value='';await start();}
    catch(error){notice(String(error));b.disabled=false;}
  };
}
function navigation(){
  const nav=get<HTMLElement>('nav');
  nav.append(button('工作区',()=>void showWorkspaces()),button('登录管理',()=>void showSessions()),button('退出',()=>void api('/api/auth/logout','POST').then(showLogin).catch(e=>notice(String(e)))));
}
async function start(){
  try{const session=await api('/api/auth/session');csrf=session.csrf;sessionId=session.id;remoteScrollSupported=Array.isArray(session.capabilities)&&session.capabilities.includes('pane-scroll');await showWorkspaces();}
  catch{showLogin();}
}
async function showWorkspaces(){
  clean(); const current=generation;
  shell(`<section class="heading"><div><div class="eyebrow">这台主机</div><h1>工作区</h1></div><span class="pill">已加密连接</span></section><section><h2>正在运行</h2><div id="live" class="grid"></div></section><section><h2>历史记录</h2><p class="muted">恢复准确记录的 agent 会话；不会重建原来的分屏布局。</p><div id="history" class="grid"></div></section>`);navigation();
  const load=async()=>{
    if(document.hidden || current!==generation)return;
    try{
      const [active,history]:[Workspace[],Workspace[]]=await Promise.all([api('/api/workspaces'),api('/api/history')]);
      if(current!==generation)return;
      renderWorkspaces('live',active.filter(w=>w.status!=='gone'),false);
      renderWorkspaces('history',history.filter(w=>w.status!=='alive'),true);
    }catch(error){if(current===generation)notice(String(error));}
  };
  await load();if(current===generation)pollTimer=window.setInterval(()=>void load(),5000);
}
function renderWorkspaces(id:string, rows:Workspace[], history:boolean){
  const container=get<HTMLElement>(id);container.replaceChildren();
  if(!rows.length){const empty=document.createElement('p');empty.className='muted';empty.textContent='暂无工作区';container.append(empty);return;}
  for(const row of rows){
    const card=document.createElement('article');card.className='card workspace';
    const title=document.createElement('h3');title.textContent=row.name;
    const meta=document.createElement('span');meta.className='pill';meta.textContent=`${row.engine||'shell'} · ${row.status==='alive'?'运行中':row.status==='unknown'?'状态未知':'已结束'}`;
    const cwd=document.createElement('p');cwd.className='path';cwd.textContent=row.cwd;
    const comment=document.createElement('p');comment.textContent=row.comment||'没有备注';
    const b=button(history?'恢复并连接':'连接终端',()=>{
      if(history){b.disabled=true;void api(`/api/history/${row.workspace_id}/restore`,'POST',{}).then(openTerminal).catch(error=>{notice(String(error));b.disabled=false;});}
      else openTerminal(row);
    },'primary');
    b.disabled=row.status==='unknown'||(history&&!row.session_id);
    card.append(meta,title,cwd,comment,b);
    if(history&&!row.session_id){const note=document.createElement('p');note.className='muted';note.textContent='会话 ID 尚未记录，请在电脑使用 cct restore。';card.append(note);}
    container.append(card);
  }
}
async function showSessions(){
  clean();const current=generation;
  shell(`<section class="heading"><div><div class="eyebrow">管理员</div><h1>浏览器登录</h1></div></section><section id="sessions" class="grid"></section>`);navigation();
  try{
    const sessions=await api('/api/auth/sessions');if(current!==generation)return;
    for(const login of sessions){
      const card=document.createElement('article');card.className='card';
      const title=document.createElement('h3');title.textContent=login.id===sessionId?'当前浏览器':'其他浏览器';
      const detail=document.createElement('p');detail.className='path';detail.textContent=login.label;
      const time=document.createElement('p');time.textContent=`最近使用：${new Date(login.seen*1000).toLocaleString()}`;
      card.append(title,detail,time,button('撤销登录',()=>void api(`/api/auth/sessions/${login.id}`,'DELETE').then(()=>login.id===sessionId?showLogin():showSessions()).catch(e=>notice(String(e)))));get<HTMLElement>('sessions').append(card);
    }
  }catch(error){notice(String(error));}
}
function send(message:unknown){if(ws?.readyState===WebSocket.OPEN)ws.send(JSON.stringify(message));}
function controls(){
  for(const id of ['window','pane','adapt','restore-size'])get<HTMLButtonElement>(id).disabled=!writer;
  root.querySelectorAll<HTMLButtonElement>('.keys button').forEach(button=>button.disabled=!writer);
  get<HTMLButtonElement>('claim').textContent=writer?'释放':'接管';
  get<HTMLButtonElement>('claim').title=writer?'释放终端控制权':'接管终端控制权';
  get<HTMLElement>('mode').textContent=writer?'你正在控制':'旁观模式';
  get<HTMLButtonElement>('restore-size').hidden=!adapted;
  get<HTMLButtonElement>('adapt').hidden=adapted;
  const frame=root.querySelector<HTMLElement>('.terminal-frame')!;
  const changed=frame.classList.contains('is-adapted')!==adapted;
  frame.classList.toggle('is-adapted',adapted);
  get<HTMLElement>('scroll-drag').parentElement!.hidden=adapted;
  reserveScrollbar();
  if(changed)requestAnimationFrame(()=>resize());
}
function terminalReady(){
  if(!writer){notice('请先接管终端，再发送内容。');return false;}
  if(!term||ws?.readyState!==WebSocket.OPEN){notice('连接尚未就绪，请稍后再试。');return false;}
  return true;
}
async function pasteClipboard(){
  if(!terminalReady())return;
  const connection=ws,current=generation;
  const active=windows.find(w=>w.active),pane=active?.panes.find(p=>p.active);
  try{
    if(!navigator.clipboard?.readText)throw new Error('Clipboard unavailable');
    const text=await navigator.clipboard.readText();
    const target=windows.find(w=>w.active),activePane=target?.panes.find(p=>p.active);
    if(current!==generation)return;
    if(!writer||ws!==connection||ws?.readyState!==WebSocket.OPEN||target?.id!==active?.id||activePane?.id!==pane?.id){notice('终端状态已变化，请重新粘贴。');return;}
    term?.paste(text);term?.focus();notice('');
  }catch{if(current===generation)notice('浏览器无法读取剪贴板，请长按终端输入区使用粘贴。');}
}
function reserveScrollbar(){
  const viewport=term?.element?.querySelector<HTMLElement>('.xterm-viewport');
  if(viewport&&term?.element){
    // Native bars may be wider than overlay bars. Keep them outside the text surface.
    const scrollbar=viewport.offsetWidth-viewport.clientWidth;
    term.element.style.paddingRight=`${adapted?0:Math.max(6,scrollbar)}px`;
  }
}
function scrollTerminal(lines:number){
  if(!term||!lines)return;
  const viewport=get<HTMLElement>('terminal');
  if(!adapted&&viewport.scrollHeight>viewport.clientHeight+1){
    const before=viewport.scrollTop;viewport.scrollTop+=lines*fontSize*1.2;
    if(viewport.scrollTop!==before)return;
  }
  if(term.buffer.active.type==='normal'&&term.buffer.active.length>term.rows){term.scrollLines(lines);return;}
  if(!writer){notice('请先接管终端，再滚动当前面板。');return;}
  if(!remoteScrollSupported){notice('服务端尚未加载滚动功能，请重启 cct serve 后刷新页面。');return;}
  const window=windows.find(w=>w.active),pane=window?.panes.find(p=>p.active);
  if(window&&pane)send({type:'scroll',window:window.id,pane:pane.id,lines:Math.max(-50,Math.min(50,lines))});
}
function scrolling(){
  const frame=root.querySelector<HTMLElement>('.terminal-frame')!;
  get<HTMLButtonElement>('scroll-up').onclick=()=>scrollTerminal(-5);
  get<HTMLButtonElement>('scroll-down').onclick=()=>scrollTerminal(5);
  let startX=0,lastY=0,vertical=false;
  frame.addEventListener('touchstart',event=>{
    if(event.touches.length!==1)return;
    startX=event.touches[0].clientX;lastY=event.touches[0].clientY;vertical=false;
  },{passive:true,capture:true});
  frame.addEventListener('touchmove',event=>{
    if(event.touches.length!==1)return;
    const touch=event.touches[0],dy=lastY-touch.clientY;
    if(!vertical&&Math.abs(touch.clientX-startX)>Math.abs(dy))return;
    const lineHeight=Math.max(8,fontSize*1.2);
    if(Math.abs(dy)<lineHeight)return;
    vertical=true;event.preventDefault();event.stopImmediatePropagation();
    const lines=Math.trunc(dy/lineHeight);lastY-=lines*lineHeight;scrollTerminal(lines);
  },{passive:false,capture:true});
  frame.addEventListener('wheel',event=>{
    if(Math.abs(event.deltaX)>Math.abs(event.deltaY))return;
    if(!event.deltaY)return;
    event.preventDefault();event.stopImmediatePropagation();
    const pixels=event.deltaMode===1?event.deltaY*fontSize*1.2:event.deltaMode===2?event.deltaY*frame.clientHeight:event.deltaY;
    scrollTerminal(Math.sign(pixels)*Math.max(1,Math.min(30,Math.round(Math.abs(pixels)/(fontSize*1.2)))));
  },{passive:false,capture:true});
  const drag=get<HTMLElement>('scroll-drag');let dragY:number|null=null;
  drag.onpointerdown=event=>{dragY=event.clientY;drag.setPointerCapture(event.pointerId);event.preventDefault();};
  drag.onpointermove=event=>{
    if(event.pointerType==='touch'||dragY===null)return; // Touch is handled by the shared gesture above.
    const distance=dragY-event.clientY,lines=Math.trunc(distance/Math.max(8,fontSize*1.2));
    if(lines){scrollTerminal(lines);dragY-=lines*Math.max(8,fontSize*1.2);}
  };
  drag.onpointerup=drag.onpointercancel=()=>{dragY=null;};
}
function fontControls(){
  get<HTMLOutputElement>('font-value').value=`${fontSize}px`;
  get<HTMLButtonElement>('font-smaller').disabled=fontSize<=MIN_FONT_SIZE;
  get<HTMLButtonElement>('font-larger').disabled=fontSize>=MAX_FONT_SIZE;
}
function setFontSize(value:number){
  fontSize=Math.max(MIN_FONT_SIZE,Math.min(MAX_FONT_SIZE,value));
  try{localStorage.setItem('cct.terminal.fontSize',String(fontSize));}catch{/* Storage may be unavailable in private browsers. */}
  if(term)term.options.fontSize=fontSize;
  fontControls();requestAnimationFrame(()=>resize());
}
function options(){
  const active=windows.find(w=>w.active);const win=get<HTMLSelectElement>('window');const pane=get<HTMLSelectElement>('pane');
  win.replaceChildren();pane.replaceChildren();
  for(const w of windows){const option=new Option(`${w.index} · ${w.name}`,w.id);option.selected=w.active;win.add(option);}
  for(const p of active?.panes||[]){const option=new Option(`${p.index} · ${p.command} · ${p.title}`,p.id);option.selected=p.active;pane.add(option);}
  if(active && term){
    reserveScrollbar();
    const cols=Math.max(10,active.width),rows=Math.max(5,active.height+statusRows);
    if(term.cols!==cols||term.rows!==rows)term.resize(cols,rows);
    send({type:'viewport',cols,rows});
  }
}
function measurement(){
  reserveScrollbar();const dims=fit?.proposeDimensions();
  if(!dims||!term?.element)return null;
  const screen=term.element.querySelector<HTMLElement>('.xterm-screen');
  const cellWidth=screen?screen.getBoundingClientRect().width/term.cols:0;
  const padding=parseFloat(term.element.style.paddingRight);
  const gutter=Number.isFinite(padding)?padding:(adapted?0:6);
  // FitAddon subtracts both our padding and the native scrollbar. Here padding
  // already contains the scrollbar, so count that space only once.
  const cols=cellWidth>0?Math.floor((get<HTMLElement>('terminal').clientWidth-gutter)/cellWidth):dims.cols;
  return {cols:Math.max(10,Math.min(500,cols)),rows:Math.max(5,Math.min(300,dims.rows))};
}
function resize(){
  document.documentElement.style.setProperty('--viewport',`${window.visualViewport?.height||window.innerHeight}px`);
  window.clearTimeout(resizeTimer);resizeTimer=window.setTimeout(()=>{
    if(autoFitEnabled&&adapted&&writer){const size=measurement();if(size)send({type:'adapt',window:windows.find(w=>w.active)?.id,...size});}
  },250);
}
function openTerminal(row:Workspace){
  clean();selected=row;const current=generation;
  shell(`<section class="toolbar"><select id="window" aria-label="window"></select><select id="pane" aria-label="pane"></select><span id="mode" class="sr-only" aria-live="polite">旁观模式</span><button id="claim">接管</button><button id="adapt" title="适配手机，会同时调整电脑端窗口">适配</button><button id="restore-size" title="恢复原窗口尺寸" hidden>恢复</button></section><div class="terminal-frame"><div id="terminal"></div><div class="terminal-scroll" aria-label="终端滚动控制"><button id="scroll-up" aria-label="向上滚动当前面板" title="向上滚动">▴</button><div id="scroll-drag" title="上下拖动，滚动当前面板" aria-label="上下拖动滚动当前面板"><span>↕</span></div><button id="scroll-down" aria-label="向下滚动当前面板" title="向下滚动">▾</button></div></div><section class="terminal-footer"><div class="keys" id="keys" aria-label="终端快捷键"></div></section>`);
  root.classList.add('terminal-page');
  root.querySelector('header')!.innerHTML=`<button id="back" title="返回工作区" aria-label="返回工作区">‹</button><div class="terminal-identity"><h1 id="workspace-name"></h1><span id="connection">连接中</span></div><details class="terminal-menu"><summary>菜单</summary><div class="menu-body"><nav id="nav"></nav><section class="font-settings" aria-label="终端字号"><span>终端字号</span><div class="font-buttons"><button id="font-smaller" aria-label="缩小终端字号">A−</button><output id="font-value" aria-live="polite"></output><button id="font-larger" aria-label="放大终端字号">A+</button><button id="font-reset">默认</button></div></section><p class="shared">与电脑共享窗口和焦点；电脑上的输入不受网页控制权限制。适配后可直接上下滑动终端回看。</p><p id="size-note" class="muted"></p></div></details>`;
  get<HTMLElement>('workspace-name').textContent=row.name;navigation();
  get<HTMLButtonElement>('back').onclick=()=>void showWorkspaces();
  term=new Terminal({fontFamily:'Menlo, Consolas, monospace',fontSize,cursorBlink:true,scrollback:3000,theme:{background:'#0b1117',foreground:'#d8e3ea',cursor:'#7ee0b5'},allowProposedApi:false});
  fit=new FitAddon();term.loadAddon(fit);term.open(get<HTMLElement>('terminal'));
  fontControls();scrolling();
  get<HTMLButtonElement>('font-smaller').onclick=()=>setFontSize(fontSize-1);
  get<HTMLButtonElement>('font-larger').onclick=()=>setFontSize(fontSize+1);
  get<HTMLButtonElement>('font-reset').onclick=()=>setFontSize(DEFAULT_FONT_SIZE);
  frameObserver=new ResizeObserver(()=>resize());frameObserver.observe(root.querySelector('.terminal-frame')!);
  term.onData(data=>{if(writer){const chunks=Array.from(data);let chunk='';let bytes=0;for(const char of chunks){const count=new TextEncoder().encode(char).length;if(bytes+count>12000){send({type:'input',data:chunk});chunk='';bytes=0;}chunk+=char;bytes+=count;}if(chunk)send({type:'input',data:chunk});}});
  const stopAutoFit=()=>{autoFitEnabled=false;window.clearTimeout(resizeTimer);};
  get<HTMLSelectElement>('window').onchange=()=>{stopAutoFit();send({type:'select-window',id:get<HTMLSelectElement>('window').value});};
  get<HTMLSelectElement>('pane').onchange=()=>{stopAutoFit();send({type:'select-pane',id:get<HTMLSelectElement>('pane').value});};
  get<HTMLButtonElement>('claim').onclick=()=>{stopAutoFit();send({type:writer?'release':'claim'});};
  get<HTMLButtonElement>('adapt').onclick=()=>{const size=measurement();if(size){autoFitEnabled=true;send({type:'adapt',window:windows.find(w=>w.active)?.id,...size});}};
  get<HTMLButtonElement>('restore-size').onclick=()=>{stopAutoFit();send({type:'restore-size'});};
  for(const [name,data] of [['Esc','\x1b'],['Tab','\t'],['↑','\x1b[A'],['↓','\x1b[B'],['←','\x1b[D'],['→','\x1b[C'],['Ctrl-C','\x03']]){
    get<HTMLElement>('keys').append(button(name,()=>{if(writer)send({type:'input',data});}));
  }
  const paste=button('粘贴',()=>void pasteClipboard());paste.id='paste';paste.title='粘贴剪贴板文字到终端，不发送回车';
  const enter=button('Enter ↵',()=>{if(terminalReady())send({type:'input',data:'\r'});},'primary');enter.id='enter';enter.title='向终端发送回车';
  get<HTMLElement>('keys').append(paste,enter);
  function connect(reconnect=false){
    if(current!==generation)return;
    ws=new WebSocket(`${location.origin.replace(/^https/,'wss')}/api/workspaces/${row.workspace_id}/terminal?run_id=${encodeURIComponent(row.run_id)}${reconnect?'&reconnect=1':''}`);ws.binaryType='arraybuffer';
    ws.onopen=()=>{if(current!==generation)return;get<HTMLElement>('connection').textContent='已连接';notice('');};
    ws.onmessage=event=>{
      if(current!==generation)return;
      if(event.data instanceof ArrayBuffer){term?.write(new Uint8Array(event.data));return;}
      const data=JSON.parse(event.data);
      if(data.type==='state'){const wasAdapted=adapted;windows=data.windows;statusRows=data.status_rows??1;writer=data.writer;adapted=data.adapted;if(wasAdapted&&!adapted)autoFitEnabled=false;options();controls();get<HTMLElement>('size-note').textContent=adapted?'正在适配手机，电脑端同一窗口也会调整。':'';}
      else if(data.type==='control'){writer=data.writer;adapted=data.adapted;controls();if(writer)notice('');}
      else if(data.type==='adapted'){adapted=true;controls();get<HTMLElement>('size-note').textContent=`已适配 ${data.width} × ${data.height}；电脑端同一窗口也会调整。`;}
      else if(data.type==='error')notice(data.message);
    };
    ws.onclose=event=>{
      if(current!==generation)return;
      writer=false;adapted=false;autoFitEnabled=false;controls();
      if(event.code===4401){showLogin();return;}
      if(event.code===4409){notice('运行实例已变化或结束，请返回工作区列表。');get<HTMLElement>('connection').textContent='已结束';return;}
      get<HTMLElement>('connection').textContent='已断开 · 重连中';notice('连接断开，未发送的输入不会自动重发。');
      reconnectTimer=window.setTimeout(()=>{term?.reset();connect(true);},2000);
    };
  }
  controls();resize();connect();
}
window.addEventListener('resize',resize);window.visualViewport?.addEventListener('resize',resize);
void start();
