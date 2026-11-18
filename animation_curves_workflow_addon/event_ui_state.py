"""Pure named-event panel state. Explicit bindings only; no bpy or file writes.

Schema v1 is the original single-key-per-channel named event.  Schema v2 is the
clip-window extension: one event, the same explicit target/binding model, and
multiple offsets per channel.  It shares this store instead of creating a
parallel event library.
"""
import copy,hashlib,json,math
from .contract import WorkflowError
from . import bindings

CLIP_SCHEMA='event-panel-state-v2'
CLIP_POLICY='clip-window-v1'
CLIP_KEY_FIELDS=('co','handle_left','handle_right','handle_left_type',
                 'handle_right_type','interpolation','easing','back','amplitude',
                 'period','type')

def empty():return {'schema':'event-panel-state-v1','targets':{},'events':{},'bindings':[]}
def empty_clip():
    return {'schema':CLIP_SCHEMA,'policy':CLIP_POLICY,'id':None,'revision':1,
            'context':{},'participants':{},'locks':[],'targets':{},
            'events':{},'bindings':[]}
def _require_legacy(state):
    if not isinstance(state,dict) or state.get('schema')!='event-panel-state-v1':
        raise WorkflowError('CONFLICT','Clip-window event state must not enter the legacy single-key editor')
def _require_clip(state):
    if not isinstance(state,dict) or state.get('schema')!=CLIP_SCHEMA or state.get('policy')!=CLIP_POLICY:
        raise WorkflowError('INVALID_REQUEST','Unknown clip event state schema/policy')
def semantic_key(key):
    if not isinstance(key,dict):raise WorkflowError('INVALID_REQUEST','Native key witness must be an object')
    out={field:copy.deepcopy(key[field]) for field in CLIP_KEY_FIELDS if field in key}
    if 'co' not in out or not isinstance(out['co'],list) or len(out['co'])!=2:
        raise WorkflowError('INVALID_REQUEST','Native key witness requires co[2]')
    for field in ('handle_left','handle_right'):
        if field in out and (not isinstance(out[field],list) or len(out[field])!=2):
            raise WorkflowError('INVALID_REQUEST','Native key witness requires '+field+'[2]')
    return out
def target_id(target):
    fields={k:target[k] for k in ('owner','data_path','array_index')}
    return 'channel_'+hashlib.sha256(json.dumps(fields,sort_keys=True).encode()).hexdigest()[:24]
def manifest(state,context):
    return {'schema':'1.0','operation':'inspect','context':dict(context),'targets':list(copy.deepcopy(state['targets']).values()),
            'operations':[],'samples':sorted({e['frame'] for e in state['events'].values()}),'locks':[]}
def capture(state,event,frame,target,inspection,locked=False):
    _require_legacy(state)
    if not isinstance(event,str) or not event.strip() or len(event)>128 or type(locked) is not bool:
        raise WorkflowError('INVALID_REQUEST','Explicit event name and lock required')
    if isinstance(frame,bool) or not isinstance(frame,(float,int)) or not math.isfinite(frame):
        raise WorkflowError('INVALID_REQUEST','Finite source frame required')
    out=copy.deepcopy(state);ident=target_id(target)
    if ident!=target['id']:raise WorkflowError('CONFLICT','Selected target identity changed')
    prior=out['events'].get(event)
    if prior and (prior['frame']!=frame or prior['locked']!=locked):
        raise WorkflowError('CONFLICT','Existing event timing/lock differs; do not overwrite it during capture')
    if any(b['event']==event and b['target']==ident for b in out['bindings']):
        raise WorkflowError('CONFLICT','Binding already exists; inspect then explicitly rebind')
    witness=bindings.capture(inspection,ident,frame)
    out['events'][event]={'id':event,'frame':frame,'locked':locked};out['targets'][ident]=copy.deepcopy(target)
    out['bindings'].append({'event':event,'target':ident,'offset':0,'expected_key':witness['expected_key'],'witness':witness})
    if len(out['targets'])>256 or len(out['events'])>512 or len(out['bindings'])>4096:
        raise WorkflowError('UNSUPPORTED','Event panel contract capacity exceeded')
    return out

