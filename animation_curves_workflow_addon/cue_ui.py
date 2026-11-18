"""0.3.6 candidate UI: explicit cue parameters, frozen confirmation, one native Undo."""
import bpy,json
from fractions import Fraction
from bpy.props import StringProperty,EnumProperty,IntProperty
from . import cue_binding as binding,cue_transaction as tx,cross_event_store as store
_REGISTERED_CLASSES=[]
_OWNS_PROPERTY=False
def current_preview(c,op):
 d=binding.decide(bpm=op.bpm,fps=op.fps,start_frame=op.start_frame,beat=op.beat,cue_id=op.cue_id,policy=op.policy,
 chosen_frame=op.chosen_frame if op.policy=='explicit_integer'else None,tolerance_frames=op.tolerance if op.policy=='explicit_integer'else None)
 r,_=store.inspect(c)
 return tx.bound_preview(store,c,d,record_id=r['id'],revision=r['revision'])
def validate_saved(c):
 text=tx.raw(c)
 if not text:raise ValueError('No saved cue binding')
 if type(text)is not str or len(text.encode('utf8'))>1048576:raise ValueError('Cue record size limit')
 def pairs(rows):
  d={}
  for k,v in rows:
   if k in d:raise ValueError('Duplicate cue JSON field')
   d[k]=v
  return d
 r=json.loads(text,object_pairs_hook=pairs)
 if type(r)is not dict or set(r)!={'schema','event_id','event_revision','decision','fps','source_sha256'}or r['schema']!='explicit-cue-binding-v1':raise ValueError('Unknown cue record')
 binding.validate_decision(r['decision']);event,_=store.inspect(c)
 if r['event_id']!=event['id']or type(r['event_revision'])is not int or r['event_revision']!=event['revision']:raise ValueError('Cue event binding expired')
 if tx.encode(r['fps'])!=tx.encode(tx.fps_witness(c)):raise ValueError('Cue scene FPS changed')
 if Fraction(r['decision']['fps'])!=Fraction(r['fps']['fps'])/Fraction(r['fps']['fps_base']):raise ValueError('Cue decision FPS mismatch')
 if event['rows'][0]['frame']!=r['decision']['target_frame']:raise ValueError('Cue target mismatch')
 if type(r['source_sha256'])is not str or len(r['source_sha256'])!=64 or any(x not in '0123456789abcdef'for x in r['source_sha256']):raise ValueError('Invalid cue provenance')
 return r
class ACW_OT_cue_parameters(bpy.types.Operator):
 bl_idname='animation_curves.cue_parameters';bl_label='设置显式 Cue 拍点'
 cue_id:StringProperty(name='Cue ID',default='explicit-impact')
 bpm:StringProperty(name='BPM',default='120');fps:StringProperty(name='FPS',default='24')
 start_frame:StringProperty(name='起始帧',default='79');beat:StringProperty(name='拍点索引',default='0')
 policy:EnumProperty(name='决策',items=[('exact','精确目标（非精确拒绝）',''),('explicit_integer','显式选择整数帧','')],default='exact')
 chosen_frame:IntProperty(name='选择整数帧',default=79);tolerance:StringProperty(name='最大误差（帧）',default='0')
 def invoke(self,c,event):return c.window_manager.invoke_props_dialog(self,width=620,confirm_text='只读预览')
 def draw(self,c):
  for k in ('cue_id','bpm','fps','start_frame','beat','policy'):self.layout.prop(self,k)
  if self.policy=='explicit_integer':
   self.layout.prop(self,'chosen_frame');self.layout.prop(self,'tolerance')
  self.layout.label(text='显式输入；没有音频分析或自动舍入。仅首次绑定空cue字段。')
 def execute(self,c):
  try:
   p=current_preview(c,self)
   return bpy.ops.animation_curves.cue_confirm('INVOKE_DEFAULT',frozen=tx.encode(p))
  except Exception as ex:self.report({'ERROR'},str(ex));return {'CANCELLED'}
