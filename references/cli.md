# 命令、依赖与结果

Python 3.10+。脚本只使用标准库，不需要 `.venv`。`--help`、`--dry-run` 与 `report.py` 不安装 fio、不访问网络、不跑 I/O。

`benchmark.py` 参数：target 必填，接受已存在的外置卷目录；output 为全新输出目录，默认当前目录下 `diskbench-results`；profile 为 quick/full，默认 full；language 指报告语言，ui-language 指提示语言，直接运行时均采用系统 locale，代理须明确传当前对话语言；format 为 html/pdf，默认 html。messages/ui-messages 分别指定完整翻译 JSON。

file-size/write-budget 接受正整数字节或 KiB/MiB/GiB/TiB，4 KiB 对齐；文件大小至少 1 MiB 且 MiB 对齐。seconds/sustained-seconds 为正整数秒。文件上限和预算必须同时满足初始化与各项工作量。keep-test-files 保留测试文件，不调用系统废纸篓。

## 安装

- 身份校验：版本形如 fio-3.x，并具备 fio 的输出/引擎/只读帮助与实际异步 I/O 引擎。Fiona 的同名 fio 不接受。
- macOS：现有 Homebrew 运行 `brew install fio`，继承 Homebrew 的来源和校验。Homebrew 缺失时使用固定 fio 3.43 源码构建，需要已有 `cc` 与 GNU make。
- Linux：现有 apt-get/dnf/pacman，非 root 使用 `sudo -n`，不弹密码、不保存密码；仓库未包含 fio 或权限不足时停止并保留日志。不改系统软件源。
- 源码后备：来自上游 GitHub fio-3.43 标签归档，固定 SHA256，解包检查路径和链接，安装到用户缓存目录。不从随机第三方下载二进制。
- Windows：固定上游 x64 MSI 和发布 SHA256 校验后通过 msiexec 安装；当前无 Windows 设备保护，因此 benchmark 入口在安装之前就会拒绝测速。此路径仅为将来平台实现保留，不声明已验收。
- 受网络、管理员权限或缺编译工具影响时，代理结合原始安装日志说明准确原因，不能绕过宿主批准或静默安装整套包管理器。源码下载校验失败的文件保留诊断，不执行。

下载/安装会产生缓存，安装日志保存在本次结果目录；现有 fio 不触发联网。缓存目录为用户 `.cache/external-disk-benchmark`，不是 skill 仓库私有路径。

## 输出和失败

标准输出为 JSON 摘要或 dry-run 计划；标准错误为本地化进度。`progress.jsonl` 保存结构化阶段事件。数字按实际 I/O 或 fio 报告运行时间计算，未知剩余时间保留 null；总体百分比代表步骤完成度，不代表耗时比例。

results.json 包含 schema_version=1、target、environment、plan、cases、series、outcome、report。每项原始 `*.fio.json-stream` 包含多个连续 JSON 对象（fio status-interval 行为），不能直接当单个 JSON 文件解析；最后完整对象为最终快照。`*.case.json` 保留该项状态、命令、测量和错误。部分失败后的已记录写入量是下限，完整实际量未知时 actual_write_bytes=null，accounted_write_bytes 为已知量。

退出码：0 成功；2 参数/语言/计划无效；3 依赖失败或引擎不支持；4 目标安全/空间/锁失败；5 运行、清理或 PDF 导出失败；130 用户取消。失败产生的部分 HTML 不代表测速完成。

输出与目标路径不接受符号链接组件。以物理设备为单位加锁，APFS 多卷共享物理盘也互斥；Linux 当前只接受可正向识别的 USB 磁盘。普通文件层级保护不抵御同权限恶意进程篡改，也不提供断电保证。系统盘、网络盘、未知设备和裸盘拒绝。

## 硬件与项目来源

每次实际测速重新采集 `hardware.computer / drive / enclosure`，字段为 model/source；enclosure 另有未确认关联的 bridge_candidates。缺失字段保留 null，不复用历史报告。`--computer-model`、`--drive-model`、`--enclosure-model` 可提供用户已确认的型号，source=user。system_profiler 只按 target.physical_devices 中的 BSD 标识选择硬盘，Linux 以 lsblk 匹配物理盘；原始硬件清单和序列号不存入结果。

`assets/project.json` 拥有报告的仓库 URL。报告包含翻译后的简介及显式 HTTPS 超链接；离线打开不访问该链接，用户点击时才联网。旧结果没有 hardware 时报告标为未确认，不把当前机器补录成过去的测试机器。
