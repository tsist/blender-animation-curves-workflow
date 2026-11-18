"""Current evaluated pose only. No frame change, repair, save or practice admission."""
import json
import math
from .leg_contract import signed_flexion
from .templates_native import OWNER


def inspect(context):
    result={'status':'UNSUPPORTED', 'reason':'', 'knees':[],
            'frame':context.scene.frame_current, 'subframe':context.scene.frame_subframe}
    try:
        if context.mode not in {'OBJECT','POSE'}:
            raise ValueError('只支持物体/姿态模式')
        selected=list(context.selected_objects)
        if len(selected)!=1 or selected[0]!=context.view_layer.objects.active:
            raise ValueError('请只选择一个自有两段腿人形骨架')
        arm=selected[0]
        if arm.type!='ARMATURE':raise ValueError('所选对象不是骨架')
        root=arm.parent
        identity=arm.get('ac_instance_uuid')
        result['rig_name']=arm.name;result['instance_uuid']=identity
        if not isinstance(identity,str) or not identity or root is None:
            raise ValueError('缺少自有模板身份或根对象')
        for obj in (arm,arm.data,root):
            if obj.library or obj.get('ac_template_owner')!=OWNER or obj.get('ac_instance_uuid')!=identity:
                raise ValueError('不是完整本地自有模板')
        meta=json.loads(root.get('ac_template_metadata',''))
        if meta['template_id']!='humanoid_fk_two_segment_v2' or meta['instance_uuid']!=identity:
            raise ValueError('不支持此模板')
        from . import templates_spec
        spec=templates_spec.in_scene_units(templates_spec.build(meta['template_id'],meta['params']),meta['meters_per_blender_unit'])
        expected={b['name']:b for b in spec['bones']}
        if set(arm.data.bones.keys())!=set(expected):raise ValueError('骨骼缺失或拓扑不支持')
        for b in arm.data.bones:
            row=expected[b.name]
            if (b.parent.name if b.parent else None)!=row['parent']:
                raise ValueError('骨骼父子关系已改变')
            if max(abs(a-c) for a,c in zip(b.head_local,row['head']))>1e-6 or max(abs(a-c) for a,c in zip(b.tail_local,row['tail']))>1e-6:
                raise ValueError('静置骨骼几何已改变')
            saved=meta['rest_matrices'][b.name]
            if max(abs(b.matrix_local[i][j]-saved[i][j]) for i in range(4) for j in range(4))>1e-6:
                raise ValueError('静置校准已改变')
        graph=context.evaluated_depsgraph_get()
        ev=arm.evaluated_get(graph)
        # World geometry: refuse shear/nonuniform scale rather than misreport a
        # transformed angle using armature-local coordinates.
        matrix=ev.matrix_world
        cols=[matrix.to_3x3().col[i] for i in range(3)]
        lengths=[v.length for v in cols]
        if min(lengths)<1e-10 or max(lengths)-min(lengths)>1e-6*max(lengths) or matrix.to_3x3().determinant()<=0 or any(abs(cols[i].dot(cols[j]))>1e-6*lengths[i]*lengths[j] for i in range(3) for j in range(i)):
            raise ValueError('不支持非均匀缩放、剪切或镜像根变换')
        for side in ('L','R'):
            thigh=ev.pose.bones['thigh.'+side]; shin=ev.pose.bones['shin.'+side]
            for pb in (thigh,shin):
                basis=pb.matrix.to_3x3(); vectors=[basis.col[i] for i in range(3)]
                if basis.determinant()<=0 or any(abs(v.length-1)>1e-5 for v in vectors) or any(abs(vectors[i].dot(vectors[j]))>1e-5 for i in range(3) for j in range(i)):
                    raise ValueError('不支持骨骼姿态缩放、剪切或镜像')
                rest_x=arm.data.bones[pb.name].matrix_local.to_3x3().col[0]
                if abs(rest_x.x-1)>1e-5 or abs(rest_x.y)>1e-5 or abs(rest_x.z)>1e-5:
                    raise ValueError('不支持改变的静置铰链轴')
            lim=spec['joint_limits']['shin.'+side]
            row={'side':side,'physical_deg':None,'geometry':'UNKNOWN','constraint':'MISSING'}
            constraints=[c for c in shin.constraints if c.name=='AC_Physical_Knee_0_150' and c.type=='LIMIT_ROTATION']
            if constraints:
                c=constraints[0]
                if c.mute or c.influence!=1.:row['constraint']='DISABLED'
                elif (c.owner_space=='LOCAL' and c.euler_order=='XYZ' and c.use_transform_limit and c.use_limit_x and c.use_limit_y and c.use_limit_z and abs(c.min_x-math.radians(lim['local_x_min_deg']))<1e-6 and abs(c.max_x-math.radians(lim['local_x_max_deg']))<1e-6 and max(abs(c.min_y),abs(c.max_y),abs(c.min_z),abs(c.max_z))<1e-6):row['constraint']='OK'
                else:row['constraint']='ALTERED'
            hip=matrix@thigh.head; knee=matrix@shin.head; end=matrix@shin.tail
            axis=matrix.to_3x3()@thigh.matrix.to_3x3().col[0]
            if (matrix@thigh.tail-knee).length>1e-6*max(lengths):
                row['geometry']='DISCONNECTED'
            else:
                try:
                    angle=signed_flexion(hip,knee,end,axis,hinge_tolerance=1e-4)
                    row['physical_deg']=angle
                    row['geometry']='REVERSE' if angle < -1e-3 else 'OVERFLEX' if angle>150.001 else 'OK'
                except ValueError as exc:
                    row['geometry']='OFF_PLANE' if 'hinge plane' in str(exc) else 'DEGENERATE'
            result['knees'].append(row)
        result['status']='MEASURED'
    except Exception as exc:
        result['status']='UNSUPPORTED';result['reason']=str(exc);result['knees']=[]
    return result
