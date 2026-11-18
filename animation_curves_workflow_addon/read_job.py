"""Single direct Blender child owner; sampled read-only candidate, no apply API."""
import json,uuid,time,subprocess,platform
from pathlib import Path
from . import native_slot
from . import read_lifecycle as h
from .review_core import service
ACTIVE=('RUNNING','CLEANUP_PENDING')
RUNTIME='c2fd82553c979a7f6ba85202c487aa1173c90db588a67d74d70cc7b0c2bea01c'
def selected(bpy):
 arm=bpy.context.active_object
 h.need(arm is not None and list(bpy.context.selected_objects)==[arm],'Select exactly one owned template armature')
 return arm
class Job:
 def __init__(self):
  self.terminal_intent=None;self.terminal_errors=[];self.started=None;self.slot_token=None;self.state='EMPTY';self.process=None;self.log=None;self.session=None;self.result=None;self.error=None;self.waited=False;self.cancelled=False;self.exit_code=None;self.cleanup_errors=[]
 def prepare(self,bpy,times,parent,explicit=None,diagnostic=None,solver=None):
  h.need(self.state=='EMPTY','Release previous review')
  if explicit is None:selected(bpy)
  h.need(platform.system()=='Linux' and platform.machine()=='x86_64','Only qualified Linux x86_64 runtime')
  if solver is not None:
   from . import support_solver as ss
   h.need(explicit is None and diagnostic is None,'No mixed request')
   request=ss.freeze(bpy,solver,uuid.uuid4().hex)
  elif diagnostic is None:
   h.need(len(times)==4,'Four explicit times required')
   request=service.freeze(bpy,list(times),uuid.uuid4().hex) if explicit is None else service.freeze_explicit(bpy,list(times),uuid.uuid4().hex,**explicit)
  else:
   from . import support_diagnostic_service as ds
   h.need(explicit is None,'No mixed request')
   request=ds.freeze(bpy,diagnostic,uuid.uuid4().hex)
  binary=h.safe(bpy.app.binary_path);h.need(h.sha(binary)==RUNTIME,'Unqualified Blender runtime')
  parent=h.safe(parent);h.need(parent.is_dir(),'Existing private output directory required')
  self.session=parent/('ACW-Readonly-'+uuid.uuid4().hex);self.session.mkdir(mode=0o700);self.state='PREPARING'
  try:
   self.request=request;self.digest=service.digest(request);self.binary=str(binary)
   root=Path(__file__).parent.resolve();self.pins={str(p):h.sha(p) for p in root.rglob('*') if p.is_file() and p.suffix in ('.py','.json')};self.pins[str(binary)]=RUNTIME;self.pins[request['source']]=request['source_sha256']
   service.write(self.session/'REQUEST.json',request)
   self.config={'request':str(self.session/'REQUEST.json'),'digest':self.digest,'request_bytes':h.sha(self.session/'REQUEST.json'),'pins':self.pins,'source':request['source'],'source_sha256':request['source_sha256'],'binary':self.binary,'output':str(self.session/'review')}
   if diagnostic is not None or solver is not None:
    copy=self.session/'SOURCE.blend'
    with copy.open('xb')as target:target.write(Path(request['source']).read_bytes())
    h.need(h.sha(copy)==request['source_sha256'],'Private copy mismatch')
    self.pins[str(copy)]=request['source_sha256'];self.config['worker_source']=str(copy)
   h.write(self.session/'CONFIG.json',self.config);self.config_sha=h.sha(self.session/'CONFIG.json');self.state='PREPARED'
  except BaseException as e:self.state='FAILED';self.error=repr(e);raise
 def start(self):
  h.need(self.state=='PREPARED','Freeze first; no repeated start');h.need(all(h.checks(self.pins).values()),'Frozen pins changed');h.need(h.sha(self.session/'CONFIG.json')==self.config_sha,'Config changed');h.need(h.sha(self.session/'REQUEST.json')==self.config['request_bytes'],'Request changed')
  self.slot_token=native_slot.acquire(self)
  try:
   (self.session/'environment').mkdir(mode=0o700);env=h.private_env(self.session/'environment')
   self.log=(self.session/'WORKER.log').open('xb')
   self.process=subprocess.Popen([self.binary,'--background','--factory-startup','--disable-autoexec','--offline-mode','--threads','2',self.config.get('worker_source',self.request['source']),'--python-exit-code','1','--python',str(Path(__file__).with_name('read_worker.py')),'--',str(self.session/'CONFIG.json'),self.config_sha],stdout=self.log,stderr=subprocess.STDOUT,env=env)
   self.started=time.monotonic();self.state='RUNNING'
  except BaseException as e:
   self.error=repr(e);self.cleanup_errors.append('launch:'+repr(e));self.state='CLEANUP_PENDING'
   # Retain even a post-Popen child and open log. close/poll owns recovery.
   self.cancel()
   if self.process is None:self.poll()
   raise
 def cancel(self):
  self.cancelled=True
  if self.state=='PREPARED':self.state='CANCELLED'
  if self.process is not None and not self.waited:
   try:
    if self.process.poll() is None:self.process.kill()
   except ProcessLookupError:pass
   except BaseException as e:self.cleanup_errors.append(repr(e));self.state='CLEANUP_PENDING'
 def poll(self):
  if self.state not in ACTIVE and self.slot_token is None:return self.state
  self.state='CLEANUP_PENDING' if self.cleanup_errors else self.state
  try:
   if self.process is not None and not self.waited:
    if self.started is None:self.cancel()
    elif time.monotonic()-self.started>120:self.error='Read-only worker timeout';self.cancel()
    if self.process.poll() is None:return self.state
    self.exit_code=self.process.wait();self.waited=True
   if self.log:self.log.close();self.log=None
  except BaseException as e:self.cleanup_errors.append(repr(e));self.error=repr(e);self.state='CLEANUP_PENDING';return self.state
  if self.terminal_intent is not None:return native_slot.finish(self,self.terminal_intent)
  try:
   h.need(self.waited and self.exit_code==0 and not self.cancelled and not self.cleanup_errors,'Worker failed/cancelled or cleanup incomplete')
   h.need(all(h.checks(self.pins).values()),'Source/code/runtime drift')
   e=json.loads((self.session/'WORKER-EXIT.json').read_text());h.need(e['success'] and not e['errors'],'Worker did not complete protection')
   r=json.loads((self.session/'review/RESULT.json').read_text())
   if self.request['kind']=='BILATERAL_CAP_WORKFLOW':
    from . import support_solver as ss
    h.need(e.get('result_digest')==service.digest(r),'Solver result digest mismatch')
    ss.validate_result(r,self.request,self.digest,self.session/'review');self.result=r;self.state='SUCCEEDED'
   elif self.request['kind']=='FLAT_CAP_SUPPORT_DIAGNOSTIC':
    from . import support_diagnostic_service as ds
    h.need(e.get('result_digest')==service.digest(r),'Diagnostic result digest mismatch')
    ds.validate_result(r,self.request,self.digest);self.result=r;self.state='SUCCEEDED'
   else:
    r=json.loads((self.session/'review/RESULT.json').read_text());h.need(r['status']=='REQUEST_BOUND_SAMPLED_REVIEW_ONLY' and r['request_sha256']==self.digest and r['request']==self.request,'Wrong result binding');h.need(r['snapshot_unchanged'] and r['source_unchanged'] and not r['may_apply'] and not r['may_write_keys'],'Wrong result qualification')
    p=r['pose'];h.need([s['time'] for s in p['samples']]==self.request['sample_times'],'Sample schedule');h.need(p['frame_restored'] and p['view_layer_unchanged'] and all(p[k] is False for k in ('may_apply','may_write_keys','continuous_interval_qualified','contact_qualified','ground_assessed')),'Unsupported pose qualification');self.result=r;self.state='SUCCEEDED'
  except BaseException as e:self.error=repr(e);self.state='CANCELLED' if self.cancelled else 'FAILED'
  native_slot.finish(self,{'state':self.state,'waited':self.waited,'exit_code':self.exit_code,'cancelled':self.cancelled,'cleanup_errors':self.cleanup_errors,'error':self.error,'request_digest':self.digest})
  return self.state
 def close(self):
  if self.slot_token is not None or self.state in ACTIVE:
   self.state='CLEANUP_PENDING';self.cancel()
   if self.process is not None and not self.waited:
    try:self.exit_code=self.process.wait(timeout=10);self.waited=True
    except BaseException as e:self.cleanup_errors.append(repr(e));raise RuntimeError('Retain owner until child reaped') from e
   self.poll();h.need(self.slot_token is None and self.state not in ACTIVE,'Cleanup pending; owner retained')
 def release(self):
  h.need(self.slot_token is None and self.state not in ACTIVE and self.log is None,'Wait for cleanup');self.__init__()
JOB=Job()
