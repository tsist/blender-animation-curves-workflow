"""Two explicit head.Y key translations; request-scoped, not a persistent event graph."""
import copy,json,math,struct
from fractions import Fraction
import bpy
from . import pose_channel as pc,pose_batch,native,core,display_witness as dw
from .contract import normalize_manifest
PATH='pose.bones["head"].rotation_euler'
_PERMIT=None
canon=lambda x:json.loads(json.dumps(x,allow_nan=False,sort_keys=True))
def active():return _PERMIT is not None
def native_target(owner,path,index):
 if not active() or (owner.as_pointer(),owner.get('ac_instance_uuid'),path,index)not in _PERMIT['addresses']:raise ValueError('No exact two-rig event permission')
 return pc.target(owner,path,index,allow_body_colors=True)
def validate_manifest(owner,spec,manifest):
 native_target(owner,spec['data_path'],spec['array_index'])
 if normalize_manifest(manifest)!=_PERMIT['manifest']:raise ValueError('Event request changed')
def exact(x):
 f=float(x)
 if not math.isfinite(f)or Fraction(f)!=x or struct.unpack('f',struct.pack('f',f))[0]!=f:raise ValueError('时间/手柄不能精确表示为float32')
 return f
def validate_sources(context,params):
 if set(params)!={'rig_a','rig_b','frame_a','frame_b','event'}:raise ValueError('Unknown source parameters')
 if any(type(params[k])not in (int,float)or not math.isfinite(params[k])for k in ('frame_a','frame_b')):raise ValueError('有限数值参数必填')
 if params['rig_a']==params['rig_b']:raise ValueError('须选择两个不同角色')
 if not isinstance(params['event'],str)or not params['event'].strip()or len(params['event'])>128:raise ValueError('显式事件名必填')
 rows=[]
 for i,side in enumerate(('a','b')):
  arm=bpy.data.objects.get(params['rig_'+side]);t=exact(Fraction(params['frame_'+side]))
  if arm is None:raise ValueError('角色不存在')
  w,pb,_=pc.target(arm,PATH,1,allow_body_colors=True)
  action=w['action'];bag=w['bag']
  if not action or action.use_frame_range or action.use_cyclic or len(action.pose_markers):raise ValueError('现有动作或事件元数据不支持')
  fc=bag.fcurves.find(PATH,index=1)
  if fc is None or fc.lock or fc.mute or len(fc.modifiers)or len(fc.sampled_points):raise ValueError('head.Y 通道不支持')
  if any(k.interpolation not in ('LINEAR','CONSTANT')or k.handle_left_type!='FREE'or k.handle_right_type!='FREE'for k in fc.keyframe_points):raise ValueError('目标通道只支持LINEAR/CONSTANT和FREE')
  ks=list(fc.keyframe_points);ix=[j for j,k in enumerate(ks)if k.co.x==t]
  if len(ix)!=1 or ix[0]in(0,len(ks)-1):raise ValueError('必须选一个现有非端点键')
  j=ix[0]
  for f in bag.fcurves:
   if f.data_path.startswith('pose.'):
    if abs(arm.path_resolve(f.data_path)[f.array_index]-f.evaluate(context.scene.frame_current_final))>1e-6:raise ValueError('已动画轴有未打键输入，请先保存姿态')
  rows.append({'arm':arm,'w':w,'frame':t,'key':native.key_state(ks[j]),'previous':float(ks[j-1].co.x),'next':float(ks[j+1].co.x)})
 if rows[0]['w']['action']==rows[1]['w']['action']:raise ValueError('共享Action拒绝')
 return rows

