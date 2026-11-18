"""Bound private-copy diagnostic reports. No save, apply, solver or live sampling."""
import json,math,hashlib
from pathlib import Path
from . import pose_gate,pose_channel,root_input,support_diagnostic as diag,read_lifecycle as h
from .review_core import service

def parameters(side,start,end,plane):
 h.need(side in ('L','R'),'明确选择小腿侧别')
 h.need(all(type(x)in(int,float)and math.isfinite(x)for x in(start,end,plane)),'有限区间/平面参数')
 count=float(end-start)*32
 h.need(-1e6<=start<end<=1e6 and count.is_integer()and count+1<=4097,'区间需1/32帧网格，最多4097采样')
 return {'side':side,'start':start,'end':end,'plane_z_m':plane,'step':1/32,'thresholds_relative_height':{'drift':.002,'height':.001,'speed_per_second':.01,'path':.004},'plane':'WORLD_HORIZONTAL_STATIC_FLAT_CAP','velocity':'ADJACENT_SAMPLE_AVERAGE_NOT_INSTANTANEOUS'}

def witness(bpy,uid):
 w=pose_gate.discover(bpy,uid,allow_root_location=True);a=w['rig'];r=w['root']
 data={'rig':a.name,'uuid':uid,'metadata':w['metadata'],'frame':bpy.context.scene.frame_current,'subframe':bpy.context.scene.frame_subframe,'unit':bpy.context.scene.unit_settings.scale_length,'fps':bpy.context.scene.render.fps,'fps_base':bpy.context.scene.render.fps_base,'root_action':pose_channel.action_witness(r.animation_data.action)if r.animation_data and r.animation_data.action else None,'rig_action':pose_channel.action_witness(w['action'])if w['action']else None,'rig_slot':w['slot'].identifier if w['slot']else None,'root_slot':r.animation_data.action_slot.identifier if r.animation_data and r.animation_data.action_slot else None,'raw_root':{'location':list(r.location),'rotation':list(r.rotation_euler),'scale':list(r.scale)},'pose':[(p.name,list(p.location),list(p.rotation_euler),list(p.scale))for p in a.pose.bones]}
 return json.loads(json.dumps(data,allow_nan=False)),w

def freeze(bpy,opts,nonce):
 service.context_gate(bpy);h.need(bpy.data.is_saved and not bpy.data.is_dirty,'必须先另存并保持干净；不读取未保存修改')
 arm=bpy.context.active_object;h.need(arm is not None and arm.type=='ARMATURE'and list(bpy.context.selected_objects)==[arm],'只选一个自有模板骨架（Object模式）')
 p=parameters(opts['side'],opts['start'],opts['end'],opts['plane_z_m']);source=h.safe(bpy.data.filepath);h.need(source.is_file(),'已保存源不存在')
 wi,_=witness(bpy,arm.get('ac_instance_uuid'))
 return {'schema':1,'kind':'FLAT_CAP_SUPPORT_DIAGNOSTIC','nonce':nonce,'scene':bpy.context.scene.name,'view_layer':bpy.context.view_layer.name,'source':str(source),'source_sha256':h.sha(source),'instance_uuid':arm['ac_instance_uuid'],'selected_rig':arm.name,'parameters':p,'witness':wi,'may_apply':False,'may_write_keys':False}

_RAW_FIELDS=('location','rotation_euler','rotation_quaternion','rotation_axis_angle','scale','delta_location','delta_rotation_euler','delta_rotation_quaternion','delta_scale')
def raw_snapshot(bpy):
 rows=[]
 for o in bpy.data.objects:
  rows.append((o,{k:list(getattr(o,k))for k in _RAW_FIELDS}))
  if o.type=='ARMATURE':
   for p in o.pose.bones:rows.append((p,{k:list(getattr(p,k))for k in _RAW_FIELDS if hasattr(p,k)}))
 return rows

