# IMGT 图像与调试监视器

这是与 `../EmbedCode/WifiImageTransfer.c` 配套的上位机。它通过一个统一的二进制帧协议接收 RGB565 图像和 `printf` 调试信息，避免把调试字符串误解为像素数据。

## 启动

在本目录执行：

```powershell
python -m pip install -r requirements.txt
python monitor.py
```

程序只依赖 Python 标准库、Tk 和 `pyserial`；图像显示不需要 Pillow。建议 Python 3.10 或更高版本。
也可以在资源管理器中双击 `启动上位机.bat`（它只调用 ASCII 文件名的 `launch_monitor.ps1`，避免 Windows `cmd.exe` 因中文脚本路径或代码页造成命令截断）。

该启动器会检查名称为 `TC4 WiFi Assistant TCP 8086` 的 Windows 防火墙入站规则。规则缺失时会请求一次 UAC 管理员授权，并创建仅允许本机 `192.168.137.1:8086`、任意网络配置文件的 TCP 入站规则；创建成功后后续启动不会重复请求授权。

若首次 UAC 创建失败，双击 `安装防火墙规则.bat` 前请右键选择“以管理员身份运行”。失败细节会写入 `firewall_rule_setup.log`，可据此确认是否被本机/域策略禁止创建本地防火墙规则。

即使防火墙规则创建失败，启动器也会继续打开监视器；在界面选择 **TCP 监听（开发板连接本机）**、保持 `0.0.0.0:8086` 并点击“开始接收”。这一步必须在嵌入式端执行 `WifiImageTransfer_Init()` 前完成，否则开发板会报告 `wifi TCP fail`。

默认选择 **TCP 监听（开发板连接本机）**、地址 `0.0.0.0`、端口 `8086`，对应嵌入式代码的默认 `WIFI_IMAGE_TRANSFER_TARGET_PORT`。电脑与开发板连接到同一网络后，点击“开始接收”，再让开发板连接到电脑的局域网 IP。若 Windows 提示防火墙权限，请允许 Python 接收专用网络连接。

界面支持：

- TCP 监听、TCP 客户端和串口三种输入方式，以及地址/端口/波特率的图形化配置；
- `IMGT v1` 二进制协议（图像和字符串）和“原始 Printf 文本”兼容模式（普通串口日志）；
- 自适应可缩放窗口、可拖动的图像/日志分栏、实时 FPS 和图像等比缩放适配；
- 完全独立的 INFO、WARNING、ERROR 文本框。每条日志均显示接收时间、描述和原始数据字段。
- “示波器”页可手动开启、暂停采样或继续采样；支持横纵轴独立缩放、历史时间轴、平移回看、自动 Y 缩放和清除操作。
- “实时图像”中的“逆透视”和“颜色检测”页均可显示实时原图与处理结果，并完整提供与嵌入式对应的图形化参数调整、JSON 导入和导出；不会改写原始帧或 PNG 保存内容。
- “保存当前帧”会异步保存 PNG；“开始/结束保存全部帧”会将接收期间的每帧 PNG 写入 `captured_frames/session_时间戳`，不阻塞预览。

SCC8660 默认选择 **RGB565 · 高字节优先（SCC8660 推荐）**。如果实际显示的红蓝互换或颜色仍异常，可在“图像颜色格式”下拉框即时尝试 RGB565/BGR565 与高/低字节优先组合，无需重新连接。包头的 flag bit 0 也会携带嵌入式端选择的字节序信息，选择“自动”时上位机会按该标记解码。

## 嵌入式接入

将更新后的 `WifiImageTransfer.c/.h` 放回工程并正常调用：

```c
WifiImageTransfer_Init();
/* 每帧图像处理完成后 */
WifiImageTransfer_SubmitRgb565Frame(image_copy, SCC8660_IMAGE_SIZE);

/* CPU0 的每次主循环均调用；负责从 printf FIFO 发送一个 SPI 分片。 */
WifiImageTransfer_Service();
```

当前实现把重定向日志写入 FIFO，实际 WiFi 发送由 `WifiImageTransfer_Service()` 完成；重定向函数本身不执行 SPI 传输。不要新增第二个 `fputc`，否则会产生重复符号。在项目已有的 TriCore GCC `write()` 重定向（通常位于 `zf_common_debug.c`）中直接使用 `WifiImageTransfer_WriteLogBytes()`；保留原有 UART 输出语句即可。

`fputc` 重定向：

```c
int fputc(int ch, FILE *stream)
{
    WifiImageTransfer_PutChar((char)ch);  /* 新增：镜像 printf 到 WiFi */
    /* 原有的串口发送代码，例如 uart_write_byte(DEBUG_UART_INDEX, ch); */
    return ch;
}
```

