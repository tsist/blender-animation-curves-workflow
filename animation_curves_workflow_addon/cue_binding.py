"""Preview-only bridge to the frozen 0.3.5 transaction. No bpy or mutation."""
import hashlib
from fractions import Fraction
from .cue_plan import plan_cues, rational

def decide(*, bpm, fps, start_frame, beat, cue_id, policy='exact', chosen_frame=None, tolerance_frames=None):
    if type(cue_id) is not str or not cue_id.strip() or len(cue_id)>128:
        raise ValueError('explicit cue ID required')
    row=plan_cues(bpm=bpm,fps=fps,start_frame=start_frame,beats=[beat])['cues'][0]
    exact=Fraction(row['exact_frame'])
    if policy=='exact':
        if chosen_frame is not None or tolerance_frames is not None:
            raise ValueError('exact policy forbids rounding arguments')
        if not row['exact_native']:raise ValueError('nonexact target requires explicit integer decision')
        target=exact
    elif policy=='explicit_integer':
        if type(chosen_frame) is not int:raise ValueError('chosen_frame must be an explicit integer')
        limit=rational(tolerance_frames,'tolerance_frames')
        if not 0<=limit<=1:raise ValueError('tolerance outside [0,1]')
        target=Fraction(chosen_frame)
        if abs(target)>1_000_000 or abs(target-exact)>limit:raise ValueError('chosen target exceeds bound or tolerance')
    else:raise ValueError('unknown decision policy')
    return dict(schema='explicit-cue-decision-v1',cue_id=cue_id,bpm=str(rational(bpm,'bpm')),fps=str(rational(fps,'fps')),
                start_frame=str(rational(start_frame,'start')),beat=str(rational(beat,'beat')),policy=policy,
                chosen_frame=chosen_frame,tolerance_frames=None if tolerance_frames is None else str(rational(tolerance_frames,'tolerance')),
                exact_frame=str(exact),target_frame=float(target),error_frames=str(target-exact),
                error_seconds=str((target-exact)/rational(fps,'fps')),mutation_allowed=False)

def validate_decision(d):
    expected=decide(**{k:d[k] for k in ('bpm','fps','start_frame','beat','cue_id','policy','chosen_frame','tolerance_frames')})
    import json
    if json.dumps(expected,sort_keys=True,allow_nan=False)!=json.dumps(d,sort_keys=True,allow_nan=False):raise ValueError('decision modified or unknown fields')

def preview(store,context,decision,*,record_id,revision):
    validate_decision(decision)
    record,_=store.inspect(context)
    if type(revision)is not int or record['id']!=record_id or record['revision']!=revision:
        raise ValueError('explicit event binding expired')
    import struct
    delta=Fraction(decision['target_frame'])-Fraction(record['rows'][0]['frame'])
    if delta==0 or abs(delta)>2_000_000 or Fraction(struct.unpack('f',struct.pack('f',float(delta)))[0])!=delta:
        raise ValueError('nonzero exact native delta required')
    # Authoritative 0.3.5 planner owns Action/lock/identity/current-state checks.
    tx=store.sync_origin(context,float(delta))
    if tx['record']!=record or Fraction(tx['record']['rows'][0]['frame'])+delta!=Fraction(decision['target_frame']):
        raise ValueError('event changed during preview')
    import copy
    return dict(schema='cue-bound-cross-preview-v1',decision=copy.deepcopy(decision),record_id=record_id,revision=revision,
                raw_sha256=hashlib.sha256(tx['raw'].encode()).hexdigest(),transaction=tx,mutation_allowed=False)

def revalidate(store,context,frozen):
    current=preview(store,context,frozen['decision'],record_id=frozen['record_id'],revision=frozen['revision'])
    if current!=frozen:raise ValueError('cue preview expired')
    return current
