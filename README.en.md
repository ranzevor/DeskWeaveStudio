![DeskWeaveStudio application mark](docs/assets/brand-mark.svg)

# DeskWeaveStudio

[![Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-2563eb)](LICENSE)
[![Engine foundation](https://img.shields.io/badge/Stage-Engine%20Foundation-8b5cf6)](ROADMAP.md) (zh-Hans)
[![Windows, macOS and Linux targets](https://img.shields.io/badge/Targets-Windows%20%7C%20macOS%20%7C%20Linux-475569)](docs/architecture/platforms.md) (zh-Hans)
[![MCP 2026-07-28](https://img.shields.io/badge/MCP-2026--07--28-15803d)](docs/interfaces/mcp.md) (zh-Hans)

[![Tauri 2.12](https://img.shields.io/badge/Tauri-2.12-24c8db?logo=tauri&logoColor=white)](docs/architecture/system.md) (zh-Hans)
[![React 19.3](https://img.shields.io/badge/React-19.3-149eca?logo=react&logoColor=white)](docs/design/studio.md) (zh-Hans)
[![TypeScript](https://img.shields.io/badge/TypeScript-strict-3178c6?logo=typescript&logoColor=white)](docs/design/studio.md) (zh-Hans)
[![Documentation and contract checks](https://img.shields.io/badge/Checks-Docs%20%26%20Contracts-475569?logo=githubactions&logoColor=white)](.github/workflows/docs.yml)

**A visual workspace for desktop automation.**

DeskWeaveStudio focuses on recording across all displays, reproducing continuous pointer paths, locating screen targets and composing workflows. The desktop workspace, command line and MCP clients share the same workflows and assets.

[中文](README.md) · [Documentation (zh-Hans)](docs/README.md) · [Interface prototypes](#interface-prototypes) · [Roadmap (zh-Hans)](ROADMAP.md) · [Contributing (zh-Hans)](CONTRIBUTING.md)

## Project status

The execution core is under development. The Python service library provides expression previews, workflow validation, workflow version storage, chunked content transfer, catalog and revision pagination, saved-request lookup, connection authentication, native local transports, a standalone Engine service process, and cross-host launch coordination. The repository also contains system designs, interactive prototypes, high-resolution mockups and public contracts. The desktop application, Rust Agent, MCP Bridge and installers follow the [roadmap (zh-Hans)](ROADMAP.md).

## Execution core

With Python 3.12 and uv 0.8.2:

```bash
uv sync --frozen
uv run --frozen python examples/engine/workspace.py
```

The example saves, reads and validates a workflow and previews an expression in a temporary workspace. Run `uv run --frozen python examples/engine/content_transfer.py` for the [large-workflow example](examples/engine/content_transfer.py), including chunked upload, resource reads and cleanup. Run `uv run --frozen python examples/engine/process.py` for controlled process startup, saving and reconnecting. Run `uv run --frozen python examples/engine/coordination.py` to share an Engine across host sessions, continue using it after the initiating session closes, and release it when idle. See [Engine development (zh-Hans)](docs/development/engine.md) for available methods and development commands.

## Interface prototypes

![Workflow editor prototype](docs/prototypes/high-fidelity/E01-editor-graph@2x.png)

The prototypes cover workspaces, graph, step and source editors, recording, target selection, assets, run debugging, observation, settings and recovery flows.

With Python 3.12 or later, start the preview from the repository root:

```bash
python tools/serve_prototypes.py
```

Open the local address printed by the command in your browser. Press `Ctrl+C` to stop the server. On Linux/macOS, use `python3` if needed. You can also open the [prototype entry file (zh-Hans)](docs/prototypes/index.html) directly in a local browser.

[High-resolution mockups (zh-Hans)](docs/design/high-fidelity.md) · [Screen catalog (zh-Hans)](docs/design/prototypes.md) · [Prototype guide (zh-Hans)](docs/prototypes/README.md)

## Planned capabilities

- **Workflow authoring:** edit one document through graph, step and source views, with parameters, conditions, bounded loops, shared run variables and subworkflows.
- **Recording across all displays:** capture continuous keyboard and pointer input across mixed scaling, portrait displays, negative coordinates and irregular layouts. Preserve intermediate positions, timing and held input for continuous drawing across displays and timeline editing.
- **Targeting and templates:** select windows or screenshot regions, test template matching, extract candidates in batches and name them through OCR, rules or manual input.
- **Execution and debugging:** run fixed workflow versions, set breakpoints, step through execution, inspect variables, start from a selected node, pause, resume, stop and trigger an emergency stop.
- **Continuous observation:** monitor targets, detect changes, filter records by time and export screenshots and results. Configure optional vision models through separate connections.
- **Assets and versions:** manage workflows, image templates, input trajectories, revision history and portable task packages; recover drafts and back up workspaces.
- **MCP integration:** target 2026-07-28 with 2025-11-25 compatibility; expose tools, resources and prompts through stdio or explicitly enabled network transports, with filtered cursor queries and shared business logic, workspace state and desktop control.

See [product requirements (zh-Hans)](docs/product/requirements.md) for the full scope and usage scenarios, and the [feature map (zh-Hans)](docs/design/feature-design-map.md) for design, prototype and acceptance references.

## Architecture

Studio, MCP and the command line share one workspace Engine for business validation, save transactions, paginated queries and run state. Each open workspace has one Engine; one Rust Desktop Agent schedules input for each interactive desktop.

| Component | Technology choice | Responsibility |
|---|---|---|
| Studio | Tauri 2.12, React 19.3, TypeScript | Workspace UI, native windows, editing interactions and Engine client |
| Engine | Python | Shared application services, workflow execution, workspace storage and vision tasks |
| Desktop Agent | Rust | Target sessions, screen capture, continuous input scheduling, recording and emergency stop |
| MCP Bridge | Python | MCP protocol, client authorization binding and Engine method mapping |
| CLI | Python | Command arguments, result output and Engine method mapping |

The platform design covers Windows, macOS and Linux. Linux X11 and Wayland handle input, recording and system permissions separately. Display coverage, recording and input capabilities are detected for the active desktop backend; see the [cross-platform design (zh-Hans)](docs/architecture/platforms.md) and [release matrix (zh-Hans)](docs/operations/release.md).

See the [system architecture (zh-Hans)](docs/architecture/system.md) and [shared application services (zh-Hans)](docs/architecture/shared-services.md) for component relationships and request handling.

## Documentation

Design documents and project guides are available in Chinese. Public contracts use English identifiers and include examples for client implementers.

| Goal | Starting points |
|---|---|
| Implement workflow editing and execution | [Workflow model (zh-Hans)](docs/design/workflow.md), [frontend structure (zh-Hans)](docs/design/studio.md), [execution control (zh-Hans)](docs/design/execution.md) |
| Implement recording and continuous input | [Recording across all displays (zh-Hans)](docs/design/recording.md), [pointer motion and drawing (zh-Hans)](docs/design/pointer-motion.md) |
| Integrate services or build clients | [Local protocol (zh-Hans)](docs/interfaces/local-protocol.md), [MCP interface (zh-Hans)](docs/interfaces/mcp.md), [command line (zh-Hans)](docs/interfaces/command-line.md), [pagination (zh-Hans)](docs/interfaces/pagination.md) |
| Design interfaces and interactions | [Interaction design (zh-Hans)](docs/design/experience.md), [visual system (zh-Hans)](docs/design/visual-system.md), [accessibility (zh-Hans)](ACCESSIBILITY.md) |
| Inspect formats and examples | [Contracts (zh-Hans)](docs/README.md#机器可读资料), [examples (zh-Hans)](examples/README.md), [content transfer (zh-Hans)](docs/interfaces/content-transfer.md) |
| Develop, test and release | [Engineering (zh-Hans)](docs/development/engineering.md), [verification (zh-Hans)](docs/quality/verification.md), [releases (zh-Hans)](docs/operations/release.md) |

## Local checks

Use Python 3.12 or later and create an isolated environment from the repository root.

Windows PowerShell:

```powershell
python -m venv .venv-docs
.\.venv-docs\Scripts\python.exe -m pip install -r requirements-docs.txt -r requirements-prototypes.txt
.\.venv-docs\Scripts\python.exe tools/check_docs.py --contracts
.\.venv-docs\Scripts\python.exe tools/check_prototypes.py
```

Linux/macOS:

```bash
python3 -m venv .venv-docs
.venv-docs/bin/python -m pip install -r requirements-docs.txt -r requirements-prototypes.txt
.venv-docs/bin/python tools/check_docs.py --contracts
.venv-docs/bin/python tools/check_prototypes.py
```

These commands check documentation links, public contracts, examples, the prototype catalog and image files. The complete command list, including checks that require Node.js 22, is in the [engineering guide (zh-Hans)](docs/development/engineering.md). See [prototype validation (zh-Hans)](docs/quality/prototype-validation.md) for page and interaction checks.

## Contributing

Use the repository's Issue forms for bugs, feature requests and design discussions. Read the [contribution guide (zh-Hans)](CONTRIBUTING.md) and [code of conduct (zh-Hans)](CODE_OF_CONDUCT.md) before submitting changes. Report vulnerabilities and sensitive information through the [security process (zh-Hans)](SECURITY.md).

## License

Project-authored source code, documentation, contracts and original examples are licensed under the [Apache License 2.0](LICENSE) (`Apache-2.0`). Third-party dependencies and materials retain their respective licenses. See [licensing details (zh-Hans)](LICENSING.md) for distribution requirements.
