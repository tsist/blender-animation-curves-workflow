# 原生 1080p 视口预览

在有实际三维视口的 Blender 窗口中录制。先保存工程、确定播放区间、准备相机和构图，确认角色已显示。

## 显示与窗口

N 侧栏「动画曲线 → 原生视口录屏」提供三种模式。

| 模式 | 显示效果 |
| --- | --- |
| 白模实体录屏 | `SOLID_WHITE_UNLIT`，白色实体、平面显示。 |
| 实体视口颜色录屏 | `SOLID_OBJECT_UNLIT`，使用各对象的 `Object.color`。 |
| 当前视口录屏 | `CURRENT`，录制当前显示，材质视口应先等待显示就绪。 |

要用颜色区分角色，在 Object Properties → Viewport Display 设置物体颜色，选择「实体视口颜色录屏」。实体预设使用 SOLID／FLAT，关闭阴影、腔隙、高光和 overlays，并在结束或取消后恢复修改过的视口字段。

实际 VIEW_3D 的可用窗口区域须至少为 1920×1080。按面板显示的区域尺寸调整窗口和侧栏。录制居中裁取原生窗口像素，先用实际画面核对角色、相机边界和构图。宽银幕相机可保留画布内的黑边。

## 编码器与操作

安装可执行的 FFmpeg。可从系统 PATH 查找，或在录屏操作参数中明确填写 FFmpeg 可执行文件路径，也可通过 `ANIMATION_FFMPEG` 指定。先确认所用文件存在并可执行。

点击所需录屏按钮，设置输出目录与起止帧。默认输出在工程旁的 `native_recordings` 目录，每次录制创建独立作业目录。播放由 Blender 实时推进，录制结果来自真实视口窗口像素。

需要取消时点击「取消录屏」，等待编码器退出，再查看该次作业的状态。录屏过程中改动视口字段可能被记录为外部变化。

脚本可调用公开操作器，使用实际文件路径和帧段。

`bpy.ops.animation_native.record_viewport('INVOKE_DEFAULT', output_root=output_directory, ffmpeg_path=encoder_path, start_frame=first_frame, end_frame=last_frame, capture_source='WINDOW_SCREENSHOT', display_mode='SOLID_OBJECT_UNLIT')`

`bpy.ops.animation_native.cancel_recording()` 请求取消当前录制。

## 验收

完成后读取该次作业的 `report.json`，核对 `technical_complete`、`encoded_count`、`missing_frames`、`queue_drops`、编码器退出状态和视口恢复记录。再解码视频，检查 1920×1080、帧率、帧数和时长。

完整播放时看起止姿态、各次接触、后续动作、支撑脚和切镜。核对物体异色及需要强调的灯材可见性。出现漏帧或队列丢帧时缩短帧段或降低视口负载，再重新录制。

需要音乐时，用原生录制的视频流复制封装配乐，保留音轨起点、采样率和时长。核对最终编码后的真峰、末尾淡出和若干音频窗口的同步，再完成听检。工程移动时携带 WAV 等依赖并独立重开。
