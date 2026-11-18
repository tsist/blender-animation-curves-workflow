"""Private 354 light admission prototype. Not registered or a released interface."""
import math
import bpy
from . import native,pose_channel as pc,pose_event as pe,display_witness as dw

def owners():
    # Animation lives on data IDs too, not just Objects.
    result=[]
    for collection in ('objects','lights','cameras','materials','worlds','scenes','node_groups'):
        for owner in getattr(bpy.data,collection):
            result.append(owner)
            tree=getattr(owner,'node_tree',None)
            if tree is not None:result.append(tree)
    return list({o.as_pointer():o for o in result}.values())

def binding_origin():
    return [{'type':o.bl_rna.identifier,'name':o.name,'ptr':o.as_pointer(),
             'action':o.animation_data.action.as_pointer() if o.animation_data and o.animation_data.action else None,
             'slot':o.animation_data.action_slot.handle if o.animation_data and o.animation_data.action_slot else None,
             'settings':native.binding_state(o)['settings']} for o in owners()]

def default_tree(data):
    tree=data.node_tree
    if not data.use_nodes or tree is None:raise ValueError('Default native light tree required')
    native.local(tree)
    if tree.animation_data is not None or len(tree.keys()):raise ValueError('Light tree animation/custom data unsupported')
    if len(tree.nodes)!=2 or len(tree.links)!=1:raise ValueError('Exact default light tree required')
    em=[n for n in tree.nodes if n.bl_idname=='ShaderNodeEmission']
    out=[n for n in tree.nodes if n.bl_idname=='ShaderNodeOutputLight']
    if len(em)!=1 or len(out)!=1:raise ValueError('Default light node types required')
    em,out=em[0],out[0]
    if any(n.mute or len(n.keys())for n in (em,out)) or not out.is_active_output or out.target!='ALL':raise ValueError('Light output state changed')
    if [s.identifier for s in em.inputs]!=['Color','Strength','Weight'] or [s.identifier for s in em.outputs]!=['Emission'] or [s.identifier for s in out.inputs]!=['Surface'] or len(out.outputs):raise ValueError('Light sockets changed')
    if tuple(em.inputs['Color'].default_value)!=(1,1,1,1) or em.inputs['Strength'].default_value!=1 or list(em.inputs)[2].default_value!=0:raise ValueError('Default light socket values changed')
    link=tree.links[0]
    if link.from_node!=em or link.to_node!=out or link.from_socket.identifier!='Emission' or link.to_socket.identifier!='Surface' or link.is_muted or not link.is_valid:raise ValueError('Default light link changed')
    return pe.canon({'ptr':tree.as_pointer(),'rna':dw.rna(tree),'nodes':[dw.rna(n)for n in tree.nodes],'links':[(l.from_socket.identifier,l.to_socket.identifier,l.is_muted)for l in tree.links]})

def validate(context,name,frame):
    if type(name)is not str or type(frame)not in(int,float)or not math.isfinite(frame):raise ValueError('Invalid light request')
    obj=bpy.data.objects.get(name)
    if obj is None or obj.type!='LIGHT' or context.scene.objects.get(name)!=obj or context.view_layer.objects.get(name)!=obj:raise ValueError('Light object outside context')
    data=obj.data
    if data.type not in {'POINT','AREA'}:raise ValueError('Only POINT/AREA lights')
    default_tree(data)
    if [o for o in bpy.data.objects if o.type=='LIGHT' and o.data==data]!=[obj]:raise ValueError('Light data must have exactly one object')
    for owner in (obj,data):
        native.local(owner)
        if set(owner.keys()) & native._MANAGED:raise ValueError('Managed light requires its domain adapter')
    spec={'id':'light','owner':{'type':'LIGHT','name':data.name},'data_path':'energy','array_index':0,'sharing':'reject'}
    manifest={'context':{'scene':context.scene.name,'view_layer':context.view_layer.name},'targets':[spec]}
    _,_,rows=native.resolve_targets(manifest,writing=True)
    row=rows[0];action=row['action'];curve=row['curve']
    if action is None or curve is None:raise ValueError('Existing light energy Action required')
    if len(row['bag'].fcurves)!=1 or sum(f.data_path=='energy' and f.array_index==0 for f in row['bag'].fcurves)!=1:raise ValueError('Ambiguous energy curves')
    if action.use_frame_range or action.use_cyclic or len(action.pose_markers):raise ValueError('Action event/range unsupported')
    keys=list(curve.keyframe_points)
    if len(keys)<3:raise ValueError('Light requires interior event key')
    if any(b.co.x-a.co.x<=native.EPSILON for a,b in zip(keys,keys[1:])):raise ValueError('Ambiguous energy key order')
    for key in keys:
        if key.interpolation not in {'LINEAR','CONSTANT'} or key.handle_left_type!='FREE' or key.handle_right_type!='FREE':raise ValueError('Light requires LINEAR/CONSTANT FREE')
        if not math.isfinite(key.co.y) or key.co.y<0:raise ValueError('Invalid energy')
    hits=[i for i,k in enumerate(keys)if abs(k.co.x-frame)<=native.EPSILON]
    if len(hits)!=1 or hits[0]in(0,len(keys)-1):raise ValueError('Select exact interior light key')
    i=hits[0]
    if float(keys[i].co.x)!=frame:raise ValueError('Ambiguous light event time')
    t=context.scene.frame_current+context.scene.frame_subframe
    if abs(float(data.energy)-curve.evaluate(t))>native.EPSILON:raise ValueError('Light has unkeyed energy input')
    return dict(row,object=obj,frame=frame,key=native.key_state(keys[i]),previous=float(keys[i-1].co.x),next=float(keys[i+1].co.x))

def witness(row):
    o=row['object'];d=row['owner']
    return pe.canon({'object':o.name,'object_ptr':o.as_pointer(),'data':d.name,'data_ptr':d.as_pointer(),
      'object_data':o.data.as_pointer(),'tree':default_tree(d),'rna':dw.rna(d),'props':dw.custom(d),'object_props':dw.custom(o),
      'action':pc.action_witness(row['action']),'meta':pc.metadata_witness(row['action']),
      'slot':[row['slot'].identifier,row['slot'].target_id_type],'energy':float(d.energy)})
