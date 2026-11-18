"""One saved, explicit pair of head.Y keys. Not an event graph or audio binding."""
import json,math,uuid,copy
import bpy
from . import pose_event as e,pose_channel as pc,pose_batch,display_witness as dw,ui
SCHEMA='pose-event-record-v1'
MAX_BYTES=2_000_000

def settings(context):return getattr(context.scene,ui.SETTINGS_PROPERTY)
def raw(context):return settings(context).pose_event_state

def _budget(value,depth=0,counter=None):
 if counter is None:counter=[0]
 counter[0]+=1
 if depth>24 or counter[0]>100000:raise ValueError('事件记录超出预算')
 if value is None or type(value)in(bool,str,int):
  if type(value)is str and len(value)>65536:raise ValueError('事件字符串过长')
  return
 if type(value)is float:
  if not math.isfinite(value):raise ValueError('事件记录非有限数')
  return
 if type(value)is list:
  if len(value)>10000:raise ValueError('事件数组过长')
  for v in value:_budget(v,depth+1,counter)
  return
 if type(value)is dict:
  if len(value)>4096 or any(type(k)is not str for k in value):raise ValueError('事件字段不支持')
  for v in value.values():_budget(v,depth+1,counter)
  return
 raise ValueError('事件记录类型不支持')

def parse(text):
 if type(text)is not str or len(text.encode('utf8'))>MAX_BYTES:raise ValueError('事件记录超出字节预算')
 if not text:return None
 def pairs(rows):
  d={}
  for k,v in rows:
   if k in d:raise ValueError('事件重复字段')
   d[k]=v
  return d
 try:r=json.loads(text,object_pairs_hook=pairs)
 except (RecursionError,json.JSONDecodeError)as ex:raise ValueError('事件JSON无效')from ex
 _budget(r)
 if type(r)is not dict or set(r)!={'schema','id','revision','event','rows','offset'}or r['schema']!=SCHEMA:raise ValueError('事件schema/字段不支持')
 if type(r['id'])is not str or len(r['id'])!=32 or any(c not in '0123456789abcdef'for c in r['id']):raise ValueError('事件ID无效')
 if type(r['revision'])is not int or not 1<=r['revision']<=1000000:raise ValueError('事件revision无效')
 if type(r['event'])is not str or not r['event'].strip()or len(r['event'])>128:raise ValueError('事件名称无效')
 if type(r['offset'])not in(int,float)or not math.isfinite(r['offset']):raise ValueError('事件offset无效')
 if type(r['rows'])is not list or len(r['rows'])!=2:raise ValueError('须有两角色记录')
 for row in r['rows']:
  if type(row)is not dict or set(row)!={'uuid','rig','frame','action','slot','witness','metadata'}:raise ValueError('记录目标字段无效')
  for k in ('uuid','rig','action'):
   if type(row[k])is not str or not row[k]or len(row[k])>256:raise ValueError('记录身份无效')
  if type(row['frame'])not in(int,float)or not math.isfinite(row['frame']):raise ValueError('记录帧无效')
  if type(row['slot'])is not list or len(row['slot'])!=2 or any(type(x)is not str for x in row['slot']):raise ValueError('记录slot无效')
  if type(row['witness'])is not dict or type(row['metadata'])is not dict:raise ValueError('记录见证无效')
 if r['rows'][0]['uuid']==r['rows'][1]['uuid']:raise ValueError('重复角色身份')
 if r['rows'][1]['frame']-r['rows'][0]['frame']!=r['offset']:raise ValueError('记录offset不一致')
 return r

def encode(r):
 text=json.dumps(r,ensure_ascii=False,sort_keys=True,allow_nan=False,separators=(',',':'))
 parse(text);return text

def stable_rows(context,params):
 rows=e.validate_sources(context,params);out=[]
 for row in rows:
  a=row['arm'];ident=a.get('ac_instance_uuid')
  if type(ident)is not str or not ident or sum(o.type=='ARMATURE' and o.get('ac_instance_uuid')==ident for o in bpy.data.objects)!=1:raise ValueError('角色UUID缺失或歧义')
  w=row['w'];act=w['action'];slot=w['slot']
  out.append({'uuid':ident,'rig':a.name,'frame':row['frame'],'action':act.name,'slot':[slot.identifier,slot.target_id_type],'witness':pc.action_witness(act),'metadata':pc.metadata_witness(act)})
 return e.canon(out)

def inspect(context):
 record=parse(raw(context))
 if record is None:raise ValueError('尚无保存事件，请先捕获')
 params={'event':record['event']}
 for side,row in zip(('a','b'),record['rows']):
  matches=[o for o in bpy.data.objects if o.type=='ARMATURE' and o.get('ac_instance_uuid')==row['uuid']]
  if len(matches)!=1 or matches[0].name!=row['rig']:raise ValueError('记录过期：角色身份/名称歧义')
  params.update({'rig_'+side:row['rig'],'frame_'+side:row['frame']})
 if json.dumps(stable_rows(context,params),sort_keys=True,allow_nan=False)!=json.dumps(record['rows'],sort_keys=True,allow_nan=False):raise ValueError('记录过期：Action/slot/键或锁定内容已改变；请显式重新捕获')
 return record,params

