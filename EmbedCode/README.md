# 嵌入式图像处理接口与配置同步

## 核对结论与边界

仓库内的公共图像处理步骤现已与上位机对齐，并通过实际编译 C 模块后的逐像素测试。输入必须是**同一张稳定的原始 RGB565 图像**，使用同一份导出配置。逆透视和颜色检测是两条独立分支，不能把逆透视结果再送入颜色检测：

```text
稳定原始帧
├─ Camera_RenderIpmRgb565         → 鸟瞰 RGB565 图像
├─ ColorDetection_ProcessImage   → 各色组件 + 地面底点 + 白板信息
│  └─ 可选 WithPreview           → 与上位机相同的 RGB888 颜色掩码
└─ WifiImageTransfer_SubmitRgb565Frame → 原始帧 → 上位机 CRC → 解码 → 两条处理分支
```

这不代表嵌入式与上位机的全部输出完全相同：嵌入式还提取组件底点地面坐标、白板底点和方向误差；上位机颜色页目前只显示颜色掩码与各色数量，不显示这些附加信息。白板检测独立进行，不会修改颜色掩码。

本仓库没有完整板端主循环、相机 DMA 驱动、链接脚本及硬件工程。因此这里验证的是算法和数据包，不是实机调度顺序、DMA 所有权、RAM 余量或板端帧率。应在真实工程中保证原图在处理和发送完成前不被覆盖。

## 完全相同的公共步骤

### 逆透视图像

1. 输出行从远端 X 到近端 X；输出列从 +Y 到 -Y，两个方向均包含端点。
2. 使用单应矩阵的逆矩阵，把输出地面坐标映射至**无畸变标定像素**。
3. 归一化相机坐标，施加正向径向畸变（2、3 或 6 系数）和切向畸变 P1/P2。
4. 转换回标定图像坐标，再按实际相机宽高与标定参考宽高之比缩放。
5. 最近邻采样；恰好位于半像素时采用 ties-to-even，和 Python `round()` 相同；越界或投影无效处填黑。

计算使用 64 位 `double`，导出浮点数保留上位机值的往返精度。不要手工添加 `f` 后缀、启用 short-double、fast-math 或允许改变表达式的浮点融合优化；桌面 MSVC 测试使用 `/fp:strict`。点接口为了兼容旧调用仍返回 `float`，它不参与图像重采样。

`Camera_RawPixelToGround()` 是相反方向的**点**计算：原始像素 → 8 次迭代去畸变 → H → 地面坐标，用于底点定位。它不是完整的鸟瞰图渲染，也不能代替上述反向图像采样。

### 颜色检测

1. ROI 边界按整数 `(dimension * percent + 50) / 100` 四舍五入；左边界包含、右边界不包含。百分比合法但舍入后为空的 ROI 检测结果为空。
2. 根据配置解释原始内存字节序及 RGB/BGR 顺序；RGB565 用位复制扩展到 RGB888。
3. 使用整数 HSL 转换，H/S/L 均为 0–240；整数除法向零截断。H 区间可跨 0，所有阈值边界均包含。
4. 按颜色列表顺序匹配；一个像素只属于第一个命中的颜色，不再排斥蓝色。
5. 逐行从上到下、从左到右选择组件种子，使用四连通域。C 的 BFS 与上位机的 DFS 得到相同的组件集合。
6. 依次执行面积、最小宽高、填充率和纵横比过滤，然后限制每种颜色的最大组件数。
7. 可选预览把有效组件涂成导出的 RGB888 标记色，包围框画白色，其余像素为黑色。

之后嵌入式输出组件底点地面坐标，再独立扫描白板 ROI。白板纵向边界不再错误使用颜色 ROI。组件面积与统计使用 32 位；队列使用线性像素索引，不再限制 x/y 小于 256。

## 唯一参数文件

[TC4ImageProcessingConfig.h](TC4ImageProcessingConfig.h) 是所有图像处理参数的唯一入口：

