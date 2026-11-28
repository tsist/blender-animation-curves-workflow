"""Separate standard owned-pose structure gate. Old practice/discovery unchanged."""
from .host_compat import is_supported_host
import hashlib
import json
import math
import re
from pathlib import Path
from . import templates_spec

OWNER='animation-curves-two-segment-template-v2'
TEMPLATE='humanoid_fk_two_segment_v2'
class Rejected(ValueError): pass

def need(ok,msg):
    if not ok: raise Rejected(msg)
def rna_pointer(x):
    p=x.as_pointer();need(type(p)is int and p!=0,'Invalid native RNA identity');return p
def close(a,b):
    return type(a) in (int,float) and math.isfinite(a) and abs(a-b)<1e-5
def vector(a,b):
    return len(a)==len(b) and all(close(x,y) for x,y in zip(a,b))
def local(x):
    need(x.library is None and x.override_library is None,'linked/override data')
def owned(x,uid):
    local(x);need(x.get('ac_template_owner')==OWNER and x.get('ac_instance_uuid')==uid,'owner/instance mismatch')
def no_dependencies(x):
    ad=x.animation_data
    if ad:
        need(not ad.drivers and not ad.nla_tracks and not ad.use_tweak_mode,'drivers/NLA/tweak')
        if ad.action is not None:need(ad.action_blend_type=='REPLACE' and close(ad.action_influence,1),'animation blend/influence')
    return ad

def action_review(obj):
    ad=no_dependencies(obj)
    need(ad is None or ad.action is None,'Practice requires NO_ACTION; never replaces an Action')
    need(ad is None or ad.action_slot is None,'Unexpected slot without Action')
    return {'status':'NO_ACTION','action':None,'slot':None}

