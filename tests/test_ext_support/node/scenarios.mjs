import assert from 'node:assert/strict';
import PCPilotExtension from '../../../extension/pcpilot@local/extension.js';
import {Overlay} from '../../../extension/pcpilot@local/overlay.js';
import {Ops} from '../../../extension/pcpilot@local/ops.js';
import {Token} from '../../../extension/pcpilot@local/input.js';
import {clipboard} from 'gi://St';
import Shell from 'gi://Shell';
import {Gio} from 'gi://Gio';
import {planPath, fittsDuration, typingCadence} from '../../../extension/pcpilot@local/path.js';
import {glide} from '../../../extension/pcpilot@local/motion.js';
import Clutter, {timelines} from 'gi://Clutter';
import {runV2Scenarios} from './v2-scenarios.mjs';

let afterPaint = null;
const virtualEvents=[];
let pointerPos=[0,0];
export const cursorTracker={inhibits:0,visible:true,inhibit_cursor_visibility(){this.inhibits++},
 uninhibit_cursor_visibility(){this.inhibits--;cursorOrder.push('visible')},get_pointer_visible(){return this.inhibits===0},connect:()=>1,disconnect(){}};
export const cursorOrder=[];
export const seatUnfocus={n:0};
globalThis.global = {
 backend:{get_cursor_tracker:()=>cursorTracker},
 screen_width:1920,screen_height:1080,display:{focus_window:null,sort_windows_by_stacking:x=>x},
 get_window_actors:()=>[],get_pointer:()=>pointerPos,workspace_manager:{get_active_workspace_index:()=>0},
  stage:{key_focus:null,context:{get_backend:()=>({get_default_seat:()=>({inhibit_unfocus(){seatUnfocus.n++},uninhibit_unfocus(){seatUnfocus.n--;cursorOrder.push('unfocus')},create_virtual_device:()=>({notify_keyval:(...x)=>virtualEvents.push(['key',...x]),notify_button:(...x)=>virtualEvents.push(['button',...x]),notify_absolute_motion:(...x)=>virtualEvents.push(['move',...x]),notify_discrete_scroll(){}})})})},
  connect:(sig,cb)=>{afterPaint=cb;return 1},disconnect(){},queue_redraw(){},set_key_focus(){}}
};

// Pure path invariants: exact endpoints, bounded durations and a genuinely curved path.
const rand=()=>0.75;
const from=[100,100],to=[1400,800];
const human=planPath(from,to,'human',{rand});
assert.deepEqual(human.at(0),from); assert.deepEqual(human.at(human.duration),to);
assert.ok(human.duration>=180 && human.duration<=900);
const fast=planPath(from,to,'fast',{rand}); assert.ok(fast.duration>=100&&fast.duration<=180);
const spatialDeviation=path=>Math.max(...Array.from({length:99},(_,i)=>{
 const [x,y]=path.at(path.duration*(i+1)/100);
 return Math.abs((to[0]-from[0])*(from[1]-y)-(from[0]-x)*(to[1]-from[1]))/Math.hypot(to[0]-from[0],to[1]-from[1]);
}));
assert.ok(spatialDeviation(fast)<1e-8);assert.ok(spatialDeviation(human)>1);
assert.equal(planPath(from,to,'instant').duration,0);
assert.ok(fittsDuration(500,rand)>=180&&fittsDuration(500,rand)<=900);
assert.ok(typingCadence([...('a '.repeat(100))],4000,rand).reduce((a,b)=>a+b,0)<=4000);

// A cancelled glide stops its compositor timeline and emits no later pointer moves.
const glideToken=new Token();let moves=0;
const gp=glide({move(){moves++}},human,glideToken).catch(e=>e.code);
await new Promise(r=>setTimeout(r,45)); glideToken.cancel();
assert.equal(await gp,'inactive');const stoppedMoves=moves;
await new Promise(r=>setTimeout(r,35));assert.equal(moves,stoppedMoves);
assert.ok(timelines.at(-1).stopped);
const pathChecks=['path-endpoints','path-duration','path-curve','typing-cadence-cap','glide-cancel'];

async function liveExtension(session=1){
 const e=new PCPilotExtension();e.enable();const originalClient=e._client;e._client.stop();
 e._client={epoch:session,send(){},reconnect(){this.reconnected=true},stop(){}};
 e._triggerDisconnect=()=>originalClient._onDisconnected();
 e._overlay.show=async()=>{e._overlay.cursor.engage();return true};
 await e._setActive({active:true,session},session);
 assert.equal(e._active,true);assert.equal(e._overlay.cursor.engaged,true);
 return e;
}
function assertCursorReleased(e){
 const c=e._overlay.cursor;assert.equal(cursorTracker.inhibits,0);assert.equal(cursorTracker.get_pointer_visible(),true);
 assert.equal(seatUnfocus.n,0);
 assert.equal(c.engaged,false);assert.deepEqual(c._trail._points,[]);assert.equal(c._trail._timeline,null);
 assert.equal(e._input._keys.length,0);assert.equal(e._input._buttons.size,0);
}
async function prepareVisibleMotion(e){
 const c=e._overlay.cursor;pointerPos=[100,100];c.sync();pointerPos=[150,150];c.sync();
 assert.ok(c._trail._points.length>=2);
 e._input.key(97,true);e._input.button(1,true);
 let count=0;const notify=e._input._pointer.notify_absolute_motion.bind(e._input._pointer);
 e._input._pointer.notify_absolute_motion=(...args)=>{count++;notify(...args)};
 const work=glide(e._input,planPath([100,100],[900,700],'human',{duration:700,rand}),e._token).catch(err=>err.code);
 await new Promise(r=>setTimeout(r,40));
 return {work,count:()=>count,timeline:timelines.at(-1)};
}

