"""Opt-in same-frame animated camera preservation witness, not a write adapter.

No evaluation, key changes or reset. Only parentless local cameras with static
camera data and one ordinary transform Action are supported. Current property
values must already equal native FCurve evaluation at the explicit saved frame.
"""
import math
from .contract import WorkflowError
from . import spatial_static_state as static

def reject(message):raise WorkflowError('UNSUPPORTED',message)
def conflict(message):raise WorkflowError('CONFLICT',message)

def validate_curves(curves,current,evaluate):
    """Pure structure/property linkage gate. evaluate is native FCurve.evaluate."""
    if not curves or len(curves)>6:reject('Camera preservation requires 1..6 transform curves')
    seen=set();count=0
    for curve in curves:
        path=curve['data_path'];index=curve['array_index']
        if path not in ('location','rotation_euler') or type(index)is not int or not 0<=index<3:reject('Unsupported camera animation channel')
        if (path,index) in seen:reject('Duplicate camera transform curve')
        seen.add((path,index))
        if curve['modifiers'] or curve['sampled_points'] or curve['muted']:reject('Unsupported camera curve dependency')
        if type(curve['key_count'])is not int or curve['key_count']<1:reject('Camera key count invalid')
        count+=curve['key_count']
        if count>20000:reject('Camera key witness budget exceeded')
        actual=current[path][index];expected=evaluate(path,index)
        if type(actual)not in (int,float) or type(expected)not in (int,float) or not math.isfinite(actual) or not math.isfinite(expected):reject('Nonfinite camera pose/evaluation')
        if actual!=expected:conflict('Saved camera pose differs from current native animation evaluation')
    return count

NO_ID_PROPERTIES={'ActionSlot','ActionLayer','ActionKeyframeStrip','ActionChannelbag','AnimData','ActionGroup','ThemeBoneColorSet','FCurve','TimelineMarker'}

def metadata(owner,budget,handled=(),self_pointers=()):
    """Bounded scalar RNA including readonly labels; unknown populated refs reject.

    Runtime scalars are retained for same-process equality, not normalized away.
    This witness is not yet a persisted/cold identity policy.
    """
    scalars={}
    for prop in owner.bl_rna.properties:
        name=prop.identifier
        if name=='rna_type' or name in handled:continue
        budget[0]+=1
        if budget[0]>static.MAX_FIELDS:reject('Animated metadata field budget exceeded')
        value=getattr(owner,name)
        if prop.type in {'BOOLEAN','INT','FLOAT','STRING','ENUM'}:
            scalars[name]=static.array_value(value,prop) if getattr(prop,'is_array',False) else static.value(value)
        elif prop.type=='POINTER':
            if name in self_pointers and value==owner:scalars[name]={'self_reference':True}
            elif value is not None:reject('Unhandled populated animated metadata pointer: '+name)
            else:scalars[name]=None
        elif prop.type=='COLLECTION':
            if len(value):reject('Unhandled populated animated metadata collection: '+name)
            scalars[name]=[]
        else:reject('Unknown animated metadata RNA kind')
    try:keys=list(owner.keys())
    except TypeError as e:
        if owner.bl_rna.identifier not in NO_ID_PROPERTIES or str(e)!="bpy_struct.keys(): this type doesn't support IDProperties":raise
        custom={'native_id_properties_supported':False}
    else:
        if len(keys)>256:reject('Animated custom property budget exceeded')
        custom={k:static.value(owner[k]) for k in keys}
    return {'scalars':scalars,'custom':custom}