def review(bpy,request,digest,output,worker_source,checkpoint=None):
 h.need(bpy.app.background,'Private background worker only');service.context_gate(bpy)
 h.need(service.digest(request)==digest and request['kind']=='FLAT_CAP_SUPPORT_DIAGNOSTIC','request binding')
 h.need(request['parameters']==parameters(**{k:request['parameters'][k]for k in('side','start','end')},plane=request['parameters']['plane_z_m']),'parameter contract')
 h.need(request['may_apply'] is False and request['may_write_keys'] is False,'readonly request')
 h.need(bpy.data.filepath==worker_source and h.sha(worker_source)==request['source_sha256']and h.sha(request['source'])==request['source_sha256'],'source/copy SHA')
 h.need((bpy.context.scene.name,bpy.context.view_layer.name)==(request['scene'],request['view_layer']),'scene/layer')
 wi,w=witness(bpy,request['instance_uuid']);h.need(wi==request['witness'],'saved-copy witness differs; no unsaved input capture')
 out=h.safe(output);out.mkdir(mode=0o700);service.write(out/'REQUEST.json',request)
 simple={'root':w['root'].name,'rig':w['rig'].name,'parts':{o.get('ac_part'):o.name for o in bpy.data.objects if o.parent==w['rig']and o.type=='MESH'}}
 before=service.capture(bpy,simple,out,'before');context_before=diag.state(bpy);raw=raw_snapshot(bpy);scene=bpy.context.scene;frame,sub=scene.frame_current,scene.frame_subframe;result=None;failure=None;restoration=[]
 try:
  if checkpoint:checkpoint('before')
  p=request['parameters'];result=diag.inspect(bpy,request['instance_uuid'],p['side'],p['start'],p['end'],p['plane_z_m'],p['step'],checkpoint=checkpoint)
  if checkpoint:checkpoint('after_sampling')
 except BaseException as exc:failure=exc
 finally:
  try:
   if bpy.context.mode!='OBJECT':bpy.ops.object.mode_set(mode='OBJECT')
   for obj in bpy.context.view_layer.objects:obj.select_set(obj.name in context_before['selected'])
   bpy.context.view_layer.objects.active=bpy.data.objects.get(context_before['active']) if context_before['active'] else None
   scene.frame_set(frame,subframe=sub)
   for obj,values in raw:
    for name,value in values.items():setattr(obj,name,value)
   bpy.context.view_layer.update()
  except BaseException as exc:restoration.append('restore:'+repr(exc))
  try:
   after=service.capture(bpy,simple,out,'after');h.need(diag.state(bpy)==context_before,'Private context/raw restoration mismatch');h.need(before==after,'Full private-worker state changed');h.need(witness(bpy,request['instance_uuid'])[0]==request['witness'],'post witness drift');h.need(h.sha(worker_source)==request['source_sha256']and h.sha(request['source'])==request['source_sha256'],'source protection')
  except BaseException as exc:restoration.append('verify:'+repr(exc))
  service.write(out/'RESTORATION.json',{'complete':not restoration,'errors':restoration,'sampling_failed':failure is not None})
 if restoration:raise RuntimeError('Private-state restoration failed: '+repr(restoration))
 if failure:raise failure
 h.need(result is not None and result['state_restored']is True,'missing complete diagnostic')
 report={'status':'REQUEST_BOUND_SUPPORT_DIAGNOSTIC','request':request,'request_sha256':digest,'diagnostic':result,'snapshot_unchanged':True,'source_unchanged':True,'may_apply':False,'may_write_keys':False}
 service.write(out/'RESULT.json',report)
 (out/'REPORT.zh-CN.md').write_text('# 静态水平平面小腿端盖支撑采样报告\n\n'+result['status']+'\n\n原因：'+', '.join(result['reasons'])+'\n\n固定端面最大切向漂移：'+str(result['max_drift_m'])+' 米；累计路程：'+str(result['max_path_m'])+' 米；最大相邻采样平均速度：'+str(result['peak_speed_m_s'])+' 米/秒（不是瞬时峰值）。\n\n源 SHA：'+request['source_sha256']+'；角色：'+request['selected_rig']+'；UUID：'+request['instance_uuid']+'\n\n这是有限采样，不是连续安全证明、不修复滑移、不生成动作、不保存工程。详细阈值、固定顶点、离地/穿地与全轨迹见 RESULT.json。',encoding='utf8')
 return report

def validate_result(r,request,digest):
 h.need(r['status']=='REQUEST_BOUND_SUPPORT_DIAGNOSTIC'and r['request']==request and r['request_sha256']==digest,'wrong diagnostic binding')
 h.need(r['snapshot_unchanged']is True and r['source_unchanged']is True and r['may_apply']is False and r['may_write_keys']is False,'incomplete protection')
 d=r['diagnostic'];h.need(d['status']in('SAMPLED_SUPPORT_PASS','SAMPLED_SUPPORT_FAIL')and d['state_restored']is True and d['continuous_proof']is False and d['may_write']is False,'diagnostic scope')
 p=request['parameters'];h.need([x['t']for x in d['rows']]==[p['start']+i*p['step']for i in range(int((p['end']-p['start'])/p['step'])+1)],'sample schedule')
