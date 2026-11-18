"""One persisted two-pose/one-light record; not music or a general event graph."""
import copy,json,uuid,math
import bpy
from . import cross_event as x,pose_event as e,pose_event_store as prior,pose_channel as pc,light_event as light
SCHEMA='cross-domain-event-v1'
def require_empty_cue(c):
 from .cue_state import read,require_no_shadow
 require_no_shadow(c)
 if read(c)!='':raise ValueError('已有或未知Cue记录，禁止通过旧跨域入口修改；请保留工程并检查关联')
def settings(c):return prior.settings(c)
def raw(c):return settings(c).cross_domain_event_state

def parse(text):
 if type(text)is not str or len(text.encode('utf8'))>prior.MAX_BYTES:raise ValueError('Cross record byte budget')
 if not text:return None
 def pairs(rows):
  d={}
  for k,v in rows:
   if k in d:raise ValueError('Duplicate JSON field')
   d[k]=v
  return d
 r=json.loads(text,object_pairs_hook=pairs);prior._budget(r)
 if type(r)is not dict or set(r)!={'schema','id','revision','event','rows','offsets','source'}or r['schema']!=SCHEMA:raise ValueError('Unknown cross record schema')
 if type(r['id'])is not str or len(r['id'])!=32 or any(c not in '0123456789abcdef'for c in r['id']):raise ValueError('Invalid record ID')
 if type(r['revision'])is not int or not 1<=r['revision']<=1000000:raise ValueError('Invalid revision')
 if type(r['event'])is not str or not r['event'].strip()or len(r['event'])>128:raise ValueError('Invalid event name')
 if type(r['source'])is not dict or set(r['source'])!={'id','revision','record'}:raise ValueError('Invalid source provenance')
 source=prior.parse(r['source']['record'])
 if source is None or source['id']!=r['source']['id']or source['revision']!=r['source']['revision']:raise ValueError('Invalid source provenance witness')
 if type(r['rows'])is not list or len(r['rows'])!=3:raise ValueError('Exactly three rows required')
 for i,row in enumerate(r['rows']):
  fields={'kind','path','index','object','data','uuid','frame','action','slot','witness','metadata','light'}
  if type(row)is not dict or set(row)!=fields:raise ValueError('Invalid row fields')
  if (row['kind'],row['path'],row['index'])!=(('ARMATURE',e.PATH,1)if i<2 else('LIGHT','energy',0)) or type(row['index'])is not int:raise ValueError('Invalid typed row')
  for k in ('object','data','action'):
   if type(row[k])is not str or not row[k]or len(row[k])>256:raise ValueError('Invalid identity')
  if type(row['uuid'])is not str or (i<2 and not row['uuid'])or (i==2 and row['uuid']!=''):raise ValueError('Invalid UUID')
  if type(row['frame'])not in(int,float)or not math.isfinite(row['frame']):raise ValueError('Invalid frame')
  if type(row['slot'])is not list or len(row['slot'])!=2 or any(type(v)is not str for v in row['slot']):raise ValueError('Invalid slot')
  if any(type(row[k])is not dict for k in ('witness','metadata','light')):raise ValueError('Invalid witness')
 if r['rows'][0]['uuid']==r['rows'][1]['uuid']:raise ValueError('Duplicate rig UUID')
 if type(r['offsets'])is not list or len(r['offsets'])!=2 or any(type(v)not in(int,float)or not math.isfinite(v)for v in r['offsets']):raise ValueError('Invalid offsets')
 if r['offsets']!=[q['frame']-r['rows'][0]['frame']for q in r['rows'][1:]]:raise ValueError('Offset mismatch')
 return r

def encode(r):
 text=json.dumps(r,ensure_ascii=False,sort_keys=True,allow_nan=False,separators=(',',':'));parse(text);return text