def inspect_state(state,inspection):
    _require_legacy(state)
    return [{'event':b['event'],'target':b['target'],**bindings.compare(inspection,b['witness'])} for b in state['bindings']]

def rebind(state,event,target,inspection):
    _require_legacy(state)
    out=copy.deepcopy(state);found=[b for b in out['bindings'] if b['event']==event and b['target']==target]
    if len(found)!=1:raise WorkflowError('CONFLICT','Select exactly one existing event/channel binding')
    row=found[0];witness=bindings.rebind(inspection,row['witness'],accept_current=True)
    row.update(witness=witness,expected_key=witness['expected_key']);return out

def edit_manifest(state,context,event,to_frame,inspection):
    _require_legacy(state)
    if event not in state['events']:raise WorkflowError('NOT_FOUND','Capture this named event first')
    rows=inspect_state(state,inspection)
    if any(r['status']!='unchanged' for r in rows):
        raise WorkflowError('CONFLICT','Bound native keys or observed identity drifted; inspect and explicitly adopt first',{'bindings':rows})
    out=manifest(state,context);out['operation']='edit'
    out['event_shift']={'schema':'1.0','events':list(copy.deepcopy(state['events']).values()),'dependencies':[],
                        'event_id':event,'target_frame':to_frame,
                        'bindings':[{k:copy.deepcopy(b[k]) for k in ('event','target','offset','expected_key')} for b in state['bindings']]}
    return out

def refresh_after_commit(state,plan,inspection):
    _require_legacy(state)
    out=copy.deepcopy(state);out['events']={e['id']:copy.deepcopy(e) for e in plan['events']}
    for b in out['bindings']:
        frame=out['events'][b['event']]['frame']+b['offset'];witness=bindings.capture(inspection,b['target'],frame)
        b.update(expected_key=witness['expected_key'],witness=witness)
    return out


def clip_digest(state):
    _require_clip(state)
    try:
        text=json.dumps(state,sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(',',':'))
    except (TypeError,ValueError,RecursionError) as exc:
        raise WorkflowError('INVALID_REQUEST','Clip event state is not JSON-safe') from exc
    return hashlib.sha256(text.encode('utf8')).hexdigest()


def add_clip_target(state,target,action_identity,domain):
    """Add one explicit target channel to a v2 clip state."""
    _require_clip(state)
    ident=target_id(target)
    if ident!=target['id']:
        raise WorkflowError('CONFLICT','Selected target identity changed')
    if domain not in {'body','root','light','material','object'}:
        raise WorkflowError('INVALID_REQUEST','Unknown clip target domain')
    prior=state['targets'].get(ident)
    row={'id':ident,'owner':copy.deepcopy(target['owner']),
         'data_path':target['data_path'],'array_index':target['array_index'],
         'sharing':'reject','domain':domain,
         'action_name':action_identity['action_name'],
         'slot_identifier':action_identity['slot_identifier'],
         'slot_type':action_identity['slot_type']}
    if prior is not None and prior!=row:
        raise WorkflowError('CONFLICT','Clip target identity or Action changed',{'target':ident})
    state['targets'][ident]=row
    if len(state['targets'])>256:
        raise WorkflowError('UNSUPPORTED','Clip event channel capacity exceeded')
    return ident