def review(context,params):
 if set(params)!={'rig_a','rig_b','frame_a','frame_b','delta','event'}:raise ValueError('Unknown event parameters')
 if type(params['delta'])not in (int,float)or not math.isfinite(params['delta']):raise ValueError('有限时间差必填')
 delta=Fraction(exact(Fraction(params['delta'])))
 if not delta:raise ValueError('时间差不能为零')
 rows=validate_sources(context,{k:v for k,v in params.items()if k!='delta'})
 targets=[];bindings=[];events=[{'id':'move','frame':params['frame_a']}]
 for side,row in zip(('a','b'),rows):
  arm=row['arm'];t=row['frame'];dest=exact(Fraction(t)+delta)
  if not -1e6<=dest<=1e6:raise ValueError('目标帧超范围')
  if not row['previous']+native.EPSILON<dest<row['next']-native.EPSILON:raise ValueError('目标占位/越过相邻锁定键')
  for field in ('co','handle_left','handle_right'):exact(Fraction(row['key'][field][0])+delta)
  spec={'id':side,'owner':{'type':'OBJECT','name':arm.name},'data_path':PATH,'array_index':1,'sharing':'reject'}
  targets.append(spec);bindings.append({'event':'move','target':side,'offset':exact(Fraction(t)-Fraction(params['frame_a'])),'expected_key':row['key']})
  fc=row['w']['bag'].fcurves.find(PATH,index=1)
  for k in (fc.keyframe_points[0],fc.keyframe_points[-1]):
   eid=side+str(k.co.x);events.append({'id':eid,'frame':float(k.co.x),'locked':True});bindings.append({'event':eid,'target':side,'offset':0,'expected_key':native.key_state(k)})
  row.update(dest=dest,spec=spec)
 manifest={'schema':'1.0','operation':'edit','context':{'scene':context.scene.name,'view_layer':context.view_layer.name},'targets':targets,'operations':[], 'event_shift':{'schema':'1.0','events':events,'dependencies':[],'event_id':'move','target_frame':exact(Fraction(params['frame_a'])+delta),'bindings':bindings}}
 return rows,manifest

def bone_selection(context):
 return {'selected':sorted(p.name for p in context.selected_pose_bones)if context.selected_pose_bones is not None else None,'active':context.active_pose_bone.name if context.active_pose_bone else None}

def structure(context):
 return {'objects':{o.name:{'ptr':o.as_pointer(),'parent':o.parent.as_pointer() if o.parent else None,'basis':[list(x)for x in o.matrix_basis],'parent_inverse':[list(x)for x in o.matrix_parent_inverse],'constraints':[dw.rna(c)for c in o.constraints]}for o in bpy.data.objects},'bones':{o.name:{pb.name:{'constraints':[dw.rna(c)for c in pb.constraints],'mode':pb.rotation_mode}for pb in o.pose.bones}for o in bpy.data.objects if o.type=='ARMATURE'}}

def global_origin():
 return {'actions':{a.name:{'ptr':a.as_pointer(),'witness':pc.action_witness(a),'meta':pc.metadata_witness(a),'fake':a.use_fake_user}for a in bpy.data.actions},'bindings':{o.name:{'action':o.animation_data.action.as_pointer() if o.animation_data and o.animation_data.action else None,'slot':o.animation_data.action_slot.handle if o.animation_data and o.animation_data.action_slot else None,'settings':native.binding_state(o)['settings']}for o in bpy.data.objects}}

def origin(context,params):
 rows,m=review(context,params)
 return canon({'global_origin':global_origin(),'params':params,'scene':context.scene.as_pointer(),'layer':context.view_layer.name,'frame':context.scene.frame_current,'subframe':context.scene.frame_subframe,'mode':context.mode,'selected':sorted(o.name for o in context.selected_objects),'active':context.active_object.name if context.active_object else None,'bone_selection':bone_selection(context),'display':dw.capture(context),'structure':structure(context),'rigs':[{'ptr':r['arm'].as_pointer(),'uuid':r['arm'].get('ac_instance_uuid'),'action_ptr':r['w']['action'].as_pointer(),'action':pc.action_witness(r['w']['action']),'meta':pc.metadata_witness(r['w']['action']),'fake':r['w']['action'].use_fake_user,'slot':r['w']['slot'].handle}for r in rows],'poses':{o.name:pose_batch.pose_state(o)for o in bpy.data.objects if o.type=='ARMATURE'},'manifest':m})

