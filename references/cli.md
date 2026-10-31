# 命令、依赖与结果

Python 3.10+。脚本只使用标准库，不需要 `.venv`。`--help`、`--dry-run` 与 `report.py` 不安装 fio、不访问网络、不跑 I/O。

`benchmark.py` 只运行一种自动完整检测。target必填，接受用户显式选定的内置/外置本地已挂载可读写目录；output必须是新目录，默认diskbench-results；language为报告语言，ui-language为提示语言，Agent明确传当前对话语言；format为html/pdf。messages/ui-messages提供完整翻译。keep-test-files保留自有文件。型号、接口事实与参考文件参数沿用，不改变工作负载。

```bash
python3 scripts/benchmark.py --target /path/to/selected-directory --output /path/to/new-results --language zh-CN --ui-language zh-CN --dry-run
```

预演不安装、不写缓存/台账/输出、不创建锁或测试文件、不调用fio。返回实际硬件、自动plan、已有授权状态与绑定目标/计划/台账revision的plan_sha256。空间/余额不足时停止，不创建象征性小负载。

## 写入授权与累计额度

去掉dry-run即可同次准备依赖、观察启动前质量、重新核空间并确认具体有限计划后执行。TTY输入RUN确认显示的目标、占用、最大写入量、保留空间和时限。Agent已有真人授权时，用有界单行标准输入对象 `{"confirm_plan_sha256":"本次真实预演摘要"}` 传递同一确认，不贴给普通用户，不把输入JSON冒真人身份验证。摘要或已有台账revision变化即拒绝，不默认yes。额度不能通过stdin或CLI调参。

私有SQLite台账位于用户.cache/drivespeed/write-allowance/write-allowance.sqlite3，与X社区状态独立。首次真实确认可建立本次显示的有限授权session；已有活动session时，即使换目录、重挂载、系统磁盘名改变或换目标，后续具体计划确认仍沿同一上限和余额，不自动新建额度。已有更低授权必须先恢复/迁移原记录，不能把缺失台账视为新64GiB许可。不得删除台账续额；需追加授权时先明确既有消耗与新有限权限，不自动续额。持续额度不以BSD/sdX瞬时设备名为域，不能把型号或卷UUID当作物理唯一ID。多个已验证既有授权只能通过内部导入/明确选择原scope记录恢复，不从设备名猜余额；新目标仍须确认本次具体计划。移除状态或更换用户不具防同权限恶意篡改保证，Agent仍须遵守真人已有累计限制。

首写前在设备锁内原子预留全部计划写量；完整可信final计量后按实际写量退回差额。失败、取消、崩溃或尾写未知保守占整笔，不能记零或自动退额。使用有限专属工作文件，不清Trash。自动公式及读前覆盖条件见[缓存观察](cache-planning.md)。

## 安装

- 身份校验：版本形如 fio-3.x，并具备 fio 的输出/引擎/只读帮助与实际异步 I/O 引擎。Fiona 的同名 fio 不接受。
- macOS：现有 Homebrew 运行 `brew install fio`，继承 Homebrew 的来源和校验。Homebrew 缺失时使用固定 fio 3.43 源码构建，需要已有 `cc` 与 GNU make。
- Linux：设备识别需要 `findmnt`、`lsblk`，fio 需要 `libaio` 引擎；现有 apt-get/dnf/pacman 安装 fio 时，非 root 使用 `sudo -n`，不弹密码、不保存密码；仓库未包含 fio 或权限不足时停止并保留日志。不改系统软件源。系统没有 `gio trash` 时保留测试文件并报告清理失败。
- 源码后备：来自上游 GitHub fio-3.43 标签归档，固定 SHA256，解包检查路径和链接，安装到用户缓存目录。不从随机第三方下载二进制。
- Windows：固定上游 x64 MSI 和发布 SHA256 校验后通过 msiexec 安装；Windows 本地测速为实验性支持，使用 windowsaio；设备识别及运行仅经模拟测试，真实设备尚待验证。
- 受网络、管理员权限或缺编译工具影响时，代理结合原始安装日志说明准确原因，不能绕过宿主批准或静默安装整套包管理器。源码下载校验失败的文件保留诊断，不执行。

下载/安装会产生缓存，安装日志保存在本次结果目录；现有 fio 不触发联网。缓存目录为用户 `.cache/drivespeed`，不是 skill 仓库私有路径。

## 输出和失败

标准输出为 JSON 摘要或 dry-run 计划；标准错误为本地化进度。`progress.jsonl` 保存结构化阶段事件。数字按实际 I/O 或 fio 报告运行时间计算，未知剩余时间保留 null；总体百分比代表步骤完成度，不代表耗时比例。

results.json 包含 schema_version=1、target、environment、plan、cases、series、outcome、report。每项原始 `*.fio.json-stream` 包含多个连续 JSON 对象（fio status-interval 行为），不能直接当单个 JSON 文件解析；最后完整对象为最终快照。`*.case.json` 保留该项状态、命令、测量和错误。部分失败后的已记录写入量是下限，完整实际量未知时 actual_write_bytes=null，accounted_write_bytes 为已知量。

