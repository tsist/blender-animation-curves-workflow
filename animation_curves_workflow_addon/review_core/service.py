"""Freeze selected owned instance -> read-only sample report. No spawn/apply API.
Caller owns single-slot execution and trusted runtime qualification. This module
adds no native admission; outputs are private audit files, never .blend saves.
"""
import hashlib,json,re
from pathlib import Path
from .discovery import discover
from .pose_review import evaluate
from . import portable,state_reader

def need(v,m):
 if not v:raise ValueError(m)
def encoded(v):return json.dumps(v,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def digest(v):return hashlib.sha256(encoded(v)).hexdigest()
def code_pins():
 root=Path(__file__).parent
 return {p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob('*')) if p.is_file() and p.suffix in ('.py','.json')}
def context_gate(bpy):
 need(bpy.context.mode=='OBJECT','Object mode required')
 need(len(bpy.data.scenes)==1,'Multiple scenes not qualified by this snapshot boundary')
 scene=bpy.context.scene;layer=bpy.context.view_layer
 need(not scene.library and not scene.override_library,'Local scene required')
 need(layer.name in scene.view_layers and scene.view_layers[layer.name]==layer,'View layer identity')
 return scene.name,layer.name

def explicit_options(bpy,times):
    """Recognized instances only; metadata is not authentication or apply authority."""
    context_gate(bpy)
    need(bpy.data.is_saved and not bpy.data.is_dirty,'saved clean source required')
    rows=[]
    for arm in bpy.data.objects:
        if arm.type!='ARMATURE':continue
        uid=arm.get('ac_instance_uuid')
        if not isinstance(uid,str):continue
        # Discovery rejects duplicate UUID/object structures, old feet and unsupported rigs.
        try:w=discover(bpy,uid,times)
        except (ValueError,RuntimeError):continue
        if not w.get('structure_recognized') is True:continue
        if w['rig']!=arm.name:continue
        rows.append({'rig_name':arm.name,'instance_uuid':uid,'source':w['source'],'source_sha256':w['source_sha256']})
    need(len({x['instance_uuid'] for x in rows})==len(rows),'Ambiguous instance UUID')
    return sorted(rows,key=lambda x:(x['rig_name'],x['instance_uuid']))

def freeze_explicit(bpy,times,nonce,rig_name,instance_uuid,source,source_sha256):
    need(type(nonce) is str and re.fullmatch('[0-9a-f]{32}',nonce),'Request nonce')
    scene,layer=context_gate(bpy)
    need(bpy.data.is_saved and not bpy.data.is_dirty,'saved clean source required')
    need(type(rig_name)is str and type(instance_uuid)is str,'Explicit rig identity required')
    arm=bpy.data.objects.get(rig_name)
    need(arm is not None and arm.type=='ARMATURE' and arm.get('ac_instance_uuid')==instance_uuid,'Explicit rig/UUID mismatch')
    w=discover(bpy,instance_uuid,times)
    need((w['rig'],w['source'],w['source_sha256'])==(rig_name,source,source_sha256),'Explicit source/instance changed')
    return {'schema':1,'kind':'OWNED_POSE_READONLY','nonce':nonce,'scene':scene,'view_layer':layer,
         'source':w['source'],'source_sha256':w['source_sha256'],'instance_uuid':instance_uuid,
         'selected_rig':rig_name,'times':w['times'],'sample_times':list(dict.fromkeys(w['times'])),
         'code_pins':code_pins(),'discovery_sha256':digest(w),'may_apply':False,'may_write_keys':False}

def freeze(bpy,times,nonce):
 need(type(nonce) is str and re.fullmatch('[0-9a-f]{32}',nonce),'Request nonce')
 scene,layer=context_gate(bpy);arm=bpy.context.active_object
 need(arm is not None and arm.type=='ARMATURE' and arm.select_get(),'Select an owned template armature')
 w=discover(bpy,arm.get('ac_instance_uuid'),times)
 need(w['rig']==arm.name,'Selected rig mismatch')
 return {'schema':1,'kind':'OWNED_POSE_READONLY','nonce':nonce,'scene':scene,'view_layer':layer,
         'source':w['source'],'source_sha256':w['source_sha256'],'instance_uuid':w['instance_uuid'],
         'selected_rig':arm.name,'times':w['times'],'sample_times':list(dict.fromkeys(w['times'])),
         'code_pins':code_pins(),'discovery_sha256':digest(w),'may_apply':False,'may_write_keys':False}

