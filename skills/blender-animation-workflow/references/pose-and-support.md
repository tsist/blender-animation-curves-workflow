# 角色姿态与支撑

本参考说明简形 FK 角色的创建、受管姿态编辑、双小腿端盖支撑诊断，以及双腿固定端盖独立补偿工程的做法。执行前先用 `blenderctl.ps1 discover --product animation` 查出当前可用的 `--edition`。

## 创建简形 FK 角色与检查双膝

用 `animation fk-character` 创建占位或教学用的简形角色，参数写进 UTF-8 JSON 并通过 `--params` 传入，写入类操作加上 `--output` 指向新的绝对 `.blend` 路径：

- **创建角色（`operation: "create"`）**：指定 `kind`（`cylinder_v1` 双节圆柱，或 `humanoid_fk_two_segment_v2` 十八骨无脚双段腿人形）与 `height_m`。人形正面朝 `-Y`、上方朝 `+Z`，胸前带橙色方片、头前带青色长片，在实体视口下开启对象颜色或材质显示即可看清朝向。
- **添加练习动作（`operation: "practice"`）**：提供 `rig`、`instance_uuid`、`frames`（`[start, peak, end]`）以及 `knee_start`、`knee_peak` 屈曲角度。
- **检查角色与双膝（`operation: "inspect"` / `"knees"`）**：核实骨架身份和双膝的实际几何屈曲（0–150°）。局部 Euler 的正负号受静置姿态影响，看膝盖是否反折以 `knees` 求值出的几何屈曲为准，按真实角度微调。改动过模板、限位约束、缩放或插值后，重新跑一次 `inspect` 确认资格。

## 编辑受管 FK 姿态

使用 `animation pose-edit` 对受管标准骨架执行姿态编辑，始终按「`inspect` → `plan` → `apply`」三步走，先审阅计划再落盘到 `--output` 指定的新副本。

请求 JSON 包含 `file`、`expected_sha256`、`context`（`scene`、`view_layer`、`frame`，可选 `subframe`）、`rig`（`object`、`instance_uuid`）和 `operation`：

| 编辑操作（`edit`） | 核心参数 | 典型用途 |
| --- | --- | --- |
| `set_key` | `bone`、`axis`、`frame`、`value_deg`、`interpolation`（`LINEAR` / `CONSTANT`） | 单骨单轴设键；膝盖选 `X` 轴并输入物理屈曲角 |
| `move_key` | `bone`、`axis`、`frame`、`to_frame` | 把单个关键帧移到空白目标帧 |
| `delete_key` | `bone`、`axis`、`frame` | 删除指定单轴关键帧 |
| `prepare-axis` | `bone`、`axis` | 复制动作并把旧 AUTO 手柄通道转为受管通道，方便继续编辑 |
| `retime-action` | `scope: "WHOLE_ACTION"`、`action`、`start`、`end` | 均匀调整整段受支持姿态 Action 的起点和时长 |
| `capture-batch` | `bones`、`active_bone` | 在当前播放头把所选变形骨的局部 Euler 旋转原子记为 `LINEAR` 键 |

在界面里手调并提交后，逐项核对新旧 Action 绑定、目标关键帧和未点名关键帧。需要保住未打键的手工姿态时，先另存一份检查点再执行编辑或撤销，因为撤销会回到旧 Action 的求值状态。

## 诊断脚底落地支撑

做完重心或下蹲动作后，用 `animation support` 在隔离的持久化工作进程里检查双小腿端盖的接地表现。请求 JSON 包含 `session_id`、`operation`、稳定的 `request_id` 以及操作参数，长耗时操作可配 `--wait-seconds` 等待回执，未完成时用 `status` 和 `result` 继续取回。

常用流程如下：

1. **打开会话并核对输入**：依次运行 `open` 与 `input`，核对源工程、米制单位和标准无脚双段腿骨架资格。
2. **跑只读诊断**：运行 `diagnostic`，读取双小腿端盖的漂移量、滑移速度以及离地/穿透报告。它只输出数据，改动画回到姿态或曲线步骤完成。诊断通过后再结合连续播放画面确认脚感。
3. **清理会话**：读完报告后运行 `readonly-release` 与 `close` 释放后台会话；中途需要停下时运行 `readonly-cancel`。

## 生成双腿固定端盖独立补偿工程

当角色只有纯根位移（`LINEAR` / `FREE`）轨迹，需要让双小腿端盖严格贴合指定水平面时，按下面步骤生成独立补偿候选：

1. **准备干净源副本**：保存一份标准无脚双段腿骨架工程，保持全静置姿态、无姿态 Action、米制 1 米单位、根物体零旋转与单位缩放，且初始双端盖正好落在目标水平面世界 Z 高度上。
2. **预检与生成**：依次运行 `cap-precheck` 和 `cap-generate`，指明帧区间、目标水平面世界 Z 和独立输出目录。求解器只生成双侧 `thigh.X` / `shin.X` 四条曲线，按固定采样密度、端盖贴合容差和 0–150° 膝屈曲约束计算；若轨迹超出腿长可达范围，先收窄根位移幅度再重跑预检。
3. **核对独立候选**：生成结果落在独立会话目录的新 `candidate.blend` 里，核对回执成功状态、文件可读性与源版本后再打开检查。磁盘文件由文件系统管理，需要回退时直接回到原工作副本，并按需执行 `reference-prepare` / `reference-generate` / `reference-release` 处理随包参考用例。

## 异常与现场恢复

遇到环境中断、回执不明或工程缺失时，先查当前运行进程、已保存工作副本和可验证备份，在独立恢复目录里展开核对版本、哈希与缺失项后再接续。结束排查时，只清理本次任务启动且身份已确认的后台进程，原文件和未知文件保持不动。
