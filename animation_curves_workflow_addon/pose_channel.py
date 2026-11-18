"""Narrow owned Pose Euler boundary. All key transactions use unchanged core."""
import math,struct
from . import pose_gate
from .leg_contract import flexion_channel,finite
_ACTIVE=None
_COLOR_ACTIVE=False

def native_target(owner,path,index):
    if _ACTIVE!=(owner.as_pointer(),owner.get('ac_instance_uuid'),path,index):
        raise ValueError('Pose path requires the dedicated single-axis transaction adapter')
    return target(owner,path,index,allow_body_colors=_COLOR_ACTIVE)


def target(owner,path,index, *, allow_root_location=False, allow_body_colors=False):
    import bpy
    if owner.type!='ARMATURE':raise ValueError('Pose path requires an owned armature')
    w=pose_gate.discover(bpy,owner.get('ac_instance_uuid'),allow_root_location=allow_root_location,allow_body_colors=allow_body_colors)
    if w['rig']!=owner:raise ValueError('Owned rig identity mismatch')
    # Resolve only canonical paths from actual, validated bones, never a regex.
    found=[p for p in owner.pose.bones if p.path_from_id('rotation_euler')==path]
    if len(found)!=1:raise ValueError('Unsupported or forged pose path')
    pb=found[0]
    if not pb.bone.use_deform:raise ValueError('Non-deform root is not a pose editing target')
    if pb.rotation_mode!='XYZ' or type(index)is not int or index not in (0,1,2):raise ValueError('Only existing XYZ Euler axes supported')
    knee=pb.name in ('shin.L','shin.R')
    if knee and index!=0:raise ValueError('Knee only supports calibrated local X')
    return w,pb,knee


def validate_manifest(owner,spec,manifest):
    w,pb,knee=target(owner,spec['data_path'],spec['array_index'],allow_body_colors=_COLOR_ACTIVE)
    if spec['sharing']!='reject':raise ValueError('Pose channel rejects shared Action isolation requests')
    if manifest.get('event_shift') or manifest.get('derived_handle_scope'):raise ValueError('Pose editing does not authorize event/automatic-handle scope')
    curve=w['bag'].fcurves.find(spec['data_path'],index=spec['array_index']) if w['bag'] else None
    if knee and curve:
        rest=w['spec']['joint_limits'][pb.name]['rest_flexion_deg']
        if any(not -1e-5<=math.degrees(k.co.y)+rest<=150+1e-5 for k in curve.keyframe_points):raise ValueError('Existing knee keys outside calibrated 0–150 range')
    if curve and any(k.interpolation not in ('LINEAR','CONSTANT') for k in curve.keyframe_points):raise ValueError('Existing pose channel must use LINEAR/CONSTANT; no implicit Bezier conversion')
    if len(manifest['targets'])!=1:raise ValueError('Pose editor supports one target channel only')
    for op in manifest['operations']:
        if op['target']!=spec['id']:raise ValueError('Pose operation target mismatch')
        if op['op']=='set_key':
            if op['interpolation']not in ('LINEAR','CONSTANT'):raise ValueError('Pose editor supports LINEAR/CONSTANT only')
            deg=math.degrees(op['value'])
            if knee:
                rest=w['spec']['joint_limits'][pb.name]['rest_flexion_deg']
                if not -1e-5<=deg+rest<=150+1e-5:raise ValueError('Knee physical angle outside 0–150 degrees')
            elif not -180<=deg<=180:raise ValueError('Local Euler angle outside -180–180 degrees')
    return w,pb,knee


def action_witness(action):
    """All layered bags, including empty/multi-slot unrelated Actions.
    This is an animation-content witness, not every RNA property/user count.
    """
    from . import native
    return {'custom':native.custom_state(action),
            'slots':[(s.handle,s.identifier,s.target_id_type) for s in action.slots],
            'layers':[{'name':l.name,'mute':getattr(l,'mute',False),'influence':getattr(l,'influence',1.),
                       'strips':[{'type':st.type,'bags':[(bag.slot_handle,native.bag_state(bag)) for bag in st.channelbags]}
                                 for st in l.strips]}for l in action.layers]}