`_write` 重定向：

```c
int _write(int fd, char *buffer, int length)
{
    WifiImageTransfer_WriteLogBytes(buffer, (uint32)length); /* 新增 */
    /* 原有的串口发送代码 */
    return length;
}
```

`PutChar` 会忽略 `\r`。服务函数会在 FIFO 中遇到 `\n` 或达到单包安全上限时封装文本帧，因此应用层不需要改动：

```c
printf("[info] Base_StartWaitingIMU\n");
printf("speed: %d\n", 666);
printf("[warning] channel_data: %d, %d, %d, %d\n", 12, 13, 14, 15);
```

日志 FIFO 数组默认 1024 字节（环形队列保留一个空位，实际可排队 1023 字节），可通过 `WIFI_IMAGE_TRANSFER_LOG_FIFO_SIZE` 修改；单个文本包默认最大 256 字节，可通过 `WIFI_IMAGE_TRANSFER_TEXT_MAX_LENGTH` 修改。FIFO 满时会丢弃新日志字节以保护图像通道。非常长而未换行的输出会被安全分片；正常调试信息应以 `\n` 结尾。`printf` 应从任务上下文调用，不要在 WiFi SPI 发送中的中断服务程序中调用。

## IMGT v1 数据包

所有多字节字段均为小端序，固定头长 22 字节。TCP 是字节流，不能假设一次 `recv` 恰好得到一帧；上位机已做缓存重组。

| 偏移 | 长度 | 含义 |
| --- | ---: | --- |
| 0 | 2 | 同步字 `AA 55` |
| 2 | 1 | 协议版本：`1` |
| 3 | 1 | 类型：`1` = RGB565 图像，`2` = UTF-8/ASCII 文本 |
| 4 | 4 | 载荷字节数 |
| 8 | 2 | 图像宽度；文本为 0 |
| 10 | 2 | 图像高度；文本为 0 |
| 12 | 1 | 像素格式；RGB565 为 `2`，文本为 0 |
| 13 | 1 | 标志位；bit 0 为 1 表示 RGB565 图像载荷为高字节在前；bit 1 为 1 表示载荷 CRC-32 存在 |
| 14 | 2 | 包序号（自然回绕） |
| 16 | 2 | 对头部 0–15 字节的 CRC-16/CCITT-FALSE |
| 18 | 4 | 载荷 CRC-32/IEEE；bit 1 标志存在时必须等于对应载荷的校验值（允许校验值本身为 0） |
| 22 | N | 图像 RGB565 或以 `\n` 结尾的 Printf 文本 |

头 CRC 可让接收端在噪声或丢字节后重新寻找下一个 `AA 55`，因而图像帧和文本帧不会相互干扰。上位机默认要求嵌入式端启用载荷 CRC；收到的完整帧只要头 CRC 错、载荷 CRC 错或“CRC 存在”标志缺失，都会被直接丢弃，不会传给图像显示、分级日志或示波器。嵌入式头文件默认已启用：

```c
#define WIFI_IMAGE_TRANSFER_ENABLE_PAYLOAD_CRC 1u
```

上位机也会在底部状态栏分别统计头校验错误、载荷校验错误和“无 CRC 丢弃”。“原始 Printf 文本”模式没有 IMGT 包头和 CRC，仅适用于普通串口的文本兼容输入，不能提供这一校验保证。

`WifiImageTransfer.h` 默认定义 `WIFI_IMAGE_TRANSFER_RGB565_MSB_FIRST` 为 `1u`，与 SCC8660 的高字节先出图像流匹配。若你的 `image` 缓冲已经被驱动转换为 CPU 原生的小端 RGB565，请在包含该头文件前覆盖为 `0u`；上位机的“自动”模式会随包头标志同步调整。

## Printf 文本解析规则

接受格式为 `"[class]describe: data1, data2, ...\n"`：

- `class` 是 `error`、`warning` 或 `info`；未提供时为 `info`。
- 冒号前为描述；没有冒号时，若整行均为数字则视为无描述通道数据。
- 逗号分隔的数据会安全转换为浮点通道。描述后是非数字时仍作为“描述文本”显示，故 `[info] Base_StartWaitingIMU` 不会导致解析异常。
- “原始 Printf 文本”模式用于未经过 IMGT 封包的串口字符串，不能传输图像；其余模式必须使用本协议。

## 示波器

在“分级调试信息 → 示波器”页勾选“启用示波器”后，软件只采样解析出的有限数值通道；纯字符串日志不会入图。IMGT 模式的样本必须先通过包头和载荷校验，原始 Printf 文本模式没有 IMGT 包校验。横轴为消息到达上位机的时间，纵轴为数值幅值，CH1、CH2 等通道使用不同颜色。该页默认保留最近 30 秒且最多 6000 个样本。

