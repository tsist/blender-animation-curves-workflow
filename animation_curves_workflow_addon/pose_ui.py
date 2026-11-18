"""Explicit single-bone dialog, no persistent scene editing settings."""
import bpy,json,math
from bpy.props import EnumProperty,FloatProperty,StringProperty
from . import pose_channel
class ACW_OT_pose_channel(bpy.types.Operator):
    bl_idname='animation_curves.edit_pose_axis';bl_label='编辑所选姿态骨关键帧';bl_options={'REGISTER','UNDO'}
    identity:StringProperty(options={'HIDDEN'})
    display_origin:StringProperty(options={'HIDDEN'})
    axis:EnumProperty(name='局部Euler轴',items=(('0','X',''),('1','Y',''),('2','Z','')))
    operation:EnumProperty(name='操作',items=(('set_key','设置关键帧','新建或改值'),('move_key','移动关键帧','只改明确键的时间'),('delete_key','删除关键帧','只删除明确键')))
    frame:FloatProperty(name='源帧（可含子帧）',default=1,min=-1000000,max=1000000)
    to_frame:FloatProperty(name='目标帧',default=2,min=-1000000,max=1000000)
    value_deg:FloatProperty(name='角度（度）',default=0,min=-180,max=180)
    interpolation:EnumProperty(name='插值',items=(('LINEAR','线性',''),('CONSTANT','保持','')))
    @classmethod
    def poll(cls,c):return bool(c.scene and c.mode=='POSE')
    def invoke(self,c,event):
        try:
            identity,w,pb,knee=pose_channel.selected(c,allow_body_colors=True)
            self.display_origin=json.dumps(pose_channel.display_origin(c),sort_keys=True)
            self.identity=json.dumps(identity,sort_keys=True);self.frame=c.scene.frame_current_final
            self.axis='0';self.value_deg=math.degrees(pb.rotation_euler.x)+(w['spec']['joint_limits'][pb.name]['rest_flexion_deg'] if knee else 0)
            if knee:self.value_deg=max(0,min(150,self.value_deg))
        except Exception as e:self.report({'ERROR'},str(e));return {'CANCELLED'}
        return c.window_manager.invoke_props_dialog(self,width=460)
    def draw(self,c):
        l=self.layout
        try:
            row=json.loads(self.identity);knee=row['bone']in('shin.L','shin.R');l.label(text=row['rig']+' / '+row['bone'])
        except Exception:knee=False
        l.prop(self,'operation');l.prop(self,'axis');l.prop(self,'frame')
        if self.operation=='set_key':
            l.prop(self,'value_deg',text='物理膝屈曲角 0–150°' if knee else '局部Euler角 -180–180°');l.prop(self,'interpolation')
        elif self.operation=='move_key':l.prop(self,'to_frame')
        l.label(text='膝仅X；不改姿态模式，不保存文件')
        l.label(text='允许主体不透明显示色；标记/材质及原色保持，不保证视觉对比')
        l.label(text='原Action增加保护标志，保存后持久；不自动保存')
        l.label(text='Undo恢复旧动作求值，未打键手动姿态可能不保留')
        l.label(text='旧FK默认AUTO通道不能直接续写；联动将拒绝回滚')
        l.label(text='可先显式使用“复制并准备当前骨轴续写”')
    def execute(self,c):
        try:
            pose_channel.execute(c,json.loads(self.identity),int(self.axis),self.operation,self.frame,self.value_deg,self.to_frame,self.interpolation,expected_display_origin=json.loads(self.display_origin))
        except Exception as e:self.report({'ERROR'},str(e));return {'CANCELLED'}
        self.report({'INFO'},'姿态单轴关键帧已编辑；未保存源工程');return {'FINISHED'}