def selected(context, *, allow_body_colors=False):
    if context.mode!='POSE' or len(context.selected_objects)!=1:raise ValueError('请在姿态模式只选一个自有骨架')
    arm=context.active_object
    bones=context.selected_pose_bones
    pb=context.active_pose_bone
    if arm is None or bones is None or len(bones)!=1 or pb is None or bones[0]!=pb:raise ValueError('请只选一个活动姿态骨')
    w,actual,knee=target(arm,pb.path_from_id('rotation_euler'),0,allow_body_colors=allow_body_colors)
    return {'rig':arm.name,'uuid':arm.get('ac_instance_uuid'),'bone':actual.name},w,actual,knee


def metadata_witness(a):
    if a is None:return None
    w=action_witness(a)
    for layer in w['layers']:
        for strip in layer['strips']:strip['bags']=[(handle,[]) for handle,_ in strip['bags']]
    w['event']={k:getattr(a,k) for k in ('use_frame_range','frame_start','frame_end','use_cyclic')}
    w['markers']=[(m.name,m.frame) for m in a.pose_markers]
    return w


def display_origin(context):
    import json
    from . import display_witness,native
    ident,w,_,_=selected(context,allow_body_colors=True)
    a=w['action'];b=native.binding_state(w['rig'])
    return json.loads(json.dumps({'identity':ident,'display':display_witness.capture(context),
        'source_pointer':a.as_pointer() if a else None,'source':action_witness(a) if a else None,
        'source_metadata':metadata_witness(a),'source_fake':a.use_fake_user if a else None,'slot':w['slot'].handle if w['slot'] else None,
        'binding_settings':b['settings'],'frame':context.scene.frame_current,'subframe':context.scene.frame_subframe,
        'scene':context.scene.as_pointer(),'view_layer':context.view_layer.as_pointer()},allow_nan=False))