def validate(bpy,request,request_sha256):
 need(digest(request)==request_sha256,'Frozen request changed')
 need(request['schema']==1 and request['kind']=='OWNED_POSE_READONLY','Request schema')
 need(request['may_apply'] is False and request['may_write_keys'] is False,'Read-only request')
 need(code_pins()==request['code_pins'],'Packaged core drift')
 scene,layer=context_gate(bpy)
 need((scene,layer)==(request['scene'],request['view_layer']),'Wrong scene/view layer')
 w=discover(bpy,request['instance_uuid'],request['times'])
 need((w['source'],w['source_sha256'],w['rig'])==(request['source'],request['source_sha256'],request['selected_rig']),'Source/instance changed')
 need(digest(w)==request['discovery_sha256'],'Discovery changed')
 need(list(dict.fromkeys(request['times']))==request['sample_times'],'Sample schedule changed')
 return w

def instance_sets(bpy,witness):
 owned=[witness['root'],witness['rig'],*witness['parts'].values()]
 need(len(owned)==len(set(owned)),'Ambiguous discovered object names')
 names={o.name for o in bpy.context.scene.objects}
 need(set(owned)<=names,'Discovered instance absent from scene')
 return {'created_owned_object_names':sorted(owned),'protected_original_object_names':sorted(names-set(owned))}

class Snapshot:
 def __init__(self,bpy,w):self.bpy=bpy;self.portable=portable;self.manifest=instance_sets(bpy,w)
 def pose_locks(self):
  return {o.name:{b.name:{'rotation_mode':b.rotation_mode,'euler':list(b.rotation_euler),'axis_angle':list(b.rotation_axis_angle),'locks':list(b.lock_rotation)} for b in o.pose.bones} for o in self.bpy.data.objects if o.type=='ARMATURE'}

def capture(bpy,w,out,label):
 p=portable
 state=state_reader.NativeBackend.protected_state(Snapshot(bpy,w))
 mats={m.name:{'rna':p.scalar(m),'custom':p.plain(dict(m.items())),'nodes':[{'rna':p.scalar(n),'inputs':{s.identifier:p.plain(s.default_value) for s in n.inputs if hasattr(s,'default_value')}} for n in m.node_tree.nodes] if m.use_nodes else []} for m in bpy.data.materials}
 result={'state':state,'materials':mats,'collections':{c.name:{'rna':p.scalar(c),'custom':p.plain(dict(c.items())),'objects':sorted(o.name for o in c.objects),'children':sorted(c.children.keys())} for c in bpy.data.collections},'actions':p.action_records(bpy.data.actions,out/(label+'-raw'))}
 write(out/(label+'.json'),result);return result

def write(path,obj):
 with path.open('xb') as f:f.write(encoded(obj))

def review(bpy,request,request_sha256,output):
 w=validate(bpy,request,request_sha256)
 out=Path(output)
 need(out.is_absolute() and '..' not in out.parts and not any(p.is_symlink() for p in (out,*out.parents)),'Private output path')
 need(not out.exists() and out.parent.is_dir(),'Fresh output required')
 out.mkdir(mode=0o700);write(out/'REQUEST.json',request)
 before=capture(bpy,w,out,'before');pose=None
 try:pose=evaluate(bpy,w,discover)
 finally:
  after=capture(bpy,w,out,'after')
  need(before==after,'Authored/raw state changed')
  validate(bpy,request,request_sha256)
 need(pose['status']=='SAMPLED_POSE_REVIEW_ONLY','Unexpected pose result')
 need([s['time'] for s in pose['samples']]==request['sample_times'],'Sample report mismatch')
 for key in ('may_apply','may_write_keys','continuous_interval_qualified','contact_qualified','ground_assessed'):
  need(pose[key] is False,'Unsupported qualification')
 need(pose['frame_restored'] and pose['view_layer_unchanged'],'Incomplete restoration')
 report={'status':'REQUEST_BOUND_SAMPLED_REVIEW_ONLY','request_sha256':request_sha256,'request':request,'pose':pose,'snapshot_unchanged':True,'source_unchanged':True,'may_apply':False,'may_write_keys':False}
 write(out/'RESULT.json',report);return report
