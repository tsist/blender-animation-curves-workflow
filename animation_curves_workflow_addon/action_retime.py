"""Whole supported Action affine retiming. No scene timing edits or file I/O."""
import copy, math, struct
from fractions import Fraction
import bpy
from . import pose_gate,pose_channel as pc,pose_prepare,native
_BUSY=False

def rational(x):
    if type(x) not in (int,float) or not math.isfinite(x):raise ValueError('时间必须是有限数值')
    return Fraction(x)

def exact32(x):
    try:y=struct.unpack('f',struct.pack('f',float(x)))[0]
    except (OverflowError,struct.error):raise ValueError('时间超出float32范围')
    if not math.isfinite(y) or Fraction(y)!=x:raise ValueError('改时结果无法由float32精确表示；请用可精确表示的起止时间')
    return y

def metadata(a):
    return {'use_frame_range':a.use_frame_range,'use_cyclic':a.use_cyclic,
            'frame_start':a.frame_start,'frame_end':a.frame_end,
            'pose_markers':[(m.name,m.frame,m.camera)for m in a.pose_markers]}

def origin(w):
    out=pose_prepare.origin(w)
    if out is not None:out['metadata']=metadata(w['action'])
    return out

def selected(context):
    if context.mode!='POSE' or len(context.selected_objects)!=1:raise ValueError('请在POSE模式只选一个自有骨架；操作影响整个Action，不按选骨过滤')
    arm=context.active_object
    if arm is None or context.selected_objects[0]!=arm:raise ValueError('活动骨架不匹配')
    w=pose_gate.discover(bpy,arm.get('ac_instance_uuid'))
    if w['rig']!=arm:raise ValueError('骨架身份不匹配')
    return {'rig':arm.name,'uuid':arm.get('ac_instance_uuid')},w

def review(context,identity):
    fresh,w=selected(context)
    if fresh!=identity:raise ValueError('目标骨架已改变')
    a=w['action']
    if a is None:raise ValueError('当前没有Action')
    if a.use_frame_range:raise ValueError('不支持手动Action范围，请先明确处理范围')
    if a.use_cyclic:raise ValueError('不支持循环Action标志')
    if len(a.pose_markers):raise ValueError('Action含姿态事件标记，本操作不自动改事件时刻')
    curves=list(w['bag'].fcurves)
    if not curves or len(curves)>51:raise ValueError('需要1–51条姿态曲线')
    seen=set();times=set();listing=[];count=0
    for fc in curves:
        key=(fc.data_path,fc.array_index)
        if key in seen:raise ValueError('重复通道')
        seen.add(key)
        spec={'id':'retime','data_path':fc.data_path,'array_index':fc.array_index,'sharing':'reject'}
        _,pb,_=pc.validate_manifest(w['rig'],spec,{'targets':[spec],'operations':[]})
        if fc.lock or fc.mute or fc.modifiers or fc.sampled_points or not fc.keyframe_points:raise ValueError('空/锁定/静音/修改器/采样曲线不支持')
        previous=None
        for k in fc.keyframe_points:
            if k.interpolation not in ('LINEAR','CONSTANT') or k.handle_left_type!='FREE' or k.handle_right_type!='FREE':raise ValueError('整个Action必须为LINEAR/CONSTANT及FREE手柄；不自动转换或跳过')
            for xy in (k.co,k.handle_left,k.handle_right):
                if not all(math.isfinite(v)for v in xy):raise ValueError('非有限坐标')
            t=Fraction(float(k.co.x))
            if previous is not None and t-previous<=Fraction(native.EPSILON):raise ValueError('重复或过近关键帧')
            previous=t;times.add(t);count+=1
        listing.append((pb.name,'XYZ'[fc.array_index],len(fc.keyframe_points)))
    for name in ('shin.L','shin.R'):
        pb=w['rig'].pose.bones[name]
        if any(not math.isfinite(pb.rotation_euler[i]) or abs(pb.rotation_euler[i])>1e-6 for i in (1,2)):raise ValueError('未动画膝Y/Z原始姿态不为零；拒绝依赖约束遮蔽坏源')
        if (pb.path_from_id('rotation_euler'),0) not in seen:
            angle=math.degrees(pb.rotation_euler.x)+w['spec']['joint_limits'][name]['rest_flexion_deg']
            if not math.isfinite(angle) or not -1e-5<=angle<=150+1e-5:raise ValueError('无X曲线膝原始姿态超出物理范围')
    if count>4096 or len(times)<2:raise ValueError('需要非零动作时长，最多4096键')
    ordered=sorted(times)
    if any(b-a<=Fraction(native.EPSILON)for a,b in zip(ordered,ordered[1:])):raise ValueError('全动作不同时刻过近')
    return w,listing,(ordered[0],ordered[-1])

