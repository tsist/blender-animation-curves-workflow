# 动作素材复用

本参考说明如何把已验证的双人动作、无骨骼分件动作与四人整段互动打包成冻结素材库，再通过 `animation clip-apply` 或侧栏面板复用到新工程，并在复用后继续手调和局部改时。

## 提取冻结素材库

把成熟动作沉淀成可复用素材时，在已保存的工作副本上调用公共 `asset prepare`（`operation: "extract"`），明确点名需要保留的角色集合与接触灯光对象，连同依赖闭包一起导出。

把导出的库文件与对应的 `.clip.json` 描述符一起保管。描述符记录库文件的绝对路径、SHA256、实际 `asset_id`、模板 UUID、源帧区间、帧率与单位；移动或复制库目录后，同步更新描述符里的 `library.file` 并核对哈希。

## 复用素材到目标工程（`clip-apply`）

复用前先在目标工程的工作副本里预设好与素材一致的 `fps`、`fps_base`、米制单位以及相机和播放区间（CLI 写入保持目标场景原相机与帧域不变）。

准备好符合 `animation-native-clip-apply-v1` 的请求清单 `APPLY.json`（内联素材描述符，并指定 `instance_name`、`start`、`end`、`scale`、`translation`、`yaw_quarter_turns`）和资源清单 `resources.json`，运行公共命令：

```powershell
$ctl = './blenderctl.ps1'
& $ctl --jobs-dir $jobs animation clip-apply $target --expected-sha256 $sha --manifest $applyJson --resources $resources --edition <版号>
```

在 Blender 侧栏「动画曲线 → 动作素材复用」里选择 `.clip.json` 同样可以调用同一核心创建新实例，并可勾选同步设置播放范围；确认后另存工作副本。

## 三类素材与选段要点

| 素材类型 | 描述符协议 | 复用与调整要点 |
| --- | --- | --- |
| 双人完整片段 | `animation-native-clip-v1` | 两名同模板角色、根变换、接触灯与标记材质同步改时；支持统一正均匀缩放、三轴平移与 Z 轴 0/90/180/270° 转向，每次复用自动发放新 UUID 并保留来源标记 `acw_source_asset_id` |
| 无骨骼分件片段 | `animation-native-rigid-clip-v1` | 覆盖 1–2 个由单位变换空物体根和静态网格部件组成的角色集合，每部件挂独占原生位置与 Euler 曲线；重复追加自动隔离 UUID 与 Action |
| 四人整段互动 | `animation-native-project-clip-v1` | 一次事务追加四名角色、接触灯材、原生 Action、四次接触与事件关系，同时保留目标场景原有项目记录；调整结束帧时只重映射 `recovery_start` 后的收势区间，带绝对空间锁的素材选用让锁点保持原地不动的变换 |

### 只取中间一段（`source_window`）

双人片段与无骨骼分件都支持区间选段复用。在请求清单里加上 `source_window: {"start": ..., "end": ...}`（或在侧栏勾选「仅复用源片段选段」），边界支持小数帧。系统按原生曲线形状切分边界、保持段内插值并舍去窗外关键帧。复用后的短段还可以再用 `asset prepare` 提取成独立短库，供后续工程二次复用。

## 复用后的手改接续与核对

1. **以保存重开后的曲线为准**：原生保存和重开可能会重算 AUTO 手柄；做局部改时前，先完成曲线手调并另存副本，以独立冷重开读出的关键帧坐标和手柄作为保护比对基准。
2. **密集关键帧顺向移动**：在密集帧区间做同向单调局改时，系统按链式顺序从远端向近端逐键移动，防止临时靠近的未移键被曲线刷新合并。
3. **冷重开核对四要素**：每次 `clip-apply` 完成后，独立冷重开作业目录产出的新候选，核对新实例 UUID、映射后的实际接触帧（忽略旧源注释里的历史帧号）、锁帧位置以及角色站位与穿插情况。