class ACW_OT_cue_confirm(bpy.types.Operator):
 bl_idname='animation_curves.cue_confirm';bl_label='确认显式 Cue 与三动作同事务';bl_options={'REGISTER','UNDO'}
 frozen:StringProperty(options={'HIDDEN'})
 def invoke(self,c,event):
  try:
   p=json.loads(self.frozen);b=p['bound'];fresh=tx.bound_preview(store,c,b['decision'],record_id=b['record_id'],revision=b['revision'])
   if tx.encode(fresh)!=tx.encode(p):raise ValueError('Cue preview expired')
   return c.window_manager.invoke_props_dialog(self,width=760,confirm_text='确认同事务改时与绑定')
  except Exception as ex:self.report({'ERROR'},str(ex));return {'CANCELLED'}
 def draw(self,c):
  p=json.loads(self.frozen);b=p['bound'];d=b['decision'];e=b['transaction'];delta=e['edit']['params']['delta'];l=self.layout
  l.label(text=f"Cue {d['cue_id']} / BPM {d['bpm']} / FPS {d['fps']} / 拍点 {d['beat']}")
  l.label(text=f"事件 {b['record_id']} / revision {b['revision']} → {b['revision']+1}")
  l.label(text=f"精确目标 {d['exact_frame']} / 选择 {d['target_frame']:g} / 策略 {d['policy']}")
  l.label(text=f"误差 {d['error_frames']} 帧 / {d['error_seconds']} 秒；共同偏移 {delta:g}")
  for r in e['record']['rows']:l.label(text=f"{r['object']} / {r['path']}[{r['index']}] {r['frame']:g} → {r['frame']+delta:g}")
  l.label(text='复制三动作、cross记录与cue同事务；保留相对偏移和相邻锁键。')
  l.label(text='撤销恢复旧动作求值，可能丢失已动画通道未打键输入。')
  l.label(text='不自动保存；不分析音频；仅首次绑定，已有记录不覆盖。')
 def execute(self,c):
  try:tx.execute(store,c,json.loads(self.frozen));self.report({'INFO'},'Cue与三动作同事务完成；请保存工程');return {'FINISHED'}
  except Exception as ex:self.report({'ERROR'},str(ex));return {'CANCELLED'}
class ACW_OT_cue_inspect(bpy.types.Operator):
 bl_idname='animation_curves.cue_inspect';bl_label='检查已保存 Cue 关联'
 def execute(self,c):
  try:r=validate_saved(c);self.report({'INFO'},f"Cue有效：{r['decision']['cue_id']} revision {r['event_revision']}");return {'FINISHED'}
  except Exception as ex:self.report({'ERROR'},str(ex));return {'CANCELLED'}
class ACW_PT_cue(bpy.types.Panel):
 bl_label='显式 Cue · 首次绑定';bl_idname='ACW_PT_cue';bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='动画曲线'
 def draw(self,c):
  self.layout.operator('animation_curves.cue_parameters');self.layout.operator('animation_curves.cue_inspect');self.layout.label(text='仅一次显式绑定；不分析音频')
def register():
 global _OWNS_PROPERTY
 if _REGISTERED_CLASSES or hasattr(bpy.types.Scene,'acw_experimental_cue'):raise RuntimeError('Cue module already registered or property occupied')
 bpy.types.Scene.acw_experimental_cue=StringProperty(default='');_OWNS_PROPERTY=True
 try:
  for cls in (ACW_OT_cue_parameters,ACW_OT_cue_confirm,ACW_OT_cue_inspect,ACW_PT_cue):bpy.utils.register_class(cls);_REGISTERED_CLASSES.append(cls)
 except BaseException:
  unregister();raise
def unregister():
 global _OWNS_PROPERTY
 while _REGISTERED_CLASSES:bpy.utils.unregister_class(_REGISTERED_CLASSES.pop())
 if _OWNS_PROPERTY:
  if hasattr(bpy.types.Scene,'acw_experimental_cue'):del bpy.types.Scene.acw_experimental_cue
  _OWNS_PROPERTY=False
