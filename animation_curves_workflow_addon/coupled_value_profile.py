"""Strict pure shared LINEAR displacement planning. No native apply/public route.

Exact supplied-curve arithmetic is not Blender evaluation or world/contact proof.
"""
import bisect,copy
from fractions import Fraction as F
from .contract import WorkflowError
from . import linear_value_adapter as base
from .spatial_bindings import native_number
SCHEMA='STRICT_SHARED_LINEAR_PROFILE_V1'
def reject(message,details=None):raise WorkflowError('UNSUPPORTED',message,details or {})
def conflict(message):raise WorkflowError('CONFLICT',message)
def unique_key(keys,frame):
    matches=[k for k in keys if abs(k['co'][0]-frame)<=1e-6]
    if len(matches)!=1 or matches[0]['co'][0]!=frame:conflict('Key address is missing or native-epsilon ambiguous')
    return matches[0]
def displacement(points,t):
    if t<points[0][0] or t>points[-1][0]:return F(0)
    i=min(bisect.bisect_right([p[0] for p in points],t)-1,len(points)-2)
    a,b=points[i:i+2];return a[1]+(b[1]-a[1])*(t-a[0])/(b[0]-a[0])
def evaluate(keys,t):
    times=[k[0] for k in keys];i=min(max(bisect.bisect_right(times,t)-1,0),len(keys)-2)
    a,b=keys[i:i+2];return a[1]+(b[1]-a[1])*(t-a[0])/(b[0]-a[0])
def compile_profile(inspection,targets,profile,cut_interval,neighbors,locks):
    try:return _compile(inspection,targets,profile,cut_interval,neighbors,locks)
    except (KeyError,TypeError,ValueError,IndexError,AttributeError,OverflowError) as e:raise WorkflowError('INVALID_REQUEST','Malformed shared-profile input') from e

