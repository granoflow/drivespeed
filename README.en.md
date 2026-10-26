# DriveSpeed

[简体中文](README.md) · English

**Wondering how quick your portable drive really is? Take it for one run and find out.**

Let your AI agent test your portable drive, explain performance bottlenecks, and create a clear HTML report you can save as a PDF.

## Install

Paste this into a local agent chat in Codex, Claude Code, or Cursor:

```text
Please install the DriveSpeed skill from https://github.com/granoflow/drivespeed.
Installation command: npx skills add granoflow/drivespeed --skill drivespeed
```

## Test your drive

After installation, ask your agent:

```text
Use DriveSpeed to test my portable drive and write the report in English.
First, let me confirm which drive to test, the space needed, and how much data will be written.
```

Your agent checks for and installs fio as needed, then guides you through the test and report.

Currently supports macOS. Linux support is experimental; Windows benchmarking is not yet supported.

[Detailed usage](references/cli.md) · [Test methodology](references/methodology.md) · [Additional tests](references/diagnostics.md) (technical guides in Chinese) · [MIT license](LICENSE) · [Third-party licenses](THIRD_PARTY_NOTICES.md)
