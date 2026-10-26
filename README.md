# 移动硬盘测速（DriveSpeed）

简体中文 · [English](README.en.md)

**你的移动硬盘几斤几两？现在就来测一下吧。**

让 AI 帮你测读写速度、分析性能瓶颈，生成看得懂的 HTML 报告，也能保存为 PDF。

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

目前支持 macOS；Linux 为实验性支持，Windows 暂不支持测速。

[详细用法](references/cli.md) · [测试方法](references/methodology.md) · [专项测试](references/diagnostics.md) · [MIT 开源许可](LICENSE) · [第三方许可](THIRD_PARTY_NOTICES.md)
