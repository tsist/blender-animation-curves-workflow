"""Portable explicit selected RNA records, not all-scene/dependency closure."""
import json,hashlib,gzip
OMIT={'rna_type','session_uid','users','is_updated','is_updated_data','is_updated_transform','is_valid','is_empty'}
def plain(v):
 if v is None or isinstance(v,(str,int,float,bool)):return v
 if hasattr(v,'to_list'):return plain(v.to_list())
 if hasattr(v,'keys'):return {str(k):plain(v[k]) for k in v.keys()}
 return [plain(x) for x in v]
def encoded(v):return json.dumps(plain(v),sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def scalar(item):
 out={}
 for prop in item.bl_rna.properties:
  name=prop.identifier
  if name in OMIT or prop.type not in {'BOOLEAN','INT','FLOAT','STRING','ENUM'}:continue
  v=getattr(item,name);out[name]=list(v) if getattr(prop,'is_array',False) else sorted(v) if isinstance(v,set) else v
 return plain(out)
def action_records(actions,out):
 result={};out.mkdir()
 for action in actions:
  digest=hashlib.sha256();count=0
  with gzip.open(out/(action.name.encode().hex()+'.jsonl.gz'),'xb') as stream:
   def emit(v):
    raw=encoded(v)+b'\n';digest.update(raw);stream.write(raw)
   emit({'action':scalar(action),'custom':dict(action.items())})
   emit({'slots':[scalar(s) for s in action.slots]})
   for li,layer in enumerate(action.layers):
    emit({'layer':li,'rna':scalar(layer)})
    for si,strip in enumerate(layer.strips):
     emit({'strip':si,'rna':scalar(strip)})
     for bi,bag in enumerate(strip.channelbags):
      emit({'bag':bi,'rna':scalar(bag),'groups':[scalar(g) for g in bag.groups]})
      for fi,fc in enumerate(bag.fcurves):
       if fc.modifiers:raise ValueError('Unsupported original FCurve modifier')
       emit({'curve':fi,'rna':scalar(fc),'group':fc.group.name if fc.group else None})
       for k in fc.keyframe_points:emit({'key':scalar(k)});count+=1
       for k in fc.sampled_points:emit({'sample':scalar(k)});count+=1
  result[action.name]={'sha256_uncompressed':digest.hexdigest(),'key_sample_count':count}
 return result
def original_objects(objects):
 result={}
 for o in objects:
  ad=o.animation_data
  if o.constraints or o.modifiers or (ad and (ad.drivers or ad.nla_tracks)):raise ValueError('Unsupported original dependencies')
  mesh=None
  if o.type=='MESH':mesh=hashlib.sha256(encoded({'vertices':[list(v.co) for v in o.data.vertices],'polygons':[list(p.vertices) for p in o.data.polygons]})).hexdigest()
  result[o.name]={'type':o.type,'data':o.data.name if o.data else None,'parent':o.parent.name if o.parent else None,'parent_type':o.parent_type,'matrix_basis':[list(r) for r in o.matrix_basis],'location':list(o.location),'rotation_euler':list(o.rotation_euler),'rotation_mode':o.rotation_mode,'scale':list(o.scale),'hide_viewport':o.hide_viewport,'hide_render':o.hide_render,'action':ad.action.name if ad and ad.action else None,'slot':ad.action_slot.identifier if ad and ad.action_slot else None,'mesh_sha256':mesh,'custom':plain(dict(o.items()))}
 return result
def layer_records(scene,names):
 result={}
 for layer in scene.view_layers:
  cols={}
  def visit(c):
   if c!=layer.layer_collection:
    if c.collection.name in cols:raise ValueError('Collection linked more than once')
    cols[c.collection.name]={k:getattr(c,k) for k in ('exclude','hide_viewport','holdout','indirect_only')}
   for child in c.children:visit(child)
  visit(layer.layer_collection)
  result[layer.name]={'collections':cols,'objects':{name:{'hide':scene.objects[name].hide_get(view_layer=layer),'selected':scene.objects[name].select_get(view_layer=layer)} for name in names},'active':layer.objects.active.name if layer.objects.active else None}
 return result
def scene_record(scene):return {'name':scene.name,'frame':scene.frame_current,'subframe':scene.frame_subframe,'camera':scene.camera.name if scene.camera else None,'range':[scene.frame_start,scene.frame_end],'fps':[scene.render.fps,scene.render.fps_base],'resolution':[scene.render.resolution_x,scene.render.resolution_y],'units':scalar(scene.unit_settings),'markers':[{'name':m.name,'frame':m.frame,'camera':m.camera.name if m.camera else None} for m in scene.timeline_markers]}

