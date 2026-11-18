"""Read-only, bounded display witness for the single-axis BODY-color adapter.
No visual contrast guarantee. Data is canonical JSON; never restores external edits.
"""
import json, math
from . import pose_gate

def identity(x):
    return None if x is None else [x.bl_rna.identifier, x.as_pointer(), getattr(x,'name','')]

def value(x):
    if x is None or isinstance(x,(str,bool,int)): return x
    if isinstance(x,float):
        if not math.isfinite(x): raise ValueError('Nonfinite display state')
        return x
    if isinstance(x,(set,frozenset)):return sorted(value(v) for v in x)
    return [value(v) for v in x]

_NODE_TYPES={'ShaderNodeBsdfPrincipled','ShaderNodeOutputMaterial','ShaderNodeRGB',
    'ShaderNodeValue','ShaderNodeMixRGB','ShaderNodeMath','ShaderNodeVectorMath',
    'ShaderNodeEmission','ShaderNodeBsdfDiffuse','NodeReroute'}
def custom(item):
    import bpy
    def plain(v,depth=0):
        if depth>6:raise ValueError('Display custom property nesting unsupported')
        if isinstance(v,bpy.types.ID):raise ValueError('Display custom resource references unsupported')
        if hasattr(v,'keys'):return {str(k):plain(v[k],depth+1)for k in sorted(v.keys())}
        if v is None or isinstance(v,(str,bool,int,float)):return value(v)
        return [plain(x,depth+1)for x in v]
    try:keys=list(item.keys())
    except (TypeError,AttributeError):return {}
    return {k:plain(item[k])for k in sorted(keys)}

# RNA state rather than a list of known shader node types. Fail closed on budget
# or unsupported fields, not silently omit a new nested shader configuration.
def rna(x,depth=0,seen=None):
    import bpy
    if x is None:return None
    if depth>10:raise ValueError('Display RNA nesting unsupported')
    seen=set() if seen is None else seen
    ptr=x.as_pointer()
    if ptr in seen:return {'reference':identity(x)}
    seen=seen|{ptr};out={'identity':identity(x),'custom':custom(x)}
    for p in x.bl_rna.properties:
        k=p.identifier
        if k in ('rna_type','id_data'):continue
        # UI screen coordinates are not rendering inputs; including them is
        # nevertheless safe and preserves existing authored node layouts.
        v=getattr(x,k)
        if p.type in ('BOOLEAN','INT','FLOAT','STRING','ENUM'):
            out[k]=value(v)
        elif p.type=='POINTER':
            if v is None or isinstance(v,(bpy.types.ID,bpy.types.Node,bpy.types.NodeSocket)):
                out[k]=identity(v)
            else:out[k]=rna(v,depth+1,seen)
        elif p.type=='COLLECTION':
            if len(v)>4096:raise ValueError('Display RNA collection too large')
            out[k]=[rna(q,depth+1,seen) for q in v]
        else:raise ValueError('Unknown display RNA property '+k)
    return out

def tree_state(tree,seen=None):
    if tree is None:return None
    seen=set() if seen is None else seen
    if tree.as_pointer() in seen:raise ValueError('Cyclic material node group unsupported')
    if len(seen)>12 or len(tree.nodes)>256:raise ValueError('Display node graph too large')
    seen=seen|{tree.as_pointer()}
    groups=[];resources=[]
    pose_gate.local(tree);pose_gate.action_review(tree)
    for node in tree.nodes:
        if node.bl_idname not in _NODE_TYPES:raise ValueError('Display shader node type unsupported: '+node.bl_idname)
        group=getattr(node,'node_tree',None)
        if group is not None:groups.append(tree_state(group,seen))
        for p in node.bl_rna.properties:
            if p.type=='POINTER' and p.identifier not in ('rna_type','id_data','node_tree'):
                ob=getattr(node,p.identifier)
                if ob is not None and hasattr(ob,'filepath'):
                    resources.append({'id':identity(ob),'filepath':ob.filepath,'source':getattr(ob,'source',None), 'size':list(getattr(ob,'size',[]))})
    return {'identity':identity(tree),'custom':custom(tree),'nodes':[rna(n) for n in tree.nodes],
            'links':[[identity(l.from_node),identity(l.from_socket),identity(l.to_node),identity(l.to_socket),l.is_muted] for l in tree.links],
            'groups':groups,'resources':resources}

def capture(context):
    import bpy
    rows=[]
    for o in sorted((o for o in bpy.data.objects if o.get('ac_template_owner')==pose_gate.OWNER),key=lambda x:x.name):
        d={'identity':identity(o),'data':identity(o.data),'color':list(o.color),
           'flags':{p.identifier:value(getattr(o,p.identifier)) for p in o.bl_rna.properties if p.type in ('BOOLEAN','INT','FLOAT','STRING','ENUM') and (p.identifier.startswith(('hide_','show_','visible_','display_')) or p.identifier in ('is_holdout','is_shadow_catcher','pass_index','lightgroup'))},
           'hidden_in_view_layer':o.hide_get(view_layer=context.view_layer),'custom':custom(o),'slots':[]}
        for slot in o.material_slots:
            mat=slot.material
            d['slots'].append({'link':slot.link,'material_identity':identity(mat),
                'diffuse':list(mat.diffuse_color) if mat else None,
                'material':rna_material(mat),'tree':tree_state(mat.node_tree) if mat else None})
        rows.append(d)
    views=[]
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type=='VIEW_3D':
                shading=area.spaces.active.shading
                views.append({'window':window.as_pointer(),'area':area.as_pointer(),'shading':{p.identifier:value(getattr(shading,p.identifier))for p in shading.bl_rna.properties if p.type in ('BOOLEAN','INT','FLOAT','STRING','ENUM') and not p.is_readonly}})
    return json.loads(json.dumps({'objects':rows,'views':views},sort_keys=True,allow_nan=False))

def rna_material(mat):
    if mat is None:return None
    # Mutable material rendering settings, not runtime update/user counters.
    pose_gate.local(mat);pose_gate.action_review(mat)
    result={'identity':identity(mat),'custom':custom(mat)}
    for p in mat.bl_rna.properties:
        if p.identifier in ('rna_type','name','name_full','tag','use_fake_user','use_extra_user') or p.is_readonly:continue
        v=getattr(mat,p.identifier)
        if p.type in ('BOOLEAN','INT','FLOAT','STRING','ENUM'):result[p.identifier]=value(v)
        elif p.type=='POINTER':result[p.identifier]=identity(v)
        elif p.type=='COLLECTION':result[p.identifier]=[identity(x) for x in v]
    return result
