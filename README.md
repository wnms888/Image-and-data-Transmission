# IMGT 图像传输与调试监视器

本项目包含一套嵌入式 WiFi 图像/日志传输协议，以及配套的 Windows 上位机。它可在同一 TCP 流中可靠区分 RGB565 图像与 `printf` 文本，避免日志字节破坏图像帧。

## 项目结构

```text
EmbedCode/
  WifiImageTransfer.c/.h   嵌入式 WiFi 图像、日志帧与 Printf 接口
  CameraIPM.c/.h           共享标定的点映射与完整逆透视图像采样
  ColorDetection.c/.h      可配置颜色列表的 HSL 检测
  TC4ImageProcessingConfig.h 上位机导出的唯一嵌入式图像处理配置文件
  README.md               处理顺序、接口契约及单头文件同步说明
HostApp/
  monitor.py               可缩放图形化上位机
  protocol.py              IMGT v1 分帧、校验、RGB565 解码和 PNG 编码
  oscilloscope.py          通过校验的数值日志多通道示波器
  image_processing.py      逆透视与多颜色 HSL 检测实时预览
  processing_configs/      带时间戳的 JSON/C 参数导出与上次会话参数
  receiver.py              TCP 监听/客户端、串口接收
  frame_saver.py           后台 PNG 单帧/连续帧保存
  启动上位机.bat           Windows 推荐启动入口
  launch_monitor.ps1       防火墙检查与上位机启动脚本（ASCII 文件名）
  README.zh-CN.md          详细协议与嵌入式重定向说明
```

## 快速开始（Windows）

1. 安装 Python 3.10 或更新版本。
2. 打开 PowerShell，进入 `HostApp` 并安装串口依赖：

   ```powershell
   python -m pip install -r requirements.txt
   ```

3. 双击 `HostApp/启动上位机.bat`。

启动器使用绝对 PowerShell 路径和 ASCII 脚本名，不依赖系统 PATH，也不会让中文文件名进入 `cmd.exe` 的命令参数。首次启动发现防火墙规则不存在时，会请求一次 UAC 管理员授权，并创建以下持久入站规则：

```powershell
New-NetFirewallRule -DisplayName "TC4 WiFi Assistant TCP 8086" `
  -Direction Inbound -Action Allow -Protocol TCP `
  -LocalAddress 192.168.137.1 -LocalPort 8086 -Profile Any
```

授权成功后规则会保留，之后无需重复授权。若不希望使用启动器，也可手动以管理员 PowerShell 执行上面的命令，再运行 `python monitor.py`。

如果 UAC 创建仍然失败，请右键 [HostApp/安装防火墙规则.bat](HostApp/安装防火墙规则.bat) 并选择“以管理员身份运行”。脚本会在 `HostApp/firewall_rule_setup.log` 写入确切错误；若日志提示策略禁止，则需要由电脑管理员允许本地防火墙规则。

防火墙规则无法创建时，启动器仍会打开上位机。务必在开发板初始化 WiFi 之前，在界面点击“开始接收”，使主机实际监听 `0.0.0.0:8086`；否则嵌入式端会显示 `wifi TCP fail`。

### `wifi TCP fail` 排查

#### 首先尝试开启上位机侦听再对嵌入式端进行初始化

当前电脑已经验证 `192.168.137.1:8086` 的本机 TCP 连通性；若开发板仍显示 `wifi TCP fail`，优先检查 Windows 防火墙。执行下面命令：

```powershell
netsh advfirewall show currentprofile
```

若输出包含 `Firewall Policy BlockInbound` 和 `LocalFirewallRules N/A (GPO-store only)`，说明电脑由组策略统一管理，本机管理员和本项目都不能添加有效的本地放行规则。需要请网络/系统管理员在 GPO 的 **Windows Defender 防火墙 → 入站规则** 中添加 TCP `8086`、本地地址 `192.168.137.1` 的允许规则（Public 或 All Profiles）。

在规则下发前，替代方案是使用一台允许本地防火墙规则的电脑作为热点和上位机。不要改为随机端口：嵌入式端的 `WIFI_IMAGE_TRANSFER_TARGET_PORT` 与上位机监听端口必须一致。

## 上位机使用

默认输入配置为 TCP 监听 `0.0.0.0:8086`，对应嵌入式默认目标端口。开发板与电脑处于同一网络后，点击“开始接收”。

- **TCP 监听**：开发板主动连接电脑，常用模式。
- **TCP 客户端**：上位机连接远端 TCP 服务。
- **串口**：下拉框显示详细设备名，例如 `COM8 (蓝牙链接上的标准串行)`。
- **IMGT v1**：接收图像和封包 Printf 日志。
- **原始 Printf 文本**：用于普通串口调试，只接收文本。

实时图像会始终按原始长宽比缩放到图像面板；界面显示接收 FPS、帧序号缺失、CRC 状态。预览绘制与接收解耦，画面高帧率时仅渲染最新帧，防止 GUI 积压。