class ACW_OT_prepare_pose_axis(bpy.types.Operator):
    bl_idname='animation_curves.prepare_pose_axis';bl_label='复制动作并准备当前通道续写';bl_options={'REGISTER','UNDO'}
    identity:StringProperty(options={'HIDDEN'})
    source_origin:StringProperty(options={'HIDDEN'})
    axis:EnumProperty(name='局部Euler轴',items=(('0','X',''),('1','Y',''),('2','Z','')))
    @classmethod
    def poll(cls,c):return bool(c.scene and c.mode=='POSE')
    def invoke(self,c,event):
        try:
            from . import pose_prepare
            identity,w,_,_=pose_channel.selected(c)
            if w['action'] is None:raise ValueError('当前没有可准备的Action')
            self.identity=json.dumps(identity,sort_keys=True);self.source_origin=json.dumps(pose_prepare.origin(w),sort_keys=True);self.axis='0'
        except Exception as e:self.report({'ERROR'},str(e));return {'CANCELLED'}
        return c.window_manager.invoke_props_dialog(self,width=530)
    def draw(self,c):
        from . import pose_prepare
        l=self.layout;l.prop(self,'axis')
        try:
            identity=json.loads(self.identity);w,fc,state,count=pose_prepare.review(c,identity,int(self.axis))
            l.label(text=identity['rig']+' / '+identity['bone']+' / '+w['action'].name)
            l.label(text='将复制Action，仅冻结此轴 '+str(count)+' 个AUTO手柄为FREE')
        except Exception as e:l.label(text=str(e),icon='ERROR')
        l.label(text='只支持LINEAR/CONSTANT；键点、当前曲线与其他通道不变')
        l.label(text='原Action仅增加保护标志，保留在当前工程内存')
        l.label(text='保存工程后才持久；本操作不自动保存或覆盖文件')
        l.label(text='后续手柄不再自动调整；以后改Bezier不等同原AUTO行为')
        l.label(text='Undo恢复绑定和原保护标志；无AUTO时不创建副本')
    def execute(self,c):
        from . import pose_prepare
        try:r=pose_prepare.execute(c,json.loads(self.identity),int(self.axis),expected_origin=json.loads(self.source_origin))
        except Exception as e:self.report({'ERROR'},str(e));return {'CANCELLED'}
        if not r['changed']:
            self.report({'INFO'},'此轴无AUTO手柄，未作修改');return {'CANCELLED'}
        self.report({'INFO'},'已准备独立副本；请另存工程以持久保留原动作');return {'FINISHED'}
class ACW_OT_retime_action(bpy.types.Operator):
    bl_idname='animation_curves.retime_pose_action';bl_label='整个姿态Action改起点和时长';bl_options={'REGISTER','UNDO'}
    identity:StringProperty(options={'HIDDEN'})
    source_origin:StringProperty(options={'HIDDEN'})
    start:FloatProperty(name='新起点',default=1)
    end:FloatProperty(name='新终点',default=37)
    @classmethod
    def poll(cls,c):return bool(c.scene and c.mode=='POSE')
    def invoke(self,c,event):
        from . import action_retime as rt,pose_prepare
        try:
            identity,w=rt.selected(c);w,listing,span=rt.review(c,identity)
            self.identity=json.dumps(identity);self.source_origin=json.dumps(rt.origin(w))
            self.start=float(span[0]);self.end=float(span[1])
        except Exception as e:self.report({'ERROR'},str(e));return {'CANCELLED'}
        return c.window_manager.invoke_props_dialog(self,width=620)
    def draw(self,c):
        from . import action_retime as rt
        l=self.layout;l.label(text='影响当前Action全部姿态曲线，不按选骨过滤',icon='ERROR')
        l.prop(self,'start');l.prop(self,'end')
        try:
            w,listing,span=rt.review(c,json.loads(self.identity))
            l.label(text=w['action'].name+' / 源范围 '+str(float(span[0]))+'–'+str(float(span[1])))
            for bone,axis,count in listing:l.label(text=bone+'.'+axis+' : '+str(count)+'键')
            rt.plan(rt.native.action_state(w['action']),self.start,self.end)
        except Exception as e:l.label(text=str(e),icon='ERROR')
        l.label(text='全部键及手柄时间正仿射变换；角度与插值保持')
        l.label(text='只接受LINEAR/CONSTANT + FREE；不支持项整单拒绝')
        l.label(text='原动作增加保护标志；保存工程后才持久，不自动保存')
        l.label(text='Undo恢复绑定和保护标志；场景范围/播放头/标记不改')
    def execute(self,c):
        from . import action_retime as rt
        try:r=rt.execute(c,json.loads(self.identity),self.start,self.end,expected_origin=json.loads(self.source_origin))
        except Exception as e:self.report({'ERROR'},str(e));return {'CANCELLED'}
        if not r['changed']:self.report({'INFO'},'时间不变，未创建副本');return {'CANCELLED'}
        self.report({'INFO'},'整个Action已改时；请另存工程以持久保留原动作');return {'FINISHED'}