退出码：0 核心完整且无执行错误；6 核心/应有采集不完整；2 参数/语言/计划无效；3 依赖失败或引擎不支持；4 目标安全/空间/锁失败；5 运行、清理或 PDF 导出失败；130 用户取消。失败产生的部分 HTML 不代表测速完成。

Windows 仅接受能唯一识别为在线、可写、非聚集物理盘的 GPT/MBR 盘符目录，网络、虚拟或无法识别的设备、挂载文件夹与 reparse 点均拒绝；以 Windows 系统范围的命名互斥锁按物理盘身份互斥。Windows 默认保留专属测试文件，需用户自行处理，空间不会自动释放；持续写入不提前发送 POSIX 信号，而是在预算或时限内结束。Windows 社区投稿与横评也为实验性实现，私有缓存须位于支持持久 ACL 的本地卷（如 NTFS）；原生权限、锁及 SQLite 尚待实机验收。

输出与目标路径不接受符号链接组件。以物理设备为单位加锁，APFS 多卷共享物理盘也互斥；Linux通过findmnt/lsblk机器可读依赖树识别本地NVMe/SATA/USB及LVM/RAID等底层盘，实验性支持；已知网络块传输（如iSCSI、NBD和NVMe-over-Fabrics）拒绝；NVMe须正向识别PCIe，传输缺失须有本地sysfs证据，不能默认当作USB或物理盘。普通文件层级保护不抵御同权限恶意进程篡改，也不提供断电保证。系统盘可写用户目录可由用户明确选择；只读/无权限目录、网络盘、无法完整识别物理依赖的设备及裸盘拒绝。磁盘映像/loop仅在本地普通backing file及其宿主物理依赖可完整解析时允许，标为虚拟文件系统性能而非物理盘性能；未知映射不使用弱锁放行。新增路径已做fixture和macOS只读目录识别验证，尚未真实I/O验收。

macOS/Linux 的物理盘锁默认保存在 `~/.cache/drivespeed/device-locks`。使用同一用户缓存根的新版程序不会因不同任务的 `TMPDIR` 产生独立锁；隔离用户主目录或修改缓存根的进程不共享该命名空间。同时保留当前临时目录中的旧版锁以兼容同一 `TMPDIR` 的旧程序。使用其他 `TMPDIR` 的旧版程序不受新锁保护，不能混用版本并发测速；遇到残留锁须核实归属，不自动删锁或终止进程。Windows 仍使用原有命名互斥锁。

## 硬件与项目来源

每次实际测速重新采集 `hardware.computer / drive / enclosure`，字段为 model/source；enclosure 另有未确认关联的 bridge_candidates。缺失字段保留 null，不复用历史报告。`--computer-model`、`--drive-model`、`--enclosure-model` 可提供用户已确认的型号，source=user。system_profiler 只按 target.physical_devices 中的 BSD 标识选择硬盘，Linux 以 lsblk 匹配物理盘，Windows 使用已识别目标磁盘的系统型号；原始硬件清单和序列号不存入结果。

`assets/project.json` 拥有报告的仓库 URL。报告包含翻译后的简介及显式 HTTPS 超链接；离线打开不访问该链接，用户点击时才联网。旧结果没有 hardware 时报告标为未确认，不把当前机器补录成过去的测试机器。


## 读取 HTML/PDF 报告

HTML 先显示关键速度、硬件及持续写入表现，环境和方法使用可展开区域；打印前自动展开，打印后恢复原来的展开状态。正常完成时省略重复的状态、空原因列；存在未完成项或原因时恢复对应列。数值、失败提示和方法限制仍保留，PDF 包含完整参数。

AI Agent 优先读取 `results.json` 的原始数值。读取 HTML 时可用 `#results` / `#measurements` 定位表格，`data-metric` 定位摘要与持续写入指标，`dt` / `dd` 对应标签和值；折叠区域不等于缺失数据。输出 JSON 和测速数据格式不因布局变更而改变。


自动持续阶段的合法字节端点或具证稳定收尾与核心完整性分开判断；没有缓存数字本身不失败。硬盘盒商品型号用--enclosure-model，主控型号用--controller-model，只填本次用户已确认的事实。

参考数据（可选）：benchmark.py 和 report.py 均支持 `--reference-file /path/to/references.json`。文件由 AI 根据实际打开的官网或可信评测整理，使用报告语言；JSON 校验和当前型号匹配不代表脚本独立核实了网页事实。缺少参考时照常生成报告。详见 [参考数据研究](reference-research.md)。


网络参考本地缓存位于用户 `.cache/drivespeed/references/<硬件组合>/<语言>.json`（或 XDG_CACHE_HOME）。默认 30 天复核，读取/翻译不更改原查阅日期。`--dry-run` 返回本次硬件、reference_cache 状态及其他已缓存语言，不写缓存；有新参考文件的正式执行/报告生成会保存公开参数。`--refresh-references` 绕过缓存，由 AI 重新核查后提供文件，Python 不暗中联网。过期离线缓存可展示并标为待更新；测量结果不进入此缓存。


