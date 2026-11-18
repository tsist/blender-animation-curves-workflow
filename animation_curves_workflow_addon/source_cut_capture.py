"""Draft readonly marker capture. Not executed or registered. No frame evaluation."""
import math
from .source_cut_contract import Rejected, digest, sha

def local_id(value,kind):
    if value is None or value.bl_rna.identifier!=kind or value.library is not None or value.override_library is not None or value.is_evaluated or value.original!=value:
        raise Rejected('unsupported nonlocal/evaluated identity')
    return {'type':kind,'name':value.name}

def custom(value, budget=None):
    budget=[0] if budget is None else budget
    def plain(v,depth=0):
        budget[0]+=1
        if budget[0]>8192 or depth>8:raise Rejected('custom node/depth budget')
        if type(v) in (str,int,float,bool) or v is None:
            if type(v) is str and (len(v)>4096 or '\0' in v):raise Rejected('custom string budget')
            if type(v) is int and v.bit_length()>128:raise Rejected('custom integer budget')
            if type(v) is float and not math.isfinite(v):raise Rejected('custom nonfinite')
            return v
        kind=(type(v).__module__,type(v).__name__)
        if type(v) is dict or kind==('idprop.types','IDPropertyGroup'):
            keys=v.keys()
            if len(keys)>64:raise Rejected('custom key budget')
            result={}
            for k in keys:
                if type(k) is not str or not 1<=len(k)<=256 or '\0' in k:raise Rejected('custom key type')
                result[k]=plain(v[k],depth+1)
            return result
        if type(v) in (list,tuple) or kind==('idprop.types','IDPropertyArray'):
            if len(v)>256:raise Rejected('custom array budget')
            return [plain(v[i],depth+1) for i in range(len(v))]
        raise Rejected('unsupported custom pointer/type')
    try:keys=value.keys()
    except TypeError as e:
        if value.bl_rna.identifier!='TimelineMarker' or str(e)!="bpy_struct.keys(): this type doesn't support IDProperties":raise
        return {'support':'measured_no_idproperties'}
    if len(keys)>64:raise Rejected('custom key budget')
    result={}
    for k in keys:
        if type(k) is not str or not 1<=len(k)<=256 or '\0' in k:raise Rejected('custom key type')
        result[k]=plain(value[k])
    return {'support':'supported','state':result}

def capture(bpy, source_sha256):
    sha(source_sha256)
    scene=bpy.context.scene
    if len(scene.objects)>256 or len(scene.timeline_markers)>512:raise Rejected('scene/marker budget')
    objects={o.name:o for o in scene.objects}
    cameras={};budget=[0]
    def camera_identity(camera):
        if camera is None:return None
        if camera.type!='CAMERA' or objects.get(camera.name)!=camera:raise Rejected('external/noncamera pointer')
        owner=local_id(camera,'Object');data=local_id(camera.data,'Camera')
        if camera.parent is not None or len(camera.constraints) or len(camera.modifiers) or camera.animation_data is not None or camera.data.animation_data is not None:
            raise Rejected('camera dependency/animation unsupported in initial cut capture')
        # This establishes switch target identity, not all camera data preservation.
        row={'owner':owner,'data':data,'owner_custom':custom(camera,budget),'data_custom':custom(camera.data,budget)}
        cameras[camera.name]=row
        return owner
    active=camera_identity(scene.camera)
    rows=[];cuts=[]
    for index,marker in enumerate(scene.timeline_markers):
        fields={p.identifier for p in marker.bl_rna.properties if p.identifier!='rna_type'}
        if fields!={'name','frame','select','camera'}:raise Rejected('unknown marker RNA schema')
        if type(marker.frame) is not int:raise Rejected('noninteger native marker')
        camera=camera_identity(marker.camera)
        rows.append({'index':index,'name':marker.name,'frame':marker.frame,'select':marker.select,'camera':camera,'custom':custom(marker,budget)})
        if camera is not None:cuts.append({'frame':marker.frame,'camera':camera['name']})
    cuts.sort(key=lambda x:x['frame'])
    if len({x['frame'] for x in cuts})!=len(cuts):raise Rejected('ambiguous camera cut frame')
    raw={'source_sha256':source_sha256,'scene':local_id(scene,'Scene'),'scene_range':[scene.frame_start,scene.frame_end],
         'saved_frame':scene.frame_current,'saved_subframe':scene.frame_subframe,'scene_camera':active,
         'markers':rows,'cameras':cameras,'coverage_status':'available' if cuts else 'unavailable_no_camera_markers',
         'scope':'camera switch identities only; not whole camera/static preservation'}
    if not math.isfinite(raw['saved_subframe']):raise Rejected('nonfinite subframe')
    projection={'source_sha256':source_sha256,'native_witness_sha256':digest(raw),'scene':scene.name,
                'scene_range':raw['scene_range'],'cameras':sorted(cameras),'cuts':cuts}
    return {'raw':raw,'projection':projection}
