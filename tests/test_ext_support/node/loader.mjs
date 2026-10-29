const names = new Map([
  ['gi://GLib', 'glib'], ['gi://Gio', 'gio'], ['gi://Clutter', 'clutter'],
  ['gi://St', 'st'], ['gi://Shell', 'shell'], ['gi://Meta', 'meta'],
  ['gi://cairo', 'cairo'], ['gi://GObject', 'gobject'],
  ['resource:///org/gnome/shell/extensions/extension.js', 'extension'],
  ['resource:///org/gnome/shell/ui/main.js', 'main'],
  ['resource:///org/gnome/shell/ui/status/keyboard.js', 'keyboard'],
]);
export async function resolve(specifier, context, nextResolve) {
  if (names.has(specifier)) return {url: `mock:${names.get(specifier)}`, shortCircuit: true};
  return nextResolve(specifier, context);
}

const actor = `class Actor {
 constructor(p={}){Object.assign(this,{visible:true,children:[],style:'',opacity:255},p);this.clutter_text={set_markup:s=>{this.markup=s}};}
 add_child(c){this.children.push(c);c.parent=this} remove_child(c){this.children=this.children.filter(x=>x!==c);c.parent=null}
 get_parent(){return this.parent||null} set(p){Object.assign(this,p)} set_style(s){this.style=s}
 set_child_above_sibling(){} destroy(){this.destroyed=true} is_mapped(){return this.visible}
 ease(p={}){Object.assign(this,p);p.onStopped?.(true)} remove_all_transitions(){} connect(){return 1} disconnect(){}
 set_position(x,y){this.x=x;this.y=y} set_size(w,h){this.width=w;this.height=h} set_pivot_point(){} queue_repaint(){} }
class Label extends Actor{} class Widget extends Actor{} class BoxLayout extends Actor{} class Icon extends Actor{}
class DrawingArea extends Actor{constructor(...a){super();this._init(...a)} _init(p={}){Object.assign(this,p)}};
`;
const sources = {
 glib: `let clockOffset=0, timeoutScale=0.1; const procStats={}; export function advanceClock(ms){clockOffset+=ms*1000} export function setTimeoutScale(v){timeoutScale=v}
 export default {
 PRIORITY_DEFAULT:0,PRIORITY_LOW:1,SOURCE_REMOVE:false,SOURCE_CONTINUE:true,SpawnFlags:{SEARCH_PATH:1},
 getenv:()=>null,build_filenamev:a=>a.join('/'),get_real_time:()=>Date.now()*1000,
 advanceClock, setTimeoutScale,procStats,
 file_get_contents(path){const pid=Number(path.match(/\\/(\\d+)\\/stat$/)?.[1]);if(!(pid in procStats))return [false,new Uint8Array()];return [true,new TextEncoder().encode(procStats[pid])]},
 markup_escape_text:s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;'),uri_peek_scheme:s=>String(s).split(':',1)[0]||null,
 timeout_add(_p,ms,cb){let id=setTimeout(()=>cb(),Math.max(1,Math.ceil(ms*timeoutScale))); timers.set(id,id);return id},
 idle_add(_p,cb){let id=setTimeout(cb,1);timers.set(id,id);return id},source_remove(id){clearTimeout(id)},
 get_monotonic_time:()=>Date.now()*1000+clockOffset}; export const timers=new Map();`,
 gio:`class Cancellable{constructor(){this.cancelled=false;this.handlers=[]}cancel(){this.cancelled=true;this.handlers.forEach(f=>f())}is_cancelled(){return !!this.cancelled}connect(_s,cb){this.handlers.push(cb);return this.handlers.length}}
 class File{static records=[];static handlerLookup=null; constructor(path){this.path=path;this.deleted=false;File.records.push(this)}
  async query_default_handler_async(_priority,cancellable){if(File.handlerLookup)return File.handlerLookup(this.path,cancellable);return null}
  async replace_async(){await new Promise(r=>setTimeout(r,10));return {close_async:async()=>{},read_async:async()=>({read_bytes_async:async()=>({toArray:()=>Array(24).fill(0)}),close(){}})}}
  async delete_async(){this.deleted=true}}
 class SocketClient{} class DataInputStream{} class OutputStream{} class InputStream{} class AppInfo{static defaultCalls=[];static schemeHandler=null;
  static async get_default_for_uri_scheme(s){return AppInfo.schemeHandler?.(s)??null}
  static async launch_default_for_uri_async(uri,_ctx,cancellable){AppInfo.defaultCalls.push({uri,cancellable})}}
 class DesktopAppInfo extends AppInfo{constructor(pid=0,emitPid=true){super();this.pid=pid;this.emitPid=emitPid}
  launch_uris_as_manager(_u,_c,_f,_x,cb){if(this.emitPid)cb(this,this.pid)} launch_uris(){}}
 const Gio={Cancellable,File,SocketClient,DataInputStream,OutputStream,InputStream,AppInfo,DesktopAppInfo,
 SpawnFlags:{SEARCH_PATH:1},_promisify(){},FileCreateFlags:{PRIVATE:0},IOErrorEnum:{CANCELLED:1}};
 Gio.File.new_for_path=p=>new File(p);Gio.File.new_for_uri=u=>new File(u); export {Gio}; export default Gio;`,
 clutter:`const timelines=[]; const C={BUTTON_PRIMARY:1,BUTTON_MIDDLE:2,BUTTON_SECONDARY:3,ButtonState:{PRESSED:1,RELEASED:0},
 InputDeviceType:{POINTER_DEVICE:1,KEYBOARD_DEVICE:2},KeyState:{PRESSED:1,RELEASED:0},ScrollSource:{WHEEL:0},
 Orientation:{VERTICAL:1},ActorAlign:{CENTER:1,START:2},AnimationMode:{EASE_IN_OUT_SINE:0,EASE_OUT_QUAD:1},BinLayout:class {},
 ScrollDirection:{UP:0,DOWN:1,LEFT:2,RIGHT:3},
 Timeline:class {constructor(p={}){this.duration=p.duration??0;this.handlers={};this.t0=0;this.id=0;this.stopped=false;timelines.push(this)}
  connect(sig,cb){(this.handlers[sig]??=[]).push(cb);return 1} get_elapsed_time(){return Date.now()-this.t0}
  start(){this.t0=Date.now();this.id=setInterval(()=>{(this.handlers['new-frame']??[]).forEach(f=>f());
   if(this.duration>0&&Date.now()-this.t0>=this.duration){this.stop();(this.handlers.completed??[]).forEach(f=>f())}},16)}
  stop(){clearInterval(this.id);this.id=0;this.stopped=true}},
 KEY_Control_L:0xffe3,KEY_Shift_L:0xffe1,KEY_v:118,KEY_Return:13};
 export {timelines}; export default C;`,
 st:`${actor} const St={Widget,Label,BoxLayout,Icon,DrawingArea,ClipboardType:{CLIPBOARD:0},
 ThemeContext:{get_for_stage:()=>({scale_factor:1})},Clipboard:{get_default:()=>clipboard}};
 const clipboard={text:'ORIGINAL',writes:[],slow:false,userCopy:null,get_text(_t,cb){setTimeout(()=>cb(null,this.text),this.slow?100:0)},
 get_content(_t,_m,cb){setTimeout(()=>cb(null,null),0)},get_mimetypes:()=>[],set_text(_t,v){this.text=v;this.writes.push(v)},set_content(){}};
 export {clipboard}; export default St;`,
 shell:`class Screenshot{static calls=0; async screenshot(){Screenshot.calls++} async screenshot_area(){Screenshot.calls++}}
 const Shell={Screenshot,WindowTracker:{get_default:()=>({get_window_app:()=>null})},AppSystem:{get_default:()=>({})}}; export default Shell;`,
 meta:`export default {WindowType:{NORMAL:0,DIALOG:1,MODAL_DIALOG:2,UTILITY:3}};`,
 cairo:`export default {LineJoin:{ROUND:1},LineCap:{ROUND:1},LinearGradient:class {addColorStopRGBA(){}}};`,
 gobject:`export default {registerClass:(a,b)=>typeof a==='function'?a:b};`,
 extension:`export class Extension {}`, keyboard:`export function getInputSourceManager(){return {currentSource:{xkbId:'us'}}};`,
 main:`export const layoutManager={monitors:[{x:0,y:0,width:1920,height:1080,index:0}],primaryIndex:0,primaryMonitor:{x:0,y:0,width:1920,height:1080},
 hotCorners:[],panelBox:{height:24},connect:()=>1,disconnect(){},addTopChrome(a){a.parent=this},
 set_child_above_sibling(){}}; export const overview={visible:false,animationInProgress:false}; export const inputMethod={currentFocus:null,commit(){}}; export let modalCount=0;`
};
export async function load(url, context, nextLoad) {
 if (!url.startsWith('mock:')) return nextLoad(url,context);
 return {format:'module',source:sources[url.slice(5)],shortCircuit:true};
}
