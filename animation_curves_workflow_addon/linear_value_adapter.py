"""Explicit pure LINEAR/AUTO existing-value plan/readback. No native apply.

Trusted complete native inspection required; future guarded caller owns source,
Action ownership, fresh writing gates, static preservation and rollback proof.
"""
import copy,hashlib
from .contract import WorkflowError
from .spatial_bindings import native_number
from .spatial_preview_identity import canonical
KEY_FIELDS={'select_left_handle','select_right_handle','select_control_point','handle_left_type','handle_right_type','interpolation','type','easing','back','amplitude','period','handle_left','co','co_ui','handle_right'}
AUTO={'AUTO','AUTO_CLAMPED'}
SCHEMA='EXPLICIT_LINEAR_AUTO_VALUE_PLAN_V1'
def reject(msg):raise WorkflowError('UNSUPPORTED',msg)
def conflict(msg):raise WorkflowError('CONFLICT',msg)
def fingerprint(v):return hashlib.sha256(canonical(v)).hexdigest()
def key_check(k):
    if type(k)is not dict or set(k)!=KEY_FIELDS:reject('Complete fixed-runtime native key schema required')
    for f in ('co','co_ui','handle_left','handle_right'):
        if type(k[f])is not list or len(k[f])!=2:reject('Complete two-coordinate key fields required')
        for n in k[f]:native_number(n)
    if k['co_ui']!=k['co']:reject('Unexpected co_ui alias')
    for f in ('back','amplitude','period'):native_number(k[f])
    for f in ('select_left_handle','select_right_handle','select_control_point'):
        if type(k[f])is not bool:reject('Native selection flags must be BOOL')
    for f in ('type','easing','interpolation','handle_left_type','handle_right_type'):
        if type(k[f])is not str or not k[f]:reject('Native key enums required')
def address(v):
    if type(v)is not dict or set(v)!={'target','frame'} or type(v['target'])is not str or not v['target']:reject('Explicit key address required')
    return v['target'],native_number(v['frame'])
def compile_plan(inspection,edits,neighbors,locked_keys):
    try:return _compile(inspection,edits,neighbors,locked_keys)
    except (KeyError,TypeError,ValueError,IndexError,AttributeError,OverflowError) as e:raise WorkflowError('INVALID_REQUEST','Malformed trusted LINEAR value-edit input') from e

