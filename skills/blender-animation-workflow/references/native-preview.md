# 原生实体视口录屏

本参考说明如何在打开的 Blender 三维视口里录制真实播放的 1080p 预览视频，供动作审看或交接下游使用。

## 三种显示模式

按审看目标挑定 `--mode`（或面板里的 `display_mode`）：

| 模式 | 画面效果 | 适用场景 |
| --- | --- | --- |
| `SOLID_WHITE_UNLIT`（默认） | 单色白模、无光影、关闭覆盖层 | 单角色纯看剪影、重心和动作节奏 |
| `SOLID_OBJECT_UNLIT` | 按对象 `Object.color` 分色、无光影、关闭覆盖层 | 双人或多人对手戏快速区分角色，直接改目标物体的 `Object.color` 即可，无需新建材质或灯光 |
| `CURRENT` | 保留当前视口显示状态 | 已备好材质或灯光，需要顺带看画面氛围 |

两种无光影实体模式只在录制期间临时调整视口显示参数，录完或取消后自动恢复改过的视口字段，对场景里的材质、灯光、网格和全局偏好保持原样。

## 录前准备

1. **摆好机位与窗口尺寸**：打开包含目标动画的 Blender 窗口，切到要录制的 `VIEW_3D` `WINDOW` 区域，确认实际可用视口区域达到或超过 1920×1080，并切进锁定好的相机视角。录屏采用居中裁取真实窗口像素（`WINDOW_SCREENSHOT`），先调好构图和窗格大小再开录。
2. **挂接现场会话**：在目标三维视口空闲状态下挂接录屏会话（提供会话队列、输出目录与 `ffmpeg` 路径），记下返回的 `--session-dir`、`--session-id` 和对应 `--edition`。

## 用 CLI 控制录屏（`record-live`）

通过 `animation record-live` 按顺序检查、起录、查状态和收尾：

```powershell
$ctl = './blenderctl.ps1'

# 1. 先检查视口上下文与 SHA
& $ctl animation record-live --edition <版号> --session-dir $sDir --session-id $sId --operation inspect

# 2. 启动 1080p 原生播放录制
& $ctl animation record-live --edition <版号> --session-dir $sDir --session-id $sId --operation start --mode SOLID_OBJECT_UNLIT --start-frame 1 --end-frame 240 --expected-context $ctxSha

# 3. 轮询进度并取回结果
& $ctl animation record-live --edition <版号> --session-dir $sDir --session-id $sId --operation status --native-job $jobId
& $ctl animation record-live --edition <版号> --session-dir $sDir --session-id $sId --operation result --native-job $jobId
```

中途需要停止录制时运行 `--operation cancel --native-job $jobId`；全部录完后运行 `--operation detach` 解除会话挂接。在 Blender 侧栏「动画曲线 → 原生视口录屏」里也可以直接点击起录和取消。

录制过程只在起点定位一次帧，随后依靠真实的 `screen.animation_play` 推进帧序，保持原生播放节奏。输出目录留空时，会在工程旁的 `//native_recordings` 下为每次录制新建独立作业目录。

## 录后验收

1. **先读 `report.json`**：核对 `technical_complete`、`encoded_count`、`missing_frames` 为 0、`queue_drops` 为 0、`encoder.reaped` 为真，以及 `view.guard` 的视口恢复记录。取消后留下的片段标记为 `partial`，留作排查记录，重新录出完整成片再交付。
2. **亲眼看一遍视频**：打开生成的 MP4 完整看一遍，核对分辨率、帧率、总帧数、切镜时机、角色滑步与穿插，再连同工程副本和审看说明交给用户或下游部门。
