"""Explicit single-axis AUTO preparation. No file I/O; persistence requires Save As."""
import copy
import json,hashlib
import bpy
from . import pose_channel as pc,native
_AUTO={'AUTO','AUTO_CLAMPED'}
_BUSY=False

def review(context,identity,axis):
    fresh,w,pb,knee=pc.selected(context)
    if fresh!=identity:raise ValueError('目标角色/骨骼已改变，请重新打开操作')
    if type(axis)is not int or axis not in (0,1,2):raise ValueError('Invalid axis')
    path=pb.path_from_id('rotation_euler')
    spec={'id':'prepare','data_path':path,'array_index':axis,'sharing':'reject'}
    pc.validate_manifest(w['rig'],spec,{'targets':[spec],'operations':[]})
    if w['action'] is None:raise ValueError('当前骨轴没有可准备的Action')
    fc=w['bag'].fcurves.find(path,index=axis)
    if fc is None or not fc.keyframe_points:raise ValueError('当前骨轴没有关键帧')
    if fc.lock or fc.mute or fc.modifiers or fc.sampled_points:raise ValueError('拒绝锁定/静音/修改器/采样通道')
    state=native.action_state(w['action']) # finite complete supported channel witness
    count=sum(k.handle_left_type in _AUTO for k in fc.keyframe_points)+sum(k.handle_right_type in _AUTO for k in fc.keyframe_points)
    return w,fc,state,count

def origin(w):
    a=w['action']
    if a is None:return None
    return {'pointer':a.as_pointer(),'name':a.name,'slot':w['slot'].handle,
            'fake':a.use_fake_user,'sha256':hashlib.sha256(json.dumps(pc.action_witness(a),sort_keys=True).encode()).hexdigest()}

def execute(context,identity,axis,checkpoint=None,expected_origin=None):
    global _BUSY
    if _BUSY:raise ValueError('Nested preparation refused')
    w,fc,state,count=review(context,identity,axis)
    if expected_origin is not None and origin(w)!=expected_origin:raise ValueError('原Action/保护标志已改变，请重新打开操作')
    if count==0:return {'changed':False,'auto_sides':0}
    source=w['action'];arm=w['rig'];binding=native.binding_state(arm)
    source_fake=source.use_fake_user
    originals={a:(pc.action_witness(a),a.use_fake_user) for a in bpy.data.actions}
    others={o:native.binding_state(o) for o in bpy.data.objects if o!=arm}
    time=(context.scene,context.view_layer,context.scene.frame_current,context.scene.frame_subframe)
    expected=copy.deepcopy(state)
    target=next(c for c in expected['curves'] if c['data_path']==fc.data_path and c['array_index']==axis)
    for k in target['keys']:
        for side in ('left','right'):
            field='handle_'+side+'_type'
            if k[field] in _AUTO:k[field]='FREE'
    expected_witness=copy.deepcopy(originals[source][0])
    for layer in expected_witness['layers']:
        for strip in layer['strips']:
            for _,curves in strip['bags']:
                for i,curve in enumerate(curves):
                    if curve['data_path']==fc.data_path and curve['array_index']==axis:curves[i]=copy.deepcopy(target)
    candidate=None;_BUSY=True
    def guard(stage):
        if checkpoint is not None and checkpoint(stage) is False:raise ValueError('Preparation cancelled')
        now,current,_,_=pc.selected(context)
        if now!=identity or time!=(context.scene,context.view_layer,context.scene.frame_current,context.scene.frame_subframe):raise ValueError('Target/context/time changed')
        if current['action']!=(candidate if stage=='bound' else source):raise ValueError('Binding changed')
        for a,(witness,fake) in originals.items():
            if pc.action_witness(a)!=witness or a.use_fake_user!=(True if a==source and stage=='bound' else fake):raise ValueError('Original Action content/protection changed')
        for o,old in others.items():
            if native.binding_state(o)!=old:raise ValueError('Other binding changed')
        if native.action_state(candidate)!=expected or pc.action_witness(candidate)!=expected_witness or candidate.use_fake_user:raise ValueError('Candidate differs from exact conversion request')
        if set(bpy.data.actions)!=set(originals)|{candidate}:raise ValueError('Unexpected Action set')
    try:
        candidate=source.copy();candidate.name=source.name+' | FREE continuation';candidate.use_fake_user=False
        bag=candidate.layers[0].strips[0].channelbag(candidate.slots[0]);out=bag.fcurves.find(fc.data_path,index=axis)
        oldkeys=[native.key_state(k) for k in fc.keyframe_points]
        for k,old in zip(out.keyframe_points,oldkeys):
            for side in ('left','right'):
                field='handle_'+side+'_type'
                if old[field] in _AUTO:setattr(k,field,'FREE')
        for k,old in zip(out.keyframe_points,oldkeys):
            # Restore only coordinates for authorized AUTO sides; never alter
            # unrelated FREE/VECTOR/ALIGNED handle settings.
            for side in ('left','right'):
                if old['handle_'+side+'_type'] in _AUTO:setattr(k,'handle_'+side,old['handle_'+side])
        out.update()
        guard('prepared')
        source.use_fake_user=True
        arm.animation_data.action=candidate;arm.animation_data.action_slot=candidate.slots[0]
        context.view_layer.update()
        guard('bound')
        return {'changed':True,'auto_sides':count,'source':source.name,'candidate':candidate.name,'source_fake_before':source_fake,'source_fake_after':True,'persisted':False}
    except BaseException:
        native.restore_binding(arm,binding);source.use_fake_user=source_fake
        if candidate is not None:bpy.data.actions.remove(candidate)
        context.view_layer.update()
        raise
    finally:_BUSY=False