def add_clip_binding(state,event_id,ident,frame,key):
    """Append one explicit key offset; duplicate channel/offset is rejected."""
    _require_clip(state)
    if event_id not in state['events']:
        raise WorkflowError('NOT_FOUND','Declare the clip event before adding bindings')
    if ident not in state['targets']:
        raise WorkflowError('NOT_FOUND','Declare the clip target before adding bindings')
    if isinstance(frame,bool) or not isinstance(frame,(int,float)) or not math.isfinite(frame):
        raise WorkflowError('INVALID_REQUEST','Finite binding frame required')
    event=state['events'][event_id]
    offset=float(frame)-float(event['frame'])
    if offset < -1e-6 or offset > float(event['end'])-float(event['frame'])+1e-6:
        raise WorkflowError('INVALID_REQUEST','Binding frame is outside the event window')
    for binding in state['bindings']:
        if binding['event']==event_id and binding['target']==ident and abs(binding['offset']-offset)<=1e-6:
            raise WorkflowError('CONFLICT','Clip channel already owns this key offset')
    witness=semantic_key(key)
    if abs(witness['co'][0]-frame)>1e-6:
        raise WorkflowError('CONFLICT','Native key time differs from the requested binding')
    state['bindings'].append({'event':event_id,'target':ident,
                              'offset':offset,'expected_key':witness})
    if len(state['bindings'])>4096:
        raise WorkflowError('UNSUPPORTED','Clip event binding capacity exceeded')
    return state


def check_clip_state(state):
    """Validate internal v2 structure without reading Blender data."""
    _require_clip(state)
    if not isinstance(state['id'],str) or len(state['id'])!=32 or any(c not in '0123456789abcdef' for c in state['id']):
        raise WorkflowError('INVALID_REQUEST','Clip event record ID must be 32 lowercase hex characters')
    if type(state['revision']) is not int or not 1<=state['revision']<=1000000:
        raise WorkflowError('INVALID_REQUEST','Clip event revision out of range')
    if not isinstance(state['context'],dict) or set(state['context'])!={'scene','view_layer'}:
        raise WorkflowError('INVALID_REQUEST','Clip event context must name scene and view layer')
    participants=state['participants']
    legacy={'rigs','roots','lights','materials'}
    if not isinstance(participants,dict):
        raise WorkflowError('INVALID_REQUEST','Clip event participants are incomplete')
    fields=set(participants)
    if fields==legacy:
        pass
    elif fields==legacy|{'objects'}:
        names=participants['objects']
        if (not isinstance(names,list) or not 1<=len(names)<=8
                or any(not isinstance(name,str) or not name
                       or len(name)>256 or '\x00' in name for name in names)):
            raise WorkflowError('INVALID_REQUEST',
                                'Clip event object participant names are malformed')
        if len(names)!=len(set(names)):
            raise WorkflowError('INVALID_REQUEST',
                                'Clip event object participant names are malformed')
        for field in legacy:
            rows=participants[field]
            if not isinstance(rows,list) or rows:
                raise WorkflowError(
                    'INVALID_REQUEST',
                    'Clip event objects mode requires empty legacy participants')
    else:
        raise WorkflowError('INVALID_REQUEST','Clip event participants are incomplete')
    if not isinstance(state['events'],dict) or len(state['events'])!=1:
        raise WorkflowError('INVALID_REQUEST','Clip event record requires exactly one event')
    event=next(iter(state['events'].values()))
    needed={'id','frame','contact','end','locked'}
    if not isinstance(event,dict) or not needed<=set(event):
        raise WorkflowError('INVALID_REQUEST','Clip event metadata is incomplete')
    if not float(event['frame'])<float(event['contact'])<float(event['end']):
        raise WorkflowError('INVALID_REQUEST','Clip event window order is invalid')
    if type(event['locked']) is not bool:
        raise WorkflowError('INVALID_REQUEST','Clip event lock flag must be boolean')
    seen={}
    for binding in state['bindings']:
        if not isinstance(binding,dict) or set(binding)!={'event','target','offset','expected_key'}:
            raise WorkflowError('INVALID_REQUEST','Clip binding fields changed')
        if binding['event'] not in state['events'] or binding['target'] not in state['targets']:
            raise WorkflowError('INVALID_REQUEST','Clip binding references an unknown event/target')
        if type(binding['offset']) not in (int,float) or not math.isfinite(binding['offset']):
            raise WorkflowError('INVALID_REQUEST','Clip binding offset must be finite')
        address=(binding['target'],round(float(binding['offset']),9))
        if address in seen:
            raise WorkflowError('INVALID_REQUEST','Duplicate clip binding offset')
        seen[address]=binding
        witness=semantic_key(binding['expected_key'])
        if abs(float(witness['co'][0])-(float(event['frame'])+float(binding['offset'])))>1e-6:
            raise WorkflowError('INVALID_REQUEST','Clip binding offset does not match expected key time')
    for ident,target in state['targets'].items():
        spec={k:target[k] for k in ('owner','data_path','array_index')}
        check={**spec,'id':ident}
        if target_id(check)!=ident:
            raise WorkflowError('INVALID_REQUEST','Clip target ID does not match its native address')
        frames=sorted(float(b['offset']) for b in state['bindings'] if b['target']==ident)
        if not frames:
            raise WorkflowError('INVALID_REQUEST','Every clip target requires at least one binding')
        contact=float(event['contact'])-float(event['frame'])
        if not any(abs(frame-contact)<=1e-6 for frame in frames):
            raise WorkflowError('INVALID_REQUEST','Every clip target requires a contact key')
    return state