def semantic_action(w):
 # Only key UI selection flags at the explicit Action-witness path.
 # Authored custom dictionaries and every other RNA field remain intact.
 w=copy.deepcopy(w)
 for layer in w['layers']:
  for strip in layer['strips']:
   for slot,curves in strip['bags']:
    for curve in curves:
     for key in curve['keys']:
      for field in ('select_control_point','select_left_handle','select_right_handle'):
       key.pop(field,None)
 return w

def semantic_rows(rows):
 rows=copy.deepcopy(rows)
 for row in rows:
  row['witness']=semantic_action(row['witness'])
  if row['kind']=='LIGHT':row['light']['action']=semantic_action(row['light']['action'])
 return rows

def stable_light(r):
 w=light.witness(r)
 def stable_rna(v,key=''):
  if isinstance(v,dict):
   # Only RNA records have these runtime properties. Authored custom trees
   # are opaque pure values: even keys named session_uid/ptr are protected.
   return {k:(copy.deepcopy(a)if k=='custom'else stable_rna(a,k))for k,a in v.items()if k not in ('session_uid','is_runtime_data')}
  if isinstance(v,list):
   if key in ('identity','reference','original','node_tree','node','from_node','to_node','from_socket','to_socket')and len(v)==3 and type(v[1])is int:return [v[0],v[2]]
   return [stable_rna(a)for a in v]
  return v
 # Exact paths, not recursive field-name deletion in authored dictionaries.
 for k in ('object_ptr','data_ptr','object_data','energy'):w.pop(k,None)
 w['rna'].pop('animation_data',None)
 w['rna'].pop('energy',None) # Current evaluated energy varies with playhead.
 w['rna']=stable_rna(w['rna'])
 w['tree'].pop('ptr',None)
 w['tree']['rna']=stable_rna(w['tree']['rna'])
 w['tree']['nodes']=[stable_rna(n)for n in w['tree']['nodes']]
 return w

def stable_rows(c,p):
 rows=x.sources(c,p);out=[]
 for i,r in enumerate(rows):
  o=r['owner'];obj=r['arm']if i<2 else r['object'];a=r['action'];slot=r['slot']
  ident=obj.get('ac_instance_uuid')if i<2 else ''
  if i<2 and (type(ident)is not str or not ident or sum(q.type=='ARMATURE'and q.get('ac_instance_uuid')==ident for q in bpy.data.objects)!=1):raise ValueError('Rig identity ambiguous')
  out.append({'kind':'ARMATURE'if i<2 else'LIGHT','path':r['path'],'index':r['index'],'object':obj.name,'data':obj.data.name,'uuid':ident,
   'frame':r['frame'],'action':a.name,'slot':[slot.identifier,slot.target_id_type],'witness':pc.action_witness(a),'metadata':pc.metadata_witness(a),'light':{}if i<2 else stable_light(r)})
 return semantic_rows(e.canon(out))

def params(record):
 r=record['rows'];return {'rig_a':r[0]['object'],'rig_b':r[1]['object'],'frame_a':r[0]['frame'],'frame_b':r[1]['frame'],'event':record['event'],'light_object':r[2]['object'],'light_frame':r[2]['frame']}

def inspect(c):
 record=parse(raw(c))
 if record is None:raise ValueError('No cross-domain record')
 p=params(record)
 if json.dumps(stable_rows(c,p),sort_keys=True)!=json.dumps(semantic_rows(record['rows']),sort_keys=True):raise ValueError('Cross-domain record expired; explicitly recapture')
 return record,p

def snapshot(c,p):
 # Capture is read-only; no fabricated delta probe.
 return e.canon({'params':p,'rows':stable_rows(c,p),'global':e.global_origin(),'data_bindings':light.binding_origin(),'lights':x.light_state(),'structure':e.structure(c),'display':x.dw.capture(c),
 'frame':[c.scene.frame_current,c.scene.frame_subframe],'scene':c.scene.as_pointer(),'layer':c.view_layer.name,'mode':c.mode,'selected':sorted(o.name for o in c.selected_objects),
 'active':c.active_object.name if c.active_object else None,'bones':e.bone_selection(c),'poses':{o.name:x.pose_batch.pose_state(o)for o in bpy.data.objects if o.type=='ARMATURE'}})

