"""Strict HOST prepare only. Does not execute the returned native plan."""
from .support_bundle_base import *
import platform
from pathlib import PureWindowsPath

def build_bundle(plan,base,root,python_path,python_hash):
 plan=copy.deepcopy(plan);manifest=json.loads(Path(plan['backend']['manifest']).read_text())
 # This manifest's source_file is provenance, explicitly frozen rather than guessed.
 historical=safe(manifest['source_file']);need(sha(historical)==manifest['source_sha256'],'historical source changed');plan['pins'][str(historical)]=manifest['source_sha256']
 python_path=safe(python_path);need(sha(python_path)==python_hash,'unsupported Python');plan['pins'][str(python_path)]=python_hash
 h=build(plan,base,root,{plan['binary']:'blender',str(python_path):'host_python'})
 mp=Path(root)/'bundle.json';m=json.loads(mp.read_text());m['platform']={'system':'Linux','machine':'x86_64'};m['runtime_policy']='EXACT_HASH_EXPLICIT_ONLY';mp.write_text(json.dumps(m,indent=2));return sha(mp)

def _absolute_values(o):
 if isinstance(o,dict):
  for k,v in o.items():
   if isinstance(k,str) and (k.startswith('/') or PureWindowsPath(k).is_absolute()):yield k
   yield from _absolute_values(v)
 elif isinstance(o,list):
  for v in o:yield from _absolute_values(v)
 elif isinstance(o,str) and (o.startswith('/') or PureWindowsPath(o).is_absolute()):yield o

def prepare(root,digest,runtimes,source,output,session):
 root=safe(root);mp=root/'bundle.json';need(sha(mp)==digest,'manifest changed');m=json.loads(mp.read_text());need(m['runtime_policy']=='EXACT_HASH_EXPLICIT_ONLY','runtime policy');need(m['platform']=={'system':platform.system(),'machine':platform.machine()},'unsupported platform')
 need(set(runtimes)=={'blender','host_python'},'explicit runtime pair required')
 compiled=compile_plan(root,digest,runtimes,source,output)
 return prepare_compiled(root,digest,runtimes,session,m,compiled['plan'])

def prepare_compiled(root,digest,runtimes,session,m,p,*,platform_info=None,resource_resolver=resolve,transform_plan=None):
 old=m['original_plan'];mapping={k:(str(safe(runtimes[v['external']])) if 'external'in v else str(resource_resolver(root,v['relative'])))for k,v in m['resources'].items()}
 session=safe(session);need(not session.exists(),'fresh prepared session');
 if platform_info is None:
  session.mkdir()
 else:
  from .windows_owned import private_directory
  private_directory(session)
 pins=p['pins'];transforms=[]
 def write(role,obj):
  path=session/(role+'.json');path.write_text(json.dumps(obj,indent=2,allow_nan=False));pins[str(path)]=sha(path);transforms.append({'role':role,'path':str(path),'sha256':sha(path)});return str(path)
 manifest=json.loads(Path(p['backend']['manifest']).read_text())
 for key in ('source_file','candidate_file'):
  need(manifest[key] in mapping,'unmapped manifest identity');manifest[key]=mapping[manifest[key]]
 need(set(_absolute_values(manifest))<=set(mapping.values()),'unknown manifest path')
 manifest_path=write('manifest',manifest);p['backend']['manifest']=manifest_path
 proposal=json.loads(Path(p['backend']['proposal']).read_text());need(isinstance(proposal.get('source_pins'),dict),'proposal source pins missing');newpins={}
 for k,h in proposal['source_pins'].items():
  need(k in mapping and m['resources'][k]['sha256']==h,'unmapped proposal pin')
  q=manifest_path if k==old['backend']['manifest'] else mapping[k];newpins[q]=sha(q)
 proposal['source_pins']=newpins;need(set(_absolute_values(proposal))<=set(newpins),'unknown proposal path');proposal_path=write('proposal',proposal);p['backend']['proposal']=proposal_path;p['backend']['proposal_sha256']=sha(proposal_path)
 snapshot=json.loads(Path(p['backend']['source_snapshot']).read_text());need(not list(_absolute_values(snapshot)),'unexpected snapshot absolute path')
 if transform_plan is not None:transform_plan(p,session,transforms)
 # Keep evidence dependency JSON bytes exactly as received. They are pins, not execution plans.
 need(all(sha(k)==h for k,h in pins.items()),'prepared pins mismatch');plan_path=session/'PLAN.json';plan_path.write_text(json.dumps(p,indent=2,allow_nan=False))
 result={'status':'PREPARED_HOST_ONLY_NOT_NATIVE_QUALIFIED','plan':str(plan_path),'plan_sha256':sha(plan_path),'runner':p['windows_reference']['runner'] if platform_info else mapping[next(k for k in mapping if k.endswith('animation_two_segment_support_native_prepare_v03_20261114/runner.py'))],'host_python':str(safe(runtimes['host_python'])),'host_python_sha256':sha(runtimes['host_python']),'bundle_root':str(root),'bundle_digest':digest,'runtimes':{k:str(safe(v))for k,v in runtimes.items()},'transforms':transforms,'platform':platform_info or m['platform']}
 (session/'PREPARED.json').write_text(json.dumps(result,indent=2));return result

def validate_prepared(receipt):
 need(receipt['platform']=={'system':platform.system(),'machine':platform.machine()},'unsupported platform');root=safe(receipt['bundle_root']);need(sha(root/'bundle.json')==receipt['bundle_digest'],'bundle changed');p=safe(receipt['plan']);need(sha(p)==receipt['plan_sha256'],'prepared plan changed');plan=json.loads(p.read_text());need(not safe(plan['output']).exists(),'output consumed');need(sha(plan['source'])==plan['source_sha256'],'source changed');need(all(sha(safe(k))==v for k,v in plan['pins'].items()),'pin changed');need(sha(safe(receipt['host_python']))==receipt['host_python_sha256'],'Python changed');m=json.loads((root/'bundle.json').read_text());expected={v['external']:v['sha256'] for v in m['resources'].values() if 'external'in v};need(set(receipt['runtimes'])==set(expected),'runtime roles changed');need(all(sha(safe(receipt['runtimes'][k]))==v for k,v in expected.items()),'runtime hash changed');need(receipt['host_python']==receipt['runtimes']['host_python'] and plan['binary']==receipt['runtimes']['blender'],'runtime identity changed');need(receipt['runner'] in plan['pins'],'runner not pinned');return True