def _compile(inspection,edits,neighbors,locks):
    rows=inspection['targets']
    if type(rows)is not list or not 1<=len(rows)<=64:reject('Inspection target budget')
    byid={r['id']:r for r in rows}
    if len(byid)!=len(rows):conflict('Duplicate inspection target IDs')
    all_key_count=0;observed_addresses=set()
    for row in rows:
        addr=(row['owner']['type'],row['owner']['name'],row['data_path'],row['array_index'])
        if addr in observed_addresses:conflict('Inspection contains duplicate physical channel aliases')
        observed_addresses.add(addr)
        if row['data_path']!=row['channel']['data_path'] or type(row['array_index'])is not int or type(row['channel']['array_index'])is not int or row['array_index']!=row['channel']['array_index']:conflict('Inspection row/channel physical address mismatch')
        keys=row['channel']['keys']
        if type(keys)is not list or len(keys)>8192:reject('Complete inspection curve key budget')
        all_key_count+=len(keys)
        if all_key_count>50000:reject('Complete inspection aggregate key budget')
    if type(edits)is not list or not 1<=len(edits)<=24 or type(neighbors)is not list or len(neighbors)>96 or type(locks)is not list or len(locks)>4096:reject('Explicit bounded edit/scope/lock lists required')
    selected={};aliases=set();actions={};owner_bindings={};total=0;allowed=[];witnesses=[];intervals=[]
    scope=[address(x) for x in neighbors];locked=[address(x) for x in locks]
    if len(set(scope))!=len(scope) or len(set(locked))!=len(locked):conflict('Duplicate neighbor/lock address')
    for e in edits:
        if type(e)is not dict or set(e)!={'target','frame','value'}:reject('Exact set-value selector required')
        t,frame=address({'target':e['target'],'frame':e['frame']});value=native_number(e['value'])
        if t in selected:reject('One selected existing key per channel')
        row=byid[t];owner=row['owner']
        if type(owner)is not dict or set(owner)!={'type','name'} or owner['type']!='OBJECT' or type(owner['name'])is not str or not owner['name'] or row['data_path']!='location' or type(row['array_index'])is not int or not 0<=row['array_index']<3:reject('Initial adapter supports Object.location only')
        physical=(owner['name'],row['data_path'],row['array_index'])
        if physical in aliases:conflict('Physical channel aliases')
        aliases.add(physical)
        binding=(row['action_name'],row['slot_identifier'],row['slot_type'])
        if any(type(x)is not str or not x for x in binding) or binding[2]!='OBJECT':reject('Complete observed OBJECT Action/slot binding required')
        if owner_bindings.setdefault(owner['name'],binding)!=binding:conflict('Mixed owner Action/slot inspection')
        if actions.setdefault(binding[0],owner['name'])!=owner['name']:reject('Shared Action owners unsupported')
        channel=row['channel'];keys=channel['keys'];props=channel['properties']
        if channel['modifiers'] or channel['sampled_points'] or props['mute']is not False or props['lock']is not False:reject('Modified/sampled/muted/locked channels unsupported')
        if type(keys)is not list or not 1<=len(keys)<=8192:reject('Curve key budget')
        total+=len(keys)
        if total>50000:reject('Aggregate selected curve key budget')
        times=[]
        for k in keys:
            key_check(k)
            if k['interpolation']!='LINEAR':reject('Entire edited curve must remain LINEAR')
            times.append(k['co'][0])
        if times!=sorted(set(times)):conflict('Key times must be unique and ordered')
        matches=[i for i,x in enumerate(times) if abs(x-frame)<=1e-6]
        if len(matches)!=1 or times[matches[0]]!=frame:conflict('Exact existing key missing/ambiguous; no insertion')
        i=matches[0];key=keys[i]
        if value==key['co'][1]:reject('No-op value edits are not scoped mutations')
        if any(key[f] not in AUTO for f in ('handle_left_type','handle_right_type')):reject('Selected key must use native automatic handles')
        selected[t]={'frame':frame,'value':value,'before':copy.deepcopy(key)}
        eligible=set(times[max(0,i-2):i]+times[i+1:i+3])
        for target,nframe in scope:
            if target!=t:continue
            if nframe not in eligible:reject('Only explicitly named nearest two neighbors each side may enter scope')
            neighbor=keys[times.index(nframe)]
            if any(neighbor[f] not in AUTO for f in ('handle_left_type','handle_right_type')):reject('Manual neighbor handles cannot enter automatic scope')
            allowed.append({'target':t,'expected_key':copy.deepcopy(neighbor)})
        witnesses.append({'target':t,'owner':copy.deepcopy(owner),'action_name':binding[0],'slot_identifier':binding[1],'slot_type':binding[2],'expected_key':copy.deepcopy(key)})
        intervals.append({'target':t,'from_frame':times[i-1] if i else None,'through_frame':times[i+1] if i+1<len(times) else None,'unbounded_endpoint_extrapolation':i==0 or i==len(times)-1})
    if any(t not in selected for t,f in scope):reject('Scope must address an edited channel')
    for row in rows:
        if row['action_name'] in actions and row['owner']!={'type':'OBJECT','name':actions[row['action_name']]}:reject('Observed Action is shared with another inspected owner')
    touched=set(scope)|{(t,v['frame']) for t,v in selected.items()}
    for t,f in locked:
        if t not in byid:reject('Unknown locked target')
        matches=[k for k in byid[t]['channel']['keys'] if abs(k['co'][0]-f)<=1e-6]
        if len(matches)!=1 or matches[0]['co'][0]!=f:conflict('Locked key must be exact existing native time')
        if (t,f) in touched:conflict('Full-key lock overlaps value or automatic-coordinate scope')
    result={'schema':SCHEMA,'status':'compiled_not_accepted','native_apply_supported':False,'inspection_sha256':fingerprint(inspection),'selectors':{'edits':copy.deepcopy(edits),'neighbors':copy.deepcopy(neighbors),'locked_keys':copy.deepcopy(locks)},'selected':selected,'selected_witnesses':witnesses,'neighbor_witnesses':allowed,'changed_intervals':intervals,'operations':[{'op':'set_key','target':t,'frame':v['frame'],'value':v['value'],'interpolation':'LINEAR'} for t,v in selected.items()],'pending':['fresh_native_writing_and_dependency_gates','distinct_set_value_handle_policy','complete_owner_static_preservation','actual_outside_interval_evaluation','rollback','cold','spatial_safety','user_acceptance']}
    return result

