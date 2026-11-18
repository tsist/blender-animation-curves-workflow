"""Deterministic native-FK blocking templates. No bpy and no scene mutation."""
import math
import copy

VERSION = '0.1.0-rebuild'
KINDS = ('cylinder_v1', 'humanoid_fk_v1')
BODY = (0.48, 0.48, 0.48, 1.0)
CHEST = (1.0, 0.22, 0.025, 1.0)
FACE = (0.015, 0.75, 1.0, 1.0)


def parameters(values=None):
    if values is None:
        values = {}
    if type(values) is not dict or set(values) - {'height_m'}:
        raise ValueError('Only height_m is supported by v1')
    h = values.get('height_m', 1.8)
    if type(h) not in (int, float) or not math.isfinite(h) or not .3 <= h <= 3.0:
        raise ValueError('height_m must be finite, 0.3 to 3.0 metres')
    return {'height_m': float(h)}


def box(center, size):
    x,y,z = center; a,b,c = (v/2 for v in size)
    vertices = [(x+dx*a,y+dy*b,z+dz*c) for dx,dy,dz in
                [(-1,-1,-1),(1,-1,-1),(1,1,-1),(-1,1,-1),
                 (-1,-1,1),(1,-1,1),(1,1,1),(-1,1,1)]]
    return vertices, [(0,3,2,1),(4,5,6,7),(0,1,5,4),(1,2,6,5),(2,3,7,6),(3,0,4,7)]


def cylinder(start, end, radius, sides=12):
    axis = tuple(b-a for a,b in zip(start,end)); length = math.sqrt(sum(v*v for v in axis))
    if length <= 0 or radius <= 0 or sides < 3:
        raise ValueError('Invalid cylinder dimensions')
    w = tuple(v/length for v in axis)
    ref = (0,0,1) if abs(w[2]) < .9 else (0,1,0)
    def cross(a,b): return (a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0])
    u = cross(ref,w); mag=math.sqrt(sum(v*v for v in u));u=tuple(v/mag for v in u)
    v = cross(w,u)
    verts=[tuple(p[j]+radius*(math.cos(i*2*math.pi/sides)*u[j]+math.sin(i*2*math.pi/sides)*v[j]) for j in range(3)) for p in (start,end) for i in range(sides)]
    faces=[tuple(reversed(range(sides))),tuple(range(sides,2*sides))]
    faces += [(i,(i+1)%sides,(i+1)%sides+sides,i+sides) for i in range(sides)]
    return verts,faces