- 相机标定参考尺寸、内参、畸变系数和单应矩阵；
- 鸟瞰图范围和输出宽高；
- 颜色数量、顺序、每种颜色的 HSL 与面积/几何阈值、RGB888 预览标记色；
- ROI 百分比与每色最大组件数；
- 原始相机内存字节序、RGB/BGR 顺序，同时用于颜色解码和 WiFi 包头；
- 底点提取、相机复制和白板检测参数。这些是嵌入式扩展，上位机每次导出使用头文件中的约定默认值，并非颜色页的可调项目。

颜色名称只用于上位机界面和 JSON；板端使用列表的 0 起始索引。阈值表和预览色表均只读，长度必须等于颜色数量，否则编译报错。增加颜色、调整阈值或者调整鸟瞰图尺寸均无需修改算法源文件。

字节序参数只描述已有图像内存，**不会**修改相机驱动或自动转换像素。TC4 为小端：`TC4_CFG_COLOR_SWAP_RGB565_BYTES=1` 表示相机内存的高字节在前，颜色解码先交换 WORD 字节；WiFi 原样发送这些字节并置包头 bit 0。`TC4_CFG_COLOR_BGR565=1` 表示交换红蓝解释，WiFi 同时置 bit 2。bit 1 仍为载荷 CRC 标志。

WiFi SSID、密码、IP/端口和日志 FIFO 大小属于连接/传输设置，仍在 `WifiImageTransfer.h`，不属于图像参数同步。

## 一次性接入与后续同步

首次升级：将本目录顶层 7 个 `.c/.h` 文件加入真实嵌入式工程，保持同一目录的唯一配置头文件；用真实的 `Header.h`、相机和 WiFi 驱动编译。删除工程里旧的 RGB565 字节序冲突覆盖、旧阈值副本；不要在主循环里重新赋值阈值。已有颜色结果接口为 `result.colours[i].count` 和 `result.colours[i].cones[j]`，`i` 对应上位机列表顺序。

以后每次：

1. 在上位机调整参数。先确认“图像颜色格式”能正确显示原图；自动模式有帧时读取包头，尚无帧时保留已恢复的格式。
2. 点击“导出配置（JSON + C）”。默认在 `HostApp/processing_configs/` 使用 `imgt_processing_config_YYMMDD_HHMM.json` 保存（北京时间），对应 C 副本同目录；项目内 `EmbedCode/TC4ImageProcessingConfig.h` 自动更新。框选产生的 HSL 与手动修改的 HSL 使用同一导出流程。
3. **只覆盖嵌入式工程的 `TC4ImageProcessingConfig.h`，然后重新编译/烧录。无需修改任何其它图像算法文件。**

颜色列表、阈值和输出数组大小在编译时确定，不是运行时热更新。改输出尺寸后，调用方数组必须通过宏声明，不能把 240 或颜色数量硬编码在数组或循环里。

## 公共接口速查

| 接口 | 输入/输出与返回值 |
| --- | --- |
| `Camera_RenderIpmRgb565(raw, output)` | 原始 RGB565 → 鸟瞰 RGB565；有效调用返回 true；投影无效像素填黑；错误参数返回 false |
| `Camera_GetIpmViewConfig(&view)` | 读取配置的范围和尺寸；NULL 不操作 |
| `Camera_RawPixelToGround(u, v, &point)` | 原始像素 → 地面；false 表示无效，不能使用输出 |
| `Camera_GroundToRawPixel(x, y, &u, &v)` | 地面 → 原始小数像素，不进行采样；false 表示无效 |
| `ColorDetection_ProcessImage(raw, &result)` | 返回有效颜色组件总数；没有组件或空指针返回 0，不生成预览 |
| `ColorDetection_ProcessImageWithPreview(raw, &result, preview)` | 同上，并生成 RGB888 掩码；preview 可为 NULL |
| `ColorDetection_GetThreshold(i)` | 0 起始只读阈值指针；索引越界返回 NULL |
| `ColorDetection_Rgb565ToHsl(word, &h, &s, &l)` | 按配置解释 WORD，输出 HSL；输出指针为空返回 0，否则 1 |
| `ColorDetection_ProcessCameraFrame(&result)` | 消费并清除完成标志；处理一帧返回 1，无帧/空指针返回 0，须满足 DMA 所有权约束 |
| `ColorDetection_SetWhiteBalance(value)` | 0 自动白平衡，0x65–0xA0 手动；无效参数返回 1，其余返回驱动状态 |
| `WifiImageTransfer_SubmitRgb565Frame(raw, bytes)` | 只发送原始相机尺寸的帧，CRC/标志自动生成；不是图像处理函数 |
| `WifiImageTransfer_WriteLogBytes(data, length)` | printf 重定向入队，不访问 SPI；FIFO 满时丢新字节 |
| `WifiImageTransfer_Service()` | 每次主循环调用，非阻塞提交一个日志分片 |