def verify_readback(before,after,plan):
    try:
        selectors=plan['selectors']
        if plan!=compile_plan(before,selectors['edits'],selectors['neighbors'],selectors['locked_keys']):conflict('Compiled plan/fresh witness drift')
        b={r['id']:r for r in before['targets']};a={r['id']:r for r in after['targets']}
        if len(a)!=len(after['targets']) or set(a)!=set(b):conflict('Readback target inventory changed')
        scope={(v['target'],v['expected_key']['co'][0]) for v in plan['neighbor_witnesses']};deltas=[]
        for t,row in b.items():
            now=a[t]
            for field in ('id','owner','data_path','array_index','slot_identifier','slot_type'):
                if row[field]!=now[field]:conflict('Readback channel/owner binding changed')
            left=copy.deepcopy(row['channel']);right=copy.deepcopy(now['channel']);oldkeys=left.pop('keys');newkeys=right.pop('keys')
            if left!=right or len(oldkeys)!=len(newkeys):conflict('Curve metadata or key inventory changed')
            selected=plan['selected'].get(t)
            for old,new in zip(oldkeys,newkeys):
                key_check(old);key_check(new);frame=old['co'][0]
                touched=selected is not None and selected['frame']==frame
                allowed=touched or (t,frame) in scope
                expected=copy.deepcopy(old)
                if touched:expected['co'][1]=expected['co_ui'][1]=selected['value']
                actual=copy.deepcopy(new)
                if allowed:
                    coords={f:{'before':expected.pop(f),'after':actual.pop(f)} for f in ('handle_left','handle_right')}
                    if expected!=actual:conflict('Value edit changed protected key metadata/time/value')
                    changes={f:v for f,v in coords.items() if v['before']!=v['after']}
                    if changes:deltas.append({'target':t,'frame':frame,'handle_changes':changes})
                elif expected!=actual:conflict('Unnamed key changed')
        return {'status':'readback_matches_not_accepted','actual_handle_deltas':deltas,'changed_intervals':copy.deepcopy(plan['changed_intervals']),'pending':['actual_outside_interval_evaluations','native_owner_action_linkage','cold','spatial_safety','user_acceptance']}
    except (KeyError,TypeError,ValueError,IndexError,AttributeError,OverflowError) as e:raise WorkflowError('INVALID_REQUEST','Malformed LINEAR readback') from e

def preflight_manifest(manifest,inspection,plan):
    """Additional rejection gate for future internal native seam; no mutation."""
    try:
        if manifest['operation']!='edit' or 'event_shift' in manifest or 'derived_handle_scope' in manifest:reject('LINEAR value path cannot mix event/old derived scope policies')
        selectors=plan['selectors']
        if plan!=compile_plan(inspection,selectors['edits'],selectors['neighbors'],selectors['locked_keys']):conflict('Fresh complete native inspection differs from pinned value plan')
        if manifest['operations']!=plan['operations']:conflict('Manifest operations differ from explicit value plan')
        declared=[address(x) for x in selectors['locked_keys']]
        actual=[(x['target'],native_number(x['frame'])) for x in manifest['locks']]
        if len(set(actual))!=len(actual) or set(actual)!=set(declared) or any(x['tolerance']!=0 for x in manifest['locks']):conflict('Native manifest must carry the same exact full-key locks at zero tolerance')
        return {target:{v['expected_key']['co'][0] for v in plan['neighbor_witnesses'] if v['target']==target} for target in plan['selected'] if any(v['target']==target for v in plan['neighbor_witnesses'])}
    except (KeyError,TypeError,ValueError,AttributeError,OverflowError) as e:raise WorkflowError('INVALID_REQUEST','Malformed internal value-plan request') from e
