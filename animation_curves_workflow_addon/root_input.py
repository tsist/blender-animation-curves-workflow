"""Read-only narrow root-location input contract, not locomotion/contact solving."""
import math,struct,json
from . import native

def need(ok,message):
    if not ok:raise ValueError(message)
def finite(values):return all(type(x)in(int,float)and math.isfinite(x)for x in values)
def review(root,context):
    need(root.type=='EMPTY'and root.parent is None and not root.constraints,'Root must remain parentless unconstrained EMPTY')
    need(root.library is None and root.override_library is None,'Root must be local')
    action,slot,bag=native.action_binding(root,'OBJECT',False)
    raw=list(root.location)+list(root.rotation_euler)+list(root.rotation_quaternion)+list(root.rotation_axis_angle)+list(root.scale)+list(root.delta_location)+list(root.delta_rotation_euler)+list(root.delta_rotation_quaternion)+list(root.delta_scale)
    need(finite(raw)and finite([x for row in root.matrix_world for x in row]),'Root finite raw/world required')
    if action is None:return None,slot,bag
    need(native.effective_users(action)==1,'Shared root Action refused')
    need(not action.use_frame_range and not action.use_cyclic and not action.pose_markers,'Root range/cycle/markers unsupported')
    need(tuple(root.delta_location)==(0,0,0)and tuple(root.delta_rotation_euler)==(0,0,0)and tuple(root.delta_rotation_quaternion)==(1,0,0,0)and tuple(root.delta_scale)==(1,1,1),'Animated root delta transform unsupported')
    need(1<=len(bag.fcurves)<=3,'Root needs 1–3 location curves')
    seen=set();t=context.scene.frame_current_final
    for fc in bag.fcurves:
        need(fc.data_path=='location'and fc.array_index in (0,1,2)and fc.array_index not in seen,'Only unique root location XYZ inputs supported');seen.add(fc.array_index)
        need(not fc.mute and not fc.lock and not fc.modifiers and not fc.sampled_points and fc.extrapolation=='CONSTANT','Root curve state unsupported')
        need(len(fc.keyframe_points)>0,'Empty root curve unsupported');last=None
        for k in fc.keyframe_points:
            need(k.interpolation in('LINEAR','CONSTANT')and k.handle_left_type==k.handle_right_type=='FREE','Root requires LINEAR/CONSTANT+FREE')
            values=list(k.co)+list(k.handle_left)+list(k.handle_right);need(finite(values),'Nonfinite root key/handles')
            need(-1e6<=k.co.x<=1e6 and abs(k.co.y)<=1e6,'Root key outside bounded range')
            need(last is None or k.co.x-last>1e-6,'Ambiguous root key times');last=k.co.x
        need(abs(root.location[fc.array_index]-fc.evaluate(t))<=1e-6,'Root has unkeyed animated location offset')
    return action,slot,bag

def witness(root,context):
    action,slot,_=review(root,context)
    from . import pose_channel
    ev=root.evaluated_get(context.evaluated_depsgraph_get())
    need(finite([x for row in ev.matrix_world for x in row]),'Root finite evaluated world required')
    data={'pointer':root.as_pointer(),'custom':native.custom_state(root),'uuid':root.get('ac_instance_uuid'),'name':root.name,'action_pointer':action.as_pointer()if action else None,'slot_pointer':slot.as_pointer()if slot else None,'slot_handle':slot.handle if slot else None,'action':pose_channel.action_witness(action)if action else None,'fake':action.use_fake_user if action else None,'event_meta':[action.use_frame_range,action.use_cyclic,len(action.pose_markers)]if action else None,'rotation_mode':root.rotation_mode,'location':list(root.location),'rotation':list(root.rotation_euler),'quaternion':list(root.rotation_quaternion),'axis_angle':list(root.rotation_axis_angle),'scale':list(root.scale),'delta_location':list(root.delta_location),'delta_rotation':list(root.delta_rotation_euler),'delta_quaternion':list(root.delta_rotation_quaternion),'delta_scale':list(root.delta_scale),'basis':[list(x)for x in root.matrix_basis],'world':[list(x)for x in root.matrix_world],'evaluated_world':[list(x)for x in ev.matrix_world]}

    # UI stores the witness in JSON; canonicalize nested Action tuple records too.
    return json.loads(json.dumps(data,allow_nan=False))
