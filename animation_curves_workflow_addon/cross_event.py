"""Narrow two head.Y + one default-tree LIGHT.energy transaction; private 354."""
import copy
from fractions import Fraction
import bpy
from . import pose_event as e,pose_channel as pc,pose_batch,native,core,display_witness as dw,light_event as light
from .contract import normalize_manifest
canon=e.canon

def light_state():
 out={}
 for d in bpy.data.lights:
  native.local(d)
  r=dw.rna(d)
  # Binding is independently and exactly guarded by native.binding_state.
  r.pop('animation_data',None)
  out[d.name]={'ptr':d.as_pointer(),'rna':r,'tree':dw.rna(d.node_tree) if d.node_tree else None,
    'objects':sorted(o.name for o in bpy.data.objects if o.type=='LIGHT' and o.data==d)}
 return canon(out)

def sources(context,p):
 if set(p)!={'rig_a','rig_b','frame_a','frame_b','event','light_object','light_frame'}:raise ValueError('Unknown cross-domain parameters')
 rows=[]
 for r in e.validate_sources(context,{k:v for k,v in p.items()if k not in ('light_object','light_frame')}):
  w=r['w'];rows.append(dict(r,owner=r['arm'],action=w['action'],slot=w['slot'],bag=w['bag'],path=e.PATH,index=1,kind='OBJECT'))
 r=light.validate(context,p['light_object'],p['light_frame'])
 rows.append(dict(r,path='energy',index=0,kind='LIGHT'))
 if len({r['action']for r in rows})!=3:raise ValueError('Three distinct exclusive Actions required')
 return rows

def review(context,p):
 delta=e.exact(Fraction(p['delta']));base={k:v for k,v in p.items()if k!='delta'}
 if delta==0:raise ValueError('Nonzero delta required')
 rows=sources(context,base);targets=[];bindings=[];events=[{'id':'move','frame':p['frame_a']}]
 for ident,r in zip(('a','b','light'),rows):
  t=r['frame'];dest=e.exact(Fraction(t)+Fraction(delta))
  if not -1e6<=dest<=1e6 or not r['previous']+native.EPSILON<dest<r['next']-native.EPSILON:raise ValueError('Destination collision/order/range')
  for f in ('co','handle_left','handle_right'):e.exact(Fraction(r['key'][f][0])+Fraction(delta))
  spec={'id':ident,'owner':{'type':r['kind'],'name':r['owner'].name},'data_path':r['path'],'array_index':r['index'],'sharing':'reject'}
  targets.append(spec);bindings.append({'event':'move','target':ident,'offset':e.exact(Fraction(t)-Fraction(p['frame_a'])),'expected_key':r['key']})
  fc=r['bag'].fcurves.find(r['path'],index=r['index'])
  for k in (fc.keyframe_points[0],fc.keyframe_points[-1]):
   eid=ident+str(k.co.x);events.append({'id':eid,'frame':float(k.co.x),'locked':True});bindings.append({'event':eid,'target':ident,'offset':0,'expected_key':native.key_state(k)})
  # Do not silently change current unkeyed input while rebinding.
  keys=[(dest if k.co.x==t else float(k.co.x),float(k.co.y),k.interpolation)for k in fc.keyframe_points];now=context.scene.frame_current_final
  if now<=keys[0][0]:value=keys[0][1]
  elif now>=keys[-1][0]:value=keys[-1][1]
  else:
   a,b=next((a,b)for a,b in zip(keys,keys[1:])if a[0]<=now<b[0]);value=a[1]if a[2]=='CONSTANT'else a[1]+(b[1]-a[1])*(now-a[0])/(b[0]-a[0])
  if abs(value-fc.evaluate(now))>1e-7:raise ValueError('当前播放头求值会改变，请移到锁定端点后重试')
  r.update(spec=spec,dest=dest)
 m={'schema':'1.0','operation':'edit','context':{'scene':context.scene.name,'view_layer':context.view_layer.name},'targets':targets,'operations':[],
 'event_shift':{'schema':'1.0','events':events,'dependencies':[],'event_id':'move','target_frame':e.exact(Fraction(p['frame_a'])+Fraction(delta)),'bindings':bindings}}
 return rows,m

def origin(context,p):
 rows,m=review(context,p)
 return canon({'params':p,'manifest':m,'global':e.global_origin(),'data_bindings':light.binding_origin(),'lights':light_state(),
 'structure':e.structure(context),'display':dw.capture(context),'frame':[context.scene.frame_current,context.scene.frame_subframe],
 'scene':context.scene.as_pointer(),'layer':context.view_layer.name,'mode':context.mode,'selected':sorted(o.name for o in context.selected_objects),
 'active':context.active_object.name if context.active_object else None,'bones':e.bone_selection(context),'poses':{o.name:pose_batch.pose_state(o)for o in bpy.data.objects if o.type=='ARMATURE'}})

