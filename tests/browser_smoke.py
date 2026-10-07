"""Optional HTTPS/Chrome smoke test; all host state and tmux sessions are temporary."""
import base64, json, os, socket, subprocess, sys, tempfile, time
from pathlib import Path
from contextlib import ExitStack
import httpx
from websockets.sync.client import connect
from cctl.auth import Auth

repo=Path(__file__).resolve().parents[1]
root=Path(tempfile.mkdtemp(prefix='cct-browser-'))
sock=str(root/'tmux.sock')
env=dict(os.environ,CCTL_HOME=str(root),CCTL_TMUX_SOCKET=sock,PYTHONPATH=str(repo))
processes=[]
connections=ExitStack()
try:
    subprocess.run(['tmux','-S',sock,'-f','/dev/null','new-session','-d','-s','browser-test','-x','100','-y','35','/bin/cat'],check=True)
    subprocess.run(['tmux','-S',sock,'new-window','-t','browser-test:1','-n','second','/bin/cat'],check=True)
    row={'name':'browser-test','comment':'手机界面测试','cwd':str(root),'tmux_session':'browser-test','created_at':'now','engine':'','session_id':''}
    (root/'workspaces.json').write_text(json.dumps([row]));(root/'history.json').write_text(json.dumps({'browser-test':row}))
    Auth(root).configure('admin','browser-smoke-password')
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-keyout',str(root/'key.pem'),'-out',str(root/'cert.pem'),'-subj','/CN=localhost'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    with socket.socket() as free:free.bind(('127.0.0.1',0));port=free.getsockname()[1]
    origin=f'https://127.0.0.1:{port}'
    log=open(root/'server.log','w')
    server=subprocess.Popen([sys.executable,'-m','cctl.cli','serve','--host','127.0.0.1','--port',str(port),'--cert-file',str(root/'cert.pem'),'--key-file',str(root/'key.pem'),'--origin',origin],env=env,cwd=repo,stdout=log,stderr=log);processes.append(server)
    for _ in range(100):
        try:
            if httpx.get(origin,verify=False,timeout=1).status_code==200:break
        except Exception:time.sleep(.1)
    else:raise RuntimeError('Server did not start: '+(root/'server.log').read_text())
    chrome_log=open(root/'chrome.log','w')
    chrome=subprocess.Popen([os.environ.get('CCT_CHROME', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'),'--headless=new','--no-first-run','--no-default-browser-check','--disable-background-networking','--disable-sync','--disable-extensions','--disable-component-update','--ignore-certificate-errors','--remote-debugging-port=0','--remote-allow-origins=http://localhost','--user-data-dir='+str(root/'chrome'),'about:blank'],stdout=chrome_log,stderr=chrome_log);processes.append(chrome)
    for _ in range(100):
        if (root/'chrome/DevToolsActivePort').exists():break
        time.sleep(.1)
    debug_port=(root/'chrome/DevToolsActivePort').read_text().splitlines()[0]
    targets=httpx.get(f'http://127.0.0.1:{debug_port}/json/list').json()
    page=next(x for x in targets if x['type']=='page')
    ws=connections.enter_context(connect(page['webSocketDebuggerUrl'],origin='http://localhost'))
    sequence=0;errors=[]
    def cdp(method,params=None):
        global sequence
        sequence+=1;sid=sequence;ws.send(json.dumps({'id':sid,'method':method,'params':params or {}}))
        while True:
            reply=json.loads(ws.recv(timeout=15))
            if reply.get('method')=='Runtime.exceptionThrown':errors.append(reply['params'])
            if reply.get('id')==sid:
                if 'error' in reply:raise RuntimeError(reply['error'])
                return reply.get('result',{})
    def js(code):
        result=cdp('Runtime.evaluate',{'expression':code,'returnByValue':True,'awaitPromise':True})
        if 'exceptionDetails' in result:raise RuntimeError(result['exceptionDetails'])
        return result.get('result',{}).get('value')
    def wait(condition):
        for _ in range(100):
            if js(condition):return
            time.sleep(.1)
        raise AssertionError('Timed out: '+condition+'; notice='+str(js("document.querySelector('#notice')?.textContent")))
    cdp('Runtime.enable');cdp('Page.enable')
    cdp('Emulation.setDeviceMetricsOverride',{'width':390,'height':844,'deviceScaleFactor':1,'mobile':True})
    cdp('Page.navigate',{'url':origin})
    wait("!!document.getElementById('username')")
    js("document.getElementById('username').value='admin';document.getElementById('password').value='browser-smoke-password';document.querySelector('#login button').click()")
    wait("!!document.querySelector('#live button')")
    js("document.querySelector('#live button').click()")
    wait("document.getElementById('connection')?.textContent==='已连接' && document.querySelector('#window')?.options.length===2")
    wait("!document.getElementById('adapt').disabled")
    wait("(() => {const screen=document.querySelector('.xterm-screen').getBoundingClientRect();const viewport=document.querySelector('.xterm-viewport');return viewport.getBoundingClientRect().right-screen.right>=Math.max(6,viewport.offsetWidth-viewport.clientWidth)-0.5;})()")
    assert js("document.getElementById('input')===null")
    row=js("(() => {const keys=document.getElementById('keys');const buttons=[...keys.querySelectorAll('button')];return {count:buttons.length,top:buttons.map(b=>b.getBoundingClientRect().top),fits:keys.scrollWidth<=keys.clientWidth+1};})()")
    assert row['count']==9 and max(row['top'])-min(row['top'])<1 and row['fits'],row
    js("window._testClipboard='cct mobile terminal · ready';Object.defineProperty(navigator.clipboard,'readText',{configurable:true,value:async()=>window._testClipboard})")
    js("(async()=>{document.getElementById('paste').click();await new Promise(r=>setTimeout(r,50));document.getElementById('enter').click();})()")
    time.sleep(.2)
    js("document.querySelector('.xterm-helper-textarea').focus()")
    cdp('Input.insertText',{'text':'DIRECT_TERMINAL_MARKER'})
    cdp('Input.dispatchKeyEvent',{'type':'keyDown','key':'Enter','code':'Enter','windowsVirtualKeyCode':13,'nativeVirtualKeyCode':13})
    cdp('Input.dispatchKeyEvent',{'type':'keyUp','key':'Enter','code':'Enter','windowsVirtualKeyCode':13,'nativeVirtualKeyCode':13})
    time.sleep(.2)
    text=subprocess.check_output(['tmux','-S',sock,'capture-pane','-p','-t','browser-test:0'],text=True)
    assert text.count('DIRECT_TERMINAL_MARKER')>=2,text
    js("document.getElementById('adapt').click()")
    wait("document.getElementById('size-note')?.textContent.includes('适配') && !document.getElementById('restore-size').hidden")
    wait("(() => {const screen=document.querySelector('.xterm-screen').getBoundingClientRect();const viewport=document.querySelector('.xterm-viewport').getBoundingClientRect();return viewport.right-screen.right>=0 && viewport.right-screen.right<=10 && screen.right<=document.querySelector('.terminal-frame').getBoundingClientRect().right-1;})()")
    geometry=js("(() => {const frame=document.querySelector('.terminal-frame').getBoundingClientRect();const screen=document.querySelector('.xterm-screen').getBoundingClientRect();const viewport=document.querySelector('.xterm-viewport').getBoundingClientRect();return {terminalRatio:frame.height/visualViewport.height,rightGutter:viewport.right-screen.right};})()")
    assert geometry['terminalRatio']>=0.70,geometry
    assert 0<=geometry['rightGutter']<=10,geometry
    assert js("document.querySelector('.terminal-scroll').hidden")
    assert js("document.getElementById('font-value').value")=='8px'
    original_cols=int(subprocess.check_output(['tmux','-S',sock,'display-message','-p','-t','browser-test:0','#{window_width}'],text=True))
    js("document.querySelector('.terminal-menu summary').click();document.getElementById('font-larger').click()")
    wait("document.getElementById('font-value').value==='9px'")
    for _ in range(100):
        actual_cols=int(subprocess.check_output(['tmux','-S',sock,'display-message','-p','-t','browser-test:0','#{window_width}'],text=True))
        if actual_cols<original_cols:break
        time.sleep(.1)
    else:raise AssertionError('Font change did not refit the adapted terminal')
    js("document.getElementById('font-smaller').click()")
    wait("document.getElementById('font-value').value==='8px'")
    js("document.getElementById('font-larger').click()")
    wait("document.getElementById('font-value').value==='9px'")
    js("document.querySelector('.terminal-menu summary').click()")
    wait("(() => {const screen=document.querySelector('.xterm-screen').getBoundingClientRect();const viewport=document.querySelector('.xterm-viewport').getBoundingClientRect();return viewport.right-screen.right>=0 && viewport.right-screen.right<=10;})()")
    screenshot=cdp('Page.captureScreenshot',{'format':'png','captureBeyondViewport':False})
    (root/'mobile.png').write_bytes(base64.b64decode(screenshot['data']))
    # Alternate-screen tmux cannot use xterm scrollback: exercise remote history.
    js("window._testClipboard=Array.from({length:160},(_,i)=>'SCROLL_LINE_'+i).join('\\n')+'\\n';document.getElementById('paste').click()")
    time.sleep(.5)
    js("window._scrollMessages=[];window._touchEvents=[];const send=WebSocket.prototype.send;WebSocket.prototype.send=function(data){if(typeof data==='string'&&JSON.parse(data).type==='scroll')window._scrollMessages.push(JSON.parse(data));return send.call(this,data);};for(const type of ['touchstart','touchmove','touchend'])document.addEventListener(type,e=>window._touchEvents.push({type,touches:e.touches.length}),true);")
    point=js("(() => {const rect=document.querySelector('.terminal-frame').getBoundingClientRect();return {x:rect.left+100,y:rect.top+100};})()")
    cdp('Input.dispatchTouchEvent',{'type':'touchStart','touchPoints':[{'x':point['x'],'y':point['y'],'id':1}]})
    cdp('Input.dispatchTouchEvent',{'type':'touchMove','touchPoints':[{'x':point['x'],'y':point['y']+85,'id':1}]})
    cdp('Input.dispatchTouchEvent',{'type':'touchEnd','touchPoints':[]})
    for _ in range(50):
        mode=subprocess.check_output(['tmux','-S',sock,'display-message','-p','-t','browser-test:0','#{pane_mode}'],text=True).strip()
        if mode=='copy-mode':break
        time.sleep(.1)
    else:raise AssertionError('Touch scrolling did not enter tmux history: '+str(js("({events:window._touchEvents,messages:window._scrollMessages,notice:document.getElementById('notice').textContent,mode:document.getElementById('mode').textContent,adapted:!document.getElementById('restore-size').hidden})")))
    assert js("document.querySelector('.terminal-scroll').hidden")
    for _ in range(30):
        cdp('Input.dispatchTouchEvent',{'type':'touchStart','touchPoints':[{'x':point['x'],'y':point['y']+85,'id':1}]})
        cdp('Input.dispatchTouchEvent',{'type':'touchMove','touchPoints':[{'x':point['x'],'y':point['y'],'id':1}]})
        cdp('Input.dispatchTouchEvent',{'type':'touchEnd','touchPoints':[]})
        time.sleep(.1)
        mode=subprocess.check_output(['tmux','-S',sock,'display-message','-p','-t','browser-test:0','#{pane_mode}'],text=True).strip()
        if not mode:break
    else:raise AssertionError('Direct touch scrolling down did not return to the live terminal')
    js("document.getElementById('window').value=document.getElementById('window').options[1].value;document.getElementById('window').dispatchEvent(new Event('change'))")
    wait("document.getElementById('window').selectedIndex===1 && document.getElementById('restore-size').hidden")
    assert not js("document.querySelector('.terminal-scroll').hidden")
    js("(async()=>{window._testClipboard='BROWSER_CCT_MARKER';document.getElementById('paste').click();await new Promise(r=>setTimeout(r,50));document.getElementById('enter').click();})()")
    time.sleep(.3)
    text=subprocess.check_output(['tmux','-S',sock,'capture-pane','-p','-t','browser-test:1'],text=True)
    assert 'BROWSER_CCT_MARKER' in text,text
    # Browser settings survive reload; this must not auto-enable shared resizing.
    cdp('Page.reload')
    wait("!!document.querySelector('#live button')")
    js("document.querySelector('#live button').click()")
    wait("document.getElementById('connection')?.textContent==='已连接' && !!document.getElementById('font-value')")
    assert js("document.getElementById('font-value').value")=='9px'
    js("document.querySelector('.terminal-menu summary').click();document.getElementById('font-reset').click()")
    wait("document.getElementById('font-value').value==='8px'")
    assert not errors,errors
    print(json.dumps({'result':'PASS: HTTPS login, mobile render, window selection, adaptation, native terminal typing, clipboard paste, one-row shortcuts, font preferences, direct touch history scrolling with adapted scrollbar hidden','screenshot':str(root/'mobile.png'),'terminal_notice':js("document.getElementById('notice').textContent"),'geometry':geometry},ensure_ascii=False))
    ws.close()
finally:
    connections.close()
    for proc in reversed(processes):
        proc.terminate()
        try:proc.wait(timeout=8)
        except subprocess.TimeoutExpired:proc.kill();proc.wait()
    subprocess.run(['tmux','-S',sock,'kill-server'],capture_output=True)
