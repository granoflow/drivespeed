# DriveSpeed

简体中文 · [English](README.en.md)

移动硬盘测速（DriveSpeed）

你的移动硬盘几斤几两？现在就来测一下吧。

Wondering how quick your portable drive really is? Take it for one run and find out.

DriveSpeed 是独立的存储性能与诊断项目，当前提供命令行、Agent Skill 和本地 HTML/PDF 报告。向 AI 提出测速请求即可使用。`#diskbench`

## 安装与运行

通过 Skills CLI 安装（需 Node.js 22.20+；测速脚本本身不需要 Node.js）：

```bash
npx skills add granoflow/drivespeed --skill drivespeed
```

按提示选择 Agent。默认安装到当前项目；需要跨项目使用时加 `-g`。安装后可以说：**“用 DriveSpeed 测试我指定的移动硬盘，先展示写入预算，报告用中文。”**

未安装时，把仓库地址 `https://github.com/granoflow/drivespeed` 给 Agent。只有已加载此 skill 的 Agent 才能稳定按名称识别；公开发布并不让所有 Agent 自动安装或认识它。

[Skills CLI](https://github.com/vercel-labs/skills) · [Agent Skills 格式](https://agentskills.io/specification)


将**整个目录**复制到支持 Agent Skills 的宿主的 skills 目录，目录名保持 `drivespeed`；无需 skills 父仓库。向代理说明目标硬盘和语言即可。也可在本目录直接运行，需 Python 3.10+：

```bash
python3 scripts/benchmark.py --target /path/to/external-volume --language zh-CN --ui-language zh-CN --dry-run
python3 scripts/benchmark.py --target /path/to/external-volume --output /path/to/new-report --language zh-CN --ui-language zh-CN --profile quick
```

缺少 fio 时自动通过现有可信包管理器安装；没有包管理器但已有 C 编译器和 GNU make 时，从固定校验和的上游源码构建。不会安装整套开发环境；受权限或网络限制时给出错误及日志。Python 脚本无第三方依赖，不使用 `pip install fio`。

macOS 为已验收平台（真实 full 集成检查）；Linux USB 测速为实验性支持；Windows 目前仅实现上游 fio 安装，测速会因设备保护尚未实现而拒绝。不要将本项目描述成所有平台均已验收。

## 测什么

顺序读写、4 KiB 随机读写、QD1/QD16、70/30 混合读写、持续写入与后续读取。报告含 MB/s、IOPS、p50/p95/p99 延迟、实际队列深度、测试参数、原始结果和持续速度曲线/数值。初始化也计入写入预算。

| 档位 | 文件空间 | 累计写入上限 | 普通项时限 | 持续写入时限 |
| --- | --- | --- | --- | --- |
| quick | 1 GiB | 8 GiB | 10 秒 | 30 秒 |
| full（默认） | 8 GiB | 64 GiB | 30 秒 | 180 秒 |

各项会在达到字节上限或时间上限时结束。初始化必须真实写满测试文件，否则不执行读取测试。空间至少保留 max(1 GiB, 文件大小的 25%)。完整计划和最长时限由 dry-run 输出。

报告不把“持续降速”当成“缓存耗尽已确认”。它直接列出末段与降速后的速度，但缓存外速度始终需要额外证据；受限写入不保证耗尽所有设备的动态缓存。专业方法的参考和差异见 [methodology](references/methodology.md)。

## 硬件型号

每次运行都重新检测当前测试电脑及目标硬盘，不写死设备名称。macOS 通过 system_profiler 的 BSD 标识精确匹配目标物理盘；Linux 使用 sysfs/DMI、lsblk。硬盘盒分为商品型号与主控型号两行；未确认的桥接信息只作为主控候选，不冒充商品型号或已确认芯片。

可显式补充真实型号：`--computer-model "电脑型号" --drive-model "硬盘型号" --enclosure-model "硬盘盒型号"`。报告将这些值标为用户提供。型号检测失败不影响有安全目标身份的测速；不采集/公开序列号。

## 报告与本地化

默认生成自包含 `report.html`。 关键速度在页首先展示，环境与方法可展开查看；打印 PDF 时自动展开，保留完整参数。右上角“下载 PDF”打开浏览器打印窗口，选择“另存为 PDF”。`--format pdf` 尝试调用系统已有 Chromium 浏览器，不额外下载浏览器。导出失败会保留 HTML 并返回失败状态。

内置中文、英文和阿拉伯语。代理可按 [localization](references/localization.md) 翻译完整语言包，支持其他用户语言。提示语言和报告语言分别控制，不能仅翻译报告标题。脚本帮助及原始上游日志采用英文，代理应以用户语言解释。

公开 HTML 隐藏完整路径和序列号。原始结果和日志仍可能包含本地路径，分享前检查。测试会产生实际写入；不进行全盘擦除。可选读回校验用于发现测试区域的数据完整性异常，不代替完整容量或商品真伪鉴定。默认测试文件移入系统废纸篓，清空前不释放空间；清理失败保留原位。不要在断连、供电不稳或目标不明确时运行。

## 开发

```bash
python3 -m unittest discover -s tests -v
```

原始 fio 不打包进本项目，通过独立进程运行。项目代码使用 MIT；fio 使用 GPL-2.0-only，见 [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES.md)。本目录是独立 Git 仓库根，不依赖共享 skills 仓库。`scripts/` 维护实现，`tests/` 维护验证，`references/` 维护使用方法，`assets/` 维护报告资源，`SKILL.md` 保留代理入口。本地报告和交接资料位于 Git 忽略的 `docs-temp/`。

项目地址：[granoflow/drivespeed](https://github.com/granoflow/drivespeed)。HTML 和 PDF 均保留可点击项目链接及一句话介绍。

可选的 GitHub Actions 模板在 [references/ci-workflow.yml](references/ci-workflow.yml)，当前未启用。拥有 workflow 写入权限时可放入 `.github/workflows/tests.yml`；模板只跑隔离测试，不安装 fio 或执行真实硬盘负载。


## 缓存容量与缓存后速度

quick/full 是有预算的基线测试，短时末段速度不代表缓存后速度。完整评估需另外运行 `--profile cache`：新文件单遍持续写入，观察降速后再等待 30 秒，并取得 60 秒稳定窗口，输出本次可用写缓存容量（GiB）和缓存后速度（MB/s）的估算。降速原因仍受温度、接口等条件影响，估算不等于固定物理容量。

先 dry-run 再决定实际写入：缓存专项写入量按资料/知识估计加余量制定，无估计时才以 1 TiB 兜底；默认 30 分钟，按空闲空间缩减并保留安全余量。它会产生大量实际写入，不能沿用短测预算。达到预算或时限而未取得结果会明确标为部分完成（退出码 6），不无限写盘。旧报告可重生成界面，但补出缓存数据必须重测。


缓存规划遵循“执行模型网络搜索 → 模型已有知识 → 1 TiB 兜底”，详见 [缓存规划](references/cache-planning.md)。有来源/知识估计时通过 `--cache-estimate-file` 输入，再按估计缓存量加稳定采样量与余量制定文件；1 TiB 仅是无可用估计时的兜底，不是硬盘缓存参数。所有参考值与实测值分别记录。


报告顶部展示顺序读写、4 KiB QD1 随机读写、持续写入末段速度和可用写缓存；“末段”只是测试最后一段采样的平均值，不代表缓存后速度。缓存尚未测出时明确标记，不借用官网数字填实测。

AI 有网络能力时优先查官网和可信实测，覆盖硬盘/硬盘盒/主控/电脑端口及同规格设备，并保留出处和条件。通过 `--reference-file /path/to/references.json` 添加可点击来源的参考对比，缺项时只显示有依据的内容，不影响基础报告。输入和可信程度规则见 [参考数据研究](references/reference-research.md)。


网络参考资料缓存在用户本机，按硬件组合和语言区分，默认 30 天复核。相同设备重复测试优先复用；缺失、过期、环境变化或 `--refresh-references` 时由 AI 补查/复核。离线可显示旧数据并标记待更新。缓存不随 skill 分发，不包含本机实测结果，也不把开发者设备参数当成他人的默认参考。

## 可选诊断

- `--profile mapping`：比较不同地址范围的随机访问和重复读取表现。
- `--profile cache --recovery-seconds 300`：持续写入后空闲，再测恢复表现。
- `--profile integrity`：写入后完整读回校验；加 `--fill-free-space` 尽量覆盖剩余空间，并保留安全余量。

这些专项默认不启用，先预演并确认写入预算。参数与解释见 [专项测试](references/diagnostics.md)。

## 安装兼容性验收

2026-10-26：使用 Skills CLI 1.7.4，从公开 GitHub 仓库在隔离项目中安装到 Codex、Claude Code、Cursor 所使用的目录。安装副本的脚本、参考资料及资源逐文件核对一致，两个 CLI 帮助入口和 69 项测试通过。此验收证明安装包完整，不代表各 Agent、各操作系统均已完成真实硬盘测速。