// All state-inactive reasons synchronously restore real cursor, clear trail and release input.
const endReasons=['user_hotkey','user_release','idle_timeout','ui_disconnected'];
for(const reason of endReasons){
 const e=await liveExtension();const motion=await prepareVisibleMotion(e);const before=motion.count();
 e._setInactive(reason);assertCursorReleased(e);assert.ok(motion.timeline.stopped);
 await new Promise(r=>setTimeout(r,35));assert.equal(motion.count(),before);
 await motion.work; e.disable();
}
const safetyChecks=['cursor-inactive-reasons','cursor-connection-loss','cursor-disable','cursor-cleanup-exception','cursor-overlay-failures'];

// Connection loss and explicit disable share the same synchronous cursor-first cleanup.
{
 const e=await liveExtension(20);await prepareVisibleMotion(e);e._triggerDisconnect();assertCursorReleased(e);e.disable();
}
{
 const e=await liveExtension(21);await prepareVisibleMotion(e);e.disable();assert.equal(cursorTracker.get_pointer_visible(),true);
}

// Cleanup continues even when an earlier step throws; cursor release remains first.
{
 const e=await liveExtension(22);e._hotCornersId=1;e._restoreHotCorners=()=>{throw new Error('cleanup injected')};
 const old=console.error;console.error=()=>{};e._setInactive('user_hotkey');console.error=old;assertCursorReleased(e);e._restoreHotCorners=()=>{};e.disable();
}

// Overlay errors after acquiring cursor inhibition and paint timeout both roll back.
{
 const e=new PCPilotExtension();e.enable();e._client.stop();e._client={epoch:30,send(){},stop(){}};
 e._overlay.show=async()=>{e._overlay.cursor.engage();throw new Error('post-show failure')};
 e._onMessage({event:'state',active:true,session:30},30);await new Promise(r=>setTimeout(r,5));assertCursorReleased(e);e.disable();
}
{
 const e=new PCPilotExtension();e.enable();e._client.stop();e._client={epoch:31,send(){},stop(){}};
 e._overlay.show=async()=>{e._overlay.cursor.engage();return false};
 await e._setActive({active:true,session:31},31);assertCursorReleased(e);e.disable();
}

// Balanced inhibit is idempotent, including repeated release calls.
{
 const e=await liveExtension(40),c=e._overlay.cursor;const base=cursorTracker.inhibits;
 assert.equal(seatUnfocus.n,1);cursorOrder.length=0;
 c.release();assert.equal(cursorTracker.inhibits,base-1);assert.equal(seatUnfocus.n,0);
 assert.deepEqual(cursorOrder,['visible','unfocus']);
 c.engage();c.engage();assert.equal(cursorTracker.inhibits,base);assert.equal(seatUnfocus.n,1);
 c.release();assert.equal(cursorTracker.inhibits,base-1);assert.equal(seatUnfocus.n,0);
 c.release();assert.equal(cursorTracker.inhibits,base-1);assert.equal(seatUnfocus.n,0);
 e._setInactive('user_hotkey');e.disable();
}

// The trail keeps ticking after its first point, accumulates points, and self-stops after fade.
{
 const e=await liveExtension(41),c=e._overlay.cursor;pointerPos=[300,300];c.sync();
 assert.ok(c._trail._timeline);assert.equal(c._trail._points.length,1);
 pointerPos=[340,320];c.sync();assert.equal(c._trail._points.length,2);
 await new Promise(r=>setTimeout(r,470));assert.equal(c._trail._timeline,null);assert.equal(c._trail._points.length,0);
 e._setInactive('user_hotkey');e.disable();
}

// Wrapup aborts mid-close / mid-glide run the real _wrapupSteps: see v2-scenarios.mjs.

// The 8s wrapup hard cap returns incomplete, restores the real cursor and fades its arrow.
{
 const e=await liveExtension(43);e._ops._wrapupSteps=async(_a,_b,_c,token)=>token.sleep(12000);
 const result=await e._ops._wrapup({},e._token);
 assert.equal(result.incomplete,true);assert.equal(cursorTracker.get_pointer_visible(),true);
 assert.equal(e._overlay.cursor.engaged,false);assert.equal(seatUnfocus.n,0);e._setInactive('user_hotkey');e.disable();
}

safetyChecks.push('balanced-cursor-inhibit','trail-idle','wrapup-cap');

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
ext._overlay.cursor.engage();
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
assertCursorReleased(ext);
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

const v2Checks=await runV2Scenarios();
console.log(JSON.stringify({ok:true,checks:['op-session','paint-timeout','screenshot-cancel','typing-estimate','paste-cancel','clipboard-ownership','timeout-queue','timeout-fail-closed','stale-generation','overlay-exception','session-ack','session-ping-pong',...pathChecks,...safetyChecks,...v2Checks],bugs:pasteCancelBug?[pasteCancelBug]:[]}));
