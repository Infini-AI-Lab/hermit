#!/usr/bin/env node
'use strict';
// Host-owned observational capture: no model calls and no aesthetic score.
const fs = require('node:fs'), fsp = fs.promises, path = require('node:path');
const http = require('node:http'), crypto = require('node:crypto');
const {performance} = require('node:perf_hooks');
// Playwright, pngjs and Three.js come from runtime/node_modules (npm ci); Three.js is served as /vendor/. The browser is
// the installed Google Chrome (Playwright channel 'chrome') unless --chrome or VISUAL_CHROME names an executable.
const ROOT = __dirname;
const hash = value => crypto.createHash('sha256').update(value).digest('hex');
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const inside = (root, child) => child === root || child.startsWith(root + path.sep);
function args(argv) {
  const out = {vendor:path.join(ROOT,'node_modules/three/build/three.module.min.js'),chrome:process.env.VISUAL_CHROME||null};
  for(let i=0;i<argv.length;i++) {
    if(argv[i]==='--help'){out.help=true;continue;}
    if(argv[i]==='--record'){out.record=true;continue;}
    const name=argv[i].replace(/^--/,'');
    if(!['workspace','output','case','vendor','chrome'].includes(name)||!argv[i+1])throw new Error('Unknown/incomplete option: '+argv[i]);
    out[name]=argv[++i];
  }
  if(out.help)return out;
  if(!out.workspace||!out.output||!['waves','lava','silk'].includes(out.case))throw new Error('--workspace, --output and --case waves|lava|silk are required');
  for(const k of ['workspace','output','vendor'])out[k]=path.resolve(out[k]);
  if(out.chrome)out.chrome=path.resolve(out.chrome);
  return out;
}
function deadline(promise, ms, label) {
  let timer;
  return Promise.race([promise,new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error(label+' timed out')),ms);})]).finally(()=>clearTimeout(timer));
}
function cappedPush(list, value) {if(list.length<100)list.push(value);}
async function writeJSON(file, value) {await fsp.writeFile(file,JSON.stringify(value,null,2)+'\n');}

async function filesForCapture(opt) {
  const workspace=await fsp.realpath(opt.workspace), file=path.join(workspace,'index.html');
  const stat=await fsp.lstat(file);
  if(!stat.isFile()||stat.isSymbolicLink()||await fsp.realpath(file)!==file)throw new Error('index.html must be a regular file, not a symlink');
  if(stat.size>8*1024*1024)throw new Error('index.html exceeds 8 MiB');
  const index=await fsp.readFile(file), files=new Map([['/index.html',{bytes:index,type:'text/html; charset=utf-8',sha256:hash(index)}]]);
  let vendor=null;
  if(fs.existsSync(opt.vendor)) {
    const real=await fsp.realpath(opt.vendor);
    if(real!==opt.vendor||(await fsp.lstat(real)).isSymbolicLink())throw new Error('Host vendor must not be a symlink');
    const bytes=await fsp.readFile(real);
    if(bytes.length>8*1024*1024)throw new Error('Host vendor exceeds 8 MiB');
    vendor={file:real,sha256:hash(bytes),bytes:bytes.length,dependencies:[]};
    files.set('/vendor/three.module.min.js',{bytes,type:'text/javascript',sha256:vendor.sha256});
    const coreFile=path.join(path.dirname(real),'three.core.min.js');
    if(fs.existsSync(coreFile)) {
      if(await fsp.realpath(coreFile)!==coreFile||!(await fsp.lstat(coreFile)).isFile())throw new Error('Host Three core must be a regular file');
      const core=await fsp.readFile(coreFile);if(core.length>8*1024*1024)throw new Error('Host Three core exceeds 8 MiB');
      const entry={file:coreFile,sha256:hash(core),bytes:core.length};vendor.dependencies.push(entry);
      files.set('/vendor/three.core.min.js',{bytes:core,type:'text/javascript',sha256:entry.sha256});
    }
  }
  return {workspace,file,files,indexSHA:hash(index),vendor};
}
async function serve(files) {
  const audit=[];
  const server=http.createServer((req,res)=>{
    let pathname;
    try {pathname=decodeURIComponent(String(req.url).split('?')[0]);}catch{res.writeHead(400);return res.end();}
    if(pathname==='/')pathname='/index.html';
    const selected=files.get(pathname);
    const permitted=['GET','HEAD'].includes(req.method)&&selected&&!pathname.includes('\0');
    cappedPush(audit,{path:pathname,status:permitted?200:404});
    if(!permitted){res.writeHead(404);return res.end('Not in capture allowlist');}
    res.writeHead(200,{'Content-Type':selected.type,'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer',
      'Content-Security-Policy':"default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src data:; connect-src 'self'; media-src data: blob:; worker-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'"});
    res.end(req.method==='HEAD'?'':selected.bytes);
  });
  await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(0,'127.0.0.1',resolve);});
  return {origin:'http://127.0.0.1:'+server.address().port,audit,close:()=>new Promise(resolve=>{server.closeAllConnections();server.close(resolve);})};
}
function difference(PNG, first, second) {
  const a=PNG.sync.read(first),b=PNG.sync.read(second);
  if(a.width!==b.width||a.height!==b.height)return {comparable:false,reason:'scene dimensions changed',first:[a.width,a.height],second:[b.width,b.height]};
  let changed=0,total=0,max=0;
  for(let i=0;i<a.data.length;i+=4) {
    let pixelMax=0;
    for(let c=0;c<3;c++){const delta=Math.abs(a.data[i+c]-b.data[i+c]);total+=delta;pixelMax=Math.max(pixelMax,delta);max=Math.max(max,delta);}
    if(pixelMax>2)changed++;
  }
  return {comparable:true,width:a.width,height:a.height,changed_pixel_fraction:changed/(a.width*a.height),mean_absolute_rgb_delta:total/(a.width*a.height*3),max_channel_delta:max,
    rule:'fraction of scene pixels with at least one RGB channel changing by more than 2/255; not a quality score'};
}

