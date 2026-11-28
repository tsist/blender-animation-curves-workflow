"""Public bounded reference-source job, not a generalized rig adapter."""
import hashlib,json,os,signal,subprocess,time,uuid

WINDOWS_CANCEL_GRACE = 12.0
from pathlib import Path
from . import native_slot
ACTIVE=('RUNNING','CANCELLING','CLEANUP_PENDING')
EXPECTED_PRESET='305b08853f1ed7e622ad417f1a8a53eed0103c4bc7d8813efdda3f67bb5b729f'
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def need(v,m):
 if not v:raise ValueError(m)
def safe(p):
 if os.name=='nt':
  from .windows_owned import safe as windows_safe
  return windows_safe(p)
 p=Path(p);need(p.is_absolute() and '..'not in p.parts and not any(q.is_symlink() for q in (p,*p.parents)),'Use an absolute non-symlink path');return p

def descriptor():
 p=Path(__file__).with_name('support_preset.json');need(sha(p)==EXPECTED_PRESET,'Preset bundle changed');d=json.loads(p.read_text());need(d['schema']==2,'descriptor schema');return d

def bundle_root(d):
 from .support_payload import bundle_root as resolve_bundle
 return resolve_bundle(Path(__file__).parent.resolve(),d['resource_root'])
def validate(source,parent,d,runtimes):
 from . import support_portable as portable
 source=safe(source);parent=safe(parent);need(source.is_file() and source.suffix.lower()=='.blend','Select saved .blend');need(sha(source)==d['source_sha256'],'Unsupported source');need(parent.is_dir(),'Existing output directory required');need(set(runtimes)=={'blender','host_python'},'Explicit runtime pair required');root=bundle_root(d);unused=parent/('unused-'+uuid.uuid4().hex)
 if os.name=='nt':
  from . import support_reference_windows as windows
  windows.compile_windows(root,d['manifest_sha256'],runtimes,source,unused)
 else:
  need(d['platform']=={'system':portable.platform.system(),'machine':portable.platform.machine()},'Unsupported platform')
  portable.compile_plan(root,d['manifest_sha256'],runtimes,source,unused)
 return root
