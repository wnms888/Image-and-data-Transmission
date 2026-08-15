# IMGT 图像传输与调试监视器

本项目包含一套嵌入式 WiFi 图像/日志传输协议，以及配套的 Windows 上位机。它可在同一 TCP 流中可靠区分 RGB565 图像与 `printf` 文本，避免日志字节破坏图像帧。

## 项目结构

```text
EmbedCode/
  WifiImageTransfer.c/.h   嵌入式 WiFi 图像、日志帧与 Printf 接口
HostApp/
  monitor.py               可缩放图形化上位机
  protocol.py              IMGT v1 分帧、校验、RGB565 解码和 PNG 编码
  oscilloscope.py          通过校验的数值日志多通道示波器
  image_processing.py      逆透视与红黄 HSL 检测实时预览
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

“分级调试信息”中的“示波器”页默认关闭。启用后，数值日志会按接收时间绘制：横轴是时间、纵轴是幅值，各通道使用不同颜色。IMGT 模式只会将已通过包头和载荷校验的数据送入日志/示波器；原始 Printf 文本模式没有数据包校验。可用“放大”“缩小”调整时间与幅值视图，“自动缩放”恢复按数据范围缩放，“清除”删除已采样曲线。无数值的文字日志不会产生曲线。

将鼠标悬停在曲线采样点附近可查看相对时间和各通道数值；在示波器图内滚动滚轮可直接放大或缩小时间与幅值视图。

“实时图像”面板还提供两个不影响原始接收/保存图像的上位机处理页：

- **逆透视**：按 `CameraIPM.h` 的相机内参、畸变参数和单应矩阵生成俯视预览；可调近端/远端 X、横向半宽和输出尺寸，并同时查看原图与结果。
- **颜色检测**：按 `ColorDetection.c` 的 HSL 0–240、红黄阈值、ROI 和四连通面积过滤生成实时检测掩码；可图形化修改各项参数，并同时查看原图与结果。

## 颜色异常排查

若图像轮廓正确但颜色异常，传输长度与图像分帧通常是正确的，问题一般是 RGB565 每像素两字节的顺序或红蓝通道解释不一致。

上位机默认使用 **RGB565 · 高字节优先（SCC8660 推荐）**。可在“图像颜色格式”中即时切换：

1. 红蓝相反：选择 `BGR565 · 高字节优先`。
2. 颜色仍明显错误：尝试对应的低字节优先选项。
3. 重新编译嵌入式端后可选择“自动”，上位机会读取包头 bit 0 的字节序标志。

嵌入式端默认以 `WIFI_IMAGE_TRANSFER_RGB565_MSB_FIRST = 1u` 标注 SCC8660 图像流。若 `image` 缓冲已被驱动转换为 CPU 小端 RGB565，则在工程配置中覆盖为 `0u`。

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

## 数据帧

IMGT v1 使用固定 22 字节小端头：`AA 55`、版本、类型、载荷长度、宽高、像素格式、标志、序号、头 CRC-16、载荷 CRC-32。类型 `1` 为 RGB565 图像，类型 `2` 为 UTF-8/ASCII Printf 文本。标志 bit 1 说明载荷 CRC 是否存在；嵌入式端与上位机默认都要求该标志和 CRC-32。头 CRC、载荷 CRC 失败或 CRC 标志缺失的完整包会直接丢弃，绝不会显示为图像、文本日志或示波器曲线。完整字段说明见 [HostApp/README.zh-CN.md](HostApp/README.zh-CN.md)。

## 验证

```powershell
Set-Location HostApp
python -m unittest discover -s tests -v
```

测试覆盖 TCP 分片重组、CRC/重同步与缺失 CRC 丢弃、Printf 格式、示波器数据模型、RGB565 高低字节解码、逆透视/颜色检测模型、等比缩放、PNG 保存和串口描述展示。
