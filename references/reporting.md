# 报告分析与交付

用于新测速后的最终本地报告、只读解读、翻译与重生成。先读results.json的原始方法、outcome、completeness和停止原因；已有历史背景证据只用于自然语言交付。格式整洁不改变原方法状态，投稿资格另按所需数据项判断。当前full v2缺有效缓存后稳态仍为partial，历史方法按原合同读取。

## 共享结构与表达范本

主报告HTML/PDF共用 `assets/report-templates/document.html`，横评用 `comparison.html`。两种格式读取同次输入，打印样式只管排版；模板槽位只接收已转义文本或可信组件，不执行模型文字中的HTML。

分析时只读当前语言的 `assets/report-templates/zh.json` 或 `en.json`。中英文也按用户表达习惯自然改写；其他语言参考英文语义。范本不提供事实。固定标签与临时 `--messages` 合同见[本地化](localization.md)；不新增并行PDF模板或在加载范本时调用模型。

## 报告与交付对话

**最终报告**使用evaluation.report_policy="verified_only"，保留已取得的有效数值、实际条件、单位、来源及supported结论。未测字段省略，不补零、不用参考填实测。网友评测只统一列表视觉，按来源选择参数、指标、范围/文字结果和自定段落，见[灵活内容块](device-evidence.md#灵活的评测内容块)。不要求每篇都有顺序/随机/缓存或固定三段说明。

有可用SMART时顶部展示数据表、实际工具版本和采集时间；网友评测附录保留原文URL，PDF也可读可点击。新增证据区块支持中英文及通过evidence.labels提供的完整本地化标签。设备字段、私有标识处理及绑定见[设备证据](device-evidence.md)。

**交付对话**承接采集过程、固件可信边界、缺测、不确定推断、一般免责、隐私/授权提醒、清理去向与补测建议。这些内容不印入报告；背景活动与可见性也不写新导出JSON或投稿JSON。按需使用[对话模板](../assets/interaction-templates/zh.json)或英文版。实际发生的I/O错误仍可作为事实记录；不能靠删免责声明把不可靠结论改成确定结论。

评价仍逐项考虑瓶颈、升级、质量疑点，按[评价合同](evaluation.md)构建内存对象。section.evidence_status为supported或needs_followup，只有前者显示。`evaluation.followup`列出未解决问题，每项含issue、reason、solution、retest_plan，均为非空文本；不把未经选择的额外项目当缺测。省略/needs_followup的段落和执行上下文仍随回执返回给Agent供对话交付。

## 原始事实与比较

只读重生成使用保存的测量、硬件、评价与来源，不重测、不读当前硬件回填历史、不自动刷新网络。用户要求更新分析/购买建议时才研究相应新资料，保留旧测量与真实查阅日期。

区分短时QD1、短时QD16、持续写入的活动窗口；初始/末段/最低连续5秒/降速后速度各按实际证据计算。尾速不等于缓存后速度，累计覆写量不等于不同地址覆盖量。Gb/s信号换算不是文件速度合格线，额定值与实测差值不是可追回收益；时序缺失不补点，候选不当身份确认。

只列本设备可核参数，不展示整份接口速率目录。具备实证瓶颈与兼容收益依据时才给推荐设备参数；星级与硬件参考受[评级规则](hardware-rating.md)约束，背景未知时不作纯净设备归因。有效数据可交付，问题在对话解释，不能改原始资格或借清爽展示获得投稿权限。

## 问题交付与重测

当前目标涉及缓存且未测得时，在对话交代曲线形状、采样资格、工作集及写量/时限。若路径可能遮蔽缓存差异，用实测和有来源的同型号持续能力解释推断；不同容量、方法或状态不直接归因。不能把无拐点写成无缓存，或把主控候选、盒内侧PCIe段写成已确认外置路径。

问题清单要给下一动作：先离线复算/核对原始证据，再考虑必要的只读核查，最后才提出改变真正相关条件的有界复验。缓存目标不可观测时不重复同一无效长写；采样失败先修采样，有干扰则选择合适窗口。新写盘仍须目标、空间、累计写量与超时计划授权；不承诺必能取得硬件未公开字段或无限测试到有结果。用户不接受补测则交付有依据的项目并结束。

交付上下文通过标准错误中的自然语言提供给Agent，再由Agent在对话中说明；机器回执只返回产物路径和数据资格，不把回执成功当整份方法完成。背景活动/可见性不放回执或其他新导出JSON。`share_recommendation_allowed`按[投稿所需数据项](community.md#投稿所需数据项)判断，不以quick/full、非必需缓存缺项、原方法partial或背景状态否决；必要字段缺失或核心测量不可靠时不能准备投稿。规则更新不替代当前冻结预览的独立同意，也不授权重测。

## 导出、隐私与检查

最终交付由Agent经Python API或 `--evaluation-stdin` 输入绑定run_id/语言的内存评价，不让用户填JSON，不创建评价JSON缓存。模板见 `assets/report-templates/evaluation.zh.json`；占位符不是实际数据，needs_followup不能为显示而直接改成supported。`--evaluation-file`仅兼容既有调用。

```bash
python3 scripts/report.py --input /path/to/results.json --evaluation-stdin --output /path/to/new-report --language zh-CN --format html
```

正文通过子进程stdin传入，不拼进shell、不交互等待用户。用户要求PDF时改 `--format pdf`，使用已有Chromium；PDF失败先交付HTML并在对话说明。重生成输出新目录，不覆盖原raw或旧冻结投稿。

交付前检查文件实际存在、数值/单位/来源/条件/语言一致，HTML布局与链接可用；PDF检查全部页面。确认缺项与免责没有混入正文，省略字段没留下空标题。报告保留项目公开链接，离线加载不访问网络；安装或一般授权说明留在对话。

默认隐藏电脑型号，用户主动要求才用 `--show-computer-model`。它仅控制本地展示，原硬件事实仍可能用于投稿内部保存，不能说该开关阻止上传。序列号/GUID/路径等不进入搜索或可分享报告；原始序列号不上传，私有`device_identity`指纹不出公开JSON。完整原日志另行核查。

## 用户修正与横评

用户主动补充按[user-input](user-input.md)加入派生结果。确认的接口可用 `--interface-name`；测后只在同run派生副本的hardware.access_interface中添加confirmed_interface，保留原target.protocol。不能覆盖raw、改hash、沿旧同意重传，不能靠速度猜接口。

横评用同一冻结comparison ID的typed事实；保留原来源、日期、条件差异和公开资格。少于3份同条件记录不作统计优势，记录数不等于独立设备数。更新网络快照仅在用户要求时执行，详见[社区流程](community.md)。
