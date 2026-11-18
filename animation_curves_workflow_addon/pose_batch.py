"""Bounded current-frame local Euler capture; one unchanged core transaction."""
import copy,json,math,struct
import bpy
from . import pose_channel as pc,pose_gate,native,core,pose_prepare,root_input
from .contract import normalize_manifest
_PERMIT=None

def active():return _PERMIT is not None

def native_target(owner,path,index):
    if not active() or (owner.as_pointer(),owner.get('ac_instance_uuid'))!=_PERMIT['owner'] or (path,index)not in _PERMIT['addresses']:
        raise ValueError('No exact batch pose capability')
    return pc.target(owner,path,index,allow_root_location=True)

def validate_manifest(owner,spec,manifest):
    native_target(owner,spec['data_path'],spec['array_index'])
    if normalize_manifest(manifest)!=_PERMIT['manifest']:raise ValueError('Batch request drift')

def pose_state(arm):
    return {p.name:{'rotation':list(p.rotation_euler),'location':list(p.location),'scale':list(p.scale)}for p in arm.pose.bones}

def evaluated(arm,context):
    return {p.name:[list(r)for r in p.matrix]for p in arm.evaluated_get(context.evaluated_depsgraph_get()).pose.bones}

def selection(context):
    if context.mode!='POSE' or len(context.selected_objects)!=1 or context.active_object!=context.selected_objects[0]:raise ValueError('姿态模式只选一个自有骨架')
    arm=context.active_object;selected=context.selected_pose_bones;pb=context.active_pose_bone
    if not selected or pb not in selected:raise ValueError('请选择姿态骨，活动骨须在选择内')
    names=sorted(p.name for p in selected)
    w=pose_gate.discover(bpy,arm.get('ac_instance_uuid'),allow_root_location=True)
    if w['rig']!=arm:raise ValueError('Rig identity mismatch')
    if any(not arm.pose.bones[n].bone.use_deform for n in names):raise ValueError('不支持root非变形骨；请取消选择')
    return {'rig':arm.name,'pointer':arm.as_pointer(),'uuid':arm.get('ac_instance_uuid'),'bones':names,'active':pb.name},w

def review(context):
    ident,w=selection(context);arm=w['rig'];t=context.scene.frame_current_final
    if not math.isfinite(t)or not -1e6<=t<=1e6 or struct.unpack('f',struct.pack('f',t))[0]!=t:raise ValueError('当前帧不能精确表示或超出支持范围')
    from . import knee_check
    knees=knee_check.inspect(context)
    if knees['status']!='MEASURED' or any(k['geometry']!='OK' or k['constraint']!='OK' for k in knees['knees']):raise ValueError('当前实际双膝几何/限位不合格')
    rows=[]
    if w['action'] and (w['action'].use_frame_range or w['action'].use_cyclic or len(w['action'].pose_markers)):raise ValueError('不支持手动Action范围/循环/姿态标记')
    for name in ident['bones']:
        pb=arm.pose.bones[name];knee=name in('shin.L','shin.R');path=pb.path_from_id('rotation_euler')
        if knee and (abs(pb.rotation_euler.y)>1e-7 or abs(pb.rotation_euler.z)>1e-7):raise ValueError('膝原始Y/Z非零，不捕获被约束夹住的输入')
        for axis in ((0,)if knee else(0,1,2)):
            pc.target(arm,path,axis,allow_root_location=True);value=float(pb.rotation_euler[axis]);deg=math.degrees(value)
            physical=deg+w['spec']['joint_limits'][name]['rest_flexion_deg']if knee else deg
            if not math.isfinite(value)or not ((-1e-5<=physical<=150+1e-5)if knee else(-180<=deg<=180)):raise ValueError('局部旋转或物理膝角超出范围')
            fc=w['bag'].fcurves.find(path,index=axis)if w['bag']else None
            if fc:
                if fc.lock or fc.mute or fc.modifiers or fc.sampled_points:raise ValueError('目标通道锁定/静音/修改器/采样不支持')
                if any(k.interpolation not in('LINEAR','CONSTANT') or k.handle_left_type!='FREE' or k.handle_right_type!='FREE' for k in fc.keyframe_points):raise ValueError('目标通道需LINEAR/CONSTANT及FREE；不自动转换AUTO/Bezier')
                if knee and any(not -1e-5<=math.degrees(k.co.y)+w['spec']['joint_limits'][name]['rest_flexion_deg']<=150+1e-5 for k in fc.keyframe_points):raise ValueError('已有膝键超出物理范围')
                if native.find_key(fc,t)is not None:raise ValueError('当前帧目标已有键，整批拒绝覆盖')
            rows.append({'bone':name,'path':path,'axis':axis,'value':value,'display_deg':physical})
    # Unselected animated manual offsets would be discarded by Action binding.
    targets={(r['path'],r['axis'])for r in rows}
    if w['bag']:
        for fc in w['bag'].fcurves:
            if (fc.data_path,fc.array_index)in targets:continue
            if fc.data_path.startswith('pose.'):
                try:raw=arm.path_resolve(fc.data_path)[fc.array_index]
                except Exception:raise ValueError('Unsupported non-target pose channel')
                if abs(raw-fc.evaluate(t))>1e-6:raise ValueError('未选择通道存在未打键手动姿态，请先保存该姿态再操作')
    return ident,w,rows

