# 双人灯光事件与音乐卡点

本参考说明怎样用 `animation event-store` 管理双人同步事件、双人加单灯跨域事件和显式 BPM 节拍卡点，以及遇到真实音频文件时如何安全地分析节拍并装配音轨。

## 双人同步事件（`paired`）

两角色头部动作需要按固定时差联动时，走 `animation event-store` 的 `paired` 仓库（加上 `discover` 查出的 `--edition`）。请求 JSON 包含 `file`、`expected_sha256`、`context`、`store: "paired"`、`operation` 以及具体参数或计划文件。

1. **捕获双人记录**：准备两名标准受管骨架及各自 `head.Y` 上的非端点关键帧，运行 `capture-plan` 预览 Action、Action Slot 与源帧，核对无误后运行 `capture-apply` 并用 `--output` 写出新副本。随后另起一次 `inspect` 冷重开核对已保存关联。
2. **按共同偏移改时**：需要整体挪动接触时刻时，运行 `shift-plan` 填入共同位移，核对双方源帧、目标帧、相对偏移和两侧锁键，再运行 `shift-apply` 一次事务复制两条 Action 并更新记录。目标键保持 `LINEAR` 或 `CONSTANT` 与 `FREE` 手柄，锁键外的其他关键帧原样保留。

若场景里只需要做一次性的双人转头规划或面板事件检查，也可用 `animation panel-events` 的 `head-plan` / `head-apply` 与 `key-capture` / `key-inspect` / `key-shift` 等操作。

## 加入一盏同步灯光（`cross`）

想让一盏接触主灯的亮度随双人接触一起移动时，先确保当前场景已有有效的双人记录，再切到 `store: "cross"`：

1. **准备灯光资格**：使用本地独占的 `POINT` 或 `AREA` 灯光，保持默认发光节点树（白色、`Strength` 为 1、`Weight` 为 0）和挂在 Light data 上的独占 Action，且曲线为唯一的 `energy` 通道。
2. **捕获跨域记录**：运行 `capture-plan` 选择该灯光对象与 `energy` 中间键，核对三方源帧后运行 `capture-apply`。
3. **三方同步改时**：运行 `shift-plan` → `shift-apply`，一次事务同时更新两角色的 `head.Y`、灯光的 `energy` 曲线和跨域记录。提交后旧双人记录会过期，后续改时都顺着三方跨域记录继续推进。

执行改时前，确认播放头停在未受影响的锁定端点（以免改动当前播放头求值），且当前灯光 `energy` 与源曲线求值一致。如果源对象、Action、Slot 或曲线发生漂移，重新运行 `capture-plan` / `capture-apply` 建立清晰关联。

## 显式 BPM 节拍卡点（`cue`）

拿到明确的 `cue_id`、`bpm`、`fps`、`start_frame` 和拍点索引 `beat`（0 对应起始帧，1 对应下一拍）时，在已有的空 cue 跨域记录上做节拍卡点：

1. **确认帧率与落点策略**：确认场景实际的 `fps` 与 `fps_base` 同输入参数精确一致。时间换算全程保留有理数精度：
   - 追求精确拍点选 `policy: "exact"`，系统核验目标时刻可精确表达后放行。
   - 需要落到整数帧选 `policy: "explicit_integer"`，同时提供 `chosen_frame` 和 0–1 帧范围内的 `tolerance_frames`。
2. **规划、提交与复查**：运行 `cue-plan` 检查事件身份、三方源/目标帧、帧/秒误差与共同位移，确认后运行 `cue-apply` 在同一事务里更新三条 Action、跨域记录和 cue 字段，最后对输出副本运行 `cue-inspect` 冷重开核对。

已有 cue 记录时，若想改用新节拍重新设计，直接回到绑定 cue 之前保存的源副本重新规划；遇到异常提示回滚未完成时，记录现场状态并从已验证的源副本恢复。

## 真实音频分析与音轨装配

需要跟着真实音乐文件找重音和段落来校对动作时，把「音频分析」和「动画改时」拆成两步来做：

1. **离线提取候选时刻**：用本地音频分析脚本（如复用环境内现成的 `librosa`）读取音频，记下原音轨的 SHA256、采样率、截段起点和起音/节拍/能量变化候选。把候选时刻按 `frame = 1 + (source_seconds - excerpt_start_seconds) * fps` 换算成场景帧，保留原始小数与取整误差，再结合剧情挑出真正需要对齐的少量关键事件。
2. **准备绝对路径工作副本**：公共动画入口要求外部静态音频使用明确的绝对路径与准确 SHA256。编辑前先在独立工作副本里把音轨设为本地未打包的普通静态 `SOUND` 条带，填好绝对路径并在资源清单里声明哈希，然后用公共 `animation inspect` / `animation run` 或 `project-events` 完成受保护改时。
3. **装配便携交付副本与合流预览**：全部动画修改收尾后，把 `.blend` 与音频文件复制到同一交付目录，将声音路径设为相对路径（如 `//track.wav`）并以 `relative_remap=False` 保存新副本，再独立冷重开验证音频解析与哈希。给原生实体视口录屏配上音乐时，使用视频流无损直拷（`-c:v copy`）合流，核对帧率、总帧数、音画起点对齐后，再亲自戴耳机听一遍节奏。