面向普通读者的“日常使用解读”从本次 results.json 动态计算：10GB 文件耗时仅为吞吐换算，不是实测复制时间；QD1/QD16 对照只展示同一设备内、块大小匹配且已完成的测试，不称为软件提速。缺项省略对应换算，不能复用开发者机器的解读或补造分数。HTML 的完整测试表和指标说明默认折叠，PDF 打印会展开并保留所有数值。

## 社区与成对报告

社区命令为 `python3 scripts/community.py --help`，完整的 prepare/preview/consent/publish/retry/receipt/compare/withdraw/list/remember 流程及退出码见 [社区工具](community.md)。离线准备/比较不安装 fio、不联网、不改原结果；只有明确同意后才授权投稿。

`report.py --comparison-id ID --community-state-dir /path/to/private-state` 额外生成 `comparison.html`，PDF 模式同时生成 `comparison.pdf`；两份报告读取同一 typed 比较版本。`--evaluation-stdin` 从标准输入读取有界、run_id/语言绑定的评价对象，不创建 JSON 缓存，与旧 `--evaluation-file` 互斥。HTML/PDF 不自动刷新远端样本；网络刷新须经社区命令显式发起。


新增 hardware.links 保存目标关联的最少链路元数据；旧结果缺失时报告显示未知，不补录当前机器。报告保留硬件链路/官方规格、五项原始速度和整份三维五级评价；质量unknown/nonquiet不作设备星级或瓶颈归因；可选参考条件与分界见[硬件与五级评价](hardware-rating.md)。schema_version仍为1；新方法drivespeed-auto-full-v1必须携带公开quality.status，旧drivespeed-file-v1缺省保持缺省、原hash不变。平均完成延迟以µs传递，服务端只验证结构，不宣称复验本地原始曲线。

## 本地电脑显示与接口事实

`report.py --show-computer-model` 仅控制本地 A/B HTML/PDF；缺省隐藏检测值、用户补充电脑型号及自动引用。显示选项不进入结果/投稿/摘要，同一结果可重新导出。原电脑事实仍可在本次投稿中内部保存，公共读取把 computer 固定为 null/unknown 并移除相关电脑依据；公共记录不是原上传字节副本。

`benchmark.py --interface-name NAME` 接受用户已确认的 1..80 字符接口事实，写入 `hardware.access_interface`，保留系统 `target.protocol`；上传使用该 name 作为原始 `storage.connection`，因此属于新投稿内容和摘要的一部分，与显示选项不同。普通 user_input 不自动覆盖它。已有冻结报告不能沿旧 ID/同意改变接口名称；测量后补充只在用户确认的同 run 派生副本上进行，原始 fio 与旧 raw 保持。

`python3 scripts/interface_types.py --name NAME --language en` 离线识别明确别名；`--catalog-stdin` 可从标准输入读取当前公开接口字典，结果只留在内存/标准输出。未知原名保留，不猜测；不因归一化启动测速或模型。
# 离线产品资料快照

`report.py --product-profile /absolute/product-profile.json` 可附带独立的产品资料快照；输入遵循 `assets/community/product-profile.schema.json`，候选准备输入由相邻 `product-profile-candidates.schema.json` 定义。此参数不联网，不重新测速，也不修改 `results.json`、投稿或原摘要。

显式缺失、非法或设备身份不符的资料会非零退出，不忽略输入或覆盖已有报告。关联首先需要已保存的 `physical` 范围和唯一物理设备；多盘、虚拟、未解析或显式未确认的匹配型号不能降级使用展示名称。旧结果未保存匹配字段时，也只有明确单一物理目标才能使用旧型号。随后核对准确原型号/已核实别名，以及型号中的明确十进制 GB/TB 容量或独立 `nominal_capacity_bytes` 硬件事实；文件系统总量/可用空间不能证明商品容量。有效资料另存为输出目录的 `product-profile.json`，返回相应 artifact 路径；省略参数保持旧导出方式。

HTML/PDF 的报告对象、硬盘名称及文档标题使用已核实商品名，硬件表另保留系统原型号。设备资料按官方资料、独立评测分组，展示全部已核实来源的名称、原链接、发布者、适用范围和核对日期；同日可合并日期，不同日逐项保留。无有效链接时省略整个来源区，可靠商品名仍保留。这些资料不改变性能评分或测量值；主控系列标识不据此升级为精确芯片型号。网站是否已支持对应展示须以其实际部署版本为准。

Web 维护命令的 `product_profile_import` 是离线 DB-only 资料导入，不是模型调用或网页抓取。`GET /api/v1/benchmarks/ID/product-profile` 仅对当前可公开报告读取资料；未关联时返回 null，隐藏/撤回报告不提供该资源。