def origin(context):
    ident,w,rows=review(context)
    return {'root_input':root_input.witness(w['root'],context),'identity':ident,'source':pose_prepare.origin(w),'frame':context.scene.frame_current,'subframe':context.scene.frame_subframe,'scene':context.scene.as_pointer(),'layer':context.view_layer.name,'pose':pose_state(w['rig']),'rows':rows}

def execute(context,expected,checkpoint=None):
    global _PERMIT
    if active()or pc._ACTIVE is not None:raise ValueError('Nested pose transaction refused')
    if origin(context)!=expected:raise ValueError('源Action/选择/时间/姿态变化，请重新打开')
    ident,w,rows=review(context);arm=w['rig'];source=w['action'];fake=source.use_fake_user if source else None
    before_pose=pose_state(arm);context.view_layer.update();before_eval=evaluated(arm,context)
    old_actions={a:(pc.action_witness(a),a.use_fake_user)for a in bpy.data.actions}
    others={o:native.binding_state(o)for o in bpy.data.objects if o!=arm}
    before_bag=native.bag_state(w['bag']);t=context.scene.frame_current_final
    specs=[{'id':'pose_'+str(i),'owner':{'type':'OBJECT','name':arm.name},'data_path':r['path'],'array_index':r['axis'],'sharing':'reject'}for i,r in enumerate(rows)]
    ops=[{'op':'set_key','target':s['id'],'frame':t,'value':r['value'],'interpolation':'LINEAR'}for s,r in zip(specs,rows)]
    manifest=normalize_manifest({'schema':'1.0','operation':'edit','context':{'scene':context.scene.name,'view_layer':context.view_layer.name},'targets':specs,'operations':ops})
    addresses={(r['path'],r['axis'])for r in rows};allowed={a:{t}for a in addresses};candidate_seen=None;candidate_witness=None;protected=False
    def guard():
        nonlocal candidate_seen,candidate_witness,protected
        candidates=set(bpy.data.actions)-set(old_actions)
        if candidates and candidate_seen is None:
            candidate_seen=next(iter(candidates));candidate_seen.use_fake_user=False
        if candidates:candidate_witness=pc.action_witness(next(iter(candidates)))
        stage='bound' if arm.animation_data and arm.animation_data.action!=source else ('prepared'if candidates else'begin')
        if checkpoint is not None and checkpoint(stage)is False:raise ValueError('Batch cancelled')
        if set(bpy.data.actions)!=set(old_actions)|candidates:raise ValueError('External Action inventory drift; unknown Actions are not removed')
        current,current_w=selection(context)
        if root_input.witness(current_w['root'],context)!=expected['root_input']:raise ValueError('Root input/raw/world/Action changed')
        if current!=ident or context.scene.as_pointer()!=expected['scene'] or context.view_layer.name!=expected['layer'] or (context.scene.frame_current,context.scene.frame_subframe)!=(expected['frame'],expected['subframe']):raise ValueError('Context/selection/time drift')
        if len(candidates)>1:raise ValueError('Unexpected extra Action')
        for a,(state,flag)in old_actions.items():
            if pc.action_witness(a)!=state or a.use_fake_user!=(True if a==source and protected else flag):raise ValueError('Original Action changed')
        for o,binding in others.items():
            if native.binding_state(o)!=binding:raise ValueError('Other binding changed')
        if pose_state(arm)!=before_pose:raise ValueError('Local pose changed')
        if candidates:
            c=next(iter(candidates))
            if c!=candidate_seen or c.use_fake_user:raise ValueError('Candidate identity/protection changed')
            cw=pc.action_witness(c)
            if cw!=candidate_witness:raise ValueError('Candidate differs from pre-callback transaction witness')
            def structure(witness):
                out=copy.deepcopy(witness)
                for layer in out['layers']:
                    for strip in layer['strips']:
                        strip['bags']=[(handle,[])for handle,_ in strip['bags']]
                return out
            if source:
                if structure(cw)!=structure(old_actions[source][0]):raise ValueError('Candidate layer/strip metadata changed')
            else:
                if len(c.slots)!=1 or c.slots[0].target_id_type!='OBJECT' or c.slots[0].identifier!='OB'+arm.name:raise ValueError('Unexpected new slot')
                expected_structure={'custom':{},'slots':[(c.slots[0].handle,'OB'+arm.name,'OBJECT')],'layers':[{'name':'Animation','mute':False,'influence':1.,'strips':[{'type':'KEYFRAME','bags':[(c.slots[0].handle,[])]}]}]}
                if structure(cw)!=expected_structure:raise ValueError('Unexpected new Action structure')
            if c.use_frame_range or c.use_cyclic or len(c.pose_markers):raise ValueError('Candidate event metadata changed')
            if native.custom_state(c)!=(native.custom_state(source)if source else {}):raise ValueError('Candidate custom metadata changed')
            if source and [(x.handle,x.identifier,x.target_id_type)for x in c.slots]!=[(x.handle,x.identifier,x.target_id_type)for x in source.slots]:raise ValueError('Candidate slot changed')
            bag=c.layers[0].strips[0].channelbag(c.slots[0])
            core._protected_curves(before_bag,native.bag_state(bag),allowed)
            # Prepared can be an empty clone before operations; strict request readback at bound.
            if stage=='bound':
                if arm.animation_data.action!=c or arm.animation_data.action_slot!=c.slots[0]:raise ValueError('Candidate binding drift')
                for r in rows:
                    fc=bag.fcurves.find(r['path'],index=r['axis']);key=native.find_key(fc,t)if fc else None
                    if key is None or key.co.x!=t or key.co.y!=r['value'] or key.interpolation!='LINEAR' or key.handle_left_type!='FREE' or key.handle_right_type!='FREE':raise ValueError('Batch key differs from request')
                context.view_layer.update()
                if pose_state(arm)!=before_pose:raise ValueError('Binding discarded manual pose')
                after=evaluated(arm,context)
                if max(abs(v-u)for n in after for x,y in zip(after[n],before_eval[n])for v,u in zip(x,y))>2e-6:raise ValueError('Binding altered evaluated pose')
                native.resolve_targets(manifest,writing=True)
                if source is not None:source.use_fake_user=True;protected=True
        return True
    _PERMIT={'owner':(arm.as_pointer(),ident['uuid']),'addresses':addresses,'manifest':manifest}
    try:
        result=core.apply_manifest(manifest,checkpoint=guard)
        return {'changed':True,'channels':len(rows),'source_fake_before':fake,'persisted':False,'core':result}
    except BaseException:
        if source is not None:source.use_fake_user=fake
        # Settle restored binding evaluation before restoring unsaved manual inputs.
        context.view_layer.update()
        for name,data in before_pose.items():
            p=arm.pose.bones[name];p.rotation_euler=data['rotation'];p.location=data['location'];p.scale=data['scale']
        context.view_layer.update()
        if pose_state(arm)!=before_pose:raise RuntimeError('Batch rollback did not restore local pose')
        raise
    finally:_PERMIT=None
