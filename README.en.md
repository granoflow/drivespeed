# DriveSpeed

[简体中文](README.md) · English

**Find out how your drive performs. Test it with DriveSpeed.**

Let your AI agent test your drive, explain performance bottlenecks, and create a clear HTML report you can save as a PDF. Select a readable, writable directory on internal or external local storage, including a user directory on the system disk. Review the space, total write budget, and time limit first; raw devices are never used.

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

macOS is supported; Linux and Windows local benchmarking are experimental and await real-device testing. Windows retains test files. Community sharing is also experimental; its private cache requires a local volume with persistent ACLs, such as NTFS.

[Detailed usage](references/cli.md) · [Test methodology](references/methodology.md) · [Additional tests](references/diagnostics.md) (technical guides in Chinese) · [MIT license](LICENSE) · [Third-party licenses](THIRD_PARTY_NOTICES.md)

## Optional sharing and comparison

Finish your local report first, then choose whether to publish it. You can decline and share a retained result later. Public reports link to your verified X profile, with an optional short introduction. Comparisons show matching conditions and sample limits; there are no leaderboards. The website is live, and an existing report has completed real X authorization, production acceptance and publication checks. Each new report still requires its own preview, consent and verification of its receipt and public status.

[Sharing and recovery](references/community.md) · [Agent data-use convention](references/agent-data-use.md) (technical guides in Chinese)


Hardware and five-level performance ratings: the Skill researches reported bridge-chip names or exact branded enclosure specifications. Reports retain five raw speeds. When background observations and reference evidence support attribution, they provide one report-level set of controller, interface and drive utilisation stars with a one-sentence assessment. Unknown or limited background visibility preserves measurements without device ratings or bottleneck attribution. The Skill researches exact official specifications first; missing facts never receive invented stars, and throughput does not establish authenticity. See [hardware and rating rules](references/hardware-rating.md).

Local HTML/PDF reports hide the computer model by default. Use `report.py --show-computer-model` only when you choose to show it; the same results can be re-exported without measuring again. This display preference does not change measurements or upload checksums. A voluntary upload still includes the original computer facts for internal storage; public reports and references omit the model. A confirmed access-interface name can be supplied with `benchmark.py --interface-name NAME`; this is an upload fact, distinct from the display preference. See the [CLI reference](references/cli.md).