def capture_origin(c,light_object,light_frame):
 require_empty_cue(c)
 source,p=prior.inspect(c);p.update(light_object=light_object,light_frame=light_frame)
 old=raw(c);parse(old);status='无跨域记录'
 if old:
  try:inspect(c);status='现有跨域记录有效'
  except Exception as ex:status='现有跨域记录失效：'+str(ex)
 return {'prior_status':status,'raw':old,'legacy':settings(c).event_state,'pose':settings(c).pose_event_state,'source':source,'snapshot':snapshot(c,p)}

def capture(c,expected,checkpoint=None):
 require_empty_cue(c)
 p=expected['snapshot']['params']
 if capture_origin(c,p['light_object'],p['light_frame'])!=expected:raise ValueError('Capture preview expired')
 record={'schema':SCHEMA,'id':uuid.uuid4().hex,'revision':1,'event':p['event'],'rows':expected['snapshot']['rows'],
 'offsets':[p['frame_b']-p['frame_a'],p['light_frame']-p['frame_a']],
 'source':{'id':expected['source']['id'],'revision':expected['source']['revision'],'record':expected['pose']}}
 written=encode(record)
 try:
  settings(c).cross_domain_event_state=written
  if checkpoint is not None and checkpoint()is False:raise ValueError('Capture cancelled')
  require_empty_cue(c)
  if raw(c)!=written or settings(c).event_state!=expected['legacy']or settings(c).pose_event_state!=expected['pose']or snapshot(c,p)!=expected['snapshot']:raise ValueError('Capture source/record drift')
  require_empty_cue(c)
  return record
 except BaseException:
  if raw(c)==written:settings(c).cross_domain_event_state=expected['raw']
  raise

def sync_origin(c,delta):
 require_empty_cue(c)
 record,p=inspect(c);p['delta']=delta
 return {'raw':raw(c),'legacy':settings(c).event_state,'pose':settings(c).pose_event_state,'record':record,'edit':x.origin(c,p)}

def execute(c,expected,checkpoint=None,record_checkpoint=None,participant_extension=None):
 require_empty_cue(c)
 if sync_origin(c,expected['edit']['params']['delta'])!=expected:raise ValueError('Cross preview expired')
 original=expected['raw'];written=None;record=None
 def old_json():
  if settings(c).event_state!=expected['legacy']or settings(c).pose_event_state!=expected['pose']:raise ValueError('Old JSON drift')
 def legacy_cue_guard():
  if participant_extension is None:require_empty_cue(c)
 def participant(verify_only=False):
  nonlocal written,record
  legacy_cue_guard()
  old_json()
  if written is None:
   if verify_only or raw(c)!=original:raise ValueError('Record phase drift')
   p=copy.deepcopy(expected['edit']['params']);d=p.pop('delta')
   for k in ('frame_a','frame_b','light_frame'):p[k]+=d
   record=copy.deepcopy(expected['record']);record['revision']+=1;record['rows']=stable_rows(c,p)
   written=encode(record);settings(c).cross_domain_event_state=written
   if record_checkpoint is not None and record_checkpoint()is False:raise ValueError('Record cancelled')
   legacy_cue_guard()
  if participant_extension is not None:participant_extension(verify_only=verify_only)
  if raw(c)!=written or inspect(c)[0]!=record:raise ValueError('Record readback drift')
  legacy_cue_guard()
 def check():
  legacy_cue_guard()
  old_json()
  if raw(c)!=(written if written is not None else original):raise ValueError('Record external drift')
  result=checkpoint()if checkpoint is not None else True
  legacy_cue_guard()
  return result
 try:return x.execute(c,expected['edit'],check,participant)
 except BaseException:
  if written is not None and raw(c)==written:settings(c).cross_domain_event_state=original
  raise
