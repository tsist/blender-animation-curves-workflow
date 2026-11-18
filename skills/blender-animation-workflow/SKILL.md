---
name: blender-animation-workflow
description: 使用 Animation Curves Workflow 创建简形角色、编辑原生动画通道与事件、接续人工曲线、复用双人和分件选段及四人互动，并录制真实 1080p 实体视口预览。
---

# Animation Curves Workflow

适用于本仓库的 Animation Curves Workflow 0.4.3。已验证的平台基线为 Windows、Blender 5.2.1 LTS。开始前核对实际 Blender 和已启用插件版本，先保存独立工作副本。

本技能随插件源码发布。动作素材由用户自己的 `.blend` 库和匹配的 `.clip.json` 提供。录屏使用可用的 FFmpeg。已有 `blenderctl` 且启用相应动画适配器时，可将其作为可选批处理入口。

## 安装与开始

1. 下载或克隆仓库，将根目录下的 `animation_curves_workflow_addon` 文件夹单独压缩为 ZIP。ZIP 内第一层应为同名文件夹，其内包含 `__init__.py`。保留文件夹的全部源码文件。
2. 在 Blender 的 Preferences → Add-ons 选择 **Install from Disk**，安装 ZIP，启用 **Animation Curves Workflow**。
3. 在三维视口按 `N`，打开「动画曲线」侧栏。Object 模式可创建简形角色、编辑对象通道和管理素材。Pose 模式可使用受支持的自有角色姿态工具。
4. 修改前保存检查点。需要保留未打键的手工姿态时另存副本，再执行复制 Action、改时或 Undo。
5. 先检查参与者、活动 Action、帧范围和锁定，预览差异后提交。选择或数据改变后重新预览。

## 按任务读取

- 创建角色、调整对象或姿态、捕获事件、接续人工修改，读取 [角色与事件](references/characters-events.md)。
- 保存自有素材、复用双人或分件选段、一次应用四人互动，读取 [动作素材](references/clip-reuse.md)。
- 输出白模、异色实体或当前视口的真实播放视频，读取 [原生视口预览](references/viewport-preview.md)。

## 工作约定

修改前明确目标对象、骨骼、通道、帧、改动量和应保留的内容。使用实际场景名称，先检查当前值和原生关键帧。局部事件改时依赖已存在的原生接触键。动作、接触位置和支撑需要在画面中核对。

提交后检查真实键值、Action 绑定、锁定和报告，再保存并独立重开。GUI 操作执行成功后仍需保存工程。普通曲线细调用 Blender 原生时间轴、Dope Sheet 和 Graph Editor。

出现 `CONFLICT` 或 `UNSUPPORTED` 时读取报告，核对源文件、Action、参与者及支持范围。过期事件经确认后采用当前曲线，随后重新预览。保留已有记录和角色身份，使用明确的维护入口恢复状态。

预览交付包含可编辑 `.blend`、外部依赖清单及实际视频。录屏报告用于核对帧数和资源回收，动作可读、滑步、接触和音画关系由实际播放检查。
