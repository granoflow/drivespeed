# 命令、依赖与结果

Python 3.10+。脚本只使用标准库，不需要 `.venv`。`--help`、`--dry-run` 与 `report.py` 不安装 fio、不访问网络、不跑 I/O。

`benchmark.py --stage quick|full` 选择快速或深入测速。CLI省略stage仍默认full以兼容旧脚本；Skill首次必须显式quick，交付真实快速结果后按资料对照建议是否深入。无需full时不例行邀请长测；用户主动要求仍按有限计划执行。深入报告交付后，可按资格另行授权容量与读写完整性检查。`--disk` 传用户所选磁盘的挂载路径，旧 `--target` 保持兼容；用户只选磁盘名称，挂载路径由 Agent 解析，不要求用户输入目录。系统盘根不可写时选同盘可写用户目录，不提权；output必须是新目录，默认diskbench-results；language为报告语言，ui-language为提示语言，默认都传当前对话语言，不另问语言；仅用户明确要求才改报告语言；format为html/pdf。messages/ui-messages提供完整翻译。keep-test-files保留自有文件。型号、接口事实与参考文件参数沿用，不改变工作负载。

```bash
python3 scripts/benchmark.py --disk /path/to/selected-volume --stage quick --output /path/to/new-results --language zh-CN --ui-language zh-CN --dry-run
```

预演不安装、不写缓存/输出/历史台账、不创建锁或测试文件、不调用fio；只返回硬件、空间与冻结计划，不是快速测速。Agent静默执行该检查，不问用户是否预演。用户已选盘并明确要求测速后，按固定quick计划正式执行，不增加一次许可问题；只查看计划的请求不授权实测。

深入测速可用 `--working-file-bytes` 指定工作文件大小，用 `--case-write-bytes` 指定每个常规写入项目的上限，均须 MiB 对齐并满足空间与总预算约束。混合项目按全部写入保守计费。两项均纳入冻结预演和确认摘要。修改大小与预算不会改变现有 full 的测试顺序，也不会增加峰值扫描。

## 峰值扫描

`--stage peak` 接受 `--propose-write-bytes`、`--working-file-bytes`、`--case-write-bytes` 和 `--peak-io-bytes` 四个字节参数，必须全部显式指定并通过同一预演确认。峰值 I/O 参数对每次扫描分别限量，计划预留四种负载在 QD16、32、64 各重复两次的最坏情况写量。先初始化文件，再测常规项目与峰值，最后持续写入；有足够稳定证据时跳过该负载的 QD64。项目采样不足、深度未达标、重复不稳定或没有确认平台时保持部分结果。峰值输出保留在本地原始结果，公共投稿沿用基础工作负载。

macOS 空间不足时，`--reuse-trashed-workspace <绝对路径>` 可指定本用户废纸篓内属于同一目标的 DriveSpeed 专属工作目录。只接受标记、目标、inode、目录内容和完整已分配容量均匹配的文件；预演绑定这些身份并计入实际空闲空间和额外 1 GiB 写入安全余量。执行时在设备锁内重新核对，初始化与覆盖仍计入写入预算；运行期间监控空闲空间，触及余量即停止。文件直接留在废纸篓，不恢复其他文件、不清空废纸篓。仅 peak 且必须给出与原文件完全一致的 `--working-file-bytes`。

## 阶段、确认与单次保护

- quick v2：1GiB专属文件，先顺序写/读1GiB，再4KiB QD1随机读/写各最多16MiB或5秒；最多写1040MiB，总采集保护90秒。不测缓存；投稿资格按四项核心及必要设备/条件/身份字段独立判断。
- full：默认8GiB专属文件，单次最多写1TiB，11项核心测量和缓存观察；总采集保护2580秒（43分钟），覆盖全部job而非每项单独43分钟。达到字节上限或采集总时限停止，按实际证据判完整性；有界计划不保证取得缓存后稳定速度。

