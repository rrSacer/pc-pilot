import assert from 'node:assert/strict';
import GLib, {setTimeoutScale} from 'gi://GLib';
import Gio from 'gi://Gio';
import Shell from 'gi://Shell';
import Clutter from 'gi://Clutter';
import {Ops} from '../../../extension/pcpilot@local/ops.js';
import {Token} from '../../../extension/pcpilot@local/input.js';
import {clampToMonitors,onMonitor} from '../../../extension/pcpilot@local/windows.js';
import {planPath,typingCadence} from '../../../extension/pcpilot@local/path.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {seatUnfocus} from './scenarios.mjs';

export async function runV2Scenarios() {
 let wins=[],createdCb=null,winEvents=[],nextSignal=1,pointer=[100,100],nowOffset=0,workspace=0;
 const display={focus_window:null,sort_windows_by_stacking:a=>a,
  connect(_sig,cb){createdCb=cb;return nextSignal++},disconnect(){createdCb=null}};
 global.display=display;
 global.create_app_launch_context=()=>({});
 global.get_window_actors=()=>wins.map(meta_window=>({meta_window}));
 global.get_pointer=()=>pointer;
 global.get_current_time=()=>1;
 global.workspace_manager={get_active_workspace_index:()=>workspace,get_workspace_by_index:()=>({activate(){workspace=0}})};
 global.screen_width=1920;global.screen_height=1080;
 const fakeApps=[{x:0,y:0,width:1000,height:800,index:0},{x:1200,y:0,width:1000,height:800,index:1}];
 Main.layoutManager.monitors=fakeApps;
 const resetWorld=(initial=[])=>{wins=[...initial];winEvents=[];display.focus_window=initial.find(w=>w.focused)??initial[0]??null;pointer=[100,100];workspace=0;};
 const add=w=>{wins.push(w);createdCb?.(display,w)};
 class FakeWindow {
  constructor(id,{x=100,y=100,width=400,height=300,title=`Window ${id}`,parent=null,focused=false,appId=`app.${id}`,pid=id,minimized=false,maximized=false,fullscreen=false,workspaceIndex=0}={}){
   Object.assign(this,{id,x,y,width,height,title,parent,focused,minimized,maximized,fullscreen,deleted:false,appId,pid,onDelete:null,workspaceIndex});
  }
  get_id(){return this.id} get_title(){return this.title} get_gtk_application_id(){return this.appId} get_wm_class(){return ''} get_pid(){return this.pid}
  get_compositor_private(){return this}
  get_frame_rect(){return {x:this.x,y:this.y,width:this.width,height:this.height}} get_buffer_rect(){return this.get_frame_rect()}
  has_focus(){return display.focus_window===this} is_override_redirect(){return false} get_window_type(){return 0}
  is_on_all_workspaces(){return false} get_workspace(){return {index:()=>this.workspaceIndex}} is_maximized(){return this.maximized} is_fullscreen(){return this.fullscreen}
  find_root_ancestor(){let w=this;while(w.parent)w=w.parent;return w}
  allows_move(){return true} allows_resize(){return true} get_work_area_current_monitor(){return {x:0,y:0,width:1000,height:800}}
  unmaximize(){this.maximized=false} maximize(){this.maximized=true} unmake_fullscreen(){this.fullscreen=false} make_fullscreen(){this.fullscreen=true}
  unminimize(){this.minimized=false} minimize(){this.minimized=true} activate(){display.focus_window=this;}
  move_resize_frame(_user,x,y,width,height){winEvents.push(['move_resize_frame',this.id,x,y,width,height]);Object.assign(this,{x,y,width,height})}
  delete(){winEvents.push(['delete',this.id]);this.onDelete?.(this);if(!this.onDelete){this.deleted=true;wins=wins.filter(x=>x!==this)}}
 }
 const makeOps=()=>{
  const input={_keys:[],_buttons:new Set(),events:[],position:()=>pointer,
   move(x,y){this.events.push(['move',x,y,Date.now()]);pointer=[x,y]},
   button(b,down){this.events.push(['button',b,down,Date.now()]);down?this._buttons.add(b):this._buttons.delete(b)},
   key(k,down){this.events.push(['key',k,down,Date.now()]);down?this._keys.push(k):this._keys.splice(this._keys.indexOf(k),1)},scroll(){}};
  const ops=new Ops({overlay:{cursor:{release(){winEvents.push(['cursor_release'])},ripple(){},scrollHint(){}},engageCursor(){}},input});
  ops._activate=async w=>{winEvents.push(['activate',w.get_id(),Date.now()]);display.focus_window=w;workspace=w.workspaceIndex;};
  ops._glideTo=async(x,y,_style,token)=>{token.check();input.move(x,y)};
  return {ops,input};
 };
 const createSession=(initial=[])=>{resetWorld(initial);const env=makeOps();env.ops.session.snapshot();return env};
 const created=(id,options)=>new FakeWindow(id,options);
 const procStat=(pid,comm,ppid,start)=>{
  const fields=Array(20).fill('0');fields[0]='S';fields[1]=String(ppid);fields[19]=String(start);
  return `${pid} (${comm}) ${fields.join(' ')}`;
 };
 const runSteps=async(ops,args={})=>{const result={closed:[],restored:[],left_open:[],focused:null};await ops._wrapupSteps(new Set((args.keep??[]).map(Number)),args.close_new!==false,args.return_to??'origin',new Token(),result);return result};

 // The pid callback must read the spawned child's identity before a reused pid
 // can appear, and every callback path must install a watcher that reaps it.
 {
  const {ops}=createSession([]);const info=new Gio.DesktopAppInfo(501,true);
  let callback=null,passedFlags=null;
  info.launch_uris_as_manager=(_uris,_ctx,flags,_childSetup,cb)=>{
   passedFlags=flags;
   // Model the reviewer's launcher: without DO_NOT_REAP_CHILD, exit and PID
   // reuse happen before the callback; with it, the zombie reserves the PID.
   GLib.procStats[501]=procStat(501,'spawned zombie',1,1000);
   if(!(flags&GLib.SpawnFlags.DO_NOT_REAP_CHILD))
    GLib.procStats[501]=procStat(501,'reused stranger',1,2000);
   callback=cb;
  };
  ops._spawn(info,[]);
  assert.equal(passedFlags&GLib.SpawnFlags.SEARCH_PATH,GLib.SpawnFlags.SEARCH_PATH);
  assert.equal(passedFlags&GLib.SpawnFlags.DO_NOT_REAP_CHILD,GLib.SpawnFlags.DO_NOT_REAP_CHILD);
  callback(info,501);
  assert.ok(ops.session._spawned.has('501:1000'));
  assert.equal(ops.session._spawned.has('501:2000'),false);
  const watch=GLib.childWatches.at(-1);assert.equal(watch.pid,501);
  assert.equal(GLib.childWatches.filter(w=>w.pid===501).length,1);
  watch.callback(501,0);assert.equal(GLib.closedPids.at(-1),501);
  // Once that process is gone, the stranger reusing its PID is never ours.
  GLib.procStats[501]=procStat(501,'reused stranger',1,2000);
  const stranger=created(44,{appId:'app.stranger',pid:501});add(stranger);
  assert.equal(ops.session.isAuthored(stranger),false);
  const result=await runSteps(ops);assert.deepEqual(winEvents.filter(e=>e[0]==='delete'),[]);
  assert.equal(result.left_open.find(w=>w.id===44)?.reason,'not_authored');

  // Even if identity reading fails, or the session has already been cleared,
  // the child watcher is still installed exactly once for that spawned PID.
  const priorWatches=GLib.childWatches.length,priorCloses=GLib.closedPids.length;
  const unreadable=new Gio.DesktopAppInfo(502,true);let unreadableCallback;
  unreadable.launch_uris_as_manager=(_u,_c,flags,_x,cb)=>{assert.ok(flags&GLib.SpawnFlags.DO_NOT_REAP_CHILD);unreadableCallback=cb};
  ops._spawn(unreadable,[]);unreadableCallback(unreadable,502);
  assert.equal(GLib.childWatches.length,priorWatches+1);assert.equal(GLib.childWatches.at(-1).pid,502);
  assert.equal(GLib.childWatches.filter(w=>w.pid===502).length,1);
  GLib.childWatches.at(-1).callback(502,0);assert.equal(GLib.closedPids.length,priorCloses+1);
  assert.equal(ops.session._spawned.has('502:0'),false);

  let clearedCallback;const cleared=new Gio.DesktopAppInfo(503,true);
  cleared.launch_uris_as_manager=(_u,_c,flags,_x,cb)=>{assert.ok(flags&GLib.SpawnFlags.DO_NOT_REAP_CHILD);clearedCallback=cb};
  ops._spawn(cleared,[]);ops.session.clear();
  const beforeClearWatch=GLib.childWatches.length;clearedCallback(cleared,503);
  assert.equal(GLib.childWatches.length,beforeClearWatch+1);assert.equal(GLib.childWatches.at(-1).pid,503);
  assert.equal(GLib.childWatches.filter(w=>w.pid===503).length,1);
  GLib.childWatches.at(-1).callback(503,0);assert.equal(GLib.closedPids.at(-1),503);
  assert.equal(ops.session._spawned.size,0);
  delete GLib.procStats[501];
 }

 // RR1: time and an overlapping AI operation never authorize a window.
 {
  const {ops}=createSession([created(1,{focused:true,appId:'app.focus',pid:100})]);
  let finishMove;ops._glideTo=()=>new Promise(r=>finishMove=r);
  const moving=ops.run('pointer.move',{x:700,y:500,style:'human'},new Token());
  while(!finishMove)await new Promise(r=>setTimeout(r,1));
  add(created(2,{appId:'app.other',pid:200})); // unrelated app during a real pointer op
  finishMove();await moving;ops._glideTo=async(x,y,_style,token)=>{token.check();pointer=[x,y]};
  add(created(3,{appId:'window:3',pid:100})); // same pid as focus, but not spawned by us
  let appWindows=[];
  const app={get_id:()=> 'app.launched',get_name:()=> 'Launched',get_windows:()=>appWindows,
   get_app_info:()=>new Gio.DesktopAppInfo(500)};
  const appWindow=created(4,{appId:'app.launched',pid:500});
  GLib.procStats[500]=procStat(500,'launched app',1,5000);
  ops._resolveApp=()=>app;
  const productionSpawn=Ops.prototype._spawn;
  ops._spawn=(info,uris)=>{const pids=productionSpawn.call(ops,info,uris);add(appWindow);return pids};
  const launch=await ops._appLaunch({app:'launched',wait_ms:1000},new Token());
  assert.equal(launch.window.id,4); // matched by the DesktopAppInfo pid callback
  const family=created(5,{appId:'window:5',pid:500});add(family);
  assert.equal(ops.session.isAuthored(family),true); // live spawned process ancestry
  const sameIdIndependent=created(6,{appId:'app.launched',pid:999});add(sameIdIndependent);
  delete GLib.procStats[500];
  assert.equal(ops.session.isAuthored(appWindow),true); // explicit result authoring is keyed by window id
  assert.equal(ops.session.isAuthored(family),false); // no stale process-tree positive
  const result=await runSteps(ops);
  assert.deepEqual(winEvents.filter(e=>e[0]==='delete').map(e=>e[1]).sort((a,b)=>a-b),[4]);
  for(const id of [2,3])assert.equal(result.left_open.find(w=>w.id===id)?.reason,'not_authored');
  assert.equal(result.left_open.find(w=>w.id===5)?.reason,'not_authored');
  assert.equal(result.left_open.find(w=>w.id===6)?.reason,'not_authored');
  assert.equal(ops.session.isAuthored(sameIdIndependent),false);
  const listed=ops._windowList().windows;
  assert.equal(listed.find(w=>w.id===3).ai,false);assert.equal(listed.find(w=>w.id===5).ai,false);
 }
 // A running app's URI launch authors only its returned window; a mismatched
 // window that appears while app.launch waits is not a fallback candidate.
 {
  const existing=created(20,{appId:'app.running',pid:700});const {ops}=createSession([existing]);
  const appWindows=[existing];let returned,other,sameTracker;
  const app={get_id:()=> 'app.running',get_name:()=> 'Running',get_windows:()=>appWindows,
   get_app_info:()=>new Gio.DesktopAppInfo(701)};
  GLib.procStats[701]=procStat(701,'launched URI process',1,7010);
  ops._resolveApp=()=>app;
  ops._spawn=(info,uris)=>{const pids=Ops.prototype._spawn.call(ops,info,uris);
   sameTracker=created(26,{appId:'app.running',pid:700});returned=created(21,{appId:'app.running',pid:701});other=created(22,{appId:'app.foreign',pid:702});
   appWindows.push(sameTracker,returned);add(sameTracker);add(returned);add(other);return pids};
  const reply=await ops._appLaunch({app:'running',uris:['file:///tmp/document.txt'],wait_ms:500},new Token());
  assert.equal(reply.window.id,21);assert.equal(ops.session.isAuthored(returned),true);
  assert.equal(ops.session.isAuthored(other),false);assert.equal(ops.session.isAuthored(sameTracker),false);
  const r=await runSteps(ops);assert.deepEqual(winEvents.filter(e=>e[0]==='delete').map(e=>e[1]),[21]);
  assert.equal(r.left_open.find(w=>w.id===22)?.reason,'not_authored');
  assert.equal(r.left_open.find(w=>w.id===26)?.reason,'not_authored');
  delete GLib.procStats[701];
 }
 {
  const {ops}=createSession([]);let stray;
  const appWindows=[];const app={get_id:()=> 'app.dbus',get_name:()=> 'D-Bus app',get_windows:()=>appWindows,
   get_app_info:()=>new Gio.DesktopAppInfo(0,false)};
  ops._resolveApp=()=>app;
  ops._spawn=(info,uris)=>{const pids=Ops.prototype._spawn.call(ops,info,uris);stray=created(23,{appId:'app.dbus',pid:0});appWindows.push(stray);add(stray);return pids};
  const reply=await ops._appLaunch({app:'dbus',wait_ms:250},new Token());
  assert.equal(reply.window.id,23);assert.equal(ops.session.isAuthored(stray),false);
  const r=await runSteps(ops);assert.deepEqual(winEvents.filter(e=>e[0]==='delete'),[]);
  assert.equal(r.left_open.find(w=>w.id===23)?.reason,'not_authored');
 }
 // Tracker matching can return a window, but a spawned pid is the only authority.
 {
  const {ops}=createSession([]);let tracked;
  const appWindows=[];const app={get_id:()=> 'app.expected',get_name:()=> 'Expected',get_windows:()=>appWindows,
   get_app_info:()=>new Gio.DesktopAppInfo(801)};
  ops._resolveApp=()=>app;
  ops._spawn=(info,uris)=>{const pids=Ops.prototype._spawn.call(ops,info,uris);tracked=created(27,{appId:'app.expected',pid:802});appWindows.push(tracked);add(tracked);return pids};
  const reply=await ops._appLaunch({app:'expected',wait_ms:250},new Token());
  assert.equal(reply.window.id,27);assert.equal(ops.session.isAuthored(tracked),false);
  const r=await runSteps(ops);assert.deepEqual(winEvents.filter(e=>e[0]==='delete'),[]);
  assert.equal(r.left_open.find(w=>w.id===27)?.reason,'not_authored');
 }
 {
  const {ops}=createSession([]);let stray;
  ops._resolveApp=()=>({get_id:()=> 'app.expected',get_name:()=> 'Expected',get_windows:()=>[],get_app_info:()=>new Gio.DesktopAppInfo(810)});
  ops._spawn=(info,uris)=>{const pids=Ops.prototype._spawn.call(ops,info,uris);stray=created(28,{appId:'app.unrelated',pid:811});add(stray);return pids};
  const reply=await ops._appLaunch({app:'expected',wait_ms:250},new Token());
  assert.equal(reply.window,undefined);assert.equal(ops.session.isAuthored(stray),false);
 }
 // open_uri authors the handler window only when it starts that handler.
 {
  const {ops}=createSession([]);const handler=new Gio.DesktopAppInfo(901);
  GLib.procStats[901]=procStat(901,'URI handler',1,9010);
  handler.get_id=()=> 'app.uri-handler';const handlerApp={get_windows:()=>[]};
  Shell.AppSystem.get_default=()=>({lookup_app:()=>handlerApp});ops._defaultHandler=async()=>handler;
  let opened;ops._spawn=(info,uris)=>{const pids=Ops.prototype._spawn.call(ops,info,uris);opened=created(24,{appId:'window:24',pid:901});add(opened);return pids};
  const reply=await ops._openUri({uri:'https://example.invalid'},new Token());
  assert.equal(reply.window.id,24);assert.equal(ops.session.isAuthored(opened),true);
  delete GLib.procStats[901];
 }
 {
  const existing=created(25,{appId:'app.running-uri',pid:902});const {ops}=createSession([existing]);
  const handler=new Gio.DesktopAppInfo(903);handler.get_id=()=> 'app.running-uri';
  Shell.AppSystem.get_default=()=>({lookup_app:()=>({get_windows:()=>[existing]})});
  ops._defaultHandler=async()=>handler;let spawned=false;ops._spawn=()=>{spawned=true;return []};
  assert.deepEqual(await ops._openUri({uri:'https://example.invalid'},new Token()),{});assert.equal(spawned,false);
 }
 // Process identity is PID + start time; reuse, reparenting, or an orphan cannot
 // inherit authorship from a process that is no longer its live ancestor.
 {
  // A recycled pid is a different process, even if its new child uses that pid.
  {
   const {ops}=createSession([created(29,{focused:true})]);
   GLib.procStats[500]=procStat(500,'spawned root',1,1000);ops.session.launchedPid(500);
   delete GLib.procStats[500];GLib.procStats[500]=procStat(500,'reused root',1,2000);
   GLib.procStats[750]=procStat(750,'child of reused root',500,3000);
   const reused=created(32,{appId:'app.reused',pid:500}),child=created(33,{appId:'app.reused',pid:750});add(reused);add(child);
   assert.equal(ops.session.spawnedByUs(500),false);assert.equal(ops.session.isAuthored(reused),false);
   assert.equal(ops.session.isAuthored(child),false);
   const result=await runSteps(ops);assert.deepEqual(winEvents.filter(e=>e[0]==='delete'),[]);
   assert.equal(result.left_open.find(w=>w.id===32)?.reason,'not_authored');
   assert.equal(result.left_open.find(w=>w.id===33)?.reason,'not_authored');
   for(const pid of [500,750])delete GLib.procStats[pid];
  }
  // A once-authored descendant stops being ours as soon as it leaves the chain.
  {
   const {ops}=createSession([created(34,{focused:true})]);
   GLib.procStats[500]=procStat(500,'spawned root',1,1000);ops.session.launchedPid(500);
   GLib.procStats[650]=procStat(650,'comm with ) (parens',500,2000);
   GLib.procStats[999]=procStat(999,'other parent',1,1800);
   const moved=created(35,{appId:'same.application',pid:650});add(moved);
   assert.equal(ops.session.isAuthored(moved),true);
   GLib.procStats[650]=procStat(650,'comm with ) (parens',999,2000);
   assert.equal(ops.session.isAuthored(moved),false);
   const result=await runSteps(ops);assert.deepEqual(winEvents.filter(e=>e[0]==='delete'),[]);
   assert.equal(result.left_open.find(w=>w.id===35)?.reason,'not_authored');
   for(const pid of [500,650,999])delete GLib.procStats[pid];
  }
  // An orphaned child no longer belongs to an exited spawned root.
  {
   const {ops}=createSession([created(36,{focused:true})]);
   GLib.procStats[500]=procStat(500,'spawned root',1,1000);ops.session.launchedPid(500);
   GLib.procStats[650]=procStat(650,'orphan child',500,2000);
   const orphan=created(37,{appId:'same.application',pid:650});add(orphan);
   assert.equal(ops.session.isAuthored(orphan),true);
   delete GLib.procStats[500];GLib.procStats[650]=procStat(650,'orphan child',1,2000);
   assert.equal(ops.session.isAuthored(orphan),false);
   const result=await runSteps(ops);assert.deepEqual(winEvents.filter(e=>e[0]==='delete'),[]);
   assert.equal(result.left_open.find(w=>w.id===37)?.reason,'not_authored');
   delete GLib.procStats[650];
  }
  // Live root and descendant work, while unreadable processes are never captured.
  {
   const {ops}=createSession([created(38,{focused:true})]);
   GLib.procStats[600]=procStat(600,'root',1,1100);ops.session.launchedPid(600);
   GLib.procStats[640]=procStat(640,'middle',600,1200);
   GLib.procStats[650]=procStat(650,'comm containing ) ( characters',640,1300);
   GLib.procStats[651]=procStat(651,'unrelated',777,1400);
   const child=created(39,{appId:'same.application',pid:650}),unrelated=created(40,{appId:'same.application',pid:651});
   add(child);add(unrelated);
   assert.equal(ops.session.spawnedByUs(650),true);assert.equal(ops.session.isAuthored(child),true);
   assert.equal(ops.session.isAuthored(unrelated),false);
   ops.session.launchedPid(700);assert.equal(ops.session._spawned.size,1);
   const unreadable=created(41,{appId:'same.application',pid:700});add(unreadable);
   assert.equal(ops.session.isAuthored(unreadable),false);
   const result=await runSteps(ops);
   assert.deepEqual(winEvents.filter(e=>e[0]==='delete').map(e=>e[1]),[39]);
   assert.equal(result.left_open.find(w=>w.id===40)?.reason,'not_authored');
   assert.equal(result.left_open.find(w=>w.id===41)?.reason,'not_authored');
   for(const pid of [600,640,650,651])delete GLib.procStats[pid];
  }
  // Explicit authoring is by window id and remains valid if its process exits.
  {
   const {ops}=createSession([created(42,{focused:true})]);
   GLib.procStats[850]=procStat(850,'returned app',1,1500);ops.session.launchedPid(850);
   const returned=created(43,{appId:'window:43',pid:850});add(returned);ops.session.author(returned);
   delete GLib.procStats[850];assert.equal(ops.session.isAuthored(returned),true);
   const result=await runSteps(ops);assert.deepEqual(result.closed.map(w=>w.id),[43]);
  }
 }
 // Cancellation during handler lookup or just before a launch must have no side effect.
 {
  const {ops}=createSession([]);let lookupStarted=false,lookupCancelled=false,spawns=0;
  Gio.AppInfo.defaultCalls=[];
  Gio.File.handlerLookup=(_uri,cancellable)=>new Promise((_resolve,reject)=>{
   lookupStarted=true;
   cancellable.connect('cancelled',()=>{lookupCancelled=true;reject(new Error('cancelled'))})});
  ops._spawn=()=>{spawns++;return []};
  const token=new Token();const call=ops._openUri({uri:'file:///tmp/x'},token).catch(e=>e.code);
  while(!lookupStarted)await new Promise(r=>setTimeout(r,1));token.cancel();
  assert.equal(await call,'inactive');assert.equal(lookupCancelled,true);assert.equal(spawns,0);assert.equal(Gio.AppInfo.defaultCalls.length,0);
  Gio.File.handlerLookup=null;
 }
 {
  const {ops}=createSession([]);let resolveHandler,spawns=0;
  Gio.AppInfo.schemeHandler=()=>new Promise(r=>resolveHandler=r);Gio.AppInfo.defaultCalls=[];
  const token=new Token();const call=ops._openUri({uri:'https://example.invalid'},token).catch(e=>e.code);
  while(!resolveHandler)await new Promise(r=>setTimeout(r,1));token.cancel();resolveHandler(null);
  assert.equal(await call,'inactive');assert.equal(spawns,0);assert.equal(Gio.AppInfo.defaultCalls.length,0);
  Gio.AppInfo.schemeHandler=null;
 }
 {
  const {ops}=createSession([]);let launches=0;
  ops._resolveApp=()=>({get_id:()=> 'app.cancel',get_name:()=> 'Cancel',get_windows:()=>[],get_app_info:()=>new Gio.DesktopAppInfo(1001)});
  ops._spawn=(info,uris)=>{launches++;return Ops.prototype._spawn.call(ops,info,uris)};const before=new Token();before.cancel();
  await assert.rejects(ops._appLaunch({app:'cancel'},before),e=>e.code==='inactive');assert.equal(launches,0);
  const waiting=new Token();const p=ops._appLaunch({app:'cancel',wait_ms:1000},waiting).catch(e=>e.code);
  while(launches===0)await new Promise(r=>setTimeout(r,1));waiting.cancel();assert.equal(await p,'inactive');
  assert.equal(ops.session._authored.size,0);
 }
 // R2: keep and pre-existing roots protect every transient in that family.
 {
  const base=created(8,{focused:true});const parent=created(6),dialog=created(7,{parent,title:'Dialog'}),preDialog=created(9,{parent:base,title:'Preexisting dialog'});
  const {ops}=createSession([base]);for(const w of [parent,dialog,preDialog]){add(w);ops.session.author(w)}
  let plan=ops._closePlan(new Set([7]),true);
  assert.equal(plan.find(([w])=>w.id===7)[1],'kept');assert.equal(plan.find(([w])=>w.id===6)[1],'family_protected');
  assert.equal(plan.find(([w])=>w.id===9)[1],'family_protected');
  assert.deepEqual(winEvents.filter(e=>e[0]==='delete'),[]);
  plan=ops._closePlan(new Set([6]),true);assert.equal(plan.find(([w])=>w.id===7)[1],'family_protected');
 }
 // R3: refusing windows and wrapup-born dialogs are marked before delete and never retried.
 {
  const {ops}=createSession([created(1,{focused:true})]);const refusing=created(4);let dialog;
  refusing.onDelete=()=>{};
  add(refusing);ops.session.author(refusing);
  const one=await runSteps(ops);assert.deepEqual(winEvents.filter(e=>e[0]==='delete').map(e=>e[1]),[4]);
  assert.equal(one.left_open.find(w=>w.id===4)?.reason,'not_closed');
  // A slow "save?" dialog appears after the original close wait has expired.
  dialog=created(5,{title:'Save?',parent:refusing});add(dialog);
  winEvents=[];const two=await runSteps(ops);assert.deepEqual(winEvents.filter(e=>e[0]==='delete'),[]);
  assert.equal(two.left_open.find(w=>w.id===4)?.reason,'not_closed');assert.equal(two.left_open.find(w=>w.id===5)?.reason,'not_closed');
 }
 // R4: journal only restores still-current AI-applied properties.
 {
  const w3=created(3,{x:100,maximized:true}),w8=created(8,{x:100,focused:true}),w9=created(9,{x:100}),w10=created(10,{maximized:true}),w11=created(11);
  const {ops}=createSession([w3,w8,w9,w10,w11]);w3.x=777;w10.x=333;
  assert.deepEqual(await ops._windowAction({id:3,action:'fit'},new Token()),{changed:false,x:777,y:100,width:400,height:300});
  await ops._windowAction({id:10,action:'maximize'},new Token()); // already maximized: no mutation recorded
  await assert.rejects(ops._windowAction({id:10,action:'resize',width:10},new Token()),e=>e.code==='bad_args');
  await ops._windowAction({id:11,action:'unmaximize'},new Token()); // already unmaximized: no mutation recorded
  const token=new Token();await ops._windowAction({id:8,action:'move',x:300,y:150,style:'instant'},token);
  await ops._windowAction({id:9,action:'move',x:300,y:150,style:'instant'},token);w9.x=777;
  const rest=ops.session.restorable();assert.deepEqual(rest.map(([w])=>w.id),[8]);
  winEvents=[];const r=await runSteps(ops);assert.deepEqual(r.restored,[8]);
  assert.equal(w8.x,100);assert.equal(w3.x,777);assert.equal(w9.x,777);assert.equal(w10.x,333);
  assert.deepEqual(winEvents.filter(e=>e[0]==='move_resize_frame').map(e=>e[1]),[8]);
  const env2=createSession([created(11,{focused:true}),created(10,{workspaceIndex:1})]);
  await env2.ops._windowAction({id:10,action:'activate'},new Token());assert.equal(workspace,1);
  await runSteps(env2.ops);assert.equal(workspace,0);
  const env3=createSession([created(13,{focused:true}),created(12,{workspaceIndex:1})]);
  await env3.ops._windowAction({id:12,action:'activate'},new Token());workspace=2;
  env3.ops._activate=async w=>{display.focus_window=w};
  await runSteps(env3.ops);assert.equal(workspace,2);
 }
 // R5: requested hold duration survives capped glides and zero-distance drags; cancel releases now.
 {
  setTimeoutScale(1);
  const {ops,input}=makeOps();const token=new Token();
  const p=ops._pointerDrag({from:{x:100,y:100},to:{x:800,y:700},style:'human',duration_ms:5000},token);
  await p;let edges=input.events.filter(e=>e[0]==='button'&&e[1]===1);
  assert.ok(edges[1][3]-edges[0][3]>=5000,edges);
  const zero=makeOps();await zero.ops._pointerDrag({from:{x:100,y:100},to:{x:100,y:100},style:'fast',duration_ms:250},new Token());
  edges=zero.input.events.filter(e=>e[0]==='button'&&e[1]===1);assert.ok(edges[1][3]-edges[0][3]>=250,edges);
  const fast=makeOps();await fast.ops._pointerDrag({from:{x:100,y:100},to:{x:500,y:400},style:'fast',duration_ms:400},new Token());
  edges=fast.input.events.filter(e=>e[0]==='button'&&e[1]===1);assert.ok(edges[1][3]-edges[0][3]>=400,edges);
  const abort=makeOps(),t=new Token();const work=abort.ops._pointerDrag({from:{x:100,y:100},to:{x:900,y:700},style:'human',duration_ms:5000},t).catch(e=>e.code);
  await new Promise(r=>setTimeout(r,1600));const press=abort.input.events.find(e=>e[0]==='button'&&e[2]);assert.ok(press);
  t.cancel();assert.equal(await work,'inactive');const release=abort.input.events.filter(e=>e[0]==='button'&&!e[2]).at(-1);assert.ok(Date.now()-release[3]<100);
  setTimeoutScale(0.1);
 }
 // R6: randomized integer cadence is bounded for long inputs and rounding boundaries.
 {
  for(const n of [44,45,63,64,6000,20000])for(const value of [0.01,0.5,0.75,0.99]){
   const out=typingCadence(Array(n).fill('a'),4000,()=>value);
   assert.ok(out.every(Number.isInteger));assert.ok(out.reduce((a,b)=>a+b,0)<=4000,`n=${n}, rand=${value}`);
  }
 }
 // Monitor-gap target is invalid; every clamped path point remains on a real monitor.
 {
  const {ops}=makeOps();
  assert.equal(onMonitor(1100,300),false);
  const clamped=clampToMonitors(1100,300);assert.ok(onMonitor(...clamped));
  assert.deepEqual(clamped,[1200,300]);
  await assert.rejects(ops._pointerMove({x:1100,y:300,style:'instant'},new Token()),e=>e.code==='bad_args');
  ops._glideTo=Ops.prototype._glideTo;
  const points=[];ops._input.move=(x,y)=>{points.push([x,y]);pointer=[x,y]};
  await ops._glideTo(1250,650,'human',new Token());
  assert.ok(points.length>0&&points.every(p=>onMonitor(...p)));
 }
 // R7: real _wrapupSteps aborts while waiting for close and during its return glide.
 {
  const {ops,input}=createSession([created(50,{focused:true})]);const refusing=created(51);refusing.onDelete=()=>{};
  add(refusing);ops.session.author(refusing);const token=new Token();
  const p=ops._wrapupSteps(new Set(),true,'origin',token,{closed:[],restored:[],left_open:[],focused:null}).catch(e=>e.code);
  while(!winEvents.some(e=>e[0]==='delete'))await new Promise(r=>setTimeout(r,2));
  token.cancel();assert.equal(await p,'inactive');const n=winEvents.length;await new Promise(r=>setTimeout(r,40));assert.equal(winEvents.length,n);
  assert.deepEqual(winEvents.filter(e=>['activate','move_resize_frame'].includes(e[0])),[]);
 }
 {
  const {ops,input}=createSession([]);pointer=[1700,700];const token=new Token();
  ops._glideTo=Ops.prototype._glideTo;
  const p=ops._wrapupSteps(new Set(),true,'origin',token,{closed:[],restored:[],left_open:[],focused:null}).catch(e=>e.code);
  const until=Date.now()+600;while(!input.events.some(e=>e[0]==='move')&&Date.now()<until)await new Promise(r=>setTimeout(r,5));
  assert.ok(input.events.some(e=>e[0]==='move'));token.cancel();assert.equal(await p,'inactive');
  const moves=input.events.filter(e=>e[0]==='move').length,events=winEvents.length;await new Promise(r=>setTimeout(r,60));
  assert.equal(input.events.filter(e=>e[0]==='move').length,moves);assert.equal(winEvents.length,events);
 }
 // Cap behavior also traverses real close/focus/return steps, rather than a substituted sleep.
 {
  const {ops}=createSession([created(60,{focused:true})]);ops.session.clear();pointer=[100,100];ops.session.snapshot();pointer=[1700,700];
  const refusing=created(61);refusing.onDelete=()=>{};add(refusing);ops.session.author(refusing);
  const token=new Token();ops._overlay.cursor={release(){winEvents.push(['release'])}};
  setTimeoutScale(0.001);const result=await ops._wrapup({},token);setTimeoutScale(0.1);assert.equal(result.incomplete,true);
  assert.ok(winEvents.some(e=>e[0]==='delete'&&e[1]===61));assert.ok(winEvents.some(e=>e[0]==='release'));
 }
 // R7: measure spatial perpendicular deviation, not timing deviation along a straight line.
 {
  const a=[100,100],b=[1400,800],len=Math.hypot(b[0]-a[0],b[1]-a[1]);
  const deviation=path=>Math.max(...Array.from({length:99},(_,i)=>{const [x,y]=path.at(path.duration*(i+1)/100);return Math.abs((b[0]-a[0])*(a[1]-y)-(a[0]-x)*(b[1]-a[1]))/len}));
  assert.ok(deviation(planPath(a,b,'fast'))<1e-8);
  assert.ok(deviation(planPath(a,b,'human',{rand:()=>0.75}))>1);
 }
 return ['r1-authorship','rr-spawned-pid-ancestry','rr-cancelled-launches','r2-transient-families','r3-idempotent-refusal','r4-journal-conflicts','r5-drag-hold','r6-cadence-budget','monitor-clamp','r7-real-wrapup-aborts','r7-real-cap','path-perpendicular','spawn-reserved-pid-reaped'];
}
