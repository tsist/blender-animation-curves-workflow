"""Pure authenticated-projection consumer; no native capture or public capability."""
import hashlib
import json
import math
import re

class Rejected(ValueError):
    pass

def exact(obj, keys):
    if type(obj) is not dict or set(obj) != set(keys):
        raise Rejected('unknown or missing fields')

def number(x):
    if type(x) not in (int,float) or abs(x)>10000000 or not math.isfinite(x):
        raise Rejected('invalid frame')
    return x

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def sha(value):
    if type(value) is not str or not re.fullmatch('[0-9a-f]{64}',value):raise Rejected('invalid SHA')

def name(value):
    if type(value) is not str or not 1<=len(value)<=256 or '\0' in value:raise Rejected('invalid name')

def certify(projection, intervals, *, expected_projection_sha256):
    exact(projection,('source_sha256','native_witness_sha256','scene','scene_range','cameras','cuts'))
    sha(projection['source_sha256']);sha(projection['native_witness_sha256']);sha(expected_projection_sha256)
    name(projection['scene'])
    bounds=projection['scene_range']
    if type(bounds) is not list or len(bounds)!=2:raise Rejected('range shape')
    start,end=map(number,bounds)
    if start>end:raise Rejected('range order')
    cameras=projection['cameras'];cuts=projection['cuts']
    if type(cameras) is not list or not 1<=len(cameras)<=256:raise Rejected('camera budget')
    for camera in cameras:name(camera)
    if len(set(cameras))!=len(cameras):raise Rejected('camera alias')
    if type(cuts) is not list or len(cuts)>512:raise Rejected('cut budget')
    if not cuts:raise Rejected('unavailable: no native camera cuts')
    previous=None
    for cut in cuts:
        exact(cut,('frame','camera'))
        f=number(cut['frame']);name(cut['camera'])
        if cut['camera'] not in cameras:raise Rejected('external camera')
        if previous is not None and f<=previous:raise Rejected('ambiguous or unordered cuts')
        previous=f
    if digest(projection)!=expected_projection_sha256:raise Rejected('projection drift')
    if type(intervals) is not list or not 1<=len(intervals)<=512:raise Rejected('interval budget')
    coverage=[]
    for interval in intervals:
        if type(interval) is not list or len(interval)!=2:raise Rejected('interval shape')
        lo,hi=map(number,interval)
        if not start<=lo<=hi<=end:raise Rejected('outside native range')
        eligible=[i for i,c in enumerate(cuts) if c['frame']<=lo]
        if not eligible:raise Rejected('no native cut covering interval')
        i=eligible[-1]
        if i+1<len(cuts) and hi>=cuts[i+1]['frame']:raise Rejected('crosses native camera cut')
        coverage.append({'interval':interval[:],'camera':cuts[i]['camera'],'cut_frame':cuts[i]['frame']})
    return {'status':'not_accepted','scope':'saved_native_camera_switch_coverage_only','projection_sha256':expected_projection_sha256,'native_witness_sha256':projection['native_witness_sha256'],'coverage':coverage,'project_agreement':'unassessed','native_capture_qualified':False}


def certify_compiled(projection, inspection, compiled_plan, *, expected_projection_sha256, expected_compiled_sha256, guarded_source_sha256):
    """Recompile entire pinned plan, derive coverage internally; no interval argument."""
    from animation_curves_workflow_addon import coupled_value_profile
    sha(expected_compiled_sha256);sha(guarded_source_sha256)
    if projection['source_sha256']!=guarded_source_sha256:raise Rejected('guarded source mismatch')
    if projection['scene']!=inspection['context']['scene']:raise Rejected('scene mismatch')
    if digest(compiled_plan)!=expected_compiled_sha256:
        raise Rejected('compiled plan drift')
    fresh=coupled_value_profile.recompile(inspection,compiled_plan)
    profile=fresh['selectors']['profile']
    support=[number(profile[0]['frame']),number(profile[-1]['frame'])]
    if fresh['changed_interval']!=support:
        raise Rejected('omitted or unbounded profile influence')
    # Existing exact LINEAR compiler proves every outside key unchanged and
    # full shared profile support. No caller-supplied subset is accepted here.
    if fresh['exact_model_proof']['outside_key_values_exact'] is not True:
        raise Rejected('outside influence unassessed')
    result=certify(projection,[support],expected_projection_sha256=expected_projection_sha256)
    result.update(compiled_plan_sha256=expected_compiled_sha256,
                  inspection_sha256=fresh['inspection_sha256'],
                  coverage_derivation='full fresh STRICT_SHARED_LINEAR_PROFILE_V1 support')
    return result

# Reconstructed optional seam, not a byte-identical recovery of lost v2.
def projection_from_capture(captured):
    exact(captured,('raw','projection'));raw=captured['raw']
    exact(raw,('source_sha256','scene','scene_range','saved_frame','saved_subframe','scene_camera','markers','cameras','coverage_status','scope'))
    exact(raw['scene'],('type','name'))
    if raw['scene']['type']!='Scene':raise Rejected('scene identity')
    markers,cameras=raw['markers'],raw['cameras']
    if type(markers)is not list or len(markers)>512 or type(cameras)is not dict or len(cameras)>256:raise Rejected('capture budget')
    for key,row in cameras.items():
        name(key);exact(row,('owner','data','owner_custom','data_custom'))
        for field,kind in [('owner','Object'),('data','Camera')]:
            exact(row[field],('type','name'));name(row[field]['name'])
            if row[field]['type']!=kind:raise Rejected('camera type')
        if row['owner']['name']!=key:raise Rejected('camera alias')
    cuts=[]
    for i,m in enumerate(markers):
        exact(m,('index','name','frame','select','camera','custom'))
        if type(m['index'])is not int or m['index']!=i or type(m['frame'])is not int or type(m['select'])is not bool:raise Rejected('marker field types')
        name(m['name']);number(m['frame'])
        if m['camera'] is not None:
            exact(m['camera'],('type','name'))
            if m['camera']['name'] not in cameras or m['camera']!=cameras[m['camera']['name']]['owner']:raise Rejected('marker linkage')
            cuts.append({'frame':m['frame'],'camera':m['camera']['name']})
    if raw['scene_camera'] is not None and raw['scene_camera'] not in [r['owner'] for r in cameras.values()]:raise Rejected('active camera linkage')
    result={'source_sha256':raw['source_sha256'],'native_witness_sha256':digest(raw),'scene':raw['scene']['name'],'scene_range':raw['scene_range'],'cameras':sorted(cameras),'cuts':sorted(cuts,key=lambda x:x['frame'])}
    if result!=captured['projection'] or raw['coverage_status']!=('available' if cuts else 'unavailable_no_camera_markers'):raise Rejected('raw projection mismatch')
    return result

def certify_capture(captured,inspection,compiled,source_sha256):
    projection=projection_from_capture(captured)
    return certify_compiled(projection,inspection,compiled,expected_projection_sha256=digest(projection),expected_compiled_sha256=digest(compiled),guarded_source_sha256=source_sha256)
