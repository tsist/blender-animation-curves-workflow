# 参考文档与证据索引

本页集中登记动画工作流的接口文档、固定包路径与各阶段实测交付证据，方便查溯来源与复核基线。当前宿主与版号以 `blenderctl.ps1 discover --product animation` 实时输出为准。

## 项目接口与交接文档

- [动画与模拟部门卡](https://github.com/tsist/blender-ai-workflow/blob/main/skills/blender-work-methodology/references/departments/animation.md)
- [镜头时序与动画支撑交接文档](../../../../docs/cli/workflows/shot-timing.md)
- [自研插件动作映射（绑定/动画/相机）](../../../../work/audit/<batch>/)
- 公共入口：`./blenderctl.ps1`
- 固定插件工作目录：`<PROJECT_ROOT>\tools\animation_curves_workflow_addon`
- Windows 交付安装包：`<PROJECT_ROOT>\deliveries\animation_curves_workflow_0.4.4_windows.zip`

## 专项实测与交付证据

- **六项姿态编辑实测**：`work/audit/<batch>/`
- **四人项目写入实跑**：`work/audit/<batch>/`
- **冻结素材公共入口、冷开与重放**：`work/audit/<batch>/`
- **双人完整片段事件首例**：
  - 流程汇总：`<PROJECT_ROOT>\work\animation-first-fight-20261116-v001\public-workflow-v001\summary.json`
  - 请求样例目录：`<PROJECT_ROOT>\work\animation-first-fight-20261116-v001\delivery-candidate-v001\`（含 `capture.json`、`retime.json`、`resources.json`）
  - 原生录屏验收：`<PROJECT_ROOT>\work\animation-first-fight-20261116-v001\native-window-recording-v002\qualification.json`
- **普通对象局部改时与连续改时**：
  - 首轮验收：`<PROJECT_ROOT>\work\charge-local-edit-20261117-v001\delivery-v001\VERIFIED.json`
  - 连续往返验收：`<PROJECT_ROOT>\work\charge-local-edit-20261117-v001\delivery-v002\VERIFIED.json`
  - 实体对象色录屏验收：`<PROJECT_ROOT>\work\charge-solid-20261117-v001\delivery-v002\README.md` 与 `native-recording-v003\qualification.json`
- **多事件四人工程与光材卡点**：
  - 四人可编辑交付：`<PROJECT_ROOT>\work\animation-completion-20261117-v001\delivery-four-v001\`
  - 光材卡点交付：`<PROJECT_ROOT>\work\animation-completion-20261117-v001\first-fight-color-v001\delivery-v001\README.md`
  - 选段与手柄复核：`<PROJECT_ROOT>\work\animation-completion-20261117-v001\trim-work\` 与 `handle-fix\COLD.json`
- **双人、无骨骼分件与四人素材复用**：
  - 双人整段提取与验收：`<PROJECT_ROOT>\work\animation-reuse-20261117-v001\ASSET-EXTRACT.json` 与 `delivery-candidate-v001\VERIFIED.json`
  - 无骨骼分件整段与选段：`<PROJECT_ROOT>\work\animation-completion-20261117-v001\rigid-parts\integration-040\` 与 `rigid-trim-integration-v001\README.md`
  - 四人整段互动复用：`<PROJECT_ROOT>\work\animation-completion-20261117-v001\four-clip-native-v001\README.md`
- **真实音频分析与局部校正首例**：`<PROJECT_ROOT>\work\animation-music-20261117-v001\delivery-candidate-v002\`
- **共享知识库参考**：Wiki 条目 `procedures/blender-clip-native-frame-and-semantic-offset-identity.md`
