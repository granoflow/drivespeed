# DriveSpeed

[简体中文](README.md) · English

Wondering how quick your portable drive really is? Take it for one run and find out.

DriveSpeed is an open-source portable-drive benchmarking and diagnostics project. It provides an Agent Skill, a Python CLI, and offline HTML/PDF reports. Results cover sequential and random I/O, sustained writes, cache behavior, and optional data-integrity checks.

## Install for your agent

Requires Node.js 22.20+ for the installer, not for the benchmark itself:

```bash
npx skills add granoflow/drivespeed --skill drivespeed
```

Choose your agent when prompted. Installation is project-local by default; add `-g` for installation across projects. For selected clients:

```bash
npx skills add granoflow/drivespeed --skill drivespeed --agent codex claude-code cursor
```

After installation, ask: **“Use DriveSpeed to test the external drive I select. Show the write budget first and produce the report in English.”**

If the agent does not know the name, provide `https://github.com/granoflow/drivespeed`. Discovery depends on the host loading installed skills; publishing does not automatically install the skill in every agent. The repository root contains a standard `SKILL.md` and its resources. You can also copy the complete repository into a supported skill directory named `drivespeed`.

[Agent Skills specification](https://agentskills.io/specification) · [Skills CLI](https://github.com/vercel-labs/skills)

## Run directly

Python 3.10+; no third-party Python dependencies. Run from this directory:

```bash
python3 scripts/benchmark.py --target /path/to/external-volume --language en --ui-language en --dry-run
python3 scripts/benchmark.py --target /path/to/external-volume --output /path/to/new-report --language en --ui-language en --profile quick
```

The first command only inspects and previews the plan. The second writes test data. Missing fio is installed through an existing package manager, or built from a pinned, checksum-verified upstream source when build tools already exist. Do not install the unrelated Python package named fio.

| Mode | Purpose |
| --- | --- |
| `quick` | Short baseline: 1 GiB file, at most 8 GiB of writes |
| `full` (default) | Sequential, 4 KiB random, mixed and sustained I/O: 8 GiB file, at most 64 GiB of writes |
| `cache` | A separate, larger sustained-write test with sourced planning and bounded observation |
| `mapping` | Random reads across increasing address ranges, comparing first and repeated passes |
| `integrity` | Write checksummed data, then read it all back |

`--recovery-seconds 300` adds an idle/recovery test to `cache`. `--fill-free-space` is opt-in for `integrity`: it leaves at least 10 GiB or 5% of the volume capacity, whichever is larger, plus filesystem headroom. Preview the exact byte and time limits before running. DRAM capacity is a sourced hardware fact, not a capacity measurement from random I/O. Verification covers the test file, not hidden physical spare blocks or existing files.

## Compatibility

- **macOS:** real full-drive-workload validation performed; not every hardware combination is tested.
- **Linux USB:** experimental device identification and benchmarking support.
- **Windows:** fio installation support only; benchmarking is blocked until safe target identification is implemented.
- **Agent hosts:** require local shell access and permission to run Python/fio. Installation support does not prove runtime support in every client or sandbox.

Only dedicated test files are used. Existing user files and raw devices are not overwritten. Files move to the operating system Trash after testing; space is not released until the user empties Trash. There is no automatic Trash emptying.

## Reports and language

HTML is self-contained. The Download PDF button opens browser printing; `--format pdf` also attempts export through an existing Chromium installation. Built-in report and progress languages are Chinese, English and Arabic. For other languages the agent prepares a complete translation pack; it must not silently switch to English.

Hardware is detected for each run. References are researched for that device and cached locally by hardware and language. Measured values remain separate from specifications and third-party results. Optional device details and test notes are collected separately; self-analysis is requested after displaying the report and then added to HTML/PDF.

## Development and release

```bash
python3 -m unittest discover -s tests -v
```

Current skill metadata version: **0.1.0**. Public source: [granoflow/drivespeed](https://github.com/granoflow/drivespeed). Local reports and device evidence under `docs-temp/` are excluded from Git and distribution. Documentation and implementation details live in `references/` and `scripts/`.

Project code: [MIT](LICENSE). fio is a separate GPL-2.0-only program and is not bundled; see [third-party notices](THIRD_PARTY_NOTICES.md).

### Installation verification

On 2026-10-26, Skills CLI 1.7.4 installed the public GitHub package in an isolated project for Codex, Claude Code and Cursor. Installed scripts, references and assets matched the source byte for byte; both CLI help commands and all 69 tests passed from the installed copy. This verifies distribution integrity, not live drive testing in every agent or operating system.
