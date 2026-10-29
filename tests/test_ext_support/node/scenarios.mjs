import assert from 'node:assert/strict';
import PCPilotExtension from '../../../extension/pcpilot@local/extension.js';
import {Overlay} from '../../../extension/pcpilot@local/overlay.js';
import {Ops} from '../../../extension/pcpilot@local/ops.js';
import {Token} from '../../../extension/pcpilot@local/input.js';
import {clipboard} from 'gi://St';
import Shell from 'gi://Shell';
import {Gio} from 'gi://Gio';

let afterPaint = null;
globalThis.global = {
 screen_width:1920,screen_height:1080,display:{focus_window:null,sort_windows_by_stacking:x=>x},
 get_window_actors:()=>[],get_pointer:()=>[0,0],
 stage:{key_focus:null,context:{get_backend:()=>({get_default_seat:()=>({create_virtual_device:()=>({notify_keyval(){},notify_button(){},notify_absolute_motion(){},notify_discrete_scroll(){}})})})},
  connect:(sig,cb)=>{afterPaint=cb;return 1},disconnect(){},queue_redraw(){},set_key_focus(){}}
};

// A real paint is the only success path; a timeout from a stage that never paints fails closed.
const overlay = new Overlay();
const originalQueue=global.stage.queue_redraw;
global.stage.queue_redraw=()=>{};
assert.equal(await overlay.show('test','hotkey'),false);
global.stage.queue_redraw=()=>{queueMicrotask(()=>afterPaint?.())};
assert.equal(await overlay.show('test','hotkey'),true);
overlay.destroy();

const ops = new Ops({overlay:{waitPaint:async()=>{},withHidden:fn=>fn()},input:{tap(){},chord(){}}});
ops._fixKeyFocus=()=>false;
ops._typeableSet=()=>({layout:'us',extra:'',unsafe:''});
const token=new Token();
await assert.rejects(ops._keyType({text:'y'.repeat(20),delay_ms:1000},token),e=>e.code==='bad_args' && /take/.test(e.message));

// A screenshot cancelled during waitPaint must neither call the compositor nor keep its file.
const shotToken=new Token(); let finishPaint;
ops._shotDir=async()=>'/tmp/unit';
ops._overlay={waitPaint:()=>new Promise(r=>{finishPaint=r}),withHidden:fn=>fn()};
const callsBefore=Shell.Screenshot.calls;
const shot=ops._screenshot({},shotToken).catch(e=>e.code);
await new Promise(r=>setTimeout(r,20)); shotToken.cancel(); finishPaint();
assert.equal(await shot,'inactive'); assert.equal(Shell.Screenshot.calls,callsBefore);
assert.equal(Gio.File.records.at(-1).deleted,true);

// Clipboard read pending at cancellation must not allow the later clipboard write.
clipboard.writes.length=0; clipboard.slow=true;
const pasteToken=new Token();
const paste=ops._paste('AI TEXT',pasteToken).catch(e=>e.code);
let pasteCancelBug=null;
setTimeout(()=>{try{pasteToken.cancel()}catch(e){pasteCancelBug=e.name+': '+e.message}},10);
assert.equal(await paste,'inactive');
assert.deepEqual(clipboard.writes,[]);
clipboard.slow=false;

// If the user replaces our clipboard selection during paste settle, do not restore stale text.
clipboard.text='OLD USER CLIPBOARD'; clipboard.writes.length=0;
ops._input={chord(){setTimeout(()=>{clipboard.text='USER COPY';},10)}};
await ops._paste('AI TEXT',new Token());
assert.equal(clipboard.text,'USER COPY');

// Active request queue: timeout cancels op child and does not start next until it settles.
const ext=new PCPilotExtension(); ext.enable();
ext._client.stop();
const sent=[]; let reconnects=0;
ext._client={epoch:7,send:(m,e)=>sent.push({m,e}),reconnect:()=>reconnects++,stop(){}};
ext._gen=1; ext._active=true; ext._sessionUp=true; ext._token=new Token();
let opStarted=0, cancelSeen=false, release;
ext._ops={run(op,_args,t){opStarted++; if(op==='hold')return new Promise(resolve=>{release=resolve;t.onCancel(()=>{cancelSeen=true;resolve({done:true})})}); return {};}};
ext._onMessage({id:1,op:'hold',args:{}},7);
ext._onMessage({id:2,op:'later',args:{}},7);
await new Promise(r=>setTimeout(r,1850));
assert.equal(cancelSeen,true); assert.equal(opStarted,2);
assert.ok(sent.some(x=>x.m.id===1 && !x.m.ok && x.m.error.code==='failed'));
assert.ok(sent.some(x=>x.m.id===2 && x.m.ok));

// If an op ignores cancellation, the grace timeout fails closed and keeps the queue held.
let finishIgnored; const priorStart=opStarted;
ext._active=true; ext._sessionUp=true; ext._gen++; ext._token=new Token();
ext._ops={run(op){opStarted++; if(op==='ignore')return new Promise(r=>{finishIgnored=r}); return {ran:true}}};
ext._onMessage({id:10,op:'ignore'},7); ext._onMessage({id:11,op:'after-ignore'},7);
await new Promise(r=>setTimeout(r,2050));
assert.equal(opStarted,priorStart+1); assert.ok(reconnects>0); assert.equal(ext._active,false);
finishIgnored({}); await new Promise(r=>setTimeout(r,5));
assert.ok(sent.some(x=>x.m.id===11 && x.m.error?.code==='inactive'));

