---
name: blender-animation-workflow
description: 使用 Animation Curves Workflow 创建简形 FK 角色、编辑受管姿态、诊断脚底支撑、编排双人灯光与四人多事件及音乐节拍卡点、复用动作片段、接续手调曲线并录制原生实体视口预览。接到角色关键帧表演、双人或多人对手戏、动作卡点调速、素材复用或动画预览交付任务时使用。
metadata:
  version: "1.0.0"
---

# 动画曲线工作流

本技能属于 [动画与模拟部门](https://github.com/tsist/blender-ai-workflow/blob/main/skills/blender-work-methodology/references/departments/animation.md)，负责把绑定好的角色或简形骨架做成节奏扎实、接触可信的动作表演，管理双人、灯光与多事件同步及音乐卡点，复用成熟动作片段，并输出原生实体视口预览交给下游。开始前先用 `blenderctl.ps1 discover --product animation` 查出当前宿主可用的版号与入口参数，所有改动都在独立工作副本上执行。

## 怎么做

### 1. 准备副本并摸清工程

在 `runtime/audit/<批次名>/` 下建好独立的作业目录，复制一份私有工作副本再开工。保留原文件当对照，改坏了随时退回。

先跑只读检查摸清场景里的角色名、Action、Action Slot、通道和当前事件记录：

```powershell
$ctl = './blenderctl.ps1'
& $ctl discover --product animation
& $ctl animation --help
```

需要保留还没打键的手工姿态时，先另存一个检查点副本再跑编辑。编辑前把目标对象、骨骼、轴向、帧号和锁定范围写进审阅计划，核对差异符合预期再应用。

已有 C94 原控件或游戏角色的普通原生 FK 骨架时，外部 FBX、左肩辅助、ROM、原生三拍与脚跟候选先走 `rigging-workflow phase3`。自有窗口内的手调写键后按最新状态指纹取快照，继续写键时显式绑定 Action、Slot 及工作副本 SHA。[已有角色的接片、摆姿和现场续作](https://github.com/tsist/blender-rigging-workflow/blob/main/skills/blender-rigging/references/rigging-posing-win53.md)

### 2. 创建简形 FK 角色并编辑受管姿态

从零排练动作或搭建占位角色时，用 `animation fk-character` 创建圆柱或无脚双段腿简形人形模板，并通过 `knees` 操作复核双膝实际几何屈曲。实体视口下开启对象颜色或材质显示，一眼分清胸前和头前的正面标记。

修改受管 FK 角色的关键帧时，走 `animation pose-edit`（带上 `discover` 查出的 `--edition`）按「`inspect` → `plan` → `apply`」三步推进：

- 单骨单轴调姿用 `set_key`、`move_key`、`delete_key`。
- 接手旧 AUTO 手柄通道先跑 `prepare-axis` 复制动作并转为受管通道，核对曲线形状后再续写。
- 整段调速用 `retime-action`，多骨同帧记姿用 `capture-batch`。

细则见 [角色姿态与支撑](references/pose-and-support.md)。

### 3. 诊断脚底落地支撑

做完重心起伏或下蹲发力后，用 `animation support` 跑只读 `diagnostic`，检查双小腿端盖的离地、穿透与滑移速度。

遇到纯根位移轨迹需要严格贴地时，准备处于静置姿态的干净模板副本，按 `cap-precheck` → `cap-generate` 生成独立的双腿固定端盖补偿工程。检查生成的独立候选无误后，再并入主流程。细则见 [角色姿态与支撑](references/pose-and-support.md)。

### 4. 编排双人、灯光同步与音乐节拍卡点

两角色对戏或带动一盏主灯闪动时，用 `animation event-store` 管理双人记录（`store: "paired"`）和双人加单灯跨域记录（`store: "cross"`）。先 `capture-plan` 再 `capture-apply` 锁定接触帧，调整节奏时走 `shift-plan` → `shift-apply`，一次事务同步更新多条 Action 并保留相对偏移。

拿到了明确的 BPM、帧率、起始帧和拍点索引时，在跨域记录上运行 `cue-plan` 与 `cue-apply` 把三方动作精准落到拍点。追求无误差落点选 `exact`，需要落到整数帧时选 `explicit_integer` 并标明容差帧数。遇到真实音频文件时，先用音频分析脚本提炼节拍与重音候选，把音轨设为绝对路径完成受保护改时，最后再单独装配便携工程。细则见 [双人灯光事件与音乐卡点](references/events-and-cues.md)。

### 5. 推进完整片段、四人多事件与手调曲线接续

需要把骨架、根节点、灯光、材质或普通无骨骼物体的变换打包成一个局部事件窗口调速时，用 `animation inspect` 读现状，再用 `animation run` 捕获和改时。

场景里同时出现多次接触、先后关系（`after`）、同步关系（`sync`）或时空锁定时，改走 `animation project-events`（或 `project-inspect` / `project-run`）。按「`inspect` → `review` → 写入（`capture` / `adopt` / `retime` / `set_relations` / `set_lock`）」推进，每次写入都生成新候选并独立重开核对。

在动画编辑器里手调完另一段曲线后，继续用公共入口改时时，把窗外关键帧和手调插值完整保留下来：

- 先在工作副本里完成手调并保存，再做新一次 `inspect` / `review`，让系统以磁盘上真实的原生浮点帧和手柄值为准。
- 同一条曲线上的其他过期记录，逐条检查后显式执行 `adopt` 或重新捕获，再推进下一轮改时。

独立微调单条物体、灯光、材质或相机曲线时间时，使用 `animation channel-inspect` 与 `animation channel-edit`。细则见 [多事件工程与完整片段](references/project-and-clip-events.md)。

### 6. 复用双人、分件与四人动作片段

把已验证的动作库放进新场景时，先确认目标工程的帧率、帧率基准和米制单位与素材一致，再运行 `animation clip-apply`：

- 双人完整片段与无骨骼分件片段支持统一缩放、平移、Z 轴九十度步进转向以及整体时段重映射。
- 只需要中间一小段动作时，在请求里加上 `source_window` 按原生曲线形状精准切段，舍去窗外关键帧。
- 四人整段互动素材在同一事务里追加四名角色、接触灯材、原生 Action、事件关系与锁定；调整结束帧时，只重映射收势区间，同时保持绝对空间锁点原地不动。

复用完成后独立重开候选工程，核对新实例 UUID、接触帧、锁帧和角色站位。细则见 [动作素材复用](references/clip-reuse.md)。

### 7. 录制原生实体视口预览

调完动作后，在打开的 Blender 三维视口里挂接录制会话，用 `animation record-live` 按真实播放录制 1080p 预览视频：

- 单纯看轮廓和节奏，选 `SOLID_WHITE_UNLIT`（白模无光影）。
- 多人对手戏要一眼分清角色，先设好各角色的 `Object.color`，选 `SOLID_OBJECT_UNLIT`。
- 需要检查当前材质或灯光氛围，选 `CURRENT`。

录完读取作业报告，确认已编码帧数完整、零缺帧、编码器已回收，再亲自打开视频看一遍节奏、滑步与穿插。细则见 [原生实体视口录屏](references/native-preview.md)。

## Blender 5.3 表演修正与转换

先用 `discover --product animation` 核对本机入口。公共 `production.run` 的 `domain` 设为 `animation`。局部表演默认用 `correction` 新建独立 NLA 修正层。点名已有层可追加键，`layer_state` 调权重和静音。直接改底层只用明确的 `slot` 请求，提供对象、Action 名、Slot identifier 和受保护帧。受管 NLA 底层另传确切 `base_track`，操作帧使用 Action 本地帧，也可点名普通 PoseBone 的原生位移、旋转和缩放通道。其他 Slot 与同槽共用者维持原绑定。

`transfer` 逐帧读源世界矩阵，可带目标帧偏移，回执对照目标矩阵误差。`rotate` 隔离目标 Action 后转换旋转表示，长弧画面用含子帧的数值检查。无法安全保留任意 NLA、共享对象、线性旋转外推或半组四元数底层时会返回冲突。不同源工程可同批并行；同一工程的修正、转换有顺序时，显式接前项工作副本。冷开核对修正层静音后底层恢复、范围外姿态及邻 Slot。[本机候选调用](<<PROJECT_ROOT>/work/blender53-production-operations/README.md>)

使用 `animation production --file <绝对工程> --expected-sha256 <源指纹> --manifest <操作JSON>` 可直接提交 0.5.0 的候选作业。该命令与 `production.run` 共用后台作业、源文件保护和回执。目标曲线用精确的 `path` 和 `index` 指定，回执会列出与驱动重叠的地址。对 Rigify 生成的驱动骨骼，在绑定侧查对应的可编辑控制器。

- `correction` 可设 `blend_type: "COMBINE"`。常用位移按增量填写，多分量旋转和缩放遵循 Blender 原生 NLA Combine 规则。每条曲线可设 `interpolation: "BEZIER"`，现有请求默认仍为 `LINEAR`。骨骼标量属性使用 `pose.bones["upper_arm_parent.L"]["IK_FK"]` 和 `index: 0`，自定义属性的中性值以原生属性默认值为准。
- `graph` 点名对象和动作槽中的既有 `channels`。工具可选 `euler_filter`、`gaussian_smooth`、`butterworth_smooth`、`decimate` 或 `stepped`。`stepped` 的 `frame_step` 默认是 2。处理前后的 Action 可在候选中做对照。
- `pose` 以骨名限定 `breakdown`、`push`、`relax` 或 `mirror_paste`。镜像需在 `options.source_bones` 单独指定来源骨骼。`motion_paths` 按选定骨骼生成或清除原生轨迹缓存。
- `pose_asset` 用 `capture` 生成 Asset Browser 的本地 Action 位姿资产，再用 `apply` 从同工程的 `asset_name` 取姿，并在指定帧写入选定骨骼。需要从其他工程复用时，先在 Asset Browser 追加对应 Action。

0.5.0 精简 ZIP 包含插件代码和运行配置。CHARGE 教学样本及冻结的支撑实验资源沿用原来的工作区资源。

## 交给下游

交付动画成果前，先对最终候选做一次独立冷重开核对，再把下面几样一起交给用户审看或下游（物理模拟、镜头灯光、剪辑部门）：

- **可编辑动画工程副本**：提供绝对路径、SHA256，写清角色名、Action 名、Action Slot、帧率与起止帧。
- **事件与卡点记录**：列出关键接触帧、节拍映射表、锁定状态及手调保留范围；涉及支撑补偿时附上独立候选与诊断报告。
- **可播放的原生预览视频**：附上 1080p 录屏路径、显示模式、帧数和人工目视核对范围；单角色动作直接交用户做**动作预览审看**，镜头或成片并入分镜预演或粗剪。
- **问题回流路径**：节奏飘、滑步或手指穿插留在动画环节修；正常姿态下破面或控制器锁死退回绑定部门。

## 例子：给双人对手戏做音乐卡点改时并出预览

接到任务：「把双人交锋副本 `work/duel/duel_base.blend` 的接触帧从 66 帧对齐到 120 BPM 的第 5 拍，同步带动接触主灯，并输出白模对象色预览给导演确认。」

1. **建目录并查入口**：新建 `work/audit/<batch>/`，算出 `duel_base.blend` 的小写 SHA256，跑 `discover --product animation` 拿到当前 `--edition`。
2. **捕获双人与灯光事件**：用 `animation event-store` 对 `paired` 做 `capture-plan` / `capture-apply`，再对 `cross` 加入接触主灯，得到带跨域记录的新工作副本。
3. **按 BPM 规划并落点**：写好 `cue-plan` 请求，填入 `bpm: 120`、`fps: 24`、`start_frame: 1`、`beat: 5` 与 `policy: "exact"`，审阅计划里的三方源/目标帧和共同偏移无误后跑 `cue-apply`。
4. **独立重开与局部复核**：对新生成的候选副本跑 `event-store` 的 `cue-inspect` 与 `animation support` 的 `diagnostic`，确认双人头部与主灯曲线同步到达目标帧、窗外关键帧和脚底支撑保持完好。
5. **录制实体视口预览**：在三维视口挂接录屏会话，运行 `animation record-live` 并选择 `SOLID_OBJECT_UNLIT`，录完检查报告零缺帧，打开成片视频核对节奏。
6. **交付审看**：把候选 `.blend` 路径与 SHA256、事件卡点清单和 1080p 预览视频交给用户做动作预览审看，通过后再转交物理模拟或镜头灯光部门。

## 参考

- [角色姿态与支撑](references/pose-and-support.md)
- [双人灯光事件与音乐卡点](references/events-and-cues.md)
- [多事件工程与完整片段](references/project-and-clip-events.md)
- [动作素材复用](references/clip-reuse.md)
- [原生实体视口录屏](references/native-preview.md)
- [已有 C94 与原生 FK 的身体表演](https://github.com/tsist/blender-rigging-workflow/blob/main/skills/blender-rigging/references/rigging-posing-win53.md)
- [参考文档与证据索引](references/sources.md)
