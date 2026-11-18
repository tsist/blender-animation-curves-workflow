"""Narrow saved-source bilateral cap experiment. Never samples the user's live scene."""
import json,math,hashlib
from pathlib import Path
from . import support_diagnostic_service as ds,pose_channel,pose_gate,read_lifecycle as h
from .review_core import service
VERSION='BILATERAL_CAP_1'
def canon(x):return json.loads(json.dumps(x,sort_keys=True,allow_nan=False))
def resources():
 root=Path(__file__).parent
 return {str(p.relative_to(root)):h.sha(p)for p in sorted(root.rglob('*'))if p.is_file()and p.suffix in('.py','.json')}
def params(opts):
 s,e,z=(opts[k]for k in('start','end','plane_z_m'))
 h.need(all(type(x)in(int,float)and math.isfinite(x)for x in(s,e,z)),'有限帧/平面参数')
 h.need(-1e6<=s<e<=1e6 and float(s*4).is_integer()and float(e*4).is_integer()and (e-s)*32+1<=4097,'1/4帧网格，最多4097采样')
 return {'start':s,'end':e,'plane_z_m':z,'sample_step':1/32,'key_step':1/4,'length_tolerance_m':1e-6,'cap_tolerance_m':1e-5}
def gate(bpy,uid):
 w=pose_gate.discover(bpy,uid,allow_root_location=True);a=w['rig'];r=w['root']
 h.need(w['action']is None and w['slot']is None,'已有姿态Action/slot，整单拒绝')
 h.need(bpy.context.scene.unit_settings.scale_length==1,'首版仅单位比例1米')
 h.need(tuple(r.rotation_euler)==(0,0,0)and tuple(r.scale)==(1,1,1),'首版root旋转零/尺度一')
 for pb in a.pose.bones:h.need(tuple(pb.location)==(0,0,0)and tuple(pb.rotation_euler)==(0,0,0)and tuple(pb.scale)==(1,1,1),'必须全骨rest原始姿态')
 h.need(r.animation_data and r.animation_data.action and r.animation_data.action_slot,'需要已有root location Action')
 bag=r.animation_data.action.layers[0].strips[0].channelbag(r.animation_data.action_slot)
 h.need(all(k.interpolation=='LINEAR'and k.handle_left_type=='FREE'and k.handle_right_type=='FREE'for fc in bag.fcurves for k in fc.keyframe_points),'root仅LINEAR/FREE')
 return w
def freeze(bpy,opts,nonce):
 base=ds.freeze(bpy,dict(opts,side='L'),nonce);gate(bpy,base['instance_uuid']);p=params(opts)
 stable={'version':VERSION,'source':base['source'],'source_sha256':base['source_sha256'],'source_bytes':Path(base['source']).stat().st_size,'instance_uuid':base['instance_uuid'],'scene':base['scene'],'view_layer':base['view_layer'],'witness':base['witness'],'parameters':p,'resources':resources()}
 mode=opts.get('mode','PRECHECK');h.need(mode in('PRECHECK','GENERATE'),'mode')
 if mode=='GENERATE':h.need(opts.get('approved_stable')==stable,'预检后输入/参数/资源变化，请重做预检')
 base.update(kind='BILATERAL_CAP_WORKFLOW',parameters=p,mode=mode,stable_payload=stable,stable_digest=service.digest(stable),may_write_keys=False)
 return base

