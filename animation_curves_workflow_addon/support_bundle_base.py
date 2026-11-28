"""HOST-only reference bundle relocation. No subprocess, Blender mutation or launch."""
from pathlib import Path
import hashlib,json,copy

def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def need(v,m):
 if not v:raise ValueError(m)
def safe(p):
 p=Path(p);need(p.is_absolute() and '..' not in p.parts and not any(x.is_symlink() for x in (p,*p.parents)),'unsafe path');return p
def resolve(root,relative):
 root=safe(root);p=Path(relative);need(not p.is_absolute() and '..'not in p.parts,'unsafe resource');return safe(root/p)
def build(plan,base,root,externals):
 base=safe(base);root=safe(root);need(not root.exists(),'fresh bundle required');root.mkdir();rows={}
 for old,digest in plan['pins'].items():
  p=safe(old);need(sha(p)==digest,'changed input')
  if old in externals:rows[old]={'external':externals[old],'sha256':digest};continue
  need(p.is_relative_to(base),'undeclared external')
  rel='resources/'+str(p.relative_to(base));q=resolve(root,rel);q.parent.mkdir(parents=True,exist_ok=True);q.write_bytes(p.read_bytes());rows[old]={'relative':rel,'sha256':digest}
 m={'schema':1,'original_plan':plan,'resources':rows};mp=root/'bundle.json';mp.write_text(json.dumps(m,indent=2));return sha(mp)
def compile_plan(root,manifest_digest,external_paths,source,output):
 root=safe(root);mp=root/'bundle.json';need(sha(mp)==manifest_digest,'manifest changed');m=json.loads(mp.read_text())
 return compile_manifest(root,m,external_paths,source,output,manifest_digest)

def compile_manifest(root,m,external_paths,source,output,manifest_digest,resource_resolver=resolve):
 need(m['schema']==1,'schema');mapping={};pins={}
 for old,row in m['resources'].items():
  p=safe(external_paths[row['external']]) if 'external'in row else resource_resolver(root,row['relative'])
  need(p.is_file() and sha(p)==row['sha256'],'resource changed: '+str(p));mapping[old]=str(p);pins[str(p)]=row['sha256']
 source=safe(source);output=safe(output);need(source.is_file() and sha(source)==m['original_plan']['source_sha256'],'source changed');need(not output.exists(),'fresh output required')
 p=copy.deepcopy(m['original_plan']);p['source']=str(source);p['output']=str(output);p['binary']=mapping[p['binary']];p['entry']=mapping[p['entry']]
 for k,v in p['backend'].items():
  if isinstance(v,str) and v in mapping:p['backend'][k]=mapping[v]
 pins[str(source)]=p['source_sha256'];p['pins']=pins
 return {'plan':p,'status':'HOST_RELOCATED_NOT_NATIVE_QUALIFIED','manifest_sha256':manifest_digest,'resource_count':len(mapping),'warning':'Byte-preserved embedded JSON and module-relative imports still require native relocation qualification before enabling public Generate.'}