“保存当前帧”将当前画面异步保存为 PNG。“开始保存全部帧”后，每个接收图像帧均保存为 PNG；再次点击“结束保存全部帧”后停止接收新帧，已排队帧会继续写入。文件位于 `HostApp/captured_frames/`。

“分级调试信息”中的“示波器”页默认关闭。启用后，数值日志会按接收时间绘制：横轴是时间、纵轴是幅值，各通道使用不同颜色。IMGT 模式只会将已通过包头和载荷校验的数据送入日志/示波器；原始 Printf 文本模式没有数据包校验。无数值的文字日志不会产生曲线。

可独立放大/缩小 X（时间）和 Y（幅值）轴，并可恢复 Y 自动缩放。“暂停采样”会停止示波器接收新数据而保持当前曲线和日志显示；“时间轴”、前后移动按钮或在图内按住左键拖动，均可回看历史数据。滚轮缩放 X 轴，`Shift + 滚轮` 缩放 Y 轴；“实时”返回最新数据。

“实时图像”面板还提供两个不影响原始接收/保存图像的上位机处理页：

- **逆透视**：默认完整匹配 `CameraIPM.h` 的图像尺寸、内参、径向/切向畸变和单应矩阵；可调俯视范围/输出尺寸，也可在“标定参数”窗口修改全部标定值。
- **颜色检测**：顶部固定提供“增加颜色”和“减少颜色”按钮，默认提供红、黄两项，也可任意新增或移除颜色规则。每项独立设置名称、标记色、HSL 六个阈值和面积上下限，均可在页面内直接输入；“高级阈值”可继续设置宽高、填充率和纵横比。按列表顺序匹配，重叠阈值由排在前面的颜色优先。ROI 和每色最大组件数仍可调整；蓝色等任意色相均可检测。
- **像素取值**：颜色检测页的实时原图支持鼠标悬停显示原始像素坐标及 H/S/L（0–240），等比缩放和新帧更新时也能准确读取。
- **流畅预览**：颜色检测与逆透视后台处理，仅保留最新待处理帧；图像缩放、颜色分类缓存和隐藏页面跳过绘制降低占用。预览最高约 30 次/秒，接收统计和全部帧保存独立工作。
- **框选自动阈值**：每种颜色的“高级阈值…”与“移除”之间提供“框选自动阈值”。点击后原图/结果暂时固定，在原图按住左键框选对应色块，松开后后台计算并填写该颜色的 HSL 六个阈值；支持反向拖动、等比缩放和跨 H=0 的红色，Esc 或右键取消。尽量紧贴色块、不包含背景；面积等几何参数不变，自动结果仍可手动微调。
- **自动记忆参数**：“退出”和窗口关闭按钮先保存完整处理参数到 `HostApp/processing_configs/last_processing_config.json`；下次启动自动恢复，旧版 `HostApp/last_processing_config.json` 和 `imgt_processing_config.json` 仍兼容读取，不移动、不删除原文件。保存失败会保留窗口与上次成功保存的配置；退出时在后台等待已排队的 PNG 写完。

“导出配置（JSON + C）”默认打开 `HostApp/processing_configs/`，文件名自动带北京时间，例如 `imgt_processing_config_261003_1454.json`；同一分钟已有同名记录时追加 `_02` 等后缀。对应 C 配置与 JSON 放在同一目录，项目内 `EmbedCode/TC4ImageProcessingConfig.h` 仍同步更新。导入也默认打开此目录，可自行选择其它位置。自动记忆文件保持固定名称，不与时间戳导出混淆。

## 颜色异常排查

若图像轮廓正确但颜色异常，传输长度与图像分帧通常是正确的，问题一般是 RGB565 每像素两字节的顺序或红蓝通道解释不一致。

上位机默认使用 **RGB565 · 高字节优先（SCC8660 推荐）**。可在“图像颜色格式”中即时切换：

1. 红蓝相反：选择 `BGR565 · 高字节优先`。
2. 颜色仍明显错误：尝试对应的低字节优先选项。
3. 重新编译嵌入式端后可选择“自动”，上位机会读取包头 bit 0 的字节序和 bit 2 的 RGB/BGR 标志。

正确选择上位机图像格式后导出配置，字节序和 RGB/BGR 顺序会一并写入 `TC4ImageProcessingConfig.h`。颜色解码和 WiFi 包头均读取该文件，禁止另外覆盖冲突的 `WIFI_IMAGE_TRANSFER_RGB565_MSB_FIRST`。这些参数只解释原始内存，不会修改相机驱动或转换像素。

## Printf 重定向

当前嵌入式实现使用非阻塞日志 FIFO：`printf` 重定向只入队，主循环中的 `WifiImageTransfer_Service()` 再发送一个 SPI 分片。因此不要在重定向函数中直接调用 WiFi/SPI 发送函数。

在项目已有的 TriCore GCC `write()` 重定向（通常位于 `zf_common_debug.c`）中调用：

```c
WifiImageTransfer_WriteLogBytes(data, (uint32)length);
```

