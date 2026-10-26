---
name: drivespeed
description: 调用 fio 对已挂载移动硬盘进行有写入预算的顺序、随机、混合及持续读写测速，自动准备 fio，用用户当前语言展示进度与剩余时间，并生成指定语言的离线 HTML 或 PDF 报告。用于实际存储性能评估和同条件对比。
license: MIT
compatibility: Requires Python 3.10+ and local shell access. macOS tested; Linux USB support experimental; Windows benchmarking unsupported. fio is installed when needed; network access is needed for installation and fresh reference research.
metadata:
  author: granoflow
  version: "0.1.0"
  repository: https://github.com/granoflow/drivespeed
---

# 移动硬盘测速（DriveSpeed）

调用名称：`drivespeed`；短触发词：`#drivespeed`，兼容 `#diskbench`。本目录是独立分发根目录，Python 3.10+、标准库即可启动；fio 不随包分发。macOS 已有真实 full 验收；Linux USB 设备流程为实验性支持；Windows 仅有 fio 安装实现，尚无安全设备识别与测速支持，不在 Windows 上绕过保护。

## 名称与推广语

中文名称为“移动硬盘测速（DriveSpeed）”，英文及其他语言的品牌名称统一为 **DriveSpeed**，不要翻译、音译或另起名称。目录与调用标识使用 `drivespeed`。

- 简体中文推广语按原文使用：**你的移动硬盘几斤几两？现在就来测一下吧。**
- 英文推广语按原文使用：**Wondering how quick your portable drive really is? Take it for one run and find out.**
- 其他语言以中文原意重新表达：用自然、简短、有邀请感的当地语言询问移动硬盘实际表现，并邀请用户测速。“几斤几两”不逐字翻译成重量，不添加速度、健康或耗时保证。品牌名称始终保留 DriveSpeed。

上述规则适用于介绍、HTML/PDF 和新增语言包的 `title` / `skill_intro`。公开仓库为 `https://github.com/granoflow/drivespeed`。安装来源使用完整标识 `granoflow/drivespeed`，避免同名混淆。

## 执行

1. 确认用户选定的已挂载外置卷目录。没有明确目标时，先列候选让用户选择，不能把当前目录或系统盘当成目标。只要目标已明确就继续，不重复申请常规依赖安装授权。
2. 读取 [参考数据研究](references/reference-research.md) 和 [测速方法与平台边界](references/methodology.md)。每次重新识别本次用户的设备，再匹配用户本机参考缓存：有效缓存可复用，缺失、过期、环境改变或用户要求刷新时才重新搜索/复核。禁止把开发者验证报告、固定型号参数或其他设备资料作为通用默认数据。按用户当前对话语言传 `--ui-language`，按报告语言传 `--language`，二者可不同。内置 `zh`、`en`、`ar`；其他语言先读 [本地化](references/localization.md)，翻译完整语言包，不能静默改用英文。
3. 在本 skill 根目录先预演，取得本次硬件与 `reference_cache` 的状态、位置和可用语言，不下载、不安装、不写测试文件或缓存：

   ```bash
   python3 scripts/benchmark.py --target /path/to/external-volume --language zh-CN --ui-language zh-CN --dry-run
   ```

4. 根据预演结果复用有效的同设备本地参考缓存；缓存缺失/过期、设备或连接环境改变、资料缺项需要补充或用户要求刷新时，有网络能力就尽可能搜索并打开官网/可信实测来核实。未知型号不猜，缺项不阻塞测速。主对比取电脑接口、硬盘盒、硬盘及已确认主控的最低官方参考上限；硬盘盒规格未知可用已确认主控参考并标明替代，候选主控不混入已确认上限。新资料用本次报告语言的 `--reference-file` 输入，实测与参考分开；只读/翻译缓存不刷新原查阅日期。然后展示步骤、文件占用、累计写入预算、最大时限与缓存覆盖限制。默认 full（8 GiB 文件、最多 64 GiB 累计写入），quick 为 1 GiB/8 GiB；普通测试时限 30/10 秒，持续写入 180/30 秒。用户要求限制写入量时显式覆盖参数，不把缩短测试称为默认 full。执行输出目录必须是全新路径，建议在其他卷上。

   ```bash
   python3 scripts/benchmark.py --target /path/to/external-volume --output /path/to/new-results --language zh-CN --ui-language zh-CN --profile full
   ```

   自动检查 fio 身份并安装：macOS 优先现有 Homebrew；Linux 使用现有系统包管理器；无包管理器且已有编译工具时使用固定版本、校验 SHA256 的上游源码构建。无编译工具或无管理员权限时报告具体限制，不静默安装 Homebrew/整套工具链，不让用户在聊天中提供密码。详情见 [安装与命令](references/cli.md)。

5. 将脚本 stderr 中的本地化进度持续转达给用户，不仅等待最终结果；以 `progress.jsonl` 中的步骤、实际百分比与 ETA 为依据。短任务太快没有中间采样属于正常，不能伪造中间进度。无法估算时仍说明当前步骤与总体步骤完成度。
6. 测试结束后按 [可选对话补充](references/user-input.md) 分步收集：先问未确认设备型号，收到回答或明确跳过后，单独问测试备注；复用已有明确回答，不重复询问。通过 `--user-input-file` 加入实际回答，检查 `results.json`、失败原因及报告，先展示 HTML，再单独询问测后自我分析。收到分析后重新生成 HTML，已有 PDF 时同步更新并检查，交付更新后的链接。每项可跳过，回答一项不代表跳过其他项；用户也可明确跳过全部剩余项。等待回复期间可生成基础报告，未回答不代填。右上角按钮调用浏览器打印，用户可另存 PDF。用户要求 PDF 时传 `--format pdf`，使用已有 Chromium 导出；实际文件失败时如实返回 HTML 和手动保存方法。