def _compile(inspection,targets,profile,cut_interval,neighbors,locks):
    if type(targets)is not list or not 2<=len(targets)<=4 or any(type(t)is not str or not t for t in targets) or len(set(targets))!=len(targets):reject('Explicit two-to-four unique participant targets required')
    if type(profile)is not list or not 3<=len(profile)<=16:reject('Profile point budget')
    points=[]
    for p in profile:
        if type(p)is not dict or set(p)!={'frame','delta'}:reject('Explicit profile frame/delta required')
        points.append((F(native_number(p['frame'])),F(native_number(p['delta']))))
    if [p[0] for p in points]!=sorted(set(p[0] for p in points)):conflict('Profile times must be unique and ordered')
    if points[0][1] or points[-1][1] or not any(d for _,d in points):reject('Nonzero profile requires exact zero endpoints')
    if type(cut_interval)is not list or len(cut_interval)!=2:reject('Explicit cut interval required')
    cut=[F(native_number(x)) for x in cut_interval]
    if not cut[0]<=points[0][0]<points[-1][0]<=cut[1]:conflict('Profile crosses declared cut interval')
    if type(neighbors)is not list or len(neighbors)>512 or type(locks)is not list or len(locks)>4096:reject('Neighbor/lock budget')
    scope=[base.address(x) for x in neighbors];locked=[base.address(x) for x in locks]
    if len(set(scope))!=len(scope) or len(set(locked))!=len(locked):conflict('Duplicate neighbor/lock address')
    rows=inspection['targets']
    if type(rows)is not list or not 1<=len(rows)<=64:reject('Complete inspection target budget')
    total=0
    for row in rows:
        keys=row['channel']['keys']
        if type(keys)is not list or not 1<=len(keys)<=8192:reject('Complete inspection curve key budget')
        total+=len(keys)
        if total>50000:reject('Complete inspection aggregate key budget')
    byid={r['id']:r for r in rows}
    if len(byid)!=len(rows):conflict('Duplicate target identities')
    old={};new={};selected=[];operations=[];indices={};owners=set();actions=set();involved=0
    for target in targets:
        row=byid[target];keys=row['channel']['keys'];times=[F(k['co'][0]) for k in keys]
        if row['array_index']!=0:reject('Initial common-world-X model requires Object.location X')
        if row['owner']['name'] in owners or row['action_name'] in actions:conflict('Participants must have distinct owners and observed Actions')
        owners.add(row['owner']['name']);actions.add(row['action_name'])
        if any(p[0] not in times for p in points):conflict('Every profile breakpoint must be an exact existing participant key')
        if not times[0]<points[0][0]<points[-1][0]<times[-1]:reject('Profile must lie strictly inside existing curve endpoints to protect extrapolation')
        changed=[];newkeys=[]
        for i,k in enumerate(keys):
            t,v=F(k['co'][0]),F(k['co'][1]);d=displacement(points,t)
            if points[0][0]<=t<=points[-1][0]:involved+=1
            if involved>512:reject('Profile involved-key budget')
            if d:
                unique_key(keys,float(t))
                exact=v+d
                try:value=native_number(float(exact))
                except WorkflowError:reject('Profile target is not exactly native representable',{'target':target,'frame':float(t),'required_value':str(exact),'quantization':'rejected, no rounding or fallback'})
                if F(value)!=exact:reject('Profile arithmetic loses exact precision',{'target':target,'frame':float(t),'required_value':str(exact)})
                if any(k[f] not in base.AUTO for f in ('handle_left_type','handle_right_type')):reject('Changed profile keys require automatic handles')
                selected.append({'target':target,'frame':float(t),'value':value,'before':copy.deepcopy(k)});operations.append({'op':'set_key','target':target,'frame':float(t),'value':value,'interpolation':'LINEAR'});changed.append(i)
                if len(selected)>128:reject('Profile changed-key budget')
                newkeys.append((t,exact))
            else:newkeys.append((t,v))
        if not changed:reject('Each participant must have a nonzero existing-key change')
        # Reuse complete native curve/address/alias/schema gates, without borrowing
        # the old adapter's single-key scope as authorization for this plan.
        first=next(s for s in selected if s['target']==target)
        base.compile_plan(inspection,[{k:first[k] for k in ('target','frame','value')}],[],[])
        old[target]=[(F(k['co'][0]),F(k['co'][1])) for k in keys];new[target]=newkeys;indices[target]=changed
    if any(row['action_name'] in actions and row['owner']['name'] not in owners for row in rows):reject('Observed Action also belongs to a nonparticipant')
    changed_addresses={(s['target'],s['frame']) for s in selected};neighbor_witnesses=[];lock_witnesses=[]
    for target,frame in scope:
        if target not in targets or (target,frame) in changed_addresses or (target,frame) in locked:conflict('Neighbor overlaps selection/lock or is outside group')
        keys=byid[target]['channel']['keys'];times=[k['co'][0] for k in keys]
        eligible={times[j] for i in indices[target] for j in range(max(0,i-2),min(len(times),i+3)) if j not in indices[target]}
        if frame not in eligible:reject('Neighbor outside explicit nearest-two changed-key neighborhood')
        k=unique_key(keys,frame);base.key_check(k)
        if any(k[f] not in base.AUTO for f in ('handle_left_type','handle_right_type')):reject('Named neighbor requires automatic handles')
        neighbor_witnesses.append({'target':target,'frame':frame,'before':copy.deepcopy(k)})
    for target,frame in locked:
        if (target,frame) in changed_addresses or (target,frame) in scope:conflict('Full-key lock overlaps edit or handle scope')
        key=unique_key(byid[target]['channel']['keys'],frame)
        base.key_check(key);lock_witnesses.append({'target':target,'frame':frame,'before':copy.deepcopy(key)})
    knots=sorted({t for seq in old.values() for t,_ in seq if points[0][0]<=t<=points[-1][0]})
    probes=sorted(set(knots+[(a+b)/2 for a,b in zip(knots,knots[1:])]))
    if len(probes)>1024:reject('Exact model proof budget')
    for target in targets:
        for t in probes:
            if evaluate(new[target],t)-evaluate(old[target],t)!=displacement(points,t):conflict('Exact supplied-curve profile identity failed')
        if any(a!=b for a,b in zip(old[target],new[target]) if not points[0][0]<a[0]<points[-1][0]):conflict('Outside profile key values changed')
    return {'schema':SCHEMA,'status':'compiled_not_accepted','native_apply_supported':False,'inspection_sha256':base.fingerprint(inspection),
            'selectors':{'targets':copy.deepcopy(targets),'profile':copy.deepcopy(profile),'cut_interval':copy.deepcopy(cut_interval),'neighbors':copy.deepcopy(neighbors),'locks':copy.deepcopy(locks)},
            'selected_witnesses':selected,'neighbor_witnesses':neighbor_witnesses,'lock_witnesses':lock_witnesses,'operations':operations,
            'changed_interval':[float(points[0][0]),float(points[-1][0])],
            'exact_model_proof':{'profile_points':[[str(t),str(d)] for t,d in points],'probe_times':[str(t) for t in probes],'participants':len(targets),'outside_key_values_exact':True,'claim':'Exact supplied LINEAR mathematical curves only, not Blender numerical evaluation/world pose/contact'},
            'pending':['fresh native dependencies/owner linkage','multi-key transaction/full final readback','outside native evaluation','rollback/cold','world/group validity','project/cut provenance','collision/contact/trajectory/user acceptance']}