def capture_origin(context,params):
 prior=raw(context);parse(prior)
 prior_status='无记录'
 if prior:
  try:inspect(context);prior_status='现有记录有效'
  except Exception as ex:prior_status='现有记录失效：'+str(ex)
 return e.canon({'prior_status':prior_status,'raw':prior,'legacy':settings(context).event_state,'params':params,'rows':stable_rows(context,params),'global':e.global_origin(),'structure':e.structure(context),'display':dw.capture(context),'frame':[context.scene.frame_current,context.scene.frame_subframe],'scene':context.scene.as_pointer(),'layer':context.view_layer.name,'mode':context.mode,'selected':sorted(o.name for o in context.selected_objects),'active':context.active_object.name if context.active_object else None,'bones':e.bone_selection(context),'poses':{o.name:pose_batch.pose_state(o)for o in bpy.data.objects if o.type=='ARMATURE'}})

def capture(context,expected,checkpoint=None):
 if capture_origin(context,expected['params'])!=expected:raise ValueError('捕获预览失效')
 prev=parse(expected['raw']);record={'schema':SCHEMA,'id':uuid.uuid4().hex,'revision':1,'event':expected['params']['event'],'rows':expected['rows'],'offset':expected['params']['frame_b']-expected['params']['frame_a']}
 text=encode(record)
 try:
  settings(context).pose_event_state=text
  if checkpoint is not None and checkpoint()is False:raise ValueError('捕获已取消')
  if raw(context)!=text:raise ValueError('事件记录写入失败')
  again=capture_origin(context,expected['params']);again['raw']=expected['raw'];again['prior_status']=expected['prior_status']
  if again!=expected:raise ValueError('捕获写入期间源改变')
  return record
 except BaseException:
  if raw(context)==text:settings(context).pose_event_state=expected['raw']
  raise

def sync_origin(context,delta):
 record,params=inspect(context);params['delta']=delta
 rows,_=e.review(context,params)
 # Keep the prior raw-pose invariant. Do not silently reevaluate unkeyed pose.
 for row in rows:
  fc=row['w']['bag'].fcurves.find(e.PATH,index=1);keys=[]
  for k in fc.keyframe_points:keys.append((row['dest']if k.co.x==row['frame']else float(k.co.x),float(k.co.y),k.interpolation))
  t=context.scene.frame_current_final
  if t<=keys[0][0]:value=keys[0][1]
  elif t>=keys[-1][0]:value=keys[-1][1]
  else:
   left,right=next((a,b)for a,b in zip(keys,keys[1:])if a[0]<=t<b[0])
   value=left[1]if left[2]=='CONSTANT'else left[1]+(right[1]-left[1])*(t-left[0])/(right[0]-left[0])
  if abs(value-fc.evaluate(t))>1e-7:raise ValueError('当前播放头处目标求值会变化；请手动移到锁定端点再预览，避免覆盖未打键姿态')
 return {'raw':raw(context),'legacy':settings(context).event_state,'record':record,'edit':e.origin(context,params)}

def execute(context,expected,checkpoint=None,record_checkpoint=None):
 if sync_origin(context,expected['edit']['params']['delta'])!=expected:raise ValueError('事件或源预览失效')
 original=expected['raw'];legacy=expected['legacy'];written=None;newrecord=None
 def participant(verify_only=False):
  nonlocal written,newrecord
  if settings(context).event_state!=legacy:raise ValueError('旧事件状态漂移')
  if written is None:
   if verify_only:raise ValueError('事件记录尚未提交')
   if raw(context)!=original:raise ValueError('事件记录在事务中漂移')
   params=copy.deepcopy(expected['edit']['params']);delta=params.pop('delta')
   params['frame_a']+=delta;params['frame_b']+=delta
   newrecord=copy.deepcopy(expected['record']);newrecord['revision']+=1;newrecord['rows']=stable_rows(context,params)
   written=encode(newrecord)
   settings(context).pose_event_state=written
   if record_checkpoint is not None and record_checkpoint()is False:raise ValueError('记录提交已取消')
  if raw(context)!=written:raise ValueError('事件记录读回失败/外部漂移')
  actual,_=inspect(context)
  if actual!=newrecord:raise ValueError('事件请求读回不一致')
 def check():
  if settings(context).event_state!=legacy:raise ValueError('旧事件状态漂移')
  if raw(context)!=(written if written is not None else original):raise ValueError('持久事件状态漂移')
  return checkpoint()if checkpoint is not None else True
 try:return e.execute(context,expected['edit'],check,participant=participant)
 except BaseException:
  if written is not None and raw(context)==written:settings(context).pose_event_state=original
  raise