def execute(context,expected,checkpoint=None,participant=None):
 if e.active()or pose_batch.active()or pc._ACTIVE is not None:raise ValueError('Nested cross-domain transaction')
 if origin(context,expected['params'])!=expected:raise ValueError('Cross-domain preview expired')
 rows,m=review(context,expected['params']);actions=set(bpy.data.actions)
 old={a:pc.action_witness(a)for a in actions};meta={a:pc.metadata_witness(a)for a in actions};fake={a:a.use_fake_user for a in actions}
 targets={r['owner']for r in rows};src={r['action']for r in rows};bindings={o:native.binding_state(o)for o in light.owners()}
 poses={o:pose_batch.pose_state(o)for o in bpy.data.objects if o.type=='ARMATURE'};seen=set();wants={}
 for r in rows:
  want=copy.deepcopy(old[r['action']]);delta=r['dest']-r['frame']
  for layer in want['layers']:
   for strip in layer['strips']:
    for _,curves in strip['bags']:
     for curve in curves:
      if (curve['data_path'],curve['array_index'])==(r['path'],r['index']):
       for k in curve['keys']:
        if k['co'][0]==r['frame']:
         for f in ('co','co_ui','handle_left','handle_right'):k[f][0]=e.exact(Fraction(k[f][0])+Fraction(delta))
  wants[r['owner']]=want
 def guard(notify=True):
  new=set(bpy.data.actions)-actions
  if not notify and not new<=seen:raise ValueError('Unknown Action during record commit')
  if notify:
   for a in new-seen:a.use_fake_user=False;seen.add(a)
  if notify and checkpoint is not None and checkpoint()is False:return False
  new=set(bpy.data.actions)-actions
  if len(new)>3 or not new<=seen:raise ValueError('Unknown Action set drift')
  if set(light.owners())!=set(bindings):raise ValueError('Owner collection drift')
  if light_state()!=expected['lights']or canon(e.structure(context))!=expected['structure']or dw.capture(context)!=expected['display']:raise ValueError('Light/tree/display/structure drift')
  if [context.scene.frame_current,context.scene.frame_subframe]!=expected['frame']or context.scene.as_pointer()!=expected['scene']or context.view_layer.name!=expected['layer']:raise ValueError('Time/context drift')
  if context.mode!=expected['mode']or e.bone_selection(context)!=expected['bones']or sorted(o.name for o in context.selected_objects)!=expected['selected']or (context.active_object.name if context.active_object else None)!=expected['active']:raise ValueError('Selection drift')
  for a in actions:
   if pc.action_witness(a)!=old[a]or pc.metadata_witness(a)!=meta[a]or a.use_fake_user!=(True if a in src else fake[a]):raise ValueError('Source Action drift')
  for o,b in bindings.items():
   if o not in targets and native.binding_state(o)!=b:raise ValueError('Non-target binding drift')
  for o,p in poses.items():
   if pose_batch.pose_state(o)!=p:raise ValueError('Pose drift')
  bound=[]
  for r in rows:
   o=r['owner'];a,slot,bag=native.action_binding(o,'OBJECT'if r['kind']=='OBJECT'else 'LIGHT',True)
   if a!=r['action']:
    bound.append(a)
    if a not in new or a.use_fake_user or pc.action_witness(a)!=wants[o]or pc.metadata_witness(a)!=meta[r['action']]:raise ValueError('Candidate differs from exact request')
    exp=dict(bindings[o]);exp.update(action=a,slot=slot)
    if native.binding_state(o)!=exp:raise ValueError('Candidate binding mismatch')
  if bound:
   if len(bound)!=3 or set(bound)!=new:raise ValueError('Incomplete three-owner commit')
   native.resolve_targets(normalize_manifest(m),writing=True)
   if notify and participant is not None:participant();guard(False);participant(verify_only=True)
  return True
 # Existing exact pose permit accepts only this manifest and these two pose addresses.
 e._PERMIT={'addresses':{(r['owner'].as_pointer(),r['owner'].get('ac_instance_uuid'),r['path'],r['index'])for r in rows[:2]},'manifest':normalize_manifest(m)}
 for a in src:a.use_fake_user=True
 try:return core.apply_manifest(m,checkpoint=guard)
 except BaseException:
  for a,v in fake.items():a.use_fake_user=v
  for o,p in poses.items():
   for name,v in p.items():
    pb=o.pose.bones[name];pb.rotation_euler=v['rotation'];pb.location=v['location'];pb.scale=v['scale']
  raise
 finally:e._PERMIT=None