正式测试前展示实际占用、最大写量、保留空间及总超时保护。深入默认文案为：“这项测试需要较长时间，难以精确预计。我们把总超时保护设为43分钟；超过这个时间上限，程序将自动停止，并按已测得的数据生成报告。” 实际时限不同时替换分钟数。依赖准备、等待用户确认不计入采集时限；watchdog停止本次活跃fio后再清理和生成报告，不保证渲染在该上限内结束。不用43分钟表示预计耗时或ETA。

TTY输入RUN确认当前计划；Agent已取得覆盖本阶段计划的授权时，以有界单行标准输入 `{"confirm_plan_sha256":"本次真实预演摘要"}` 确认。不向普通用户贴hash/JSON，也不把该输入冒充身份验证。首次quick复用用户对所选盘测速的请求；full须等快速结果展示后的深入同意。目标或计划改变时重新预演，只有仍在既有授权内才可传入新摘要，否则停下核对。

默认 `RunWriteProtection` 只约束当前运行，在首写前核对并预留本次计划的写量，采集期间累计计量，未知尾写不当作零；下一次测试使用独立单次计划，不因旧余额不足阻止运行。旧SQLite台账及scope/charged历史只读保留，不删除、不重置，也不自动迁移成新的运行授权。`--propose-write-bytes N` 保留兼容，用于当次normal/full的有限写量提案，不变成跨运行额度；参数本身不授权写入，不能扩大用户已同意的计划。容量检查使用自身的独立有限提案，不与该参数混用。不无限续写或自动重试。

使用有限专属普通文件，不清Trash；公式及读前覆盖条件见[缓存观察](cache-planning.md)。


## 可选容量与读写完整性检查

仅在完整深入测速报告已交付且来源满足条件后邀请；quick不足以作为容量检查来源，拒绝或未回答不执行。使用同一个已选目标、正常测速的原始结果和全新输出路径先预演：

```bash
python3 scripts/benchmark.py --target /path/to/same-selected-directory --capacity-from /path/to/normal/results.json --output /path/to/new-capacity-results --language zh-CN --ui-language zh-CN --dry-run
```

向用户展示预演的占用、覆盖范围、安全保留、本次写入量与估计时间范围。计划覆盖当前可用空间扣除安全保留后的区域，不实际填至 0；不让用户选择小/中/大档位。只创建唯一新普通文件，先完整写入一次再完整读取 CRC，不循环覆盖，不读写个人文件，不用正常测速的历史写入量冒充已校验容量。疑似扩容盘可能地址回绕而破坏已有数据，执行前必须说明风险，重要数据应先备份。估时用正常结果中的实测缓存后写速与持续读速，给时间范围而非完成保证；缺少依据时明确无法可靠估计。

获得该具体计划的明确授权后，仅去掉 `--dry-run`，其余参数保持相同：

```bash
python3 scripts/benchmark.py --target /path/to/same-selected-directory --capacity-from /path/to/normal/results.json --output /path/to/new-capacity-results --language zh-CN --ui-language zh-CN
```

沿用上文TTY `RUN` 或Agent标准输入 `confirm_plan_sha256`，绑定当前目标和独立容量计划；正常测速同意不覆盖容量检查。使用同一物理盘锁及单次RunWriteProtection，在首写前预留全部有限写量，旧SQLite历史不参与额度判断。在一个可选检查问题中展示并取得该具体计划同意，未答或拒绝就停止；不要求用户填写文件大小、强度或额度，不降低覆盖规模冒充全覆盖。空间或身份使计划变化时重新预演，不沿旧摘要确认。

