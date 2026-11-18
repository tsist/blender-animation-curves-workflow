"""Three editable FK keys. LINEAR is bounded, not Bezier/IK/contact locking."""
import math,struct
from .leg_contract import finite,flexion_channel
from . import templates_spec

def build(params,frames,knee_start=10.,knee_peak=45.):
 if len(frames)!=3:raise ValueError('Exactly three times required')
 requested=[finite(x)for x in frames]
 if any(abs(x)>1000000 for x in requested):raise ValueError('Frame outside supported ±1000000 domain')
 times=[struct.unpack('f',struct.pack('f',x))[0]for x in requested]
 if not times[0]<times[1]<times[2] or times[-1]-times[0]>10000:raise ValueError('Three increasing finite times; span <=10000')
 spec=templates_spec.build('humanoid_fk_two_segment_v2',params)
 curves=[]
 for bone,index,values in [('head',2,(0.,25.,0.)),('upper_arm.L',0,(0.,-45.,0.)),('upper_arm.R',0,(0.,-45.,0.))]:
  curves.append({'bone':bone,'index':index,'values':[math.radians(v)for v in values]})
 for side in ('L','R'):
  bone='shin.'+side;rest=spec['joint_limits'][bone]['rest_flexion_deg'];values=[flexion_channel(v,rest)['local_x_deg']for v in (knee_start,knee_peak,knee_start)]
  curves.append({'bone':bone,'index':0,'values':[math.radians(v)for v in values]})
 return {'requested_times':requested,'times':times,'curves':curves,'interpolation':'LINEAR','knee_physical_degrees':[finite(knee_start),finite(knee_peak),finite(knee_start)],'semantics':'FK practice only; no IK, foot, ground contact, locomotion or collision qualification'}
