"""Readonly bounded static shader graph preservation, never shader editing."""
from . import spatial_static_state as state
NODES={'ShaderNodeBsdfPrincipled','ShaderNodeOutputMaterial','ShaderNodeOutputWorld','ShaderNodeOutputLight','ShaderNodeBackground','ShaderNodeEmission','ShaderNodeValue','ShaderNodeRGB'}
SOCKETS={'NodeSocketShader','NodeSocketFloat','NodeSocketFloatFactor','NodeSocketFloatUnsigned','NodeSocketFloatAngle','NodeSocketFloatPercentage','NodeSocketFloatDistance','NodeSocketFloatWavelength','NodeSocketFloatTime','NodeSocketFloatTimeAbsolute','NodeSocketVector','NodeSocketVectorTranslation','NodeSocketVectorDirection','NodeSocketVectorVelocity','NodeSocketVectorAcceleration','NodeSocketVectorEuler','NodeSocketVectorXYZ','NodeSocketColor','NodeSocketBool','NodeSocketInt','NodeSocketIntUnsigned','NodeSocketIntFactor'}

def nested_gate(owner,handled):
    for prop in owner.bl_rna.properties:
        if prop.type not in {'POINTER','COLLECTION'}:continue
        key=prop.identifier
        if key in state.RUNTIME_NESTED or key in state.COMMON_NESTED or key in handled:continue
        v=getattr(owner,key)
        if (v is not None if prop.type=='POINTER' else len(v)>0):state.reject('Unsupported static shader dependency: '+key)

def capture(tree,budget,owner):
    from . import native
    native.local(tree);state.no_animation(tree)
    if owner.node_tree!=tree:state.reject('Shader graph ownership mismatch')
    if tree.bl_idname!='ShaderNodeTree':state.reject('Only static ShaderNodeTree supported')
    if len(tree.nodes)>32 or len(tree.links)>64:state.reject('Static graph node/link budget exceeded')
    nested_gate(tree,{'nodes','links','interface'})
    if tree.interface.items_tree:state.reject('Shader group interface is unsupported')
    nested_gate(tree.interface,{'items_tree'})
    result={'owner':{'type':owner.bl_rna.identifier,'name':owner.name},'name':tree.name,'scalars':state.scalar_state(tree,budget),'custom':state.custom_state(tree),
            'interface':state.scalar_state(tree.interface,budget),'nodes':[],'links':[],
            'active_node':tree.nodes.active.name if tree.nodes.active else None}
    count=0
    for node in sorted(tree.nodes,key=lambda n:n.name):
        if node.bl_idname not in NODES:state.reject('Unsupported static shader node: '+node.bl_idname)
        if node.parent is not None:state.reject('Shader parent/frame dependency unsupported')
        nested_gate(node,{'inputs','outputs','parent','internal_links','panel_states'})
        if node.internal_links:state.reject('Shader internal links require separate preservation support')
        row={'name':node.name,'type':node.bl_idname,'scalars':state.scalar_state(node,budget),'custom':state.custom_state(node)}
        panels=getattr(node,'panel_states',())
        if len(panels)>32:state.reject('Shader panel-state budget exceeded')
        row['panel_states']=[]
        for panel in panels:
            nested_gate(panel,set())
            row['panel_states'].append({'scalars':state.scalar_state(panel,budget),'custom':state.custom_state(panel)})
        for direction in ('inputs','outputs'):
            values=[]
            for socket in getattr(node,direction):
                count+=1
                if count>256:state.reject('Static graph socket budget exceeded')
                if socket.bl_idname not in SOCKETS:state.reject('Unsupported shader socket type: '+socket.bl_idname)
                nested_gate(socket,{'node','links'})
                if socket.node!=node:state.reject('Shader socket ownership mismatch')
                values.append({'identifier':socket.identifier,'name':socket.name,'type':socket.bl_idname,'scalars':state.scalar_state(socket,budget),'custom':state.custom_state(socket)})
            row[direction]=values
        result['nodes'].append(row)
    def endpoint(node,socket,direction):
        sockets=list(getattr(node,direction));matches=[i for i,s in enumerate(sockets) if s==socket]
        if len(matches)!=1:state.reject('Ambiguous shader link endpoint')
        return {'node':node.name,'index':matches[0],'identifier':socket.identifier}
    for link in tree.links:
        nested_gate(link,{'from_node','to_node','from_socket','to_socket'})
        result['links'].append({'from':endpoint(link.from_node,link.from_socket,'outputs'),
                                'to':endpoint(link.to_node,link.to_socket,'inputs'),'scalars':state.scalar_state(link,budget)})
    return result