同一正式命令保留容量检查独立 results/report，并自动生成 `combined-results.json` 和 `combined-report/report.html`；`--format pdf` 时也生成派生 PDF。该派生正常测速报告包含容量摘要，原测速 raw、原方法和已有冻结投稿保持不变，不自动上传或重投。公共投稿 schema 本轮仍只含正常测速数据；容量证据留本地，不隐式增加字段、不沿旧同意改变 payload，也不能声称网站已展示容量检查。读取错误、CRC 不一致、取消及覆盖不完整必须保留真实状态与实际范围，不能标通过。通过仅说明本次计划文件写读一致，不是整盘无坏道、无假货或标称容量真实性保证；完整边界见 [诊断说明](diagnostics.md#可选容量与读写完整性检查)。本轮只有源码与模拟验证，不代表真实设备已通过。

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

正常测速退出码：0 核心完整且无执行错误；6 核心/应有采集不完整；2 参数/语言/计划无效；3 依赖失败或引擎不支持；4 目标安全/空间/锁失败；5 运行、清理或 PDF 导出失败；130 用户取消。失败产生的部分 HTML 不代表测速完成。

Windows 仅接受能唯一识别为在线、可写、非聚集物理盘的 GPT/MBR 盘符目录，网络、虚拟或无法识别的设备、挂载文件夹与 reparse 点均拒绝；以 Windows 系统范围的命名互斥锁按物理盘身份互斥。Windows 默认保留专属测试文件，需用户自行处理，空间不会自动释放；持续写入不提前发送 POSIX 信号，而是在预算或时限内结束。Windows 社区投稿与横评也为实验性实现，私有缓存须位于支持持久 ACL 的本地卷（如 NTFS）；原生权限、锁及 SQLite 尚待实机验收。

输出与目标路径不接受符号链接组件。以物理设备为单位加锁，APFS 多卷共享物理盘也互斥；Linux通过findmnt/lsblk机器可读依赖树识别本地NVMe/SATA/USB及LVM/RAID等底层盘，实验性支持；已知网络块传输（如iSCSI、NBD和NVMe-over-Fabrics）拒绝；NVMe须正向识别PCIe，传输缺失须有本地sysfs证据，不能默认当作USB或物理盘。普通文件层级保护不抵御同权限恶意进程篡改，也不提供断电保证。系统盘根不可写时由Agent解析同盘现有可写用户目录；只读/无权限目录、网络盘、无法完整识别物理依赖的设备及裸盘拒绝。磁盘映像/loop仅在本地普通backing file及其宿主物理依赖可完整解析时允许，标为虚拟文件系统性能而非物理盘性能；未知映射不使用弱锁放行。新增路径已做fixture和macOS只读目录识别验证，尚未真实I/O验收。

macOS/Linux 的物理盘锁默认保存在 `~/.cache/drivespeed/device-locks`。使用同一用户缓存根的新版程序不会因不同任务的 `TMPDIR` 产生独立锁；隔离用户主目录或修改缓存根的进程不共享该命名空间。同时保留当前临时目录中的旧版锁以兼容同一 `TMPDIR` 的旧程序。使用其他 `TMPDIR` 的旧版程序不受新锁保护，不能混用版本并发测速；遇到残留锁须核实归属，不自动删锁或终止进程。Windows 仍使用原有命名互斥锁。

## 硬件与项目来源

每次实际测速重新采集 `hardware.computer / drive / enclosure`，字段为 model/source；enclosure 另有未确认关联的 bridge_candidates。缺失字段保留 null，不复用历史报告。`--computer-model`、`--drive-model`、`--enclosure-model` 可提供用户已确认的型号，source=user。system_profiler 只按 target.physical_devices 中的 BSD 标识选择硬盘，Linux 以 lsblk 匹配物理盘，Windows 使用已识别目标磁盘的系统型号；原始硬件清单和序列号不存入结果。

`assets/project.json` 拥有报告的仓库 URL。报告包含翻译后的简介及显式 HTTPS 超链接；离线打开不访问该链接，用户点击时才联网。旧结果没有hardware时报告省略该区块，在交付对话说明，不把当前机器补录成过去的测试机器。


## 读取 HTML/PDF 报告

HTML 先显示关键速度、硬件及持续写入表现，环境和方法使用可展开区域；打印前自动展开，打印后恢复原来的展开状态。正常完成时省略重复的状态、空原因列；存在未完成项或原因时恢复对应列。数值、失败提示和方法限制仍保留，PDF 包含完整参数。

AI Agent 优先读取 `results.json` 的原始数值。读取 HTML 时可用 `#results` / `#measurements` 定位表格，`data-metric` 定位摘要与持续写入指标，`dt` / `dd` 对应标签和值；折叠区域不等于缺失数据。输出 JSON 和测速数据格式不因布局变更而改变。


深入full的当前v2持续阶段必须有有效连续采样的稳定降速窗口和正有限缓存后写速，且其余核心统计完整才返回exit0；字节/时间先到但未取得该值返回partial/exit6，真实错误与取消保留原码。历史v1按旧判据读取，不追改结论。硬盘盒商品型号用--enclosure-model，主控型号用--controller-model，只填本次用户已确认的事实。

参考数据（可选）：benchmark.py 和 report.py 均支持 `--reference-file /path/to/references.json`。文件由 AI 根据实际打开的官网或可信评测整理，使用报告语言；JSON 校验和当前型号匹配不代表脚本独立核实了网页事实。缺少参考时照常生成报告。详见 [参考数据研究](reference-research.md)。


网络参考本地缓存位于用户 `.cache/drivespeed/references/<硬件组合>/<语言>.json`（或 XDG_CACHE_HOME）。默认 30 天复核，读取/翻译不更改原查阅日期。`--dry-run` 返回本次硬件、reference_cache 状态及其他已缓存语言，不写缓存；有新参考文件的正式执行/报告生成会保存公开参数。`--refresh-references` 绕过缓存，由 AI 重新核查后提供文件，Python 不暗中联网。过期离线缓存保留原日期展示，待更新状态仅在交付对话说明；测量结果不进入此缓存。


面向普通读者的“日常使用解读”从本次 results.json 动态计算：10GB 文件耗时仅为吞吐换算，不是实测复制时间；QD1/QD16 对照只展示同一设备内、块大小匹配且已完成的测试，不称为软件提速。缺项省略对应换算，不能复用开发者机器的解读或补造分数。HTML 的完整测试表和指标说明默认折叠，PDF 打印会展开并保留所有数值。

## 社区与成对报告

社区命令为 `python3 scripts/community.py --help`，完整的 prepare/preview/consent/publish/retry/receipt/compare/withdraw/list/remember 流程及退出码见 [社区工具](community.md)。离线准备/比较不安装 fio、不联网、不改原结果；只有明确同意后才授权投稿。

`report.py --comparison-id ID --community-state-dir /path/to/private-state` 额外生成 `comparison.html`，PDF 模式同时生成 `comparison.pdf`；两份报告读取同一 typed 比较版本。`--evaluation-stdin` 从标准输入读取有界、run_id/语言绑定的评价对象，不创建 JSON 缓存，与旧 `--evaluation-file` 互斥。HTML/PDF 不自动刷新远端样本；网络刷新须经社区命令显式发起。


新增 hardware.links 保存目标关联的最少链路元数据；旧结果缺失时报告省略对应项，在对话说明；不补录当前机器。报告保留硬件链路/官方规格、五项原始速度和整份三维五级评价；质量unknown/nonquiet不作设备星级或瓶颈归因；可选参考条件与分界见[硬件与五级评价](hardware-rating.md)。schema_version仍按当前合同；投稿只核四项QD1核心及必要设备/条件/私有身份数据，不要求full的11项或cache.status/post_cache_mb_s。quality/measurement_quality不写新报告、导出JSON或投稿JSON，背景仅对话说明；旧原始记录和hash保留。平均完成延迟以µs传递，服务端只验证结构，不宣称复验本地原始曲线。

## 本地电脑显示与接口事实

`report.py --show-computer-model` 仅控制本地 A/B HTML/PDF；缺省隐藏检测值、用户补充电脑型号及自动引用，不主动询问；仅用户主动要求显示时才传该选项。显示选项不进入结果/投稿/摘要，同一结果可重新导出。原电脑事实仍可在本次投稿中内部保存，公共读取把 computer 固定为 null/unknown 并移除相关电脑依据；公共记录不是原上传字节副本。

`benchmark.py --interface-name NAME` 接受用户已确认的 1..80 字符接口事实，写入 `hardware.access_interface`，保留系统 `target.protocol`；上传使用该 name 作为原始 `storage.connection`，因此属于新投稿内容和摘要的一部分，与显示选项不同。普通 user_input 不自动覆盖它。已有冻结报告不能沿旧 ID/同意改变接口名称；测量后补充只在用户确认的同 run 派生副本上进行，原始 fio 与旧 raw 保持。

`python3 scripts/interface_types.py --name NAME --language en` 离线识别明确别名；`--catalog-stdin` 可从标准输入读取当前公开接口字典，结果只留在内存/标准输出。未知原名保留，不猜测；不因归一化启动测速或模型。
# 离线产品资料快照

`report.py --product-profile /absolute/product-profile.json` 可附带独立的产品资料快照；输入遵循 `assets/community/product-profile.schema.json`，候选准备输入由相邻 `product-profile-candidates.schema.json` 定义。此参数不联网，不重新测速，也不修改 `results.json`、投稿或原摘要。

显式缺失、非法或设备身份不符的资料会非零退出，不忽略输入或覆盖已有报告。关联首先需要已保存的 `physical` 范围和唯一物理设备；多盘、虚拟、未解析或显式未确认的匹配型号不能降级使用展示名称。旧结果未保存匹配字段时，也只有明确单一物理目标才能使用旧型号。随后核对准确原型号/已核实别名，以及型号中的明确十进制 GB/TB 容量或独立 `nominal_capacity_bytes` 硬件事实；文件系统总量/可用空间不能证明商品容量。有效资料另存为输出目录的 `product-profile.json`，返回相应 artifact 路径；省略参数保持旧导出方式。

HTML/PDF 的报告对象、硬盘名称及文档标题使用已核实商品名，硬件表另保留系统原型号。设备资料按官方资料、独立评测分组，展示全部已核实来源的名称、原链接、发布者、适用范围和核对日期；同日可合并日期，不同日逐项保留。无有效链接时省略整个来源区，可靠商品名仍保留。这些资料不改变性能评分或测量值；主控系列标识不据此升级为精确芯片型号。网站是否已支持对应展示须以其实际部署版本为准。

Web 维护命令的 `product_profile_import` 是离线 DB-only 资料导入，不是模型调用或网页抓取。`GET /api/v1/benchmarks/ID/product-profile` 仅对当前可公开报告读取资料；未关联时返回 null，隐藏/撤回报告不提供该资源。

## 临时目录与历史残留

程序在所选磁盘根目录（系统盘不可写时为同盘可写位置）创建 `.diskbench-<run_id>`，仅包含 payload.bin、README.txt 和归属标记。报告保存在独立 output，不随临时目录清理。正式执行取得物理盘独占锁后静默检查该位置的历史目录；仅精确匹配标记、根/目录身份、当前用户及普通单链接文件，且未明确 retain 的目录自动移入系统废纸篓。未知/未标记的旧目录、链接、夹带个人文件的目录保留，不递归搜索整盘，也不解锁正在进行的测试。结果的 startup_cleanup 记录实际处理及失败；预演不清理，旧SQLite历史不因清理而删除或重置，单次写量计数不因空间复用抵扣。废纸篓数据仍占空间，程序不自动清空；Windows 按既有保留策略处理，不宣称已释放。