def execute(context,identity,axis,operation,frame,value_deg=0.,to_frame=0.,interpolation='LINEAR',checkpoint=None, *, expected_display_origin=None):
    global _ACTIVE,_COLOR_ACTIVE
    from . import core,native
    import bpy
    allow_colors=expected_display_origin is not None
    if allow_colors and display_origin(context)!=expected_display_origin:raise ValueError('Display/source/context changed since dialog; reopen operation')
    fresh,w,pb,knee=selected(context,allow_body_colors=allow_colors)
    if fresh!=identity:raise ValueError('目标角色/骨骼已改变，请重新打开操作')
    if axis not in (0,1,2)or type(axis)is not int:raise ValueError('Invalid axis')
    value=finite(value_deg)
    if knee:
        value=flexion_channel(value,w['spec']['joint_limits'][pb.name]['rest_flexion_deg'])['local_x_deg']
    elif not math.isfinite(value)or not -180<=value<=180:raise ValueError('Euler angle must be -180–180 degrees')
    spec={'id':'pose_axis','owner':{'type':'OBJECT','name':fresh['rig']},'data_path':pb.path_from_id('rotation_euler'),'array_index':axis,'sharing':'reject'}
    op={'op':operation,'target':'pose_axis','frame':frame}
    if operation=='set_key':op.update(value=math.radians(value),interpolation=interpolation)
    elif operation=='move_key':op['to_frame']=to_frame
    manifest={'schema':'1.0','operation':'edit','context':{'scene':context.scene.name,'view_layer':context.view_layer.name},'targets':[spec],'operations':[op]}
    # Recheck inside the existing transaction, including its post-bind checkpoint.
    # Do not discover post-commit errors after core has already returned success.
    arm=w['rig'];old=w['action'];actions=set(bpy.data.actions)
    old_states={a:action_witness(a) for a in actions}
    before_bag=native.bag_state(w['bag'])
    source_curve=w['bag'].fcurves.find(spec['data_path'],index=axis) if w['bag'] else None
    source_key=native.find_key(source_curve,frame) if source_curve else None
    source_key_state=native.key_state(source_key) if source_key else None
    f32=lambda x:struct.unpack('f',struct.pack('f',x))[0]
    allowed={(spec['data_path'],axis):{frame,to_frame} if operation=='move_key' else {frame}}
    time=(context.scene,context.view_layer,context.scene.frame_current,context.scene.frame_subframe)
    other_bindings={o:native.binding_state(o) for o in bpy.data.objects if o!=arm}
    staged={'action':None,'state':None}
    old_fake=old.use_fake_user if old else None
    old_fakes={a:a.use_fake_user for a in actions}
    old_metadata={a:metadata_witness(a) for a in actions} if allow_colors else {}
    expected_metadata=metadata_witness(old)
    initialized=set()
    def guarded():
        if allow_colors:
            # Normalize only the transaction's newly cloned candidate BEFORE
            # callbacks; never hide injected protection-flag drift.
            new_before=set(bpy.data.actions)-actions
            if len(new_before)==1:
                c=next(iter(new_before))
                if c not in initialized:c.use_fake_user=False;initialized.add(c)
        if checkpoint is not None and checkpoint() is False:return False
        if allow_colors:
            from . import display_witness
            if display_witness.capture(context)!=expected_display_origin['display']:raise ValueError('Protected display state changed during transaction')
        now,current,_,_=selected(context,allow_body_colors=allow_colors)
        if now!=identity or time!=(context.scene,context.view_layer,context.scene.frame_current,context.scene.frame_subframe):raise ValueError('Pose target/context/time changed during transaction')
        for a,state in old_states.items():
            if allow_colors and metadata_witness(a)!=old_metadata[a]:raise ValueError('Original Action metadata changed')
            if allow_colors and a.use_fake_user!=(True if a==old else old_fakes[a]):raise ValueError('Action protection flag drift')
            if action_witness(a)!=state:raise ValueError('Pre-existing Action contents changed')
        for o,binding in other_bindings.items():
            if native.binding_state(o)!=binding:raise ValueError('Another object binding changed')
        new=set(bpy.data.actions)-actions
        if len(new)>1:raise ValueError('Unexpected extra Action during transaction')
        candidate=next(iter(new)) if new else None
        if candidate is not None:
            if allow_colors:
                if candidate not in initialized or candidate.use_fake_user:raise ValueError('Candidate protection flag drift')
                meta=metadata_witness(candidate)
                if old is not None:
                    if meta!=expected_metadata:raise ValueError('Candidate metadata differs from original')
                else:
                    if len(candidate.layers)!=1 or candidate.layers[0].name!='Animation' or getattr(candidate.layers[0],'mute',False) or len(candidate.slots)!=1 or candidate.slots[0].identifier!='OB'+arm.name or candidate.use_frame_range or candidate.use_cyclic or len(candidate.pose_markers):raise ValueError('Unexpected new Action metadata')
            if len(candidate.slots)!=1 or len(candidate.layers)!=1 or len(candidate.layers[0].strips)!=1:raise ValueError('Unexpected candidate Action shape')
            candidate_bag=candidate.layers[0].strips[0].channelbag(candidate.slots[0])
            core._protected_curves(before_bag,native.bag_state(candidate_bag),allowed)
        if current['action']==old:
            if candidate is not None:
                if staged['action'] is not None and staged['action']!=candidate:raise ValueError('Candidate identity drift')
                staged.update(action=candidate,state=action_witness(candidate) if allow_colors else native.action_state(candidate))
        else:
            if candidate is None or current['action']!=staged['action'] or candidate!=staged['action'] or (action_witness(candidate) if allow_colors else native.action_state(candidate))!=staged['state']:
                raise ValueError('Fresh committed candidate differs from precommit witness')
            actual=candidate_bag.fcurves.find(spec['data_path'],index=axis)
            find=lambda t:native.find_key(actual,t) if actual else None
            if operation=='set_key':
                key=find(frame)
                if key is None or key.co.x!=f32(frame) or key.co.y!=f32(op['value']) or key.interpolation!=interpolation:
                    raise ValueError('Committed set key differs from explicit request')
            elif operation=='move_key':
                key=find(to_frame)
                if source_key_state is None or key is None or key.co.x!=f32(to_frame) or key.co.y!=source_key_state['co'][1] or key.interpolation!=source_key_state['interpolation'] or (to_frame!=frame and find(frame) is not None):
                    raise ValueError('Committed moved key differs from source/request')
            elif operation=='delete_key' and find(frame) is not None:
                raise ValueError('Committed deleted key still exists')
            # Fresh narrow-path resolution also verifies binding/slot/constraints.
            from .contract import normalize_manifest
            native.resolve_targets(normalize_manifest(manifest),writing=True)
        return True
    if _ACTIVE is not None:raise ValueError('Nested Pose transaction refused')
    _ACTIVE=(arm.as_pointer(),fresh['uuid'],spec['data_path'],axis)
    _COLOR_ACTIVE=allow_colors
    if allow_colors and old is not None:old.use_fake_user=True
    try:return core.apply_manifest(manifest,checkpoint=guarded)
    except BaseException:
        if allow_colors and old is not None:old.use_fake_user=old_fake
        raise
    finally:_ACTIVE=None;_COLOR_ACTIVE=False
