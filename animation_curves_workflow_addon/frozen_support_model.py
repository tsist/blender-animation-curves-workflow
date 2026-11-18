"""HOST FK/positive-branch solver from exact v2 snapshot and raw keys; no bpy."""
from pathlib import Path
import json,gzip,math,bisect,hashlib,sys
import numpy as np
from scipy.spatial.transform import Rotation
from scipy.optimize import brentq
from .leg_contract import solve_two_segment,signed_flexion

def need(x,msg):
 if not x:raise ValueError(msg)
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def f32(v):return float(np.float32(v))
def qm(q):
 q=np.asarray(q);M=np.eye(4);M[:3,:3]=Rotation.from_quat([*q[1:],q[0]]).as_matrix();return M
def mq(M):
 q=Rotation.from_matrix(M[:3,:3]).as_quat();return np.array([q[3],*q[:3]])
def normalize(q):return np.asarray(q)/np.linalg.norm(q)
def blend(a,b,w):
 if w==0:return np.array(a)
 if w==1:return np.array(b)
 if np.dot(a,b)<0:b=-np.array(b)
 return normalize((1-w)*np.array(a)+w*np.array(b))
def weight(t):
 if t<=470 or t>=500:return 0.
 if 474<=t<=494:return 1.
 u=(t-470)/4 if t<474 else (500-t)/6
 return u*u*u*(10+u*(-15+6*u))
def load_curves(path):
 curves={};cur=None;header={}
 with gzip.open(path,'rt')as f:
  for line in f:
   r=json.loads(line)
   if 'curve'in r:
    cur=(r['rna']['data_path'],r['rna']['array_index']);need(cur not in curves,'duplicatecurve');curves[cur]=[];header[cur]=r
   elif 'key'in r:
    k=r['key'];need(k['interpolation']=='LINEAR','only linear');curves[cur].append(k['co'])
 return {k:np.array(v)for k,v in curves.items()},header