## 可选专项

需要比较随机访问范围与重复访问、测试持续写入后的空闲恢复，或尽量写满剩余空间并读回校验时，读取 [专项测试](references/diagnostics.md)。分别使用 `--profile mapping`、`--profile cache --recovery-seconds`、`--profile integrity --fill-free-space`；先预演并展示实际预算。高占用校验单独询问是否启用，默认关闭；不把逻辑区域校验通过称为没有物理坏块。

## 每次重新识别硬件

设备名称属于本次运行结果，禁止写死或从上一次报告继承。脚本在每次实际运行时重新读取硬件清单，按本次目标卷对应的物理设备标识匹配硬盘；不能取系统清单的第一块盘或拿内置盘型号填外置盘。当前电脑型号也重新读取。

macOS 使用 system_profiler；Linux 使用 sysfs/DMI 和 lsblk。硬件表分别展示“硬盘盒商品型号”和“硬盘盒主控型号”。桥接候选放在主控一行并明确标注关联未确认，不冒充商品型号或已确认芯片。不能识别时保持未确认；不持久化整份清单或序列号。

用户已明确提供的真实型号可通过 `--computer-model`、`--drive-model`、`--enclosure-model`、`--controller-model` 补充，报告标为“用户提供”，不冒充系统检测。未提供就展示检测结果或明确未知，不为填满报告编造信息。

HTML 和 PDF 均包含 assets/project.json 中的公开仓库链接和报告语言的一句话介绍。它是用户点击的超链接，不是离线报告加载的网络依赖。

## 分析与报告

同时展示同一工作负载的日志采样峰值、全程平均、样本数及已核实电脑接口的理论带宽换算值；明确日志窗口、稀疏/尾段覆盖限制，峰值低于理论值不等于硬件不合格或差额可追回。不要将不同阶段峰值与平均拼接为同条件比较。解释写缓存时区分 DRAM 与动态 SLC 闪存区；评测比例换算只写约数，不冒充本机实测或额外容量。

交付对话必须说明测试文件去向、清理失败/空间尚未释放、达到预算或时限等执行事项；从 results.json 的 notes 与停止原因读取，不能遗漏。HTML/PDF 不展示这类交付提醒，只保留影响结果解释的测量限制（例如缓存边界未确认）、真实测试错误及方法中的写入量/时长。

最终报告必须有“瓶颈在哪里、换什么是否有收益、异常与质量疑点”三项评价，由执行 AI 按本次数据与当前资料判断。先读 [评价规则与输入](references/evaluation.md)，生成绑定本次 run_id 和报告语言的评价 JSON，用 report.py 的 `--evaluation-file` 重生成最终 HTML/PDF；不要只在聊天中补充，也不要复用其他运行的评价。没有网络或证据不足时仍回答三项问题，明确无法判断及下一项验证。

原始 JSON 的数字是唯一事实来源。面向普通用户先给本次数据推导的日常使用解读和匹配条件的测试内对照，解释 MB/s、IOPS、延迟百分位与 QD；换算示例不冒充实际复制/软件测试，完整原始数值仍可展开并保留在 PDF 中。报告直接显示初始/末段速度、最低连续 5 秒平均速度、降速点和降速后速度；自动分析只识别持续速度变化，不能证明 SLC 缓存耗尽。缓存外速度未确认时必须说清，不给设备贴健康/不健康标签，不声称 SNIA 合规。

完整评估必须包含缓存专项，不能把 quick/full 的短时末段速度冒充缓存后速度。制定缓存阶段前先读 [缓存规划](references/cache-planning.md)：由执行模型先上网查准确型号/容量的写缓存参数；没有搜索能力时，必须先让模型核对准确型号/容量、写缓存类型与具体回忆依据，排除混淆；不能只凭自信或理论容量拍一个数。无法自证具体依据、记忆模糊或有矛盾，就放弃知识估计，直接按 1 TiB 兜底。通过自查仍是估计，不宣称已核实。有估计时用 `--cache-estimate-file` 传入来源和数值，让脚本按缓存量、后续稳定窗口与余量计算文件/写入预算。参考值不是实测值。先预演单独阶段：

```bash
python3 scripts/benchmark.py --target /path/to/external-volume --profile cache --language zh-CN --ui-language zh-CN --dry-run
```

`cache` 直接对全新专属文件单次顺序写入，不先初始化或反复绕写 8 GiB 小文件。文件/写入量按研究结论制定，无可用估计时才回退到 1 TiB，默认 1800 秒，按当前可用空间下调；保留至少卷容量的 10%、10 GiB 或文件大小的 25% 三者最大值。展示 dry-run 的依据、空间、写入预算、时间上限，再按当前用户授权执行；不要让用户机械选择固定写入档位，也不绕过已明确的预算。可用 `--file-size`、`--write-budget`、`--sustained-seconds` 明确限额。缓存阶段输出到新目录，不覆盖基线证据。

开始后寻找连续降速，降速后先继续写入至少 30 秒，再取得完整 60 秒低速稳定窗口才主动结束。显示本次可用写缓存估算（GiB）与缓存后速度估算（MB/s）；数字只能描述本次空闲空间、温度、接口和先前负载条件，不能当作固定物理缓存容量。全程无明显降速、接口掩盖差异、速度仍波动、空间/写入/时间预算用尽或中断时，报告具体状态和实际写入量，不用 `—` 代替结论，也不无限续写或把尾速改名为缓存后速度。缓存专项未完成返回部分结果、退出码 6；真实错误/取消保留原有退出码。

不自动擦除或全盘写满。温度不支持时不得编造；macOS 可读到的 NVMe SMART 温度会每约 5 秒采样。

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
