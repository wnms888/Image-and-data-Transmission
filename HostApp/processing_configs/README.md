# 图像处理配置目录

- 手动导出的 JSON 默认名：`imgt_processing_config_YYMMDD_HHMM.json`，时间为北京时间。
- 同一分钟再次导出已有名字时，默认使用 `_02`、`_03` 等后缀，避免覆盖之前的调参记录。
- 对应的 `*_embedded_config.h` 与 JSON 放在同一目录；`EmbedCode/TC4ImageProcessingConfig.h` 仍自动同步。
- `last_processing_config.json` 是退出时自动保存、下次启动恢复的状态文件，保持固定名称。
- 老版本位于 `HostApp/` 的配置仍能读取，不会自动移动或删除；正常退出后将参数保存至本目录。
- 导出/导入对话框默认打开本目录，仍允许手动选择其它位置。
