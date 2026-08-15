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
- “保存当前帧”会异步保存 PNG；“开始/结束保存全部帧”会将接收期间的每帧 PNG 写入 `captured_frames/session_时间戳`，不阻塞预览。

SCC8660 默认选择 **RGB565 · 高字节优先（SCC8660 推荐）**。如果实际显示的红蓝互换或颜色仍异常，可在“图像颜色格式”下拉框即时尝试 RGB565/BGR565 与高/低字节优先组合，无需重新连接。包头的 flag bit 0 也会携带嵌入式端选择的字节序信息，选择“自动”时上位机会按该标记解码。

## 嵌入式接入

将更新后的 `WifiImageTransfer.c/.h` 放回工程并正常调用：

```c
WifiImageTransfer_Init();
/* 每帧图像处理完成后 */
WifiImageTransfer_SubmitRgb565Frame(image_copy, SCC8660_IMAGE_SIZE);
```

然后在工程**已经存在**的 `printf` 重定向函数中加入 WiFi 输出。不要新增第二个 `fputc`，否则会产生重复符号。常见的两种工具链写法如下，保留原本的 UART 输出语句即可。

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

`PutChar` 会忽略 `\r`，在收到 `\n` 时把整行封装成一个文本帧。因此应用层不需要改动：

```c
printf("[info] Base_StartWaitingIMU\n");
printf("speed: %d\n", 666);
printf("[warning] channel_data: %d, %d, %d, %d\n", 12, 13, 14, 15);
```

文本缓存默认最大 256 字节，可在 `WifiImageTransfer.h` 中修改 `WIFI_IMAGE_TRANSFER_TEXT_MAX_LENGTH`。非常长而未换行的输出会被安全分片；正常调试信息应以 `\n` 结尾。`printf` 应从任务上下文调用，不要在 WiFi SPI 发送中的中断服务程序中调用。

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
| 13 | 1 | 标志位；bit 0 为 1 表示 RGB565 图像载荷为高字节在前 |
| 14 | 2 | 包序号（自然回绕） |
| 16 | 2 | 对头部 0–15 字节的 CRC-16/CCITT-FALSE |
| 18 | 4 | 载荷 CRC-32；默认 0，表示未启用 |
| 22 | N | 图像 RGB565 或以 `\n` 结尾的 Printf 文本 |

头 CRC 可让接收端在噪声或丢字节后重新寻找下一个 `AA 55`，因而图像帧和文本帧不会相互干扰。TCP 下可靠传输无需额外载荷 CRC；若将同一协议用于容易受干扰的串口，可在嵌入式工程的全局配置中设定：

```c
#define WIFI_IMAGE_TRANSFER_ENABLE_PAYLOAD_CRC 1u
```

上位机将自动验证非零的 CRC-32。

`WifiImageTransfer.h` 默认定义 `WIFI_IMAGE_TRANSFER_RGB565_MSB_FIRST` 为 `1u`，与 SCC8660 的高字节先出图像流匹配。若你的 `image` 缓冲已经被驱动转换为 CPU 原生的小端 RGB565，请在包含该头文件前覆盖为 `0u`；上位机的“自动”模式会随包头标志同步调整。

## Printf 文本解析规则

接受格式为 `"[class]describe: data1, data2, ...\n"`：

- `class` 是 `error`、`warning` 或 `info`；未提供时为 `info`。
- 冒号前为描述；没有冒号时，若整行均为数字则视为无描述通道数据。
- 逗号分隔的数据会安全转换为浮点通道。描述后是非数字时仍作为“描述文本”显示，故 `[info] Base_StartWaitingIMU` 不会导致解析异常。
- “原始 Printf 文本”模式用于未经过 IMGT 封包的串口字符串，不能传输图像；其余模式必须使用本协议。

## 验证解析器

```powershell
Set-Location HostApp
python -m unittest discover -s tests -v
```

测试覆盖了 TCP/串口常见的任意长度分片、损坏帧后的重同步、RGB565 转换和题述三种 Printf 格式。
