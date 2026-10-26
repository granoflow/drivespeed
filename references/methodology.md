# 测速方法与专业报告依据

本项目采用文件级、有限预算测试，适合有用户数据的移动硬盘；不是整盘破坏性认证。fio 本身是测量引擎，设备状态、主机和连接决定可观察结果。

## 维度与原理

1 MiB 顺序访问对应大文件吞吐；4 KiB 随机访问展示小块 I/O 的带宽、IOPS 和完成延迟分位数。QD1 与 QD16 对比请求并发，并保留 achieved depth，不能把配置 QD 当成实际队列深度。macOS 使用 posixaio，Linux 使用 libaio；缺少引擎则拒绝，避免偷偷改成同步引擎。

初始化先对专属文件真正写入一遍，稀疏预设长度不算初始化成功。后续读取只读该文件。`io_size` 限制每项总 I/O，`runtime` 提供时间上限，不使用会循环覆盖预算的 time_based。70/30 随机混合保守地按全部 I/O 算入潜在写入预算，因此实际写入不会因为混合比例而超额。

`direct=1` 请求绕过 OS 文件缓存；无法排除 SSD 控制器/伪 SLC 缓存。`end_fsync=1` 请求最终同步，I/O 统计与进程总耗时均保留。主机缓存处理失败时不改用缓存模式冒充同一测试。工作集可以被重复访问，持续测试不等于全盘单次写入。

## 持续性能的数值

- 初始平均：第一个 5 秒内已记录样本的时间加权平均。
- 末段：最后三分之一时间，最多 30 秒；列加权平均和样本中位数。
- 最低连续 5 秒平均：仅有完整、连续采样窗口时计算，缺样不补零。
- 持续降速候选：前 5 秒之后，连续 10 个约 1 秒采样都低于初始平均的 70%；列首个候选时间、按带宽积分估算的累计字节和此后平均速度。
- 降幅：1 - 末段平均/初始平均，可为负（表示变快）。样本不足则留空，不伪造“缓存内/缓存外”两档数值。

上述阈值是本工具的可解释描述算法，不是 SNIA 稳态判定。降速点可能受温度、缓存整理、后台任务、供电和主机负载影响，不能据此确认 SLC 缓存大小。报告保持“缓存耗尽未确认”；“降速后速度”有数据时直接给数值。缓存外性能确认需要另有控制变量与更充分的持续写入证据。

可为用户目标增大文件、写入预算和持续时间，但先展示副作用。不同满盘比例、TRIM 状态、先前写入、空闲恢复时间、温度和线材会影响可比性，保持一致并记录未知项。工具不会自动擦盘、全盘预处理、改变 TRIM 或生成综合评分。

## 来源与边界（2026-10-26 核对）

- [fio 官方文档](https://fio.readthedocs.io/en/latest/fio_doc.html)：io_size/runtime、direct、end_fsync、status-interval 及平台引擎。固定源码及上游 Windows 安装包为 [fio 3.43](https://github.com/axboe/fio/releases/tag/fio-3.43)；[Homebrew fio](https://formulae.brew.sh/formula/fio)。
- [SNIA 吞吐测试说明](https://www.snia.org/forums/sssi/pts/tp)：强调稳态窗口和可比较的测试条件。本工具不执行其完整预处理，也不声称通过 SNIA 认证或满足其稳态准则。
- [Tom’s Hardware T7 Shield 测试](https://www.tomshardware.com/reviews/samsung-t7-shield-portable-ssd-review/3)：长时间连续写入以观察写缓存饱和后的表现。本工具借鉴其分段观察思路，但默认时限/写入量更小，不能保证同等覆盖。
- [StorageReview 持续性能测试](https://www.storagereview.com/review/the-best-portable-ssd-for-sustained-performance)：说明持续写入、设备状态与随后读写的影响。本项目不默认复刻该文的长时间大规模负载。