async function run(opt) {
  if(fs.existsSync(opt.output))throw new Error('Output directory must be new');
  if(inside(opt.workspace,opt.output)||inside(opt.output,opt.workspace))throw new Error('Output and actor workspace must not overlap');
  await fsp.mkdir(opt.output,{recursive:true});
  const started=performance.now();
  const summary={schema_version:1,case:opt.case,started_utc:new Date().toISOString(),status:'capturing',capture_ok:false,model_calls:0,
    method:{desktop:{width:1280,height:800},phone:{width:390,height:844},device_scale_factor:1,requested_desktop_seconds:[0,2,5],
      time_origin:'after ready observation and two browser animation frames; actual elapsed times recorded, not deterministic simulation checkpoints',
      allowed_files:['index.html','vendor/three.module.min.js','vendor/three.core.min.js (both host frozen dependencies)'],external_requests:'blocked by route plus page CSP',
      reduced_motion:'no-preference to observe the requested animation',interaction:'pause, wait, reset, set intensity to 75% of declared range, run two seconds, capture, keyboard ArrowLeft, restore 75%, phone capture',
      no_score:true},screenshots:[],diagnostic_images:[],observations:{},console_errors:[],page_errors:[],blocked_requests:[],csp_violations:[],dialogs:[],capture_errors:[],
    limitations:['Frame differences indicate visible change, not physical correctness, beauty or semantic fidelity.',
      'Browser requestAnimationFrame intervals are scheduling observations, not proof of application render FPS or GPU throughput.',
      'getState and DOM control states are application self reports; independent screenshots are retained separately.',
      'This is restricted browser serving and network routing, not a general-purpose hostile-code sandbox.']};
  let browserServer,browser,context,host, frozen, watchdog, video;
  const killBrowser=()=>{const child=browserServer?.process();if(child&&child.exitCode===null)child.kill('SIGKILL');};
  const interrupted=()=>{killBrowser();process.exit(143);};
  process.once('SIGTERM',interrupted);process.once('SIGINT',interrupted);
  watchdog=setTimeout(()=>{killBrowser();},43000);
  async function body() {
    frozen=await filesForCapture(opt);
    summary.input={index_sha256:frozen.indexSHA,index_bytes:frozen.files.get('/index.html').bytes.length,vendor:frozen.vendor};
    host=await serve(frozen.files);
    const {chromium}=require('playwright'),{PNG}=require('pngjs');
    browserServer=await chromium.launchServer({...(opt.chrome?{executablePath:opt.chrome}:{channel:'chrome'}),headless:true,timeout:15000,args:['--force-webrtc-ip-handling-policy=disable_non_proxied_udp']});
    browser=await chromium.connect(browserServer.wsEndpoint());
    context=await browser.newContext({viewport:summary.method.desktop,deviceScaleFactor:1,reducedMotion:'no-preference',serviceWorkers:'block',acceptDownloads:false,
      ...(opt.record?{recordVideo:{dir:opt.output,size:summary.method.desktop}}:{})});
    await context.route('**/*',route=>{
      const url=route.request().url();let permitted=false;
      try {const parsed=new URL(url);permitted=parsed.origin===host.origin&&['/','/index.html','/vendor/three.module.min.js','/vendor/three.core.min.js'].includes(parsed.pathname);}catch{}
      if(permitted)return route.continue();
      cappedPush(summary.blocked_requests,{url:url.slice(0,500),resource_type:route.request().resourceType()});return route.abort('blockedbyclient');
    });
    await context.routeWebSocket('**/*',ws=>{cappedPush(summary.blocked_requests,{url:ws.url().slice(0,500),resource_type:'websocket'});ws.close();});
    await context.addInitScript(()=>{
      const samples=[],violations=[];let previous=null;
      function frame(now){if(previous!==null&&samples.length<10000)samples.push(now-previous);previous=now;requestAnimationFrame(frame);}requestAnimationFrame(frame);
      document.addEventListener('securitypolicyviolation',e=>{if(violations.length<100)violations.push({directive:e.effectiveDirective,blocked_uri:e.blockedURI});});
      Object.defineProperty(window,'__HOST_OBSERVE__',{value:()=>({intervals:samples.slice(),violations:violations.slice()}),writable:false,configurable:false});
    });
    const page=await context.newPage();page.setDefaultTimeout(3500);video=page.video();
    context.on('page',popup=>{if(popup!==page){cappedPush(summary.blocked_requests,{url:popup.url(),resource_type:'popup'});popup.close().catch(()=>{});}});
    page.on('console',message=>{if(message.type()==='error')cappedPush(summary.console_errors,message.text().slice(0,2000));});
    page.on('pageerror',error=>cappedPush(summary.page_errors,String(error).slice(0,2000)));
    page.on('dialog',dialog=>{cappedPush(summary.dialogs,{type:dialog.type(),message:dialog.message().slice(0,500)});dialog.dismiss().catch(()=>{});});
    await page.goto(host.origin+'/index.html',{waitUntil:'load',timeout:10000});
    async function state(){return page.evaluate(()=>{
      try {const s=window.__SHOWCASE__?.getState?.();if(!s||typeof s!=='object')return {available:false};
        const result={available:true};for(const k of ['ready','paused','simTime','intensity','resetCount'])result[k]=typeof s[k]==='boolean'||typeof s[k]==='number'||typeof s[k]==='string'?s[k]:null;return result;
      }catch(e){return {available:false,error:String(e).slice(0,500)};}
    });}
    try {await page.waitForFunction(()=>window.__SHOWCASE__?.getState?.().ready===true,null,{timeout:4000});summary.observations.ready_contract=true;}
    catch {summary.observations.ready_contract=false;}
    await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
    const clockOrigin=performance.now();
    async function sceneBuffer(){
      const scene=page.locator('[data-testid="scene"]');
      if(await scene.count()===1&&await scene.isVisible())return {bytes:await scene.screenshot({animations:'allow',timeout:4000}),scope:'scene'};
      return {bytes:await page.screenshot({animations:'allow',timeout:4000}),scope:'viewport_fallback_includes_controls'};
    }
    async function capture(name,label,target=null) {
      await page.evaluate(()=>{document.documentElement.scrollTop=0;document.body.scrollTop=0;});
      const elapsed=(performance.now()-clockOrigin)/1000;
      const screenshot=await page.screenshot({path:path.join(opt.output,name+'.png'),animations:'allow',timeout:5000});
      const record={id:name,label,file:name+'.png',sha256:hash(screenshot),bytes:screenshot.length,elapsed_seconds:elapsed,target_seconds:target,viewport:page.viewportSize(),state:await state()};
      summary.screenshots.push(record);return record;
    }
    const sceneFrames=[];
    for(const seconds of [0,2,5]) {
      await sleep(Math.max(0,clockOrigin+seconds*1000-performance.now()));
      await capture('desktop-t'+seconds,'Desktop, requested t≈'+seconds+' seconds',seconds);sceneFrames.push(await sceneBuffer());
    }
    summary.observations.animation_deltas=[{from:'desktop-t0',to:'desktop-t2',scope:sceneFrames[0].scope,...difference(PNG,sceneFrames[0].bytes,sceneFrames[1].bytes)},
      {from:'desktop-t2',to:'desktop-t5',scope:sceneFrames[1].scope,...difference(PNG,sceneFrames[1].bytes,sceneFrames[2].bytes)}];
    summary.observations.controls=await page.evaluate(()=>Object.fromEntries(['pause','reset','intensity','scene'].map(name=>{
      const all=document.querySelectorAll('[data-testid="'+name+'"]'),e=all[0];return [name,e?{count:all.length,tag:e.tagName,type:e.getAttribute('type'),aria_pressed:e.getAttribute('aria-pressed'),min:e.getAttribute('min'),max:e.getAttribute('max'),step:e.getAttribute('step'),value:e.value??null}:null];
    })));
    async function interaction(name,fn){try {await fn();}catch(error){summary.observations[name]={error:String(error).slice(0,2000)};}}
    await interaction('pause',async()=>{
      const control=page.locator('[data-testid="pause"]');await control.click();await sleep(150);
      const beforeState=await state(),before=await sceneBuffer();await sleep(500);const after=await sceneBuffer(),afterState=await state();
      await fsp.writeFile(path.join(opt.output,'diagnostic-paused.png'),after.bytes);
      summary.diagnostic_images.push({file:'diagnostic-paused.png',label:'Actual scene after pause interval',sha256:hash(after.bytes)});
      summary.observations.pause={before:beforeState,after:afterState,aria_pressed:await control.getAttribute('aria-pressed'),wait_ms:500,scope:before.scope,...difference(PNG,before.bytes,after.bytes)};
    });
    await interaction('reset',async()=>{
      const before=await state();await page.locator('[data-testid="reset"]').click();const after=await state();
      summary.observations.reset={before,after,pause_aria_pressed:await page.locator('[data-testid="pause"]').getAttribute('aria-pressed')};
    });
    await interaction('intensity',async()=>{
      const input=page.locator('[data-testid="intensity"]');
      const changed=await input.evaluate(e=>{
        if(e.tagName!=='INPUT'||e.type!=='range')throw new Error('Intensity must be a native range input');
        const min=Number(e.min||0),max=Number(e.max||100),step=e.step==='any'?null:Number(e.step||1);
        if(!Number.isFinite(min)||!Number.isFinite(max)||max<=min)throw new Error('Invalid intensity limits');
        let target=min+.75*(max-min);if(step&&step>0)target=min+Math.round((target-min)/step)*step;target=Math.max(min,Math.min(max,target));
        const before=e.value;e.value=target;e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));return {before,requested_value:target,dom_value:e.value,min,max,step};
      });
      summary.observations.intensity={...changed,state:await state()};
    });
    await sleep(2000);await capture('desktop-interaction','About two seconds after reset, intensity set to 75% of its range');
    await interaction('keyboard',async()=>{
      const input=page.locator('[data-testid="intensity"]'),before=await input.inputValue();
      await input.focus();await page.keyboard.press('ArrowLeft');const after=await input.inputValue();
      await input.evaluate((e,value)=>{e.value=value;e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));},before);
      summary.observations.keyboard={key:'ArrowLeft',before,after,restored:await input.inputValue()};
    });
    async function layout(){return page.evaluate(()=>({viewport:{width:innerWidth,height:innerHeight},document:{width:document.documentElement.scrollWidth,height:document.documentElement.scrollHeight},
      controls:['pause','reset','intensity'].map(name=>{const e=document.querySelector('[data-testid="'+name+'"]');if(!e)return {name,present:false};const r=e.getBoundingClientRect();return {name,present:true,x:r.x,y:r.y,width:r.width,height:r.height,inside_viewport:r.left>=0&&r.right<=innerWidth&&r.top>=0&&r.bottom<=innerHeight};})}));}
    summary.observations.desktop_layout=await layout();
    const desktopSchedule=await page.evaluate(()=>window.__HOST_OBSERVE__());
    await page.setViewportSize(summary.method.phone);await sleep(700);
    await capture('phone','Phone viewport after the same interaction');summary.observations.phone_layout=await layout();
    const finalSchedule=await page.evaluate(()=>window.__HOST_OBSERVE__());summary.csp_violations=finalSchedule.violations;
    function frameStats(values){const sorted=values.filter(Number.isFinite).sort((a,b)=>a-b);return {samples:sorted.length,median_ms:sorted.length?sorted[Math.floor(sorted.length*.5)]:null,p95_ms:sorted.length?sorted[Math.floor(sorted.length*.95)]:null,max_ms:sorted.length?sorted.at(-1):null,intervals_over_50ms:sorted.filter(v=>v>50).length,measurement:'host requestAnimationFrame cadence; includes screenshots/interactions and is not application FPS'};}
    summary.observations.browser_frame_intervals={desktop:frameStats(desktopSchedule.intervals),whole_capture:frameStats(finalSchedule.intervals)};
    summary.observations.final_state=await state();
    summary.capture_ok=summary.screenshots.length===5;summary.status=summary.capture_ok?'capture_complete':'capture_incomplete';
  }
  try {await deadline(body(),40000,'Capture');}
  catch(error){summary.status='capture_error';summary.capture_errors.push({name:error.name,message:String(error.message).slice(0,4000)});}
  finally {
    // connect() is a remote Playwright connection even on localhost: video.path()
    // is unsupported. Start saveAs before closing the context that finalizes it.
    const videoSave=video?video.saveAs(path.join(opt.output,'capture.webm')).then(()=>({saved:true}),e=>({error:String(e.message)})):null;
    if(context)await deadline(context.close(),3000,'Context close').catch(()=>{});
    if(videoSave){try{const saved=await deadline(videoSave,2000,'Video finalization');summary.video=saved.error?saved:{file:'capture.webm',bytes:(await fsp.stat(path.join(opt.output,'capture.webm'))).size,format:'webm',description:'Real browser recording including desktop interactions and viewport resize; no generated intermediate states'};}catch(e){summary.video={error:String(e.message)};}}
    if(browser)await deadline(browser.close(),3000,'Browser close').catch(()=>{});
    if(browserServer)await deadline(browserServer.close(),1500,'Browser server close').catch(()=>{});
    killBrowser();clearTimeout(watchdog);process.removeListener('SIGTERM',interrupted);process.removeListener('SIGINT',interrupted);
    if(host){summary.http_audit=host.audit;await host.close();}
    if(frozen){try{summary.workspace_unchanged=hash(await fsp.readFile(frozen.file))===frozen.indexSHA;}catch{summary.workspace_unchanged=false;}}
    summary.finished_utc=new Date().toISOString();summary.elapsed_seconds=(performance.now()-started)/1000;
    await writeJSON(path.join(opt.output,'summary.json'),summary);
  }
  console.log(JSON.stringify({status:summary.status,capture_ok:summary.capture_ok,case:summary.case,summary:path.join(opt.output,'summary.json'),screenshots:summary.screenshots.map(s=>s.file),page_errors:summary.page_errors.length,console_errors:summary.console_errors.length,model_calls:0}));
  if(!summary.capture_ok)process.exitCode=1;
  return summary;
}
if(require.main===module) {
  let opt;try {opt=args(process.argv.slice(2));}catch(e){console.error(e.message);process.exitCode=1;}
  if(opt?.help)console.log('Usage: node browser_capture.cjs --workspace DIR --output NEWDIR --case waves|lava|silk [--vendor FILE] [--chrome FILE] [--record]');
  else if(opt)run(opt).catch(e=>{console.error(String(e.stack||e));process.exitCode=1;});
}
module.exports={args,difference,filesForCapture,serve};