def capture(obj,frame,budget):
    from . import native
    import bpy
    scene=bpy.context.scene
    if type(frame)not in (int,float) or not math.isfinite(frame):reject('Camera frame must be finite numeric, not bool')
    if float(scene.frame_current)+float(scene.frame_subframe)!=frame:conflict('Camera witness frame differs from current scene')
    if obj.type!='CAMERA' or obj not in list(scene.objects):reject('Expected in-scene camera')
    native.local(obj);static.object_dependency_gate(obj);static.nested_gate(obj)
    if obj.children or obj.rotation_mode!='XYZ':reject('Camera child or rotation-mode dependency unsupported')
    if list(obj.delta_location)!=[0,0,0] or list(obj.delta_rotation_euler)!=[0,0,0] or list(obj.delta_rotation_quaternion)!=[1,0,0,0] or list(obj.delta_scale)!=[1,1,1]:reject('Camera delta transforms unsupported')
    action,slot,bag=native.action_binding(obj,'OBJECT',writing=False)
    if action is None or native.effective_users(action)!=1:reject('Expected exclusively owned camera Action')
    if action.original!=action or action.is_evaluated:reject('Expected original non-evaluated camera Action')
    if action.asset_data is not None:reject('Camera Action asset metadata unsupported')
    if not action.is_action_layered or action.is_action_legacy:reject('Expected layered camera Action')
    if not 1<=len(bag.fcurves)<=6:reject('Camera curve budget exceeded')
    curves=list(bag.fcurves)
    if len(bag.groups)>6 or set(g.name for g in bag.groups)!={c.group.name for c in curves if c.group is not None}:reject('Unsupported unused or excessive camera groups')
    if any(len(g.channels)>6 for g in bag.groups):reject('Camera group channel budget exceeded')
    if any(len(c.modifiers) or len(c.sampled_points) for c in curves):reject('Camera modifiers or samples unsupported')
    records=[{'data_path':c.data_path,'array_index':c.array_index,'modifiers':[],'sampled_points':[],'muted':c.mute,'key_count':len(c.keyframe_points)} for c in curves]
    by_address={(c.data_path,c.array_index):c for c in curves}
    validate_curves(records,{'location':list(obj.location),'rotation_euler':list(obj.rotation_euler)},lambda p,i:by_address[(p,i)].evaluate(frame))
    # Deliberately do not call id_state(obj): its no-animation rejection remains
    # the default API. This explicit witness supplies the missing Action gate.
    object_state={'name':obj.name,'rna_type':obj.bl_rna.identifier,'editable_scalars':static.scalar_state(obj,budget),'custom':static.custom_state(obj),'nested':{key:static.nested_state(getattr(obj,key),budget) if getattr(obj,key) is not None else None for key in static.NESTED_FIELDS['Object']},'field':static.field_witness(obj,budget),'collections':sorted(c.name for c in obj.users_collection)}
    data=static.id_state(obj.data,budget)
    if obj.data.dof.focus_object is not None or obj.data.background_images:reject('Camera focus/background dependency unsupported')
    static.nested_gate(obj.data.dof);static.nested_gate(obj.data.stereo)
    data['dof']=static.scalar_state(obj.data.dof,budget);data['stereo']=static.scalar_state(obj.data.stereo,budget)
    layer=action.layers[0];strip=layer.strips[0];ad=obj.animation_data
    if bag.slot!=slot or len(ad.action_suitable_slots)!=1 or ad.action_suitable_slots[0]!=slot:reject('Unexpected camera bag/suitable-slot linkage')
    if getattr(ad,'action_tweak_storage',None) is not None:reject('Stored tweak Action unsupported')
    animation_metadata={
        'action':metadata(action,budget,handled=('slots','layers','groups','fcurves','preview','asset_data'),self_pointers=('original',)),
        'slot':metadata(slot,budget,handled=('users',)),
        'layer':metadata(layer,budget,handled=('strips',)),
        'strip':metadata(strip,budget,handled=('channelbags',)),
        'bag':metadata(bag,budget,handled=('slot','fcurves','groups')),
        'groups':[{'properties':metadata(g,budget,handled=('channels','colors')),'colors':metadata(g.colors,budget)} for g in bag.groups],
        'curve_metadata':[metadata(c,budget,handled=('keyframe_points','sampled_points','modifiers','group')) for c in curves],
        'group_channels':{g.name:[{'path':c.data_path,'index':c.array_index} for c in g.channels] for g in bag.groups},
        'anim_data':metadata(ad,budget,handled=('action','action_slot','action_tweak_storage','drivers','nla_tracks','action_suitable_slots')),
        'explicit_binding':{'owner':obj.name,'owner_type':obj.type,'action':action.name,'slot_identifier':slot.identifier,'slot_handle':slot.handle,'slot_type':slot.target_id_type,'bag_slot_matches':True,'suitable_slots':[slot.identifier]},
        'excluded':'Action preview UI thumbnail not accessed; asset data rejected. Legacy groups/fcurves aliases excluded for proven layered Action; active bag/groups fully witnessed. Slot users inferred from exclusive Action ownership and exact active slot. Runtime scalars retained, cold policy not qualified. Native ID-property capability handling limited to nine measured exact TypeError types; real keys preserved if supported'}
    return {'schema':'ANIMATED_CAMERA_SAME_FRAME_WITNESS_V2','frame':frame,'object':object_state,'data':data,'binding':{'action_name':action.name,'slot_identifier':slot.identifier,'slot_type':slot.target_id_type,'settings':native.binding_state(obj)['settings']},'action':native.action_state(action),'animation_metadata':animation_metadata,'scope':'same-frame object/data/settings/custom and full selected Action curve/key witness; not Action asset/preview metadata or trajectory safety'}

def compare(expected,actual):
    if expected!=actual:conflict('Protected animated camera witness changed')


def persisted_projection(witness):
    """Cold comparison only for trusted guarded captures, never authorization.

    Raw session UID stays in reports and strict same-process compare(). Remove
    only this known process-local scalar; all other fields remain exact.
    """
    import copy
    try:
        if witness['schema']!='ANIMATED_CAMERA_SAME_FRAME_WITNESS_V2':reject('Unknown camera witness schema')
        meta=witness['animation_metadata'];a=meta['action']['scalars'];binding=witness['binding'];link=meta['explicit_binding'];slot=meta['slot']['scalars']
        uid=a['session_uid']
        if link['owner']!=witness['object']['name'] or link['owner_type']!='CAMERA':reject('Cold owner identity linkage differs')
        if type(uid)is not int or uid<=0:reject('Expected actual Action session UID')
        if a['original']!={'self_reference':True} or a['is_evaluated'] is not False or a['library']is not None or a['override_library']is not None:reject('Cold Action identity gate failed')
        if type(a['users'])is not int or type(a['use_fake_user'])is not bool or a['users']-int(a['use_fake_user'])!=1:reject('Cold camera Action ownership gate failed')
        if a['name']!=binding['action_name'] or a['name']!=link['action']:reject('Cold Action label linkage differs')
        if type(slot['handle'])is not int or type(link['slot_handle'])is not int:reject('Cold slot handle must be typed integer')
        if binding['slot_identifier']!=link['slot_identifier'] or slot['identifier']!=link['slot_identifier'] or binding['slot_type']!=link['slot_type'] or slot['target_id_type']!=link['slot_type'] or slot['handle']!=link['slot_handle']:reject('Cold composite slot linkage differs')
        if link['bag_slot_matches']is not True or link['suitable_slots']!=[link['slot_identifier']]:reject('Cold native slot mapping missing')
        result=copy.deepcopy(witness);del result['animation_metadata']['action']['scalars']['session_uid'];return result
    except (KeyError,TypeError,AttributeError) as e:reject('Malformed trusted camera witness: '+str(e))


def compare_persisted(expected,actual):
    if persisted_projection(expected)!=persisted_projection(actual):conflict('Persisted camera witness changed beyond Action session UID')
