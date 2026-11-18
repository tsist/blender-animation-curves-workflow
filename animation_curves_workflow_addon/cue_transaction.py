"""Experimental participant wrapper; no separate post-commit JSON write."""
import json,copy
from fractions import Fraction
from .cue_binding import preview,revalidate
from .cue_state import KEY,read as raw,require_no_shadow
def write(c,s):
 require_no_shadow(c)
 setattr(c.scene,KEY,s)
def fps_witness(c):
    f=c.scene.render.fps;b=c.scene.render.fps_base
    if type(f)is not int or f<=0 or type(b)not in(int,float)or b<=0:raise ValueError('Invalid scene FPS')
    return {'fps':f,'fps_base':str(Fraction(b))}
def encode(x):return json.dumps(x,sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(',',':'))
def bound_preview(store,c,decision,*,record_id,revision):
    old=raw(c)
    if type(old)is not str or old!='':raise ValueError('Existing cue record: no overwrite/continuation supported')
    fps=fps_witness(c)
    if Fraction(fps['fps'])/Fraction(fps['fps_base'])!=Fraction(decision['fps']):raise ValueError('Explicit cue FPS differs from scene FPS')
    return {'schema':'experimental-cue-transaction-v1','old_cue':old,'fps':fps,'bound':preview(store,c,decision,record_id=record_id,revision=revision)}
def execute(store,c,frozen,*,checkpoint=None):
    b=frozen['bound']
    now=bound_preview(store,c,b['decision'],record_id=b['record_id'],revision=b['revision'])
    if encode(now)!=encode(frozen):raise ValueError('Cue transaction preview expired')
    written=None
    def check_state():
        require_no_shadow(c)
        if fps_witness(c)!=frozen['fps']:raise ValueError('Scene FPS drift')
        if raw(c)!=(written if written is not None else frozen['old_cue']):raise ValueError('Cue external conflict')
    def check():
        check_state()
        result=checkpoint() if checkpoint is not None else True
        check_state()
        return result
    def participant(verify_only=False):
        nonlocal written
        check_state()
        if written is None:
            if verify_only:raise ValueError('Cue missing commit')
            r,_=store.inspect(c)
            if r['id']!=b['record_id'] or r['revision']!=b['revision']+1:raise ValueError('Unexpected cross commit')
            written=encode({'schema':'explicit-cue-binding-v1','event_id':r['id'],'event_revision':r['revision'],
                            'decision':copy.deepcopy(b['decision']),'fps':frozen['fps'],'source_sha256':b['raw_sha256']})
            write(c,written)
        check_state()
    try:
        result=store.execute(c,b['transaction'],checkpoint=check,participant_extension=participant)
        # All validation belongs INSIDE transaction; no fallible post-commit check.
        return result
    except BaseException as error:
        # Only our own system-RNA channel may be restored; custom shadows stay.
        if written is not None:
            try:
                if not hasattr(type(c.scene),KEY):raise RuntimeError('系统RNA已注销，无法核验或恢复')
                if getattr(c.scene,KEY)==written:
                    setattr(c.scene,KEY,frozen['old_cue'])
                    if getattr(c.scene,KEY)!=frozen['old_cue']:raise RuntimeError('恢复后读回不一致')
            except BaseException as rollback_error:
                raise RuntimeError('事务失败且Cue回滚未完成：'+str(rollback_error)+'；原错误：'+str(error)) from error
        raise