def _discover(bpy, instance_uuid, allow_root_location=False, allow_body_colors=False,
              allow_animated_root=False, allow_hand_quaternion=False,
              allow_animated_material_trees=(), allow_custom_material_color=False, support_runtime=False):
    """Dedicated in-memory writer gate. No saved/clean-source requirement.
    Does not alter or bypass the separate frozen read-only service.
    """
    if support_runtime:
        from . import read_lifecycle

        read_lifecycle.require_observed_runtime(bpy)
    else:
        need(is_supported_host(bpy.app.version), 'Observed RNA requires Blender 5.3.0 or later')
    allowed_trees=(True if allow_animated_material_trees is True
                   else set(allow_animated_material_trees))
    need(type(instance_uuid) is str and re.fullmatch('[0-9a-f]{32}',instance_uuid),'instance UUID')
    uid=instance_uuid
    cols=[c for c in bpy.data.collections if c.get('ac_instance_uuid')==uid]
    need(len(cols)==1,'ambiguous/missing collection');col=cols[0];owned(col,uid)
    need(not col.children,'nested instance collections')
    need(col.users==1, 'shared collection links')
    objs=[o for o in bpy.data.objects if o.get('ac_instance_uuid')==uid]
    need(set(map(rna_pointer,objs))==set(map(rna_pointer,col.objects)),'instance membership')
    need(all(o in list(bpy.context.scene.objects) for o in objs),'instance not in active scene')
    for o in objs:
        owned(o,uid);need(not o.constraints,'object constraints')
        need(len(o.users_collection)==1 and o.users_collection[0]==col, 'shared object collection links')
    roots=[o for o in objs if o.type=='EMPTY'];arms=[o for o in objs if o.type=='ARMATURE']
    need(len(roots)==len(arms)==1,'unique root/rig');root,arm=roots[0],arms[0]
    need(root.parent is None and arm.parent==root,'root/rig parenting')
    # Root is parentless, unconstrained and non-animated; rig basis is identity.
    # Thus its basis linear transform must preserve calibrated world angles.
    basis=root.matrix_basis.to_3x3();cols=[basis.col[i] for i in range(3)];lengths=[v.length for v in cols]
    need(all(math.isfinite(x)for v in cols for x in v) and min(lengths)>1e-10 and basis.determinant()>0 and max(lengths)-min(lengths)<=1e-6*max(lengths) and all(abs(cols[i].dot(cols[j]))<=1e-6*lengths[i]*lengths[j] for i in range(3)for j in range(i)), 'root nonuniform scale/shear/reflection unsupported')
    identity=((1,0,0,0),(0,1,0,0),(0,0,1,0),(0,0,0,1))
    need(all(vector(a,b) for a,b in zip(arm.matrix_basis,identity)), 'rig local transform')
    for o in objs:
        need(o.parent_type=='OBJECT' and o.parent_bone=='', 'unsupported parent binding')
        inv=o.matrix_parent_inverse
        need(len(inv)==4 and all(len(row)==4 for row in inv) and all(vector(a,b) for a,b in zip(inv,identity)), 'parent inverse drift')
    need(root.get('ac_template_metadata')==col.get('ac_template_metadata'),'metadata mismatch')
    try: meta=json.loads(col['ac_template_metadata'])
    except (ValueError,TypeError,KeyError) as e: raise Rejected('metadata JSON') from e
    need(meta['template_id']==TEMPLATE and meta['instance_uuid']==uid,'template identity')
    spec=templates_spec.in_scene_units(templates_spec.build(TEMPLATE,meta['params']),bpy.context.scene.unit_settings.scale_length)
    for field in ('generator_version','height_m','front_axis','up_axis','bone_map','capabilities','meters_per_blender_unit','joint_limits'):
        need(json.dumps(meta[field],sort_keys=True)==json.dumps(spec[field],sort_keys=True),'metadata '+field)
    ad=arm.data;owned(ad,uid);need(ad.users==1,'shared armature');need(not no_dependencies(ad),'armature data animation')
    need(len(ad.bones)==len(spec['bones'])==18,'footless bone count')
    for row in spec['bones']:
        bone=ad.bones.get(row['name']);need(bone is not None,'missing bone')
        need((bone.parent.name if bone.parent else None)==row['parent'],'bone parent')
        need(vector(bone.head_local,row['head']) and vector(bone.tail_local,row['tail']) and not bone.use_connect and bone.use_deform==row['deform'],'rest geometry')
        need(len(bone.matrix_local)==4 and all(len(r)==4 for r in bone.matrix_local), 'rest matrix shape')
        need(all(vector(a,b) for a,b in zip(bone.matrix_local,meta['rest_matrices'][row['name']])), 'rest matrix drift')
        if row['name'] in ('thigh.L','shin.L','thigh.R','shin.R'):
            # Independent canonical invariant, NOT trusted from source metadata.
            need(close(row['head'][0],row['tail'][0]) and close(row['roll'],0.), 'unknown template leg basis')
            need(vector([bone.matrix_local[i][0] for i in range(3)],(1.,0.,0.)), 'noncanonical leg rest positive axis')
        pb=arm.pose.bones[row['name']]
        if allow_hand_quaternion and row['name'] in ('hand.L','hand.R'):
            need(pb.rotation_mode=='QUATERNION','Clip policy permits QUATERNION only on the named hand bones')
        else:
            need(pb.rotation_mode=='XYZ','Practice requires existing XYZ pose modes')
        need(vector(pb.location,(0,0,0)) and vector(pb.scale,(1,1,1)),'Pose channel requires untranslated/unscaled pose bones')
        if not bone.use_deform:need(all(vector(a,b)for a,b in zip(pb.matrix_basis,identity)),'Non-deform root pose must remain default')
        if row['name'] not in spec['joint_limits']:
            need(not pb.constraints,'extra bone constraints');continue
        lim=spec['joint_limits'][row['name']];need(len(pb.constraints)==1,'knee constraint count');c=pb.constraints[0]
        need(c.type=='LIMIT_ROTATION' and c.owner_space=='LOCAL' and c.euler_order=='XYZ' and not c.mute and close(c.influence,1) and c.use_transform_limit,'knee constraint mode')
        need(c.use_limit_x and c.use_limit_y and c.use_limit_z,'knee axes')
        for axis in ('x','y','z'):
            for edge in ('min','max'):
                need(close(getattr(c,edge+'_'+axis),math.radians(lim['local_'+axis+'_'+edge+'_deg'])),'knee bound')
        need(tuple(pb.lock_rotation)==(False,True,True),'knee locks')
    meshobjs=[o for o in objs if o.type=='MESH'];need(len(objs)==len(meshobjs)+2 and len(meshobjs)==len(spec['parts']),'part count')
    parts={}
    for o in meshobjs:
        name=o.get('ac_part');need(name not in parts,'duplicate part');parts[name]=o
    for row in spec['parts']:
        o=parts.get(row['name']);need(o is not None and o.parent==arm and o.get('ac_bone')==row['bone'],'part binding')
        need(all(vector(a,b) for a,b in zip(o.matrix_basis,identity)), 'mesh local transform')
        if allow_body_colors and row.get('marker') is False:
            need(all(math.isfinite(v) and 0<=v<=1 for v in o.color) and o.color[3]==1, 'BODY display color must be finite opaque RGBA in [0,1]')
        else:need(vector(o.color,row['color']), 'part display color')
        mesh=o.data;owned(mesh,uid);
        need(len(mesh.materials)==1, 'material count');owned(mesh.materials[0],uid)
        material=mesh.materials[0]
        need(not no_dependencies(material),'animated material unsupported')
        if material.node_tree is not None:
            if allowed_trees is not True and material.name not in allowed_trees:
                need(not no_dependencies(material.node_tree),'animated material node tree unsupported')
        if allow_custom_material_color:
            need(all(math.isfinite(v) and 0<=v<=1 for v in material.diffuse_color), 'custom material display color must be finite RGBA in [0,1]')
        else:
            need(vector(material.diffuse_color,row['color']), 'material display color')
        need(mesh.users==1 and mesh.shape_keys is None,'shared mesh/shape keys');need(not no_dependencies(mesh),'mesh animation')
        need(not no_dependencies(o),'part animation')
        need(len(o.modifiers)==1,'part modifiers');mod=o.modifiers[0]
        need(mod.type=='ARMATURE' and mod.object==arm and mod.use_vertex_groups and not mod.use_bone_envelopes and mod.show_viewport and mod.show_render,'armature modifier')
        need(len(o.vertex_groups)==1 and o.vertex_groups[0].name==row['bone'],'vertex group')
        need(len(mesh.vertices)==len(row['vertices']) and len(mesh.polygons)==len(row['faces']),'mesh topology')
        for v,expected in zip(mesh.vertices,row['vertices']):
            need(vector(v.co,expected) and len(v.groups)==1 and v.groups[0].group==o.vertex_groups[0].index and close(v.groups[0].weight,1),'rigid mesh geometry/weights')
        need(all(tuple(p.vertices)==tuple(f) for p,f in zip(mesh.polygons,row['faces'])),'mesh faces')
    if allow_root_location:
        from . import root_input
        root_input.review(root,bpy.context)
    elif allow_animated_root:
        # Point-named clip policy: the root may own one exclusive Action, but
        # the caller still validates its exact six native channels.  NLA,
        # drivers, tweak mode and shared Actions remain rejected here.
        ad=no_dependencies(root)
        need(ad is not None and ad.action is not None,'Animated root requires an existing Action')
        from . import native
        need(native.effective_users(ad.action)==1,'Shared root Action refused')
    else:action_review(root)  # Old paths preserve NO_ACTION.
    from . import native
    action,slot,bag=native.action_binding(arm,'OBJECT',True)
    need(action is None or native.effective_users(action)==1,'shared Action refused')
    return {'rig':arm,'root':root,'metadata':meta,'spec':spec,'action':action,'slot':slot,'bag':bag}

def discover(bpy, instance_uuid, *, allow_root_location=False, allow_body_colors=False,
             allow_animated_root=False, allow_hand_quaternion=False,
             allow_animated_material_trees=(), allow_custom_material_color=False, support_runtime=False):
    try:return _discover(bpy,instance_uuid,allow_root_location,allow_body_colors,
                         allow_animated_root,allow_hand_quaternion,
                         allow_animated_material_trees,allow_custom_material_color,support_runtime)
    except Rejected:raise
    except (KeyError,TypeError,AttributeError,IndexError,ValueError,OSError,OverflowError) as exc:
        raise Rejected('Malformed owned pose structure: '+str(exc)) from exc
