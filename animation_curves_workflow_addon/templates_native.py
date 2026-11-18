"""Create owned native FK data; no rendering, playback, keyframes or file writes."""
import json
import math
import uuid
from . import templates_spec

OWNER = 'animation-curves-two-segment-template-v2'


def create_template(context, kind, parameters=None):
    """Returns live references and an immutable UUID; display names are not identity."""
    spec=templates_spec.build(kind,parameters)  # Reject requests before any bpy changes.
    import bpy
    from mathutils import Vector
    if context.mode!='OBJECT' or not context.scene or not context.view_layer:
        raise ValueError('Template creation requires Object mode and an active scene/view layer')
    if context.scene.library or context.view_layer not in list(context.scene.view_layers):
        raise ValueError('Editable scene and matching view layer required')
    if (context.scene != bpy.context.scene or context.view_layer != bpy.context.view_layer
            or context.window != bpy.context.window or context.mode != bpy.context.mode):
        raise ValueError('Passed context does not match the active bpy operator context')
    spec=templates_spec.in_scene_units(spec,context.scene.unit_settings.scale_length)
    layer=context.view_layer
    old_active=layer.objects.active
    old_selected=[o for o in layer.objects if o.select_get()]
    identity=uuid.uuid4().hex
    owned={k:[] for k in ('objects','meshes','armatures','materials','collections')}
    arm=None
    def remember(kind,data):
        owned[kind].append(data)
        data['ac_template_owner']=OWNER;data['ac_instance_uuid']=identity
        return data
    def object_(name,data,collection):
        obj=remember('objects',bpy.data.objects.new('AC_'+name+'_'+identity,data))
        collection.objects.link(obj)
        return obj
    def restore_selection():
        for obj in layer.objects:
            if obj.select_get():obj.select_set(False)
        for obj in old_selected:obj.select_set(True)
        layer.objects.active=old_active
    def exit_edit():
        if arm is not None and arm.mode!='OBJECT':
            layer.objects.active=arm
            if bpy.ops.object.mode_set(mode='OBJECT') != {'FINISHED'}:
                raise RuntimeError('Unable to exit new armature edit mode')
    def cleanup():
        errors=[]
        try:exit_edit()
        except Exception as exc:errors.append('edit mode: '+str(exc))
        for category in ('objects','meshes','armatures','materials','collections'):
            for data in reversed(owned[category]):
                try:
                    if data.get('ac_template_owner')!=OWNER or data.get('ac_instance_uuid')!=identity:
                        raise RuntimeError('Ownership changed; refusing removal')
                    getattr(bpy.data,category).remove(data,do_unlink=True)
                except Exception as exc:errors.append(category+': '+str(exc))
        return errors
    try:
        col=remember('collections',bpy.data.collections.new('AC_TEMPLATE_'+identity))
        context.scene.collection.children.link(col)
        root=object_('ROOT',None,col);root.empty_display_type='PLAIN_AXES';root.empty_display_size=.12*spec['height_blender_units'];root.rotation_mode='XYZ'
        ad=remember('armatures',bpy.data.armatures.new('AC_ARMATURE_'+identity))
        arm=object_('RIG',ad,col);arm.parent=root;arm.display_type='WIRE';ad.display_type='STICK'
        for obj in old_selected:obj.select_set(False)
        arm.select_set(True);layer.objects.active=arm
        if bpy.ops.object.mode_set(mode='EDIT') != {'FINISHED'}:raise RuntimeError('Could not enter new armature edit mode')
        for row in spec['bones']:
            b=ad.edit_bones.new(row['name']);b.head=row['head'];b.tail=row['tail'];b.roll=row['roll'];b.use_connect=False;b.use_deform=row['deform']
            if row['parent']:b.parent=ad.edit_bones[row['parent']]
        exit_edit()
        for b in arm.pose.bones:b.rotation_mode='XYZ'
        for name,lim in spec['joint_limits'].items():
            pb=arm.pose.bones[name];pb.lock_rotation=(False,True,True)
            c=pb.constraints.new('LIMIT_ROTATION');c.name='AC_Physical_Knee_0_150'
            c.owner_space='LOCAL';c.euler_order='XYZ';c.influence=1.;c.use_transform_limit=True
            c.use_limit_x=c.use_limit_y=c.use_limit_z=True
            c.min_x=math.radians(lim['local_x_min_deg']);c.max_x=math.radians(lim['local_x_max_deg'])
            c.min_y=c.max_y=c.min_z=c.max_z=0.

        materials={}
        for role,color in [('body',templates_spec.BODY),('chest',templates_spec.CHEST),('face',templates_spec.FACE)]:
            mat=remember('materials',bpy.data.materials.new('AC_'+role+'_'+identity));mat.diffuse_color=color;mat.use_nodes=True
            bsdf=next(n for n in mat.node_tree.nodes if n.type=='BSDF_PRINCIPLED')
            bsdf.inputs['Base Color'].default_value=color;bsdf.inputs['Roughness'].default_value=.75
            materials[color]=mat
        parts={};markers={}
        for row in spec['parts']:
            mesh=remember('meshes',bpy.data.meshes.new('AC_'+row['name']+'_'+identity))
            mesh.from_pydata(row['vertices'],[],row['faces']);mesh.update()
            obj=object_(row['name'],mesh,col);obj.parent=arm;obj.color=row['color'];obj['ac_part']=row['name'];obj['ac_bone']=row['bone']
            mesh.materials.append(materials[row['color']])
            group=obj.vertex_groups.new(name=row['bone']);group.add(list(range(len(mesh.vertices))),1.0,'REPLACE')
            modifier=obj.modifiers.new('AC_Rigid_Binding','ARMATURE');modifier.object=arm;modifier.use_vertex_groups=True;modifier.use_bone_envelopes=False
            parts[row['name']]=obj
            if row['marker']:markers[row['name']]=obj
        metadata={k:spec[k] for k in ('schema_version','template_id','generator_version','params','height_m','unit','front_axis','up_axis','bone_map','capabilities','height_blender_units','meters_per_blender_unit','geometry_unit')}
        metadata.update(joint_limits=spec['joint_limits'],contact_semantics=spec['contact_semantics'],instance_uuid=identity,root_role='world_motion',root_bone_role='nondeform_no_world_motion',rest_matrices={b.name:[list(row) for row in b.matrix_local] for b in ad.bones},references={})
        for name,row in spec['references'].items():
            point=ad.bones[row['bone']].matrix_local.inverted() @ Vector(row['rest_point'])
            metadata['references'][name]={'bone':row['bone'],'bone_local':list(point),'rest_point':list(row['rest_point'])}
        col['ac_template_metadata']=json.dumps(metadata,sort_keys=True,allow_nan=False)
        root['ac_template_metadata']=col['ac_template_metadata']
        restore_selection()
        return dict(instance_uuid=identity,collection=col,root=root,armature=arm,parts=parts,markers=markers,metadata=metadata)
    except BaseException as original:
        errors=cleanup()
        try:restore_selection()
        except Exception as exc:errors.append('selection restoration: '+str(exc))
        if errors:raise RuntimeError('Template creation failed; incomplete cleanup: '+'; '.join(errors)) from original
        raise


def require_capability(handle, capability):
    if handle['metadata']['capabilities'].get(capability) is not True:
        raise ValueError('Template capability unsupported: '+str(capability))
    return handle