def build(kind, values=None):
    if kind not in KINDS: raise ValueError('Unknown template kind')
    p=parameters(values);h=p['height_m'];bones=[];parts=[]
    def point(x): return tuple(v*h for v in x)
    def bone(name,head,tail,parent=None):
        bones.append(dict(name=name,head=point(head),tail=point(tail),parent=parent,roll=0.,connected=False,deform=name!='root'))
    def part(name,bone_name,geometry,color=BODY,marker=False):
        verts,faces=geometry
        parts.append(dict(name=name,bone=bone_name,vertices=verts,faces=faces,color=color,marker=marker))
    def block(name,bone_name,center,size,color=BODY,marker=False):
        part(name,bone_name,box(point(center),point(size)),color,marker)
    bone('root',(0,0,0),(0,0,.10))
    if kind=='cylinder_v1':
        bone('chest',(0,0,.40),(0,0,.80),'root');bone('head',(0,0,.82),(0,0,1),'chest')
        part('torso','chest',cylinder((0,0,0),(0,0,.8*h),.14*h))
        part('head','head',cylinder((0,0,.82*h),(0,0,h),.10*h))
        chest_width,chest_height,chest_depth,chest_z=.28*h,.8*h,.28*h,.4*h
        head_width,head_height,head_depth,head_z=.20*h,.18*h,.20*h,.91*h
        references={'base':{'bone':'chest','rest_point':(0,0,0)},'head':{'bone':'head','rest_point':(0,0,h)}}
    else:
        bone('pelvis',(0,0,.53),(0,0,.60),'root');bone('spine',(0,0,.60),(0,0,.69),'pelvis')
        bone('chest',(0,0,.69),(0,0,.82),'spine');bone('neck',(0,0,.82),(0,0,.88),'chest');bone('head',(0,0,.88),(0,0,1),'neck')
        block('pelvis','pelvis',(0,0,.565),(.18,.13,.10));block('abdomen','spine',(0,0,.635),(.15,.12,.09))
        block('torso','chest',(0,0,.75),(.26,.14,.16));block('neck','neck',(0,0,.85),(.07,.08,.06));block('head','head',(0,0,.935),(.14,.12,.13))
        references={'head':{'bone':'head','rest_point':(0,0,h)}}
        for suffix,sign in (('L',1),('R',-1)):
            def q(x,y,z):return (sign*x,y,z)
            chain=[('clavicle',(0,0,.79),q(.13,0,.80),'chest',.025),('upper_arm',q(.13,0,.80),q(.25,-.015,.64),'clavicle.'+suffix,.035),('forearm',q(.25,-.015,.64),q(.33,0,.50),'upper_arm.'+suffix,.028),('hand',q(.33,0,.50),q(.35,0,.45),'forearm.'+suffix,.025),('thigh',q(.065,0,.53),q(.065,-.015,.29),'pelvis',.045),('shin',q(.065,-.015,.29),q(.065,0,.07),'thigh.'+suffix,.035),('foot',q(.065,0,.07),q(.065,-.13,.04),'shin.'+suffix,0)]
            for label,start,end,parent,radius in chain:
                name=label+'.'+suffix;bone(name,start,end,parent)
                if label=='foot':
                    block(name,name,q(.065,-.055,.04),(.07,.18,.08))
                    for label2,y in (('sole',-.055),('heel',.035),('toe',-.145)):
                        references[label2+'.'+suffix]={'bone':name,'rest_point':point(q(.065,y,0))}
                else:part(name,name,cylinder(point(start),point(end),radius*h))
        chest_width,chest_height,chest_depth,chest_z=.26*h,.16*h,.14*h,.75*h
        head_width,head_height,head_depth,head_z=.14*h,.13*h,.12*h,.935*h
    thickness=max(.003,.002*h);gap=max(.001,.0008*h)
    side=.70*min(chest_width,chest_height)
    if kind=='cylinder_v1':side=min(side,.16*h)
    for name,bname,w,zsize,depth,z,color in [('chest_marker','chest',side,side,chest_depth,chest_z,CHEST),('face_marker','head',.35*head_width,.75*head_height,head_depth,head_z,FACE)]:
        part(name,bname,box((0,-depth/2-gap-thickness/2,z),(w,thickness,zsize)),color,True)
    human=kind=='humanoid_fk_v1'
    capabilities=dict(world_root=True,torso=True,head=True,arms=human,legs=human,foot_reference=human,base_reference=not human,ik=False)
    return dict(schema_version=1,template_id=kind,generator_version=VERSION,params=p,height_m=h,unit='m',front_axis='-Y',up_axis='+Z',bones=bones,bone_map={b['name']:b['name'] for b in bones},parts=parts,references=references,capabilities=capabilities,marker_gap=gap,marker_thickness=thickness)


def in_scene_units(spec, meters_per_blender_unit):
    """Scale only new template geometry. Physical metadata stays in metres."""
    scale=meters_per_blender_unit
    if type(scale) not in (int,float) or not math.isfinite(scale) or not .001 <= scale <= 1000.:
        raise ValueError('Scene scale_length must be finite, 0.001 to 1000 metres per Blender unit')
    result=copy.deepcopy(spec)
    def convert(point):return tuple(x/scale for x in point)
    for bone in result['bones']:
        bone['head']=convert(bone['head']);bone['tail']=convert(bone['tail'])
    for part in result['parts']:part['vertices']=[convert(p) for p in part['vertices']]
    for ref in result['references'].values():ref['rest_point']=convert(ref['rest_point'])
    for field in ('marker_gap','marker_thickness'):result[field]/=scale
    result['height_blender_units']=result['height_m']/scale
    result['meters_per_blender_unit']=float(scale)
    result['geometry_unit']='BLENDER_UNIT'
    return result