并在 CPU0 的每次主循环中调用服务函数（即使本次没有提交图像也要调用）：

```c
WifiImageTransfer_Service();
```

若工程使用字符级 `fputc`，可改为调用 `WifiImageTransfer_PutChar((char)ch)`。应用层的 `printf` 无需修改：

```c
printf("[info] Base_StartWaitingIMU\n");
printf("speed: %d\n", 666);
printf("[warning] channel_data: %d, %d, %d, %d\n", 12, 13, 14, 15);
```

日志 FIFO 数组默认 1024 字节（环形队列保留一个空位，实际可排队 1023 字节），单个文本包的安全上限默认 256 字节；服务函数在 `\n` 或达到该上限时封包。`[error]`、`[warning]`、`[info]` 分别显示在三个独立文本框；省略等级时默认 `info`。FIFO 满时会丢弃最新日志字节而不会影响图像传输。

## 图像处理配置同步到嵌入式端

“实时图像”标题栏的“导出配置（JSON + C）”会同时生成三份内容：用于再次导入上位机的 JSON、与 JSON 同目录的 `*_embedded_config.h`，以及项目内已同步更新的 [EmbedCode/TC4ImageProcessingConfig.h](EmbedCode/TC4ImageProcessingConfig.h)。C 文件是完整头文件，不是零散参数片段。

首次接入时，将本仓库更新后的 `CameraIPM.c/.h`、`ColorDetection.c/.h` 和 `TC4ImageProcessingConfig.h` 放入嵌入式工程并重新编译。新版 `ColorDetectionResult` 使用 `colours[i].count` 和 `colours[i].cones[j]` 表示导出列表中第 `i` 种颜色（从 0 开始），替代旧的 `red_count/yellow_count` 和 `red/yellow` 数组；旧工程中引用这些字段的位置需一次性更新。此后每次在上位机增减颜色或调整逆透视、颜色阈值，只需点击导出、把导出的 `TC4ImageProcessingConfig.h` 整个覆盖到嵌入式工程同名文件，然后重新编译；不需要再修改任何 C 源文件。JSON 配置已升级为 v2，仍可导入旧版 v1 红黄配置。

首次升级还须同步新版 `WifiImageTransfer.c/.h`，它们与颜色检测共用配置中的像素布局，之后不再单独修改字节序开关。完整流程、接口返回值和缓冲区要求见 [嵌入式接入说明](EmbedCode/README.md)。

该文件同步标定、鸟瞰图范围/尺寸、有序颜色 HSL/面积/几何阈值、ROI、每色组件数、预览标记色和像素字节/通道布局。阈值表只读，颜色数量与数组尺寸自动由宏生成；修改参数后只需完整覆盖 `TC4ImageProcessingConfig.h` 并重新编译烧录。调用方数组应使用 `CAMERA_IPM_OUTPUT_*` 和 `COLOR_DETECTION_COLOR_COUNT`，不要硬编码尺寸和红/黄数量。C 配置头现为 v2（区别于 IMGT v1 传输协议）。

核对后已修正 ROI 舍入/空边界、白板 ROI 串用、像素格式不同步及逆透视单精度舍入差异，并补齐 `Camera_RenderIpmRgb565()` 和可选颜色掩码输出接口。两条图像分支都输入同一张稳定**原始图像**，不是先逆透视再颜色检测；颜色掩码与逆透视结果已通过真实 C/Python 逐像素对比。嵌入式额外的底点地面坐标和白板方向信息不属于上位机颜色掩码输出。完整板端主循环/DMA 工程未包含在仓库中，实际硬件时序及缓冲稳定性仍需实机确认。

## 数据帧

IMGT v1 使用固定 22 字节小端头：`AA 55`、版本、类型、载荷长度、宽高、像素格式、标志、序号、头 CRC-16、载荷 CRC-32。类型 `1` 为 RGB565 图像，类型 `2` 为 UTF-8/ASCII Printf 文本。标志 bit 1 说明载荷 CRC 是否存在；嵌入式端与上位机默认都要求该标志和 CRC-32。头 CRC、载荷 CRC 失败或 CRC 标志缺失的完整包会直接丢弃，绝不会显示为图像、文本日志或示波器曲线。完整字段说明见 [HostApp/README.zh-CN.md](HostApp/README.zh-CN.md)。

## 验证

```powershell
Set-Location HostApp
python -m unittest discover -s tests -v
```

测试覆盖 TCP 分片/CRC、Printf、示波器模型、图像解码/处理、配置同步、PNG 保存、串口描述和真实 Tk 参数恢复/退出。跨端测试会实际编译 `EmbedCode` 三个 C 模块，比较四种像素布局下全部 65,536 个 WORD 的 HSL、逆透视和颜色掩码逐像素结果，并验证只更换配置头即可调整颜色数量和输出尺寸。Windows 需 Visual Studio C++ Build Tools；无 C 编译器时该部分明确跳过。驱动桩仅位于 `HostApp/tests/embedded_fixture`，不得加入嵌入式编译。
