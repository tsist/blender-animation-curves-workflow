"""Exactly one retained child, final wait and exclusive receipts."""
from .host_compat import is_supported_host
import json,hashlib,subprocess,time,signal,os,stat
from pathlib import Path

def need(ok,msg):
 if not ok:raise ValueError(msg)
def sha(p):
 with Path(p).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()
def safe(p):
 if os.name=='nt':
  from .windows_owned import safe as windows_safe
  return windows_safe(p)
 p=Path(p);need(p.is_absolute() and '..'not in p.parts and not any(z.is_symlink() for z in(p,*p.parents)),'redirected path');return p
def write(p,x):
 with Path(p).open('x',encoding='utf-8')as f:json.dump(x,f,indent=2,allow_nan=False)
def checks(pins):
 out={}
 for p,h in pins.items():
  try:out[p]=sha(safe(p))==h
  except BaseException:out[p]=False
 return out
def require_observed_runtime(bpy):
 if os.name=='nt':
  from .windows_owned import runtime_identity
  return runtime_identity(bpy)
 need(is_supported_host(bpy.app.version),'Observed RNA requires Blender 5.3.0 or later')
def wait(child,r,timeout):
 try:r['exit_code']=child.wait(timeout=timeout);r['waited']=True
 except BaseException as e:r['errors'].append(repr(e))
 finally:
  while not r['waited']:
   try:
    if child.poll()is None:child.kill()
   except BaseException as e:r['errors'].append('kill:'+repr(e))
   try:r['exit_code']=child.wait();r['waited']=True
   except BaseException as e:
    r['errors'].append('wait:'+repr(e))
    try:time.sleep(.05)
    except BaseException as e:r['errors'].append('backoff:'+repr(e))
def private_env(root):
 env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1'}
 for key,name in {'HOME':'home','BLENDER_USER_CONFIG':'config','BLENDER_USER_SCRIPTS':'scripts','BLENDER_USER_DATAFILES':'datafiles','XDG_CONFIG_HOME':'xdg-config','XDG_CACHE_HOME':'xdg-cache','XDG_RUNTIME_DIR':'xdg-runtime','TMPDIR':'tmp'}.items():
  p=safe(root/name);p.mkdir(mode=0o700);s=p.lstat();need(stat.S_IMODE(s.st_mode)==0o700 and s.st_uid==os.geteuid(),'private owned0700');env[key]=str(p)
 return env


def request_stop(child,r,sig):
 r['signals'].append(sig)
 if child is not None:
  try:child.kill()
  except BaseException as e:r['errors'].append('requested signal kill:'+repr(e))