def execute(context,expected,checkpoint=None, *, participant=None):
 global _PERMIT
 if active()or pose_batch.active()or pc._ACTIVE is not None:raise ValueError('Nested event transaction')
 params=expected['params']
 if origin(context,params)!=expected:raise ValueError('参数/源/姿态改变，请重新预览')
 rows,manifest=review(context,params);actions=set(bpy.data.actions);old={a:pc.action_witness(a)for a in actions};meta={a:pc.metadata_witness(a)for a in actions};fake={a:a.use_fake_user for a in actions}
 arms={r['arm'] for r in rows};sources={r['w']['action']for r in rows};bindings={o:native.binding_state(o)for o in bpy.data.objects};poses={o:pose_batch.pose_state(o)for o in bpy.data.objects if o.type=='ARMATURE'}
 wants={};seen=set();candidates={};time=(context.scene,context.view_layer,context.scene.frame_current,context.scene.frame_subframe)
 for r in rows:
  want=copy.deepcopy(old[r['w']['action']]);delta=r['dest']-r['frame']
  for layer in want['layers']:
   for strip in layer['strips']:
    for _,curves in strip['bags']:
     for curve in curves:
      if (curve['data_path'],curve['array_index'])==(PATH,1):
       for k in curve['keys']:
        if k['co'][0]==r['frame']:
         for field in ('co','co_ui','handle_left','handle_right'):k[field][0]=exact(Fraction(k[field][0])+Fraction(delta))
  wants[r['arm']]=want
 def guard(notify=True):
  new=set(bpy.data.actions)-actions
  if not notify and not new<=seen:raise ValueError('Unknown Action appeared during record commit')
  if notify:
   for a in new-seen:a.use_fake_user=False;seen.add(a)
  if notify and checkpoint is not None and checkpoint()is False:return False
  new=set(bpy.data.actions)-actions
  if len(new)>2 or not new<=seen:raise ValueError('Unknown Action set drift')
  if time!=(context.scene,context.view_layer,context.scene.frame_current,context.scene.frame_subframe):raise ValueError('Time/context drift')
  if dw.capture(context)!=expected['display']or canon(structure(context))!=expected['structure']:raise ValueError('Display/structure drift')
  if bone_selection(context)!=expected['bone_selection']:raise ValueError('Bone selection drift')
  if context.mode!=expected['mode']or sorted(o.name for o in context.selected_objects)!=expected['selected']or (context.active_object.name if context.active_object else None)!=expected['active']:raise ValueError('Selection drift')
  for a in actions:
   if pc.action_witness(a)!=old[a]or pc.metadata_witness(a)!=meta[a]or a.use_fake_user!=(True if a in sources else fake[a]):raise ValueError('Source Action drift')
  for o,b in bindings.items():
   if o not in arms and native.binding_state(o)!=b:raise ValueError('Other owner binding drift')
  for o,p in poses.items():
   if pose_batch.pose_state(o)!=p:raise ValueError('Unkeyed pose drift')
  bound=[]
  for r in rows:
   a=r['arm'];w,_,_=pc.target(a,PATH,1,allow_body_colors=True)
   if w['action']!=r['w']['action']:
    c=w['action'];bound.append(c)
    if c not in new or c.use_fake_user or pc.action_witness(c)!=wants[a]or pc.metadata_witness(c)!=meta[r['w']['action']]:raise ValueError('Bound candidate differs from explicit request')
    exp=dict(bindings[a]);exp.update(action=c,slot=c.slots[0])
    if native.binding_state(a)!=exp:raise ValueError('Candidate binding mismatch')
  if bound:
   if len(bound)!=2 or set(bound)!=new:raise ValueError('Incomplete two-owner commit')
   native.resolve_targets(normalize_manifest(manifest),writing=True)
   if notify and participant is not None:
    participant()
    guard(False)
    participant(verify_only=True)
  return True
 _PERMIT={'addresses':{(r['arm'].as_pointer(),r['arm'].get('ac_instance_uuid'),PATH,1)for r in rows},'manifest':normalize_manifest(manifest)}
 for a in sources:a.use_fake_user=True
 try:return core.apply_manifest(manifest,checkpoint=guard)
 except BaseException:
  for a,v in fake.items():a.use_fake_user=v
  for o,p in poses.items():
   for name,v in p.items():
    pb=o.pose.bones[name];pb.rotation_euler=v['rotation'];pb.location=v['location'];pb.scale=v['scale']
  raise
 finally:_PERMIT=None
