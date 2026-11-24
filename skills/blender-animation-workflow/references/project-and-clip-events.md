# 多事件工程与完整片段

本参考说明如何把角色、根物体、灯光、材质或普通无骨骼物体打成局部事件窗口调速，如何在同一场景里管理四人及多次接触的多事件工程，以及如何接续手调曲线并保护窗外关键帧。

## 完整片段局部事件（`animation inspect` / `run`）

当一段动作同时涉及骨骼旋转、根节点变换、接触灯光（`energy` 与 RGB `color`）和唯一未链接的 Principled 材质通道（`Base Color`、`Emission Color`、`Emission Strength`）时，用公共入口按局部时间窗整体改时：

```powershell
$ctl = './blenderctl.ps1'
& $ctl --jobs-dir $jobs animation inspect $src --expected-sha256 $sha --manifest $manifest --resources $resources --edition <版号>
& $ctl --jobs-dir $jobs animation run     $src --expected-sha256 $sha --manifest $manifest --resources $resources --edition <版号>
```

按下面顺序推进：

1. **先读清参与者与资源**：用 `inspect` 核对当前场景的对象名、通道数和外部依赖；即使没有外部文件，`--resources` 也显式传入空数组 `[]`。
2. **捕获再改时**：先跑 `capture` 写入事件关联记录并拿到新作业候选；随后写好 `retime` 清单，指明当前 `event.contact`、目标 `target_contact` 以及事件窗外的锁帧，再跑 `run` 生成新候选。
3. **核对实际映射**：检查返回里的 `record_summary`、真实绑定键的 `retime_mapping`、`verification.status` 和冷重开结果，再进视口看动作是否顺畅。

### 普通无骨骼物体的局部改时

要对场景里 1–8 个无骨骼的 `MESH` 或 `EMPTY` 做局部 TRS 改时，在同一个 `animation-clip-event-v1` 清单里把精确名称填进 `participants.objects`，其余 `rigs`、`roots`、`lights`、`materials` 保持空列表。要求参与物体位于当前场景视图层、无父级与约束、带独占 Action 与正缩放，且在接触帧有唯一原生关键帧（窗口内为 `LINEAR` 或 `BEZIER`）。

## 四人及多事件工程（`project-events`）

一个场景里有多次接触、先后次序或同步约束时，改用 `animation project-events`（或 `project-inspect` / `project-run`），按「读现状 → 审阅计划 → 写新候选」三步推进：

- **只读与审阅**：`inspect` 读取全部或指定事件的状态、参与者与锁定情况；`select` 选定目标事件；`review` 生成带完整差异的审阅计划。
- **受保护写入（配 `--output`）**：
  - `capture`：登记带原生接触键和窗口的新事件。
  - `retime`：调整目标事件的接触帧，同时校验关联约束。
  - `set_relations`：建立事件间关系，如 `after`（最小帧间隔）或 `sync`（偏移与容差）。
  - `set_lock`：锁定关键接触时刻，或绑定基于当前求值世界顶点/骨骼点的显式空间锁。
  - `adopt`：当同曲线上的手调让某条记录过期时，核对无误后逐条显式采用新状态。

在 Blender 侧栏「动画曲线 → 多事件工程」里同样可以先刷新事件、填写新接触帧或切换时间锁，点击预览确认后再提交并另存副本。

## 独立单通道改时（`channel-inspect` / `channel-edit`）

需要单独调整某条物体变换、灯光、材质或相机曲线的时间点时，先用 `animation channel-inspect` 查明通道清单，再用 `animation channel-edit` 提交修改。受管灯光方案需要保留专属手控辅助灯时，在灯光工具侧配置 `keep_lights`，动画改完后再回灯光侧核对整体方案。相机镜头编排交给镜头部门，这里只调已有曲线时间。

## 接续手调曲线与保护窗外关键帧

把自动化改时和动画师手调结合起来时，守住下面四条做法，窗外关键帧和手调插值就能安稳留下来：

1. **按原生浮点时间认键**：事件记录里既有语义偏移，也有原生 float32 关键帧坐标 `key.co`。连续多次改时时，让插件和 CLI 直接从保存后的原生键建立映射，保持精确浮点时间一致；做小数帧数值抽样时，调用 `scene.frame_set(int_frame, subframe=frac)`。
2. **手调后先保存再重审**：在曲线编辑器里改完另一段非事件区间的动作后，先保存独立工作副本，再跑新一次 `inspect` / `review`。如果手调触发了 AUTO 手柄重算并波及已捕获的事件键，先完成全部手调，再在清理旧记录后的新副本上重新捕获当前事件。
3. **过期记录逐条核对**：手调采用只更新点名事件，同一曲线上依赖旧状态的其他过期记录逐条检查并显式 `adopt`，确认窗外关键帧与手柄完整后再做下一轮改时。
4. **单事件切换时先留档**：若旧单事件工作流需要转去编辑场景里的下一个事件，先保留当前检查点和记录备份，通过公开维护入口 `animation_curves.clip_clear_record` 结束本轮，再捕获新事件。
