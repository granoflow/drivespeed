# 用户语言与翻译包

内置 assets/locales/en.json、zh.json、ar.json。语言码按首段匹配（例如 zh-CN）。用户要求繁体或特定地区措辞时，由代理提供准确的完整翻译包，不把内置简体称为繁体。

其他语言由代理复制 en.json 的全部键，逐项翻译值，保存 UTF-8 JSON 到报告卷上的准备目录，再分别传 `--messages`（报告）与 `--ui-messages`（进度）。不要省键、增加键、翻译 JSON 字段或 `{seconds}` 占位符。`messages` 对整包键进行校验；语言包只当文本处理，不执行代码。翻译须覆盖步骤、失败恢复、ETA、单位说明、图表、数值区和打印按钮，不只翻译标题。

无内置包且没有显式翻译包时脚本拒绝开始，而不是在写入后才报语言错误。代理发现未知 locale 时解释并提供正确语言包；脱离代理直接运行可传 `--language en --ui-language en`。HTML 设置 lang，ar/he/fa/ur 使用 RTL。

进度：开始时列出全部步骤；阶段切换立即输出；交互终端测量中限频约 1 秒、普通日志约 5 秒。当前百分比来自字节或收到的运行时快照；未知则显示“百分比待确定”。总体百分比来自成功完成步骤/计划步骤。失败、跳过、取消不增加成功完成数。短测试开始与完成之间可能没有中间快照，不伪造动画。

阶段 ETA 使用剩余字节/实际速度和已知剩余测试时间的较小值；整体 ETA 因安装、同步、翻译/报告时间未知而显示不可估算。用户中途换语言时，代理用新语言转述后续事件；脚本本次输出语言固定，不为了换语言重启磁盘测试。

## DriveSpeed 品牌与推广语

中文名称为“硬盘测速（DriveSpeed）”；英文及其他语言名称为 DriveSpeed，大小写保持不变。英文及其他语言的 `title` 保持 DriveSpeed，报告各章节仍按用户语言翻译。

中文 `skill_intro`：你的硬盘表现如何？现在就来测一下吧。

英文 `skill_intro`：Find out how your drive performs. Test it with DriveSpeed.

Skill报告以中英文范本为依据；其他语言统一以英文为语义依据，按用户当地语法及本人习惯重新描述。中文和英文也自然改写，不机械照抄。`skill_intro` 保留实测邀请的含义，不加入速度、健康、耗时或安全保证。进度和技术说明照常完整翻译。

报告结尾的官网标签 `website_label` 与扩展推广语 `translation_promo` 必须随用户报告语言翻译。中文原文为“chrome翻译扩展，翻译你的pdf，免费，私密，快速。”；其他语言保留免费、私密、快速及翻译 PDF 的含义，不增加承诺。`https://anz.tv`、`https://translate.anz.tv` 两个网址不翻译；通过项目配置维护。

社区提示另用 `assets/community/locales/en.json`；横评报告另用 `assets/community/report-locales/en.json`，均须完整翻译且保留占位符。它们是软件语言模板，可保存为 JSON；不包含用户投稿或评价缓存。

## 报告范本与网页资源边界

Skill只维护 `assets/report-templates/en.json`、`zh.json` 两份分析范本及一份共享 `document.html`。它们不是语言限制：所有报告语言的措辞均可经完整临时messages包和evaluation输入调整。网页端独立预翻译translate项目实际语言清单的固定文案，不把网页77语言包复制进Skill，也不临时翻译访问中的网页。原内置ar等软件兼容资源保留；自然表达规则同样适用于用户选择这些语言时。

新增SMART/评测区块的固定标签在report-templates/en.json和zh.json的evidence维护。en与zh-CN直接使用对应标签；其他语言在evaluation.evidence.labels提供与英文evidence结构/全部键一致的本地化文本，renderer按同一输入转义渲染，缺项拒绝。标签、正文及可选内容块都要按用户语言自然表达；不将网页77个预翻译包复制进Skill。英文评价输入模板为evaluation.en.json，中文为evaluation.zh.json。