class Model:
 def __init__(self, *, snapshot_path, manifest_path, native_evidence_path, source_path, expected_source_sha256, expected_hashes):
  SNAP=Path(snapshot_path).resolve();MANIFEST=Path(manifest_path).resolve();EVIDENCE=Path(native_evidence_path).resolve();SOURCE=Path(source_path).resolve();SOURCE_SHA=expected_source_sha256
  expected_hashes={str(Path(p).resolve()):h for p,h in expected_hashes.items()}
  for path in (SNAP,MANIFEST,EVIDENCE,SOURCE):
   need(str(path) in expected_hashes and sha(path)==expected_hashes[str(path)],'input pin missing or changed')
  need(len(SOURCE_SHA)==64 and all(c in "0123456789abcdef" for c in SOURCE_SHA),"exact source hash")
  self.snapshot=json.loads(SNAP.read_text());self.manifest=json.loads((MANIFEST).read_text());need(self.manifest['candidate_sha256']==SOURCE_SHA,'v2 identity');self.owned=self.snapshot['state']['owned'];self.rig=self.owned[self.manifest['owner']['object_name']];self.root=self.owned[self.manifest['root_object']];self.bones=self.rig['armature']['bones'];need(len(self.bones)==18 and not any(b.startswith('foot')for b in self.bones),'two-segment rig')
  self.rest={b:np.array(v['rest'])for b,v in self.bones.items()};self.rest_inv={b:np.linalg.inv(v)for b,v in self.rest.items()}
  self.pose_raw=SNAP.parent/'raw'/(self.rig['action'].encode().hex()+'.jsonl.gz');self.root_raw=SNAP.parent/'raw'/(self.root['action'].encode().hex()+'.jsonl.gz');need(all(str(p.resolve()) in expected_hashes and sha(p)==expected_hashes[str(p.resolve())] for p in (self.pose_raw,self.root_raw)), 'raw pin missing or changed');self.pose,self.headers=load_curves(self.pose_raw);self.rootkeys,_=load_curves(self.root_raw);need(len(self.pose)==72 and len(self.rootkeys)==7,'72+7 channels')
  self.mesh={};self.vertices={};self.cap={}
  for s in 'LR':
   n='shin.'+s;m=next(v for v in self.owned.values()if v.get('custom',{}).get('ac_part')==n);self.mesh[s]=m;self.vertices[s]=np.array([(*v,1.)for v in m['mesh']['vertices']]);self.cap[s]=m['mesh']['polygons'][1];need(m['mesh']['vertex_groups']==[[n,0,False]] and all(v==[[0,1.]]for v in m['mesh']['weights']),'rigid shin mesh');cs=self.rig['armature']['pose'][n]['constraints'];need(len(cs)==1 and cs[0]['type']=='LIMIT_ROTATION' and cs[0]['owner_space']=='LOCAL' and cs[0]['influence']==1 and cs[0]['use_limit_x'] and cs[0]['use_limit_y'] and cs[0]['use_limit_z'],'real local knee constraint')
  paths=[SNAP,MANIFEST,EVIDENCE,SOURCE,self.pose_raw,self.root_raw,Path(__file__),Path(__file__).with_name('leg_contract.py')]
  self.pins={str(p.resolve()):sha(p) for p in paths}
  need(sha(SOURCE)==SOURCE_SHA,'source bytes')
  need(all(sha(p)==h for p,h in expected_hashes.items()),'input changed while loading')
  self.source_sha256=SOURCE_SHA
  self.snapshot_sha256=sha(SNAP)
  self.input_status='FROZEN_HOST_ONLY_REQUIRES_NATIVE_QUALIFICATION'
 def verify_frozen(self):
  need(all(Path(p).is_file() and sha(p)==h for p,h in self.pins.items()),'frozen input changed')
  return True
 @staticmethod
 def value(curves,path,size,t):return np.array([np.interp(t,curves[(path,i)][:,0],curves[(path,i)][:,1])for i in range(size)])
 def q(self,b,t,overrides=None):
  if overrides and b in overrides:return overrides[b]
  return self.value(self.pose,f'pose.bones["{b}"].rotation_quaternion',4,t)
 def fk(self,t,overrides=None):
  Wm=qm(self.value(self.rootkeys,'rotation_quaternion',4,t));Wm[:3,3]=self.value(self.rootkeys,'location',3,t);pose={};world={}
  for b in ('root','pelvis','thigh.L','shin.L','thigh.R','shin.R'):
   p=self.bones[b]['parent'];pose[b]=(pose[p]@self.rest_inv[p]@self.rest[b]@qm(self.q(b,t,overrides)))if p else self.rest[b]@qm(self.q(b,t,overrides));world[b]=Wm@pose[b]
  front=(Wm@pose['pelvis']@self.rest_inv['pelvis'])[:3,:3]@np.array([0.,-1.,0.]);out={'world':world,'rig_world':Wm,'front':front,'sides':{}}
  for s in 'LR':
   th,sh='thigh.'+s,'shin.'+s;H=world[th][:3,3];K=world[sh][:3,3];E=(world[sh]@np.array([0.,self.bones[sh]['rna']['length'],0.,1.]))[:3];verts=(world[sh]@self.rest_inv[sh]@self.vertices[s].T).T[:,:3];cap=verts[self.cap[s]];mi=int(np.argmin(verts[:,2]));out['sides'][s]={'hip':H,'knee':K,'end':E,'vertices':verts,'cap':cap,'minimum_index':mi,'contact_point':verts[mi],'minimum_z':float(verts[:,2].min()),'cap_minimum_z':float(cap[:,2].min()),'angle':signed_flexion(H.tolist(),K.tolist(),E.tolist(),world[th][:3,0].tolist(),hinge_tolerance=1e-4)}
  return out
 def solve_at(self,t,xy,z):
  base=self.fk(t);H=base['sides']['L']['hip'];E=np.array([*xy,z]);l1=self.bones['thigh.L']['rna']['length'];l2=self.bones['shin.L']['rna']['length'];sol=solve_two_segment(H.tolist(),E.tolist(),l1,l2,base['front'].tolist());K=np.array(sol['knee']);n=np.array(sol['positive_hinge_axis']);MT=np.eye(4);MS=np.eye(4)
  for M,a,b in [(MT,H,K),(MS,K,E)]:
   y=(b-a)/np.linalg.norm(b-a);M[:3,:3]=np.column_stack([n,y,np.cross(n,y)]);M[:3,3]=a
  parent=base['world']['pelvis'];th_basis=self.rest_inv['thigh.L']@self.rest['pelvis']@np.linalg.inv(parent)@MT;sh_basis=self.rest_inv['shin.L']@self.rest['thigh.L']@np.linalg.inv(MT)@MS
  euler=Rotation.from_matrix(sh_basis[:3,:3]).as_euler('xyz');need(max(abs(euler[1:]))<1e-6,'shin nonhinge');need(max(np.linalg.norm(th_basis[:3,3]),np.linalg.norm(sh_basis[:3,3]))<4e-6,'translation residue');q={'thigh.L':mq(th_basis),'shin.L':np.array([math.cos(euler[0]/2),math.sin(euler[0]/2),0.,0.])}
  fk=self.fk(t,q);fk['inverse_rest_physical_angle_residual_deg']=float(fk['sides']['L']['angle']-sol['flexion_deg'])
  if abs(fk['inverse_rest_physical_angle_residual_deg'])>=1e-3:raise ArithmeticError('inverse-rest physical angle conversion residual')
  return q,fk
 def solve_contact(self,t,xy,previous_z=None,minimum_height=0.):
  b=self.fk(t);leg=b['sides']['L'];H=leg['hip'];L=self.bones['thigh.L']['rna']['length']+self.bones['shin.L']['rna']['length'];dxy=np.linalg.norm(np.array(xy)-H[:2]);need(dxy<L,'horizontal unreachable');lo=H[2]-math.sqrt(L*L-dxy*dxy)+1e-7;seed=leg['end'][2]-leg['minimum_z']+minimum_height;hi=max(lo+1e-7,seed+.02);scans=[]
  for z in np.linspace(lo,hi,65):
   try:q,fk=self.solve_at(t,xy,float(z));scans.append((float(z),fk['sides']['L']['minimum_z']-minimum_height))
   except ValueError:continue
  roots=[]
  for a,b in zip(scans,scans[1:]):
   if a[1]*b[1]<0:
    z=brentq(lambda z:self.solve_at(t,xy,z)[1]['sides']['L']['minimum_z']-minimum_height,a[0],b[0],xtol=1e-12);roots.append(float(z))
   elif abs(a[1])<1e-12:roots.append(a[0])
  need(roots,'no cap-height root');ref=seed if previous_z is None else previous_z;z=min(roots,key=lambda z:abs(z-ref));q,fk=self.solve_at(t,xy,z);return q,fk,z,roots
