# External Disk Benchmark

一个可独立分发的 Agent Skill：使用 fio 对移动硬盘做受限读写测试，显示用户语言的步骤、进度和预计剩余时间，生成离线 HTML/PDF 报告。`#diskbench`

## 安装与运行

将**整个目录**复制到支持 Agent Skills 的宿主的 skills 目录，目录名保持 `external-disk-benchmark`；无需 skills 父仓库。向代理说明目标硬盘和语言即可。也可在本目录直接运行，需 Python 3.10+：

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

每次运行都重新检测当前测试电脑及目标硬盘，不写死设备名称。macOS 通过 system_profiler 的 BSD 标识精确匹配目标物理盘；Linux 使用 sysfs/DMI、lsblk。硬盘盒商品型号若不能可靠识别，会明确写“未确认”；控制器候选单独标注，不能冒充商品型号。

可显式补充真实型号：`--computer-model "电脑型号" --drive-model "硬盘型号" --enclosure-model "硬盘盒型号"`。报告将这些值标为用户提供。型号检测失败不影响有安全目标身份的测速；不采集/公开序列号。

## 报告与本地化

默认生成自包含 `report.html`。右上角“下载 PDF”打开浏览器打印窗口，选择“另存为 PDF”。`--format pdf` 尝试调用系统已有 Chromium 浏览器，不额外下载浏览器。导出失败会保留 HTML 并返回失败状态。

内置中文、英文和阿拉伯语。代理可按 [localization](references/localization.md) 翻译完整语言包，支持其他用户语言。提示语言和报告语言分别控制，不能仅翻译报告标题。脚本帮助及原始上游日志采用英文，代理应以用户语言解释。

公开 HTML 隐藏完整路径和序列号。原始结果和日志仍可能包含本地路径，分享前检查。测试会产生实际写入；不进行全盘擦除或容量真伪鉴定。默认测试文件移入系统废纸篓，清空前不释放空间；清理失败保留原位。不要在断连、供电不稳或目标不明确时运行。

## 开发

```bash
python3 -m unittest discover -s tests -v
```

原始 fio 不打包进本项目，通过独立进程运行。项目代码使用 MIT；fio 使用 GPL-2.0-only，见 [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES.md)。当前目录可直接作为未来独立仓库根，不含父仓专用配置或 Git submodule。

项目地址：[granoflow/external-disk-benchmark](https://github.com/granoflow/external-disk-benchmark)。HTML 和 PDF 均保留可点击项目链接及一句话介绍。

可选的 GitHub Actions 模板在 [references/ci-workflow.yml](references/ci-workflow.yml)，当前未启用。拥有 workflow 写入权限时可放入 `.github/workflows/tests.yml`；模板只跑隔离测试，不安装 fio 或执行真实硬盘负载。