def capture(bpy,a):
 from .review_core import portable as pt,state_reader
 class S:
  def __init__(self):
   self.bpy=bpy;self.portable=pt;self.manifest={'created_owned_object_names':[o.name for o in bpy.context.scene.objects],'protected_original_object_names':[]}
  def pose_locks(self):return {o.name:{b.name:{'rotation_mode':b.rotation_mode,'euler':list(b.rotation_euler),'axis_angle':list(b.rotation_axis_angle),'locks':list(b.lock_rotation)}for b in o.pose.bones}for o in bpy.data.objects if o.type=='ARMATURE'}
 state=state_reader.NativeBackend.protected_state(S())
 mats={m.name:{'rna':pt.scalar(m),'custom':pt.plain(dict(m.items())),'nodes':[{'rna':pt.scalar(n),'inputs':{v.identifier:pt.plain(v.default_value)for v in n.inputs if hasattr(v,'default_value')}}for n in m.node_tree.nodes]if m.use_nodes else [],'links':[(l.from_node.name,l.from_socket.identifier,l.to_node.name,l.to_socket.identifier)for l in m.node_tree.links]if m.use_nodes else []}for m in bpy.data.materials}
 return canon({'scope':state,'materials':mats,'actions':{ac.name:pose_channel.action_witness(ac)for ac in bpy.data.actions},'objects':{o.name:{'parent':o.parent.name if o.parent else None,'type':o.type,'location':list(o.location),'rotation':list(o.rotation_euler),'scale':list(o.scale)}for o in bpy.data.objects},'meshes':{o.name:{'v':[list(v.co)for v in o.data.vertices],'f':[list(f.vertices)for f in o.data.polygons]}for o in bpy.data.objects if o.type=='MESH'},'raw_pose':{b.name:[list(b.location),list(b.rotation_euler),list(b.scale)]for b in a.pose.bones},'frame':bpy.context.scene.frame_current,'subframe':bpy.context.scene.frame_subframe})
def protected_scope(snapshot,rig):
 x=canon(snapshot);x.pop('actions');x['scope']['data_inventory'].pop('actions')
 x['scope']['owned'][rig]['action']=None;x['scope']['owned'][rig]['slot']=None
 for n in('thigh.L','shin.L','thigh.R','shin.R'):
  x['raw_pose'][n][1]=[0,0,0];x['scope']['pose_locks'][rig][n]['euler']=[0,0,0]
 return x
def locate(bpy,uid):
 arms=[o for o in bpy.data.objects if o.type=='ARMATURE'and o.get('ac_instance_uuid')==uid];h.need(len(arms)==1,'rig身份不唯一');return arms[0]
def at(bpy,t):bpy.context.scene.frame_set(math.floor(t),subframe=t-math.floor(t))
def mesh_points(bpy):
 dg=bpy.context.evaluated_depsgraph_get();result={}
 for o in bpy.data.objects:
  if o.type=='MESH':
   ev=o.evaluated_get(dg);result[o.name]=[ev.matrix_world@v.co for v in ev.data.vertices]
 return result