def plan(state,start,end):
    lo=min(Fraction(k['co'][0])for c in state['curves']for k in c['keys']);hi=max(Fraction(k['co'][0])for c in state['curves']for k in c['keys'])
    start,end=rational(start),rational(end)
    if end<=start or hi<=lo:raise ValueError('新终点必须大于新起点')
    if not -1000000<=start<end<=1000000:raise ValueError('插件关键帧范围限制为-1000000至1000000；拒绝不可导航区间')
    scale=(end-start)/(hi-lo);out=copy.deepcopy(state);mapping={}
    for c in out['curves']:
        for k in c['keys']:
            for field in ('co','co_ui','handle_left','handle_right'):
                old=Fraction(k[field][0]);new=exact32(start+(old-lo)*scale)
                k[field][0]=new
                mapping[old]=Fraction(new)
    ordered=sorted(mapping)
    if any(mapping[b]-mapping[a]<=Fraction(native.EPSILON)for a,b in zip(ordered,ordered[1:])):raise ValueError('映射会使不同时刻坍塌或过近')
    return out

def execute(context,identity,start,end,checkpoint=None,expected_origin=None):
    global _BUSY
    if _BUSY:raise ValueError('Nested retiming refused')
    w,listing,span=review(context,identity);source=w['action'];arm=w['rig']
    if expected_origin is not None and origin(w)!=expected_origin:raise ValueError('原Action/slot/内容/保护标志改变，请重新打开')
    state=native.action_state(source);expected=plan(state,start,end)
    if expected==state:return {'changed':False}
    original={a:(pc.action_witness(a),a.use_fake_user)for a in bpy.data.actions}
    original_meta={a:metadata(a)for a in bpy.data.actions}
    expected_w=copy.deepcopy(original[source][0])
    expected_w['layers'][0]['strips'][0]['bags'][0]=(expected_w['layers'][0]['strips'][0]['bags'][0][0],copy.deepcopy(expected['curves']))
    binding=native.binding_state(arm);others={o:native.binding_state(o)for o in bpy.data.objects if o!=arm}
    scene=context.scene
    timing=(scene.frame_current,scene.frame_subframe,scene.frame_start,scene.frame_end,scene.use_preview_range,scene.frame_preview_start,scene.frame_preview_end,[(m.name,m.frame,m.camera)for m in scene.timeline_markers])
    def current_timing():return (scene.frame_current,scene.frame_subframe,scene.frame_start,scene.frame_end,scene.use_preview_range,scene.frame_preview_start,scene.frame_preview_end,[(m.name,m.frame,m.camera)for m in scene.timeline_markers])
    selection=(context.view_layer,tuple(context.selected_objects),context.active_object,tuple(context.selected_pose_bones or []),context.active_pose_bone)
    candidate=None;_BUSY=True
    def guard(stage):
        if checkpoint and checkpoint(stage)is False:raise ValueError('Retiming cancelled')
        fresh,current=selected(context);review(context,identity)
        if fresh!=identity or current['action']!=(candidate if stage=='bound'else source):raise ValueError('目标/绑定漂移')
        if context.scene!=scene or timing!=current_timing() or selection!=(context.view_layer,tuple(context.selected_objects),context.active_object,tuple(context.selected_pose_bones or []),context.active_pose_bone):raise ValueError('时间线/选择上下文漂移')
        for a,(old,fake)in original.items():
            if metadata(a)!=original_meta[a] or pc.action_witness(a)!=old or a.use_fake_user!=(True if a==source and stage=='bound'else fake):raise ValueError('原Action内容或标志改变')
        if any(native.binding_state(o)!=old for o,old in others.items()):raise ValueError('其他绑定改变')
        if native.action_state(candidate)!=expected or pc.action_witness(candidate)!=expected_w or candidate.use_fake_user or metadata(candidate)!=original_meta[source]:raise ValueError('候选与完整改时请求不符')
        if set(bpy.data.actions)!=set(original)|{candidate}:raise ValueError('Action集合改变')
    try:
        candidate=source.copy();candidate.name=source.name+' | Whole Action retime';candidate.use_fake_user=False
        bag=candidate.layers[0].strips[0].channelbag(candidate.slots[0])
        for row in expected['curves']:
            fc=bag.fcurves.find(row['data_path'],index=row['array_index'])
            for k,old in zip(fc.keyframe_points,row['keys']):
                k.co=old['co'];k.handle_left=old['handle_left'];k.handle_right=old['handle_right']
            fc.update()
        guard('prepared');source.use_fake_user=True
        arm.animation_data.action=candidate;arm.animation_data.action_slot=candidate.slots[0];context.view_layer.update();guard('bound')
        return {'changed':True,'source':source.name,'candidate':candidate.name,'channels':listing,'persisted':False}
    except BaseException:
        native.restore_binding(arm,binding);source.use_fake_user=original[source][1]
        if candidate is not None:bpy.data.actions.remove(candidate)
        context.view_layer.update();raise
    finally:_BUSY=False
