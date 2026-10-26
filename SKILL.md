---
name: external-disk-benchmark
description: 调用 fio 对已挂载移动硬盘进行有写入预算的顺序、随机、混合及持续读写测速，自动准备 fio，用用户当前语言展示进度与剩余时间，并生成指定语言的离线 HTML 或 PDF 报告。用于实际存储性能评估和同条件对比。
---

# 移动硬盘测速

短触发词：`#diskbench`。本目录是独立分发根目录，Python 3.10+、标准库即可启动；fio 不随包分发。macOS 已有真实 full 验收；Linux USB 设备流程为实验性支持；Windows 仅有 fio 安装实现，尚无安全设备识别与测速支持，不在 Windows 上绕过保护。

## 执行

1. 确认用户选定的已挂载外置卷目录。没有明确目标时，先列候选让用户选择，不能把当前目录或系统盘当成目标。只要目标已明确就继续，不重复申请常规依赖安装授权。
2. 读取 [测速方法与平台边界](references/methodology.md)。按用户当前对话语言传 `--ui-language`，按报告语言传 `--language`，二者可不同。内置 `zh`、`en`、`ar`；其他语言先读 [本地化](references/localization.md)，翻译完整语言包，不能静默改用英文。
3. 在本 skill 根目录先预演，不下载、不安装、不写测试文件：

   ```bash
   python3 scripts/benchmark.py --target /path/to/external-volume --language zh-CN --ui-language zh-CN --dry-run
   ```

4. 展示步骤、文件占用、累计写入预算、最大时限与缓存覆盖限制。默认 full（8 GiB 文件、最多 64 GiB 累计写入），quick 为 1 GiB/8 GiB；普通测试时限 30/10 秒，持续写入 180/30 秒。用户要求限制写入量时显式覆盖参数，不把缩短测试称为默认 full。执行输出目录必须是全新路径，建议在其他卷上。

   ```bash
   python3 scripts/benchmark.py --target /path/to/external-volume --output /path/to/new-results --language zh-CN --ui-language zh-CN --profile full
   ```

   自动检查 fio 身份并安装：macOS 优先现有 Homebrew；Linux 使用现有系统包管理器；无包管理器且已有编译工具时使用固定版本、校验 SHA256 的上游源码构建。无编译工具或无管理员权限时报告具体限制，不静默安装 Homebrew/整套工具链，不让用户在聊天中提供密码。详情见 [安装与命令](references/cli.md)。

5. 将脚本 stderr 中的本地化进度持续转达给用户，不仅等待最终结果；以 `progress.jsonl` 中的步骤、实际百分比与 ETA 为依据。短任务太快没有中间采样属于正常，不能伪造中间进度。无法估算时仍说明当前步骤与总体步骤完成度。
6. 检查 `results.json`、失败原因及报告后交付 HTML 链接。右上角按钮调用浏览器打印，用户可另存 PDF。用户要求 PDF 时传 `--format pdf`，使用已有 Chromium 导出；实际文件失败时如实返回 HTML 和手动保存方法。

## 每次重新识别硬件

设备名称属于本次运行结果，禁止写死或从上一次报告继承。脚本在每次实际运行时重新读取硬件清单，按本次目标卷对应的物理设备标识匹配硬盘；不能取系统清单的第一块盘或拿内置盘型号填外置盘。当前电脑型号也重新读取。

macOS 使用 system_profiler；Linux 使用 sysfs/DMI 和 lsblk。只提取型号、来源与必要桥接候选，不持久化整份硬件清单或序列号。硬盘盒控制器名称不等于硬盘盒商品型号；不能确认关联/商品型号时保持“未确认”，不能从网上猜成某个零售产品。

用户已明确提供的真实型号可通过 `--computer-model`、`--drive-model`、`--enclosure-model` 补充，报告标为“用户提供”，不冒充系统检测。未提供就展示检测结果或明确未知，不为填满报告编造信息。

HTML 和 PDF 均包含 assets/project.json 中的公开仓库链接和报告语言的一句话介绍。它是用户点击的超链接，不是离线报告加载的网络依赖。

## 分析与报告

原始 JSON 的数字是唯一事实来源。报告直接显示初始/末段速度、最低连续 5 秒平均速度、降速点和降速后速度；自动分析只识别持续速度变化，不能证明 SLC 缓存耗尽。缓存外速度未确认时必须说清，不给设备贴健康/不健康标签，不声称 SNIA 合规。

需要更长缓存测试时，说明额外累计写入和测试空间，再按用户目标设置 `--file-size`、`--write-budget`、`--sustained-seconds`。不自动全盘写满、擦除或无限循环。温度不支持时不得编造；macOS 可读到的 NVMe SMART 温度会每约 5 秒采样。

可只重生成报告而不测速、不安装 fio：

```bash
python3 scripts/report.py --input /path/to/results.json --output /path/to/report-copy --language en --format html
```

报告可离线阅读，不调用外部服务。公开 HTML 默认不含完整路径或序列号；原始 JSON/日志可能含路径，分享前检查。

## 中断、失败与恢复

脚本只写唯一的 `.diskbench-<run_id>/payload.bin`，不写裸盘、不覆盖用户文件。Ctrl-C 会停止本次 fio 并保留部分结果。默认将测试目录移到系统废纸篓；这不会立即释放空间。`--keep-test-files` 可明确保留原位。清理失败保留文件并报告位置，不能擅自永久删除。

错误先读 `results.json`、`*.stderr.log` 和安装日志。不要因为失败自动重跑 full；只在原因解决后重新计划必要测试，避免反复增加写入。设备断连或身份变化、无可靠目标、权限拒绝时停止写入。锁残留时先核实无活动 fio，再通过系统回收站清理对应空锁目录，不盲目解锁。

## 验证与开源边界

从本目录或它的独立副本运行：`python3 -m unittest discover -s tests -v`。真实平台证据及未验证项见项目交接；本目录自带 [README](README.md)、[许可证](LICENSE) 和 [第三方说明](THIRD_PARTY_NOTICES.md)，迁出无需父仓脚本、私有路径或 registry。调用本 skill 不包括创建 GitHub 仓库或发布的授权。