// Requests queued before inactive belong to the old generation even after reactivation.
let permit;
ext._active=true; ext._sessionUp=true; ext._gen++; ext._token=new Token();
ext._ops={run(op){if(op==='slow')return new Promise(r=>{permit=r}); return {ran:true}}};
ext._onMessage({id:3,op:'slow'},7); ext._onMessage({id:4,op:'stale'},7);
await new Promise(r=>setTimeout(r,1));
ext._client.epoch=8;
ext._setInactive('user_hotkey');
ext._active=true; ext._sessionUp=true; ext._gen++; ext._token=new Token();
assert.ok(sent.some(x=>x.m.id===4 && x.m.error?.code==='inactive'));
assert.equal(sent.find(x=>x.m.id===4).e,7);
permit?.({}); await new Promise(r=>setTimeout(r,5));
assert.ok(!sent.some(x=>x.m.id===4 && x.m.result?.ran));
ext.disable();

// A rejected overlay show is caught by the event path and rolls back to inactive.
const bad=new PCPilotExtension(); bad.enable(); bad._client.stop();
const errors=[]; const oldError=console.error; console.error=(...x)=>errors.push(x.join(' '));
bad._client={epoch:1,send(){},stop(){}}; bad._overlay.show=async()=>{throw new Error('paint exploded')};
bad._onMessage({event:'state',active:true},1);
await new Promise(r=>setTimeout(r,10)); console.error=oldError;
assert.equal(bad._active,false); assert.equal(bad._sessionUp,false); assert.ok(errors.some(x=>x.includes('activation failed')));
bad.disable();

// An unpainted activation must roll back and never ack.
const unpainted=new PCPilotExtension(); unpainted.enable(); unpainted._client.stop();
const acks=[]; unpainted._client={epoch:2,send:m=>acks.push(m),stop(){}};
unpainted._overlay.show=async()=>false;
await unpainted._setActive({active:true},2);
assert.equal(unpainted._active,false); assert.equal(acks.some(x=>x.event==='state_ack'),false);
unpainted.disable();

const ackExt=new PCPilotExtension(); ackExt.enable(); ackExt._client.stop();
const ackWire=[]; ackExt._client={epoch:3,send:(m,e)=>ackWire.push({m,e}),stop(){}};
ackExt._overlay.show=async()=>true;
await ackExt._setActive({active:true,session:77},3);
assert.deepEqual(ackWire[0],{m:{event:'state_ack',active:true,session:77},e:3});
ackExt.disable();

// Session-scoped health lease: only matching active session receives an immediate pong.
const lease=new PCPilotExtension(); lease.enable(); lease._client.stop();
const wire=[]; lease._client={epoch:9,send:(m,e)=>wire.push({m,e}),stop(){}};
lease._active=true; lease._session=51; lease._overlay._active=true;
lease._onMessage({event:'ping',session:50},9);
lease._onMessage({event:'ping',session:51},9);
assert.equal(wire.filter(x=>x.m.event==='pong').length,1);
assert.deepEqual(wire.find(x=>x.m.event==='pong'),{m:{event:'pong',session:51},e:9});
lease._active=false; lease._onMessage({event:'ping',session:51},9);
assert.equal(wire.filter(x=>x.m.event==='pong').length,1);
lease._active=true; lease._session=52; lease._overlay._active=true;
lease._ops={run(){return new Promise(r=>setTimeout(()=>r({}),100))}};
lease._onMessage({id:99,op:'busy'},9); lease._onMessage({event:'ping',session:52},9);
assert.equal(wire.filter(x=>x.m.event==='pong').at(-1).m.session,52);
lease.disable();

// Ops carry the daemon's session id; one for another session is refused without running.
const sess=new PCPilotExtension(); sess.enable(); sess._client.stop();
const sw=[]; sess._client={epoch:4,send:(m,e)=>sw.push(m),stop(){}};
sess._active=true; sess._session=5; sess._gen=1; sess._token=new Token(); sess._sessionUp=true;
let sessRan=0; sess._ops={run(){sessRan++; return {};}};
sess._onMessage({id:1,op:'x',session:4},4); sess._onMessage({id:2,op:'x',session:5},4);
await new Promise(r=>setTimeout(r,10));
assert.equal(sessRan,1); assert.equal(sw.find(m=>m.id===1).error.code,'inactive'); assert.equal(sw.find(m=>m.id===2).ok,true);
sess.disable();

console.log(JSON.stringify({ok:true,checks:['op-session','paint-timeout','screenshot-cancel','typing-estimate','paste-cancel','clipboard-ownership','timeout-queue','timeout-fail-closed','stale-generation','overlay-exception','session-ack','session-ping-pong'],bugs:pasteCancelBug?[pasteCancelBug]:[]}));
