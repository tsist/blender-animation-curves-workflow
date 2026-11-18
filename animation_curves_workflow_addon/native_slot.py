"""Process-wide fail-closed owner lease, retained across add-on module reloads."""
import sys,types,uuid
KEY='_acw_native_worker_slot_v1'
def registry():
 if KEY not in sys.modules:
  m=types.ModuleType(KEY);m.lease=None;sys.modules[KEY]=m
 m=sys.modules[KEY]
 if not hasattr(m,'lease'):raise RuntimeError('Unknown slot registry; blocked')
 return m
def acquire(owner):
 m=registry()
 if m.lease is not None:raise RuntimeError('Native worker slot already owned; await original owner cleanup')
 token=uuid.uuid4().hex;m.lease=(owner,token);return token
def release(owner,token,process,waited,log):
 m=registry()
 if m.lease!=(owner,token):raise RuntimeError('Wrong/lost lease identity; blocked')
 if (process is not None and not waited) or log is not None:raise RuntimeError('Worker not reaped/log not closed; retain slot')
 m.lease=None

def write_receipt(path,payload):
 """Atomic exclusive publication; retain failed temp evidence, permit safe retry."""
 import json,os
 from pathlib import Path
 path=Path(path);data=json.dumps(payload,sort_keys=True,allow_nan=False).encode()
 if path.exists():
  if path.read_bytes()!=data:raise RuntimeError('Existing receipt conflicts; retain ownership')
  return
 temp=path.with_name(path.name+'.attempt-'+uuid.uuid4().hex)
 with temp.open('xb') as stream:
  stream.write(data);stream.flush();os.fsync(stream.fileno())
 os.link(temp,path) # exclusive; never overwrite a prior receipt
 # Retain the attempt hardlink as audit evidence; no post-publication cleanup failure.

def finish(owner,payload):
 """Freeze terminal intent once. Later publication/release errors are separate."""
 import json
 if owner.terminal_intent is None:
  owner.terminal_intent=json.loads(json.dumps({**payload,'lease_token':owner.slot_token},allow_nan=False))
 try:
  write_receipt(owner.session/'OWNER-EXIT.json',owner.terminal_intent)
  release(owner,owner.slot_token,owner.process,owner.waited,owner.log)
  owner.slot_token=None;owner.state=owner.terminal_intent['state']
 except BaseException as exc:
  event={'error':repr(exc),'lease_token':owner.slot_token,'phase':'terminal-publication-or-release'}
  owner.terminal_errors.append(event);owner.error=repr(exc);owner.state='CLEANUP_PENDING'
  try:write_receipt(owner.session/('OWNER-CLEANUP-'+uuid.uuid4().hex+'.json'),event)
  except BaseException as audit_error:owner.terminal_errors.append({'audit_write_error':repr(audit_error)})
 return owner.state