class Job:
 def __init__(self):
  self.terminal_intent=None;self.terminal_errors=[];self.started=None;self.slot_token=None;self.state='EMPTY';self.process=None;self.log=None;self.session=None;self.error=None;self.cancelled=False;self.result=None;self.waited=False;self.exit_code=None;self.cleanup_errors=[];self.cancel_marker=None;self.cancel_started=None
 def prepare(self,source,parent,runtimes):
  from . import support_portable as portable
  need(self.state=='EMPTY','Release the previous session before preparing again');d=descriptor();bundle=validate(source,parent,d,runtimes);root=safe(parent)/('ACW-Support-'+uuid.uuid4().hex)
  if os.name=='nt':
   from . import support_reference_windows as windows
   from .windows_owned import private_directory
   private_directory(root)
   prepare=windows.prepare
  else:
   root.mkdir(mode=0o700)
   prepare=portable.prepare
  self.session=root;self.state='PREPARING'
  try:
   self.source=str(safe(source));self.runtimes={k:str(safe(v))for k,v in runtimes.items()};self.receipt=prepare(bundle,d['manifest_sha256'],self.runtimes,safe(source),root/'candidate_run',root/'prepared');self.preset={**d,'host_python':self.receipt['host_python'],'host_python_sha256':self.receipt['host_python_sha256'],'runner':self.receipt['runner']};self.plan=Path(self.receipt['plan']);self.digest=self.receipt['plan_sha256'];self.state='PREPARED';self.frozen_parent=str(safe(parent));return root
  except BaseException as e:self.state='FAILED';self.error='Prepare failed; session retained: '+repr(e);raise
 def start(self):
  need(self.state=='PREPARED','Prepare first; repeated Create is rejected')
  from . import support_portable as portable
  if os.name=='nt':
   from . import support_reference_windows as windows
   windows.validate_prepared(self.receipt)
  else:
   validate(self.source,self.frozen_parent,descriptor(),self.runtimes)
   portable.validate_prepared(self.receipt)
  need(sha(self.plan)==self.digest,'Prepared plan changed')
  need(not (self.session/'candidate_run').exists(),'Output already consumed')
  need(sha(safe(self.preset['host_python']))==self.preset['host_python_sha256'],'Host interpreter changed')
  self.slot_token=native_slot.acquire(self)
  try:
   self.log=(self.session/'HOST.log').open('xb')
   if os.name=='nt':
    self.process,self.cancel_marker=windows.launch(self.receipt,self.log,self.digest)
   else:
    # The frozen Linux runner drains its children before exiting on SIGTERM.
    self.process=subprocess.Popen([self.preset['host_python'],'-B',self.preset['runner'],'run-once',str(self.plan),self.digest],stdout=self.log,stderr=subprocess.STDOUT,env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1'})
   self.started=time.monotonic();self.state='RUNNING'
  except BaseException as e:
   self._retain_cleanup('launch',e);self.cancel()
   if self.process is None:self.poll()
   raise
 def _retain_cleanup(self,label,exc):
  self.cleanup_errors.append(label+': '+repr(exc));self.error=self.cleanup_errors[-1];self.state='CLEANUP_PENDING'
 def cancel(self):
  if self.state=='PREPARED':self.state='CANCELLED';self.cancelled=True;return
  if self.state in ACTIVE:
   self.cancelled=True
   if self.state!='CLEANUP_PENDING':self.state='CANCELLING'
   if self.process is not None and not self.waited:
    try:
     if self.process.poll() is None:
      if os.name=='nt':
       from .support_reference_windows import request_cancel
       if self.cancel_started is None:self.cancel_started=time.monotonic()
       request_cancel(self.cancel_marker)
      else:self.process.send_signal(signal.SIGTERM)
    except ProcessLookupError:pass
    except BaseException as e:self._retain_cleanup('cancel',e)
 def poll(self):
  if self.state not in ACTIVE and self.slot_token is None:return self.state
  try:
   if self.process is not None and not self.waited:
    if self.started is None:self.cancel()
    elif time.monotonic()-self.started>self.preset['timeout_seconds'] and not self.cancelled:
     self.error='UI job timeout; waiting for existing runner child cleanup';self.cancel()
    code=self.process.poll()
    if code is None and os.name=='nt' and self.cancel_started is not None and time.monotonic()-self.cancel_started>WINDOWS_CANCEL_GRACE:
     self.process.kill()
     code=self.process.poll()
    if code is None:return self.state
    self.exit_code=self.process.wait();self.waited=True
   if self.log is not None:self.log.close();self.log=None
  except BaseException as e:
   self._retain_cleanup('wait/log close',e);return self.state
  if self.terminal_intent is not None:return native_slot.finish(self,self.terminal_intent)
  code=self.exit_code
  try:
   need(sha(self.source)==self.preset['source_sha256'],'Original source changed')
   need(not self.cleanup_errors,'Worker cleanup encountered errors; recovered resources but retain failure evidence')
   need(self.process is not None and self.waited,'No successfully launched worker')
   need(code==0 and not self.cancelled,'Cancelled or worker failed; output retained as evidence')
   run=self.session/'candidate_run';exit=json.loads((run/'EXIT.json').read_text());r=json.loads((run/'RESULT.json').read_text())
   need(not exit['errors'] and not exit['signals'] and all(exit['pins_unchanged'].values()),'Worker protection failed')
   need(all(s['waited'] and s['exit_code']==0 for s in exit['stages']) and [s['stage'] for s in exit['stages']]==['apply','cold'],'Incomplete apply/cold chain')
   candidate=safe(r['candidate']);need(candidate==run/'apply/candidate.blend' and sha(candidate)==r['candidate_sha256'],'Candidate receipt mismatch')
   need(r['status']=='V2_LEFT_SUPPORT_COLD_PASS_NOT_VISUAL_OR_CONTACT_LOCK','Unexpected worker qualification')
   self.result=r;self.state='SUCCEEDED'
  except BaseException as e:self.error=str(e);self.state='CANCELLED' if self.cancelled else 'FAILED'
  native_slot.finish(self,{'state':self.state,'waited':self.waited,'exit_code':self.exit_code,'cancelled':self.cancelled,'cleanup_errors':self.cleanup_errors,'error':self.error,'request_digest':self.digest})
  return self.state
 def release(self):
  need(self.slot_token is None and self.state not in ACTIVE and self.log is None,'Wait for worker cleanup before releasing')
  self.__init__() # Deliberately retain all private output files.
 def close(self):
  if self.slot_token is not None or self.state in ACTIVE:
   self.state='CLEANUP_PENDING';self.cancel()
   if self.process is not None and not self.waited:
    try:
     if os.name=='nt':
      try:self.exit_code=self.process.wait(timeout=WINDOWS_CANCEL_GRACE)
      except subprocess.TimeoutExpired:
       self.process.kill()
       self.exit_code=self.process.wait(timeout=WINDOWS_CANCEL_GRACE)
     else:self.exit_code=self.process.wait()
     self.waited=True
    except BaseException as e:
     self._retain_cleanup('close wait',e)
     raise RuntimeError('Cleanup is still pending; ownership and UI retained') from e
   self.poll()
   need(self.slot_token is None and self.state not in ACTIVE,'Cleanup is still pending; ownership and UI retained')
JOB=Job()
