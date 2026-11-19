# Animation Curves Workflow 0.4.4

在 Blender 中创建简形角色，编辑原生 Action 和姿态曲线，管理片段事件并复用动作素材。0.4.4 已在 Windows 的 Blender 5.3.0（`02f0899d78e3`）和 5.2.1 LTS（`9e2066aef7ef`）复测。[配套技能](skills/blender-animation-workflow/SKILL.md)

## 安装

1. 下载发布页的 `animation_curves_workflow_0.4.4_windows.zip)。[GitHub 发布页](https://github.com/tsist/blender-animation-curves-workflow/releases/tag/v0.4.4)
2. 在 Blender 的 **Preferences → Add-ons → Install from Disk** 选择 ZIP 并启用 **Animation Curves Workflow**。
3. 在三维视口按 `N` 打开「动画曲线」。可以创建简形模板、登记动作或载入与素材库对应的 `.clip.json`。

## 5.3 已复测的操作

- 隔离配置安装和独立冷启动。用公开操作创建 18 骨双段腿 FK 模板及三关键帧练习 Action，编辑膝关节的一条 Euler 曲线并保留原 Action。
- 保存并独立重开后，校验五条原生曲线及 Action 槽位。对四个时刻执行只读姿态检查，确认源文件和状态哈希一致。
- 通过注册操作追加四人动作素材，在独立重开后检查四项事件及接触诊断。
- 空合成栈下允许空间编辑的结构预检。存在合成效果时保留拒绝边界。5.3 的集合排序字段参与只读见证，仍要求同一集合内的链接状态一致。

空间编辑全链、复杂专项约束、视口录屏和公共 blenderctl 动画分发由各自的运行包验证。升级工程时保留旧工程和素材库，在另存副本上检查原生 Action 与媒体路径。