完整的参数、缓冲区寿命、返回值和限制说明也已写入各模块头文件。

## 推荐接入方式

```c
/* 全局/静态存储，不能放进容量不足的任务栈。头文件变化会自动调整大小。 */
static uint16 stable_raw[SCC8660_H][SCC8660_W];
static ColorDetectionResult colour_result;
/* 可选输出；不需要对应功能时，不声明、不调用，避免占用 RAM。 */
static uint16 bird_eye[CAMERA_IPM_OUTPUT_HEIGHT][CAMERA_IPM_OUTPUT_WIDTH];
static uint8 colour_preview[SCC8660_H][SCC8660_W][3];

/* 初始化相机及 WiFi 后，主循环每帧：
 * 先使用真实驱动的双缓冲/锁定/停止采集机制取得 coherent stable_raw。
 * DMA 不得在复制、处理、发送期间覆盖 stable_raw。驱动方法因工程而异。
 * 下面两个算法输入均为 stable_raw，不得相互串联。 */
(void)ColorDetection_ProcessImageWithPreview(stable_raw, &colour_result, colour_preview);
(void)Camera_RenderIpmRgb565(stable_raw, bird_eye);
WifiImageTransfer_SubmitRgb565Frame(&stable_raw[0][0], sizeof(stable_raw));
/* 每轮主循环都执行，包括无图像的一轮。 */
WifiImageTransfer_Service();
```

这里只演示库接口，不含未知驱动的采集锁实现。颜色模块共享静态工作区，非可重入，不能在多个核/任务并发调用。复制开关开启也不能单独防止 DMA 在 memcpy 过程中写入。

默认 160×128、240×240 鸟瞰图：原始稳定帧 40,960 字节；可选鸟瞰输出 115,200 字节；可选颜色预览 61,440 字节。颜色内部还需标签图、BFS 队列、底点行统计和结果空间。颜色数超过 254 或图像像素数超过 65,535 会自动加宽标签/队列类型；上限可表示不代表板端 RAM 足够。

WiFi 图像发送是同步调用，SPI 持续忙时会等待；保持原来的发送时序不等于不阻塞。日志 FIFO 不是多生产者同步队列，应串行地由 CPU0 输出日志，不要从多个核/中断同时写入。

## 跨端验证

从项目根目录运行：

```powershell
python -B -m unittest discover -s HostApp/tests -v
```

`HostApp/tests/test_embedded_parity.py` 在临时目录复制原始 C 模块，仅替换配置头和相机尺寸，实际编译后调用。Windows 需要 Visual Studio C++ Build Tools；其它平台需要 `cc`。找不到编译器时跨端测试明确跳过，并不代表已验证。

测试覆盖四种像素布局下全部 65,536 个 WORD 的 HSL、C 实际生成的图像/日志包及 CRC、逆透视逐像素、半像素取整、颜色优先级/数量限制、ROI 舍入与空 ROI、独立白板 ROI、面积和大尺寸索引、260 种颜色的头文件单独更新。测试专用驱动桩位于 `HostApp/tests/embedded_fixture`，禁止加入板端工程。
