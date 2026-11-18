"""Explicit adoption of inspected saved keys. Consumes trusted native inspection output, not arbitrary validated JSON. Never applies or guesses edits."""
import copy
import math
from .contract import WorkflowError

IDENTITY=('id','owner','data_path','array_index','action_name','slot_identifier','slot_type')

def _capture(inspection, target, frame):
    if not isinstance(frame,(int,float)) or isinstance(frame,bool) or not math.isfinite(frame):
        raise WorkflowError('INVALID_REQUEST','Finite explicit key frame required')
    rows=[row for row in inspection['targets'] if row['id']==target]
    if len(rows)!=1:
        raise WorkflowError('CONFLICT','Target missing or ambiguous; select explicitly')
    row=rows[0]
    if not all(k in row for k in IDENTITY) or not row['action_name'] or not row['slot_identifier']:
        raise WorkflowError('CONFLICT','Native Action/slot identity required')
    keys=[k for k in (row.get('channel') or {}).get('keys',[]) if abs(k['co'][0]-frame)<=1e-6]
    if len(keys)!=1:
        raise WorkflowError('CONFLICT','Saved key missing or ambiguous; select explicitly')
    return {'identity':{k:copy.deepcopy(row[k]) for k in IDENTITY},'expected_key':copy.deepcopy(keys[0])}

def _compare(inspection, witness):
    current=capture(inspection,witness['identity']['id'],witness['expected_key']['co'][0])
    return {'status':'unchanged' if current==witness else 'drift',
            'expected':copy.deepcopy(witness),'current':current}

def rebind(inspection, witness, *, accept_current=False):
    result=compare(inspection,witness)
    if result['status']=='drift' and accept_current is not True:
        raise WorkflowError('CONFLICT','Saved binding drift requires explicit acceptance of current key',result)
    return result['current']


def capture(inspection, target, frame):
    try:
        return _capture(inspection,target,frame)
    except (KeyError,TypeError,ValueError,OverflowError,IndexError,AttributeError) as exc:
        raise WorkflowError('INVALID_REQUEST','Malformed native inspection or key selector') from exc


def compare(inspection, witness):
    try:
        return _compare(inspection,witness)
    except (KeyError,TypeError,ValueError,OverflowError,IndexError,AttributeError) as exc:
        raise WorkflowError('INVALID_REQUEST','Malformed binding witness') from exc