def recompile(inspection,plan):
    selectors=plan['selectors']
    fresh=compile_profile(inspection,selectors['targets'],selectors['profile'],selectors['cut_interval'],selectors['neighbors'],selectors['locks'])
    if fresh!=plan:conflict('Fresh complete profile plan differs from pinned request')
    return fresh

def preflight_manifest(manifest,inspection,plan):
    """Internal fixed-policy seam only; no mutation or public registration."""
    try:
        if manifest['operation']!='edit' or 'event_shift' in manifest or 'derived_handle_scope' in manifest:reject('Shared-profile path cannot mix other edit policies')
        recompile(inspection,plan)
        if manifest['operations']!=plan['operations']:conflict('Normalized manifest differs from exact profile operations')
        declared=[base.address(x) for x in plan['selectors']['locks']]
        actual=[(x['target'],native_number(x['frame'])) for x in manifest['locks']]
        if len(set(actual))!=len(actual) or set(actual)!=set(declared) or any(x['tolerance']!=0 for x in manifest['locks']):conflict('Profile manifest requires identical full-key locks at zero tolerance')
        return {target:{w['frame'] for w in plan['neighbor_witnesses'] if w['target']==target} for target in plan['selectors']['targets'] if any(w['target']==target for w in plan['neighbor_witnesses'])}
    except (KeyError,TypeError,ValueError,IndexError,AttributeError,OverflowError) as e:raise WorkflowError('INVALID_REQUEST','Malformed internal profile request') from e

def verify_readback(before,after,plan):
    """Complete recorded curve/key readback; native bindings verified by core."""
    try:
        recompile(before,plan)
        b={r['id']:r for r in before['targets']};a={r['id']:r for r in after['targets']}
        if len(a)!=len(after['targets']) or set(a)!=set(b):conflict('Readback target inventory differs')
        selected={(s['target'],s['frame']):s for s in plan['selected_witnesses']};scope={(s['target'],s['frame']) for s in plan['neighbor_witnesses']};deltas=[]
        for target,row in b.items():
            now=a[target]
            if {k:v for k,v in row.items() if k not in ('channel','samples','action_name')}!={k:v for k,v in now.items() if k not in ('channel','samples','action_name')}:conflict('Readback recorded channel identity differs')
            old_channel=copy.deepcopy(row['channel']);new_channel=copy.deepcopy(now['channel']);oldkeys=old_channel.pop('keys');newkeys=new_channel.pop('keys')
            if old_channel!=new_channel or len(oldkeys)!=len(newkeys):conflict('Readback curve metadata/inventory differs')
            for old,new in zip(oldkeys,newkeys):
                base.key_check(old);base.key_check(new);address=(target,old['co'][0]);changed=selected.get(address);expected=copy.deepcopy(old);actual=copy.deepcopy(new)
                if changed:expected['co'][1]=expected['co_ui'][1]=changed['value']
                if changed or address in scope:
                    coords={f:{'before':expected.pop(f),'after':actual.pop(f)} for f in ('handle_left','handle_right')}
                    if expected!=actual:conflict('Profile changed protected key value/time/metadata')
                    differences={k:v for k,v in coords.items() if v['before']!=v['after']}
                    if differences:deltas.append({'target':target,'frame':old['co'][0],'handle_changes':differences})
                elif expected!=actual:conflict('Unnamed profile key changed')
        return {'status':'profile_readback_matches_not_accepted','actual_handle_deltas':deltas,'changed_intervals':[{'target':t,'from_frame':plan['changed_interval'][0],'through_frame':plan['changed_interval'][1],'unbounded_endpoint_extrapolation':False} for t in plan['selectors']['targets']],
                'pending':['actual native displacement/outside samples','cold','complete project preservation','world/contact/trajectory/user acceptance']}
    except (KeyError,TypeError,ValueError,IndexError,AttributeError,OverflowError) as e:raise WorkflowError('INVALID_REQUEST','Malformed profile readback') from e
