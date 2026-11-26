# Animation Curves Workflow 0.5.0

在 Blender 中创建简形角色，编辑原生 Action 和姿态曲线，管理片段事件并复用动作素材。0.5.0 在 Windows 的 Blender 5.3.0（`02f0899d78e3`）验收。Blender 5.2.1 LTS 可继续使用 0.4.4。[配套技能](skills/blender-animation-workflow/SKILL.md) · [0.4.4](https://github.com/tsist/blender-animation-curves-workflow/releases/tag/v0.4.4)

## 安装

1. 下载发布页的 `animation_curves_workflow_0.5.0_windows.zip`。[GitHub 发布页](https://github.com/tsist/blender-animation-curves-workflow/releases/tag/v0.5.0)
2. 在 Blender 的 **Preferences → Add-ons → Install from Disk** 选择 ZIP 并启用 **Animation Curves Workflow**。
3. 在三维视口按 `N` 打开「动画曲线」。可以创建简形模板、登记动作或载入与素材库对应的 `.clip.json`。

## 5.3 表演修正

在 Animation Curves 侧栏的 **Production Operations** 中运行名为 `Animation Production Request` 的 Text 数据块。局部表演默认用 `correction` 新建 NLA 修正层，点名可续改原层，`layer_state` 调整权重与静音。用 `slot` 显式编辑共享 Action 的指定 Slot，受管 NLA 基础轨另传 `base_track`。普通姿态骨的原生变换通道也可指定。条带内的键和采样使用 Action 本地帧。

`transfer` 按指定帧段匹配目标的世界变换，`rotate` 转换活动 Action 的旋转表示。后台工作副本用 blenderctl v0.64.0 提交 `production.run`，设置 `params.domain` 为 `animation` 和 `params.release` 为 `1.0.0`。[blenderctl v0.64.0](https://github.com/tsist/blenderctl/releases/tag/v0.64.0)

## 既有 5.3 验收

- 隔离配置安装和独立冷启动。用公开操作创建 18 骨双段腿 FK 模板及三关键帧练习 Action，编辑膝关节的一条 Euler 曲线并保留原 Action。
- 保存并独立重开后，校验五条原生曲线及 Action 槽位。对四个时刻执行只读姿态检查，确认源文件和状态哈希一致。
- 通过注册操作追加四人动作素材，在独立重开后检查四项事件及接触诊断。
- 空合成栈下允许空间编辑的结构预检。存在合成效果时保留拒绝边界。5.3 的集合排序字段参与只读见证，仍要求同一集合内的链接状态一致。

空间编辑全链、复杂专项约束和视口录屏由对应工作流的运行包验证。升级工程时保留旧工程和素材库，在另存副本上检查原生 Action 与媒体路径。
