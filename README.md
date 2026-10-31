# 硬盘测速（DriveSpeed）

简体中文 · [English](README.en.md)

**你的硬盘表现如何？现在就来测一下吧。**

让 AI 帮你测读写速度、分析性能瓶颈，生成看得懂的 HTML 报告，也能保存为 PDF。目标可选内置或外置本地硬盘上的可读写目录，包括系统盘用户目录；先明确目录并查看空间、累计写入量与时限，不访问裸盘。

## 安装

在 Codex、Claude Code 或 Cursor 的本地 Agent 对话中，复制发送：

```text
请帮我安装 DriveSpeed Skill，仓库是 https://github.com/granoflow/drivespeed。
安装命令：npx skills add granoflow/drivespeed --skill drivespeed
```

## 开始测速

安装后告诉 Agent：

```text
用 DriveSpeed 帮我测试移动硬盘，报告用中文。
先让我确认要测哪块硬盘，以及测试需要的空间和写入量。
```

Agent 会检查并安装所需的 fio 测速工具，引导你完成测试和查看报告。

macOS 支持；Linux 与 Windows 本地测速为实验性支持，仍需实机验证。Windows 测试文件会保留；社区分享同样为实验性，私有缓存需支持 ACL 的本地卷（如 NTFS）。

[详细用法](references/cli.md) · [测试方法](references/methodology.md) · [诊断边界](references/diagnostics.md) · [MIT 开源许可](LICENSE) · [第三方许可](THIRD_PARTY_NOTICES.md)

## 自愿公开与横向测评

先完成本地报告，再决定是否公开；拒绝后可随时从保留结果改主意。公开报告关联本人验证过的 X 链接，可附一句介绍。相似设备只按可比条件提供参考，不设排行榜；样本不足时明确说明。网站已上线，已有报告完成真实 X 授权、生产接收与公开验证；每份新报告仍须独立预览、同意并核对回执与公开状态。

[分享与恢复](references/community.md) · [评价规则](references/evaluation.md) · [Agent 数据使用约定](references/agent-data-use.md)


硬件链路与五级评价：Skill 可研究目标设备名称直接暴露的主控，以及品牌硬盘盒的官方规格；报告保留五项原始速度，测量背景满足归因条件且参考充分时，整份报告展示相对主控、接口和硬盘的能力利用率星级及一句点评。背景未知或可见性不足时保留实测事实，不作设备评分或瓶颈归因。先补查准确型号的官方参考，缺资料不编造星级，不据测速鉴定真伪。详见[规则与参考输入](references/hardware-rating.md)。

本地 HTML/PDF 默认隐藏电脑型号；明确选择显示时用 `report.py --show-computer-model`，可从同一结果重新导出。显示选择不改变测量或投稿摘要；原电脑事实仍可能随自愿投稿供服务器内部保存，网站和公开参考不提供该型号。用户确认的接口名可用 `benchmark.py --interface-name NAME` 补充，它属于投稿事实，和显示开关不同。详见 [CLI 说明](references/cli.md)。