def compare_clip_state(state,current_by_target):
    """Compare every recorded binding with current semantic native keys.

    current_by_target maps target id to a list of semantic keys.  The complete
    key set inside the window must match, so added/moved/deleted target keys
    cannot be silently ignored.
    """
    _require_clip(state);check_clip_state(state)
    rows=[]
    for ident,target in state['targets'].items():
        event=next(iter(state['events'].values()))
        expected=[b for b in state['bindings'] if b['target']==ident]
        expected_by_offset={round(float(b['offset']),6):b for b in expected}
        current=current_by_target.get(ident,[])
        current_by_offset={round(float(k['co'][0])-float(event['frame']),6):k for k in current}
        missing=sorted(set(expected_by_offset)-set(current_by_offset))
        extra=sorted(set(current_by_offset)-set(expected_by_offset))
        drift=[]
        for offset,binding in expected_by_offset.items():
            key=current_by_offset.get(offset)
            if key is not None and key!=binding['expected_key']:
                drift.append({'offset':offset,'expected':binding['expected_key'],'current':key})
        if missing or extra or drift:
            rows.append({'target':ident,'owner':target['owner'],'data_path':target['data_path'],
                         'array_index':target['array_index'],'status':'drift',
                         'missing_offsets':missing,'extra_offsets':extra,'drift':drift})
        else:
            rows.append({'target':ident,'owner':target['owner'],'data_path':target['data_path'],
                         'array_index':target['array_index'],'status':'unchanged'})
    return rows


def refresh_clip_state(state,current_by_target,action_identity_by_target,event,
                       offset_map_by_target):
    """Return a new v2 state after a committed local retime."""
    _require_clip(state);check_clip_state(state)
    out=copy.deepcopy(state)
    old=next(iter(out['events'].values()))
    new_event=copy.deepcopy(event)
    new_event.setdefault('previous_contact',old['contact'])
    out['events'][new_event['id']]=new_event
    for ident,target in out['targets'].items():
        current=current_by_target.get(ident)
        if current is None:
            raise WorkflowError('CONFLICT','Fresh native target is missing after commit',{'target':ident})
        identity=action_identity_by_target.get(ident)
        if identity is None:
            raise WorkflowError('CONFLICT','Fresh Action identity is missing after commit',{'target':ident})
        target.update(copy.deepcopy(identity))
        expected=[b for b in out['bindings'] if b['target']==ident]
        by_offset={round(float(k['co'][0])-float(new_event['frame']),6):k for k in current}
        moved=offset_map_by_target.get(ident)
        if moved is None:
            raise WorkflowError('CONFLICT','Fresh retime offset map is missing',{'target':ident})
        for binding in expected:
            old_offset=round(float(binding['offset']),6)
            if old_offset not in moved:
                raise WorkflowError('CONFLICT','Fresh retime offset map lacks a recorded key',
                                    {'target':ident,'offset':old_offset})
            wanted=round(float(moved[old_offset]),6)
            if wanted not in by_offset:
                raise WorkflowError('CONFLICT','Fresh key set lacks a requested retimed offset',
                                    {'target':ident,'offset':wanted})
            binding['offset']=wanted
            binding['expected_key']=copy.deepcopy(by_offset[wanted])
    return out