- **暂停采样 / 继续采样**：暂停时仍保持曲线和日志显示，但新的数值日志不再写入示波器缓存；
- **X 轴放大 / 缩小**：只调整时间窗；**Y 轴放大 / 缩小**：只调整幅值范围；
- **Y 自动**：取消手动幅值范围，按当前可见数据的范围留白显示；
- **时间轴、◀/▶、实时**：在最近 30 秒的缓存中选择历史时间段、平移已放大的窗口或回到最新数据；图内按住左键拖动也可平移；
- **清除**：删除当前示波器缓存，不影响分级日志；
- **启用开关**：关闭时不会继续采样，重新开启后只记录新的合格数值数据。

鼠标悬停在曲线采样点附近会显示该点相对当前时刻的时间和所有通道幅值；在图内滚动滚轮缩放 X 轴，按住 `Shift` 滚动缩放 Y 轴。

## 实时图像处理

### 逆透视

“逆透视”页按 `CameraIPM.h` 中的完整 `CAMERA_FX/FY/CX/CY`、Skew、`CAMERA_RADIAL_COUNT`、K1–K6、P1/P2 和 `CAMERA_IPM_H` 进行与嵌入式 `raw pixel → ground` 方向相反的重采样，得到俯视图。页面始终并排显示接收原图和处理结果；可通过滑条调整近端 X、远端 X、横向半宽 Y，并通过数值框调整输出宽高。点击“标定参数…”可以图形化编辑全部相机标定参数。

标定原始分辨率为 `160×128`。若接收图像的分辨率不同，上位机会按宽高比例换算映射坐标以便预览；若需要测量级精度，应重新标定并同步更新嵌入式与 `HostApp/image_processing.py` 中的参数。

### 颜色检测

“颜色检测”页复用 `ColorDetection.c` 的 HSL `0–240` 定义、红色/黄色默认阈值、ROI 和四连通组件几何过滤。原图和黑底检测结果并排显示，合格组件会以红色或黄色掩码与白色外框标出。主界面可调整红黄 H/S/L、ROI 顶部/左右范围和每色最大组件数；“完整阈值…”窗口还可调整每种颜色的面积上下限、最小宽高、填充率与纵横比上下限。默认值与 `g_color_detection_red_threshold`、`g_color_detection_yellow_threshold` 对齐；调整立即用于上位机预览，导出 C 配置后也会同步到嵌入式端。

### 参数配置导入导出

实时图像面板顶部的“导出配置（JSON + C）”会保存逆透视和颜色检测的全部参数：JSON 用于再次导入上位机，C 头文件用于直接同步嵌入式端；“导入处理配置”会先验证版本、数值范围、ROI、单应矩阵可逆性，再一次性应用两个页面的配置。

### 导出并直接同步到嵌入式端

点击实时图像面板顶部的“导出配置（JSON + C）”后，上位机先保存完整 JSON，随后自动生成同目录的 `*_embedded_config.h`，并同步覆盖项目内的 `../EmbedCode/TC4ImageProcessingConfig.h`。导出的 C 文件是可直接替换的完整头文件；不要只复制其中几行宏定义。

首次使用新版同步机制时，需要把 `CameraIPM.c/.h`、`ColorDetection.c/.h` 和 `TC4ImageProcessingConfig.h` 一起放入嵌入式工程并重新构建。以后每次在上位机调整参数，只需把新导出的 `TC4ImageProcessingConfig.h` 整个覆盖嵌入式工程的同名文件并重新编译，其他嵌入式源码无需再改动。

这一个文件包含相机标定分辨率、内参、畸变、单应矩阵、逆透视鸟瞰范围/输出尺寸、红黄 HSL、组件几何过滤、ROI 百分比和每色最大组件数。`Camera_RawPixelToGround()` 会按标定参考尺寸自动换算 SCC8660 原始像素；`Camera_GroundToRawPixel()` 供嵌入式鸟瞰图渲染反查像素；`Camera_GetIpmViewConfig()` 直接返回导出的鸟瞰范围和输出尺寸。红黄检测的 ROI 同样按导出的百分比换算到实际帧尺寸。白色标志板的保护阈值保持嵌入式默认值，当前不在上位机颜色页面中调整。

## 验证解析器

```powershell
Set-Location HostApp
python -m unittest discover -s tests -v
```

测试覆盖了 TCP/串口常见的任意长度分片、损坏/缺失 CRC 帧的丢弃与重同步、RGB565 转换、示波器暂停与历史回看、逆透视/颜色检测、JSON 与嵌入式 C 配置导出模型和题述三种 Printf 格式。