def review(bpy,request,digest,output,worker_source,checkpoint=None):
 from mathutils import Vector
 from .leg_contract import signed_flexion
 h.need(bpy.app.background and Path(bpy.data.filepath).resolve()==Path(worker_source).resolve(),'仅本轮私有后台副本允许采样')
 h.need(service.digest(request)==digest and request['stable_digest']==service.digest(request['stable_payload']),'request binding')
 h.need(h.sha(worker_source)==request['source_sha256']and h.sha(request['source'])==request['source_sha256'],'源SHA')
 bpy.context.window.scene=bpy.data.scenes[request['scene']];bpy.context.window.view_layer=bpy.context.scene.view_layers[request['view_layer']]
 wi,_=ds.witness(bpy,request['instance_uuid']);h.need(wi==request['witness'],'保存源witness不同')
 h.need(resources()==request['stable_payload']['resources'],'资源变化')
 w=gate(bpy,request['instance_uuid']);a=w['rig'];root=w['root'];before=capture(bpy,a);p=request['parameters'];root_witness=canon(pose_channel.action_witness(root.animation_data.action));root_slot=root.animation_data.action_slot.identifier
 output.mkdir(mode=0o700,parents=False,exist_ok=False);checkpoint=checkpoint or(lambda stage:None)
 targets={};plans=[];reject=[];candidate=None
 try:
  at(bpy,p['start']);start=root.location.copy();dg=bpy.context.evaluated_depsgraph_get();ev=a.evaluated_get(dg);world=ev.matrix_world;points=mesh_points(bpy)
  for side in('L','R'):
   th=ev.pose.bones['thigh.'+side];sh=ev.pose.bones['shin.'+side];objs=[o for o in bpy.data.objects if o.parent==a and o.get('ac_part')=='shin.'+side];h.need(len(objs)==1,'shin身份')
   axis=(world.to_3x3()@th.matrix.to_3x3()).col[0];h.need((axis-Vector((1,0,0))).length<1e-6,'铰链轴不支持')
   cap=[points[objs[0].name][i]for i in range(12,24)];h.need(max(abs(v.z-p['plane_z_m'])for v in cap)<=1e-5,'初始端盖不在声明平面')
   H,K,E=world@th.head,world@sh.head,world@sh.tail
   targets[side]={'H':list(H),'K':list(K),'cap':[list(v)for v in cap],'mesh':objs[0].name,'rest_angle':signed_flexion(H,K,E,axis)}
  for i in range(round((p['end']-p['start'])*32)+1):
   t=p['start']+i/32;at(bpy,t);q=root.location-start;row={'time':t,'legs':{}}
   for side,b in targets.items():
    u=Vector(b['K'])-Vector(b['H']);v=u-q;phi=math.atan2(u.y*v.z-u.z*v.y,u.y*v.y+u.z*v.z);res=abs(v.length-u.length);plane=abs(v.x-u.x);angle=b['rest_angle']-math.degrees(phi)
    row['legs'][side]={'phi':phi,'length_residual':res,'plane_residual':plane,'knee':angle}
    if res>1e-6 or plane>1e-6 or not 0<=angle<=150:reject.append({'time':t,'side':side,'length_residual':res,'plane_residual':plane,'knee':angle})
   plans.append(row)
  checkpoint('prechecked')
  if reject:status='PRECHECK_REJECTED'
  elif request['mode']=='PRECHECK':status='PRECHECK_ELIGIBLE'
  else:
   h.need(a.animation_data is None or a.animation_data.action is None,'pose conflict')
   for row in plans[::8]:
    for side,b in row['legs'].items():
     for bone,value in(('thigh.',b['phi']),('shin.',-b['phi'])):
      pb=a.pose.bones[bone+side];pb.rotation_euler.x=value;pb.keyframe_insert(data_path='rotation_euler',index=0,frame=row['time'])
   candidate=a.animation_data.action;candidate.name='ACW_Bilateral_Cap_Candidate'
   for fc in candidate.layers[0].strips[0].channelbag(a.animation_data.action_slot).fcurves:
    for k in fc.keyframe_points:k.interpolation='LINEAR';k.handle_left_type=k.handle_right_type='FREE'
   import struct
   f32=lambda v:struct.unpack('f',struct.pack('f',v))[0]
   bag=candidate.layers[0].strips[0].channelbag(a.animation_data.action_slot)
   requested={('pose.bones["'+bone+side+'"].rotation_euler',0):[(f32(row['time']),f32(sign*row['legs'][side]['phi']))for row in plans[::8]]for side in('L','R')for bone,sign in(('thigh.',1),('shin.',-1))}
   h.need(len(bag.fcurves)==4 and {(fc.data_path,fc.array_index)for fc in bag.fcurves}==set(requested),'仅四条请求曲线')
   for fc in bag.fcurves:
    h.need([(float(k.co.x),float(k.co.y))for k in fc.keyframe_points]==requested[(fc.data_path,fc.array_index)],'请求键读回')
    h.need(all(k.interpolation=='LINEAR'and k.handle_left_type=='FREE'and k.handle_right_type=='FREE'for k in fc.keyframe_points),'请求插值读回')
   expected_candidate=canon(pose_channel.action_witness(candidate));candidate_name=candidate.name
   def candidate_check(arm):
    h.need(set(bpy.data.actions.keys())==set(before['actions'])|{candidate_name},'Action集合越界')
    h.need(arm.animation_data and arm.animation_data.action and arm.animation_data.action.name==candidate_name,'候选绑定变化')
    h.need(canon(pose_channel.action_witness(arm.animation_data.action))==expected_candidate,'候选请求见证变化')
   checkpoint('created');candidate_check(a)
   def validate_live():
    arm=locate(bpy,request['instance_uuid']);rt=arm.parent
    h.need(canon(pose_channel.action_witness(rt.animation_data.action))==root_witness and rt.animation_data.action_slot.identifier==root_slot,'root Action改变')
    maxima={'cap_drift':0.,'cap_height':0.,'penetration':0.}
    for row in plans:
     at(bpy,row['time']);dg=bpy.context.evaluated_depsgraph_get();ar=arm.evaluated_get(dg);W=ar.matrix_world;pts=mesh_points(bpy)
     for side,b in targets.items():
      vs=pts[b['mesh']];drift=max((vs[j]-Vector(b['cap'][j-12])).length for j in range(12,24));height=max(abs(vs[j].z-p['plane_z_m'])for j in range(12,24));th=ar.pose.bones['thigh.'+side];sh=ar.pose.bones['shin.'+side];angle=signed_flexion(W@th.head,W@sh.head,W@sh.tail,(W.to_3x3()@th.matrix.to_3x3()).col[0])
      h.need(drift<=1e-5 and height<=1e-5 and 0<=angle<=150,'候选端盖/膝采样失败');maxima['cap_drift']=max(maxima['cap_drift'],drift);maxima['cap_height']=max(maxima['cap_height'],height)
     minimum=min(v.z for name,vs in pts.items()for v in vs);h.need(minimum>=p['plane_z_m']-1e-5,'实体穿地');maxima['penetration']=max(maxima['penetration'],p['plane_z_m']-minimum)
    return maxima
   metrics=validate_live();at(bpy,before['frame']+before['subframe']);candidate_check(a);now=capture(bpy,a)
   h.need(protected_scope(now,a.name)==protected_scope(before,a.name),'完整非目标范围变化')
   for name,old in before['actions'].items():h.need(now['actions'].get(name)==old,'原Action改变')
   h.need(now['meshes']==before['meshes']and now['objects']==before['objects'],'非目标物体/几何改变')
   for n,v in before['raw_pose'].items():
    if n not in('thigh.L','shin.L','thigh.R','shin.R'):h.need(now['raw_pose'][n]==v,'非目标姿态改变')
   expected=now;path=output/'candidate.blend';h.need(not path.exists(),'禁止覆盖')
   checkpoint('before_save');candidate_check(a);h.need(capture(bpy,a)==expected,'保存前漂移');bpy.ops.wm.save_as_mainfile(filepath=str(path));checkpoint('after_save');candidate_sha=h.sha(path)
   bpy.ops.wm.open_mainfile(filepath=str(path),load_ui=False,use_scripts=False);checkpoint('reopened')
   a=locate(bpy,request['instance_uuid']);candidate_check(a);h.need(capture(bpy,a)==expected,'磁盘重开数据不符');metrics=validate_live();h.need(h.sha(path)==candidate_sha,'candidate改变')
   status='SAMPLED_CANDIDATE_READY'
 finally:
  if candidate is None:
   at(bpy,before['frame']+before['subframe']);h.need(capture(bpy,a)==before,'私有预检状态未恢复')
 h.need(h.sha(request['source'])==request['source_sha256']and h.sha(worker_source)==request['source_sha256'],'源字节变化')
 r={'schema':1,'status':status,'request':request,'request_sha256':digest,'stable_digest':request['stable_digest'],'source_unchanged':True,'samples':len(plans),'rejections':reject,'same_process_disk_reopen':status=='SAMPLED_CANDIDATE_READY','continuous_proof':False,'may_apply_live':False}
 if status=='SAMPLED_CANDIDATE_READY':r.update(candidate=str(path),candidate_sha256=candidate_sha,metrics=metrics)
 service.write(output/'RESULT.json',r);(output/'REPORT.zh-CN.md').write_text('状态：'+status+'\n有限采样；非步行/通用IK。独立文件不受Ctrl+Z撤销。\n'+('同进程磁盘重开验证；非独立进程冷开。'if status=='SAMPLED_CANDIDATE_READY'else'预检未创建候选。'))
 return r

def validate_result(r,request,digest,output):
 h.need(r['request']==request and r['request_sha256']==digest and r['stable_digest']==request['stable_digest']and r['source_unchanged']and not r['continuous_proof']and not r['may_apply_live'],'结果绑定/范围')
 allowed=('PRECHECK_REJECTED','PRECHECK_ELIGIBLE')if request['mode']=='PRECHECK'else('PRECHECK_REJECTED','SAMPLED_CANDIDATE_READY')
 h.need(r['status']in allowed,'结果状态')
 if r['status']=='SAMPLED_CANDIDATE_READY':
  path=Path(output)/'candidate.blend';h.need(r['candidate']==str(path)and r['same_process_disk_reopen']and h.sha(path)==r['candidate_sha256'],'候选路径/SHA')
