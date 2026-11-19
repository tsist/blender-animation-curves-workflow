"""Read-only structural discovery. No bpy import, writes, evaluation or apply API."""
import hashlib
import json
import math
import re
from pathlib import Path
from .spec import templates_spec

OBSERVED_RNA=json.loads((Path(__file__).parent/'OBSERVED-ACTION-RNA.json').read_text())

def require_rna(obj,kind):
    actual={p.identifier:{'type':p.type,'readonly':p.is_readonly} for p in obj.bl_rna.properties}
    need(actual==OBSERVED_RNA[kind], 'unsupported '+kind+' RNA drift')

OWNER='animation-curves-two-segment-template-v2'
TEMPLATE='humanoid_fk_two_segment_v2'
class Rejected(ValueError): pass

def need(ok,msg):
    if not ok: raise Rejected(msg)
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
        need(ad.action_blend_type=='REPLACE' and close(ad.action_influence,1),'animation blend/influence')
    return ad

def action_review(obj):
    ad=no_dependencies(obj)
    if not ad or not ad.action: return {'status':'NO_ACTION','action':None,'slot':None}
    a=ad.action;local(a)
    need(a.users==1,'shared Action')
    slot=ad.action_slot
    need(len(a.slots)==1 and slot is not None and slot==a.slots[0] and slot.target_id_type=='OBJECT','Action slot')
    need(len(a.layers)==1 and len(a.layers[0].strips)==1,'layer/strip cardinality')
    layer=a.layers[0]
    require_rna(layer,'layer');require_rna(slot,'slot')
    strip=layer.strips[0];require_rna(strip,'strip');need(strip.type=='KEYFRAME','strip type')
    bag=strip.channelbag(slot)  # Intentionally no ensure=True; never creates slots.
    need(bag is not None and len(strip.channelbags)==1,'channelbag')
    rows=[];seen=set()
    for fc in bag.fcurves:
        identity=(fc.data_path,fc.array_index)
        need(identity not in seen,'duplicate curve');seen.add(identity)
        need(not fc.modifiers and not fc.mute,'curve modifiers/mute')
        need(not fc.sampled_points, 'sampled curve unsupported')
        keys=[list(k.co) for k in fc.keyframe_points]
        need(all(len(k)==2 and all(type(v) in (int,float) and math.isfinite(v) for v in k) for k in keys),'nonfinite keys')
        need(all(a[0]<b[0] for a,b in zip(keys,keys[1:])),'key ordering')
        rows.append({'path':fc.data_path,'index':fc.array_index,'keys':keys})
    return {'status':'ACTION_DISCOVERED_NOT_EVALUATED','action':a.name,'slot':slot.identifier,'slot_type':slot.target_id_type,'curves':rows}

def _discover(bpy, instance_uuid, times):
    """bpy is actual Blender module in native use, test double in HOST tests.
    A saved clean source is hashed as observed, not matched to a legacy source pin.
    Authored structure and keys are read; physical interval evaluation stays pending.
    """
    need(tuple(bpy.app.version) in {(5,2,1),(5,3,0)}, 'observed RNA requires Blender 5.2.1 or 5.3.0')
    need(type(instance_uuid) is str and re.fullmatch('[0-9a-f]{32}',instance_uuid),'instance UUID')
    need(len(times)==4 and all(type(t) in (int,float) and math.isfinite(t) for t in times),'four finite times')
    need(times[0]<times[1]<=times[2]<times[3] and math.isfinite(times[3]-times[0]),'time ordering')
    need(bpy.data.is_saved and not bpy.data.is_dirty,'saved clean source required')
    source=Path(bpy.data.filepath)
    need(source.is_absolute() and source.suffix.lower()=='.blend' and source.is_file() and not any(p.is_symlink() for p in (source,*source.parents)),'source path')
    digest=hashlib.sha256(source.read_bytes()).hexdigest()
    uid=instance_uuid
    cols=[c for c in bpy.data.collections if c.get('ac_instance_uuid')==uid]
    need(len(cols)==1,'ambiguous/missing collection');col=cols[0];owned(col,uid)
    need(not col.children,'nested instance collections')
    need(col.users==1, 'shared collection links')
    objs=[o for o in bpy.data.objects if o.get('ac_instance_uuid')==uid]
    need(set(map(id,objs))==set(map(id,col.objects)),'instance membership')
    need(all(o in list(bpy.context.scene.objects) for o in objs),'instance not in active scene')
    for o in objs:
        owned(o,uid);need(not o.constraints,'object constraints')
        need(len(o.users_collection)==1 and o.users_collection[0]==col, 'shared object collection links')
    roots=[o for o in objs if o.type=='EMPTY'];arms=[o for o in objs if o.type=='ARMATURE']
    need(len(roots)==len(arms)==1,'unique root/rig');root,arm=roots[0],arms[0]
    need(root.parent is None and arm.parent==root,'root/rig parenting')
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
        need(vector(o.color,row['color']), 'part display color')
        mesh=o.data;owned(mesh,uid);
        need(len(mesh.materials)==1, 'material count');owned(mesh.materials[0],uid)
        need(vector(mesh.materials[0].diffuse_color,row['color']), 'material display color')
        need(mesh.users==1 and mesh.shape_keys is None,'shared mesh/shape keys');need(not no_dependencies(mesh),'mesh animation')
        need(not no_dependencies(o),'part animation')
        need(len(o.modifiers)==1,'part modifiers');mod=o.modifiers[0]
        need(mod.type=='ARMATURE' and mod.object==arm and mod.use_vertex_groups and not mod.use_bone_envelopes and mod.show_viewport and mod.show_render,'armature modifier')
        need(len(o.vertex_groups)==1 and o.vertex_groups[0].name==row['bone'],'vertex group')
        need(len(mesh.vertices)==len(row['vertices']) and len(mesh.polygons)==len(row['faces']),'mesh topology')
        for v,expected in zip(mesh.vertices,row['vertices']):
            need(vector(v.co,expected) and len(v.groups)==1 and v.groups[0].group==o.vertex_groups[0].index and close(v.groups[0].weight,1),'rigid mesh geometry/weights')
        need(all(tuple(p.vertices)==tuple(f) for p,f in zip(mesh.polygons,row['faces'])),'mesh faces')
    bindings={'root':action_review(root),'rig':action_review(arm)}
    need(hashlib.sha256(source.read_bytes()).hexdigest()==digest and not bpy.data.is_dirty,'source drift')
    return {'status':'OWNED_TEMPLATE_DISCOVERED_REVIEW_ONLY','structure_recognized':True,
            'source':str(source),'source_sha256':digest,'instance_uuid':uid,
            'collection':col.name,'root':root.name,'rig':arm.name,
            'parts':{k:v.name for k,v in parts.items()},'times':list(times),'bindings':bindings,
            'time_range_status':'NO_ACTION' if all(bindings[k]['status']=='NO_ACTION' for k in ('root','rig')) else 'NOT_EVALUATED',
            'time_range_evaluated':False,'may_apply':False,'may_write_keys':False,
            'remaining':['evaluated knee/mesh samples','ground qualification','snapshot/raw freeze','proposal and rollback/cold']}


def discover(bpy, instance_uuid, times):
    try:
        return _discover(bpy, instance_uuid, times)
    except Rejected:
        raise
    except (KeyError, TypeError, AttributeError, IndexError, ValueError, OSError, OverflowError) as exc:
        raise Rejected('Malformed or unavailable native discovery input: '+str(exc)) from exc
