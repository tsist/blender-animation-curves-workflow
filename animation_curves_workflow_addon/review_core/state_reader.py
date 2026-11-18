"""Finite read-only state serializer reused without any B mutation/schema gates."""
def need(v,m):
 if not v:raise ValueError(m)
class NativeBackend:
 def protected_state(self):
   # Full finite state owned by this workflow, not an all-RNA universe claim.
   bpy=self.bpy;p=self.portable;scene=bpy.context.scene;names=[o.name for o in scene.objects];owned={}
   for name in self.manifest['created_owned_object_names']:
    o=bpy.data.objects[name];ad=o.animation_data;d=o.data;record={'rna':p.scalar(o),'custom':p.plain(dict(o.items())),'parent':o.parent.name if o.parent else None,'parent_inverse':[list(x)for x in o.matrix_parent_inverse],'basis':[list(x)for x in o.matrix_basis],'data':d.name if d else None,'action':ad.action.name if ad and ad.action else None,'slot':ad.action_slot.identifier if ad and ad.action_slot else None,'constraints':[p.scalar(x)for x in o.constraints],'modifiers':[]}
    for mod in o.modifiers:
     need(mod.type=='ARMATURE','unknown owned modifier');record['modifiers'].append({'rna':p.scalar(mod),'object':mod.object.name if mod.object else None})
    if o.type=='MESH':record['mesh']={'rna':p.scalar(d),'custom':p.plain(dict(d.items())),'vertices':[list(v.co)for v in d.vertices],'edges':[list(e.vertices)for e in d.edges],'polygons':[list(f.vertices)for f in d.polygons],'materials':[x.name if x else None for x in d.materials],'weights':[[[g.group,g.weight]for g in v.groups]for v in d.vertices],'vertex_groups':[[g.name,g.index,g.lock_weight]for g in o.vertex_groups]}
    elif o.type=='ARMATURE':record['armature']={'rna':p.scalar(d),'custom':p.plain(dict(d.items())),'bones':{b.name:{'rna':p.scalar(b),'parent':b.parent.name if b.parent else None,'rest':[list(x)for x in b.matrix_local],'custom':p.plain(dict(b.items()))}for b in d.bones},'pose':{b.name:{'rotation_mode':b.rotation_mode,'location':list(b.location),'scale':list(b.scale),'quaternion':list(b.rotation_quaternion),'constraints':[p.scalar(x)for x in b.constraints],'custom':p.plain(dict(b.items()))}for b in o.pose.bones}}
    owned[name]=record
   originals=[bpy.data.objects[n]for n in self.manifest['protected_original_object_names']]
   return {'original_objects':p.original_objects(originals),'scene':p.scene_record(scene),'layers':p.layer_records(scene,names),'owned':owned,'pose_locks':self.pose_locks(),'data_inventory':{'objects':sorted(bpy.data.objects.keys()),'meshes':sorted(bpy.data.meshes.keys()),'armatures':sorted(bpy.data.armatures.keys()),'actions':sorted(bpy.data.actions.keys())}}