class ACW_OT_capture_pose_batch(bpy.types.Operator):
    bl_idname='animation_curves.capture_pose_batch';bl_label='为所选骨骼记录当前旋转姿态';bl_options={'REGISTER','UNDO'}
    expected:StringProperty(options={'HIDDEN'})
    @classmethod
    def poll(cls,c):return bool(c.scene and c.mode=='POSE')
    def invoke(self,c,event):
        from . import pose_batch
        try:self.expected=json.dumps(pose_batch.origin(c),sort_keys=True)
        except Exception as e:self.report({'ERROR'},str(e));return {'CANCELLED'}
        return c.window_manager.invoke_props_dialog(self,width=650)
    def draw(self,c):
        l=self.layout
        try:
            o=json.loads(self.expected);l.label(text=o['identity']['rig']+' / 当前帧 '+str(o['frame']+o['subframe']))
            l.label(text=str(len(o['identity']['bones']))+'骨 / '+str(len(o['rows']))+'个LINEAR新键；包含零值轴')
            for bone in o['identity']['bones']:
                rows=[r for r in o['rows']if r['bone']==bone]
                label=bone+' : '+', '.join('XYZ'[r['axis']]+' '+format(r['display_deg'],'.3f')+'°'for r in rows)
                l.label(text=label+('（物理膝角）'if bone in('shin.L','shin.R')else'（局部输入）'))
        except Exception:l.label(text='无有效预览',icon='ERROR')
        l.label(text='只捕获局部Euler输入；不是视觉姿态或IK烘焙')
        l.label(text='可接续已有根位置动画；不编辑根、不保证支撑或步行')
        l.label(text='任一目标已有同帧键/AUTO/Bezier整批拒绝；不覆盖、不转换')
        l.label(text='一次独立Action副本；提交时未选骨姿态保持')
        l.label(text='Undo撤销本次键与绑定，恢复旧动作求值')
        l.label(text='可能丢失已动画骨未打键手动姿态；建议先保存副本')
        l.label(text='Redo恢复本次捕获；失败回滚与原生Undo语义不同')
        l.label(text='原动作增加保护标志，保存工程后才持久；不自动保存')
    def execute(self,c):
        from . import pose_batch
        try:r=pose_batch.execute(c,json.loads(self.expected))
        except Exception as e:self.report({'ERROR'},str(e));return {'CANCELLED'}
        self.report({'INFO'},str(r['channels'])+'个姿态键已一次写入；未保存工程');return {'FINISHED'}
class ACW_PT_pose_channel(bpy.types.Panel):
    bl_idname='ACW_PT_pose_channel';bl_label='自有角色 · 姿态单轴关键帧';bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='动画曲线'
    def draw(self,c):
        self.layout.operator('animation_curves.capture_pose_batch');self.layout.operator('animation_curves.edit_pose_axis');self.layout.operator('animation_curves.prepare_pose_axis');self.layout.operator('animation_curves.retime_pose_action');self.layout.label(text='POSE：只选一个自有骨架和活动骨')
_CLASSES=(ACW_OT_capture_pose_batch,ACW_OT_pose_channel,ACW_OT_prepare_pose_axis,ACW_OT_retime_action,ACW_PT_pose_channel);_REGISTERED=[]
def register():
    if _REGISTERED:raise RuntimeError('Pose UI already registered')
    try:
        for cls in _CLASSES:bpy.utils.register_class(cls);_REGISTERED.append(cls)
    except Exception:unregister();raise

def unregister():
    while _REGISTERED:bpy.utils.unregister_class(_REGISTERED[-1]);_REGISTERED.pop()
