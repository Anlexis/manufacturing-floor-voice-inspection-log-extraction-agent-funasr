# Voice Inspection Log Extraction Agent

AI agent for extracting structured quality records from spoken floor inspections, built with Agentic Star.

> **Category**: Cat 2 (domain-specific pipeline)
> **Industry**: Manufacturing
> **Template ID**: MFG-C2-061

## Overview

Turns a spoken production-line inspection into a validated quality record. Floor inspectors
dictate their quality checks; this agent takes the recognised transcript of that recording,
separates the speaker turns, extracts the part number, inspector ID, measurements and verdict
remarks, normalises the terminology to JIS B 9700, checks every measurement against the product
line's specification ranges, and produces an ISO 9001-style QMS inspection record together with a
human-readable inspection log entry — so verbal checks reach the quality management system without
manual transcription and re-keying.

Callers send the audio request envelope (`format`, `duration_s`, and the audio payload or file
reference — the payload itself is digested at ingest and never stored) together with the recognised
text as `input_context.transcript`, and may add specification-range overrides, a product line and a
plant identifier. Every field is bounds-checked: numbers must be finite and in range, identifiers
are locked to an inert alphabet, and the transcript is refused — never masked into a record — when
it carries chat-template control tokens, model-directive phrases, credential-shaped strings or
personal data. A measurement that is not a finite number is reported as a non-conformance, never
as a pass. Without a transcript the pipeline runs on a fixed offline baseline and labels the result
as such.

The finished record passes an output boundary that withholds it entirely on a credential-shaped
leak and refuses to publish a report that disagrees with the structured record. No FunASR client
ships with this template: a configured endpoint fails closed rather than fabricating a transcript.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. If the platform is unreachable or the SDK version does not match, the agent fails at
graph compile / start-up preflight rather than starting in a partially working state. This is
intentional — a half-running agent is worse than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Project Structure

```
src/          agent implementation (nodes, graphs, the caller contract, schemas)
tests/        unit, integration and boundary tests
config/       agent manifest and runtime parameters
docs/         design specification and test specification
```

See `docs/` for the design and the test specification.

## Customising

1. Adjust `config/config.yaml` — specification ranges, product line and plant identifiers — for
   your own product lines.
2. Extend the JIS B 9700 term map and the measurement extraction patterns under `src/nodes/` for
   the metrics your inspectors dictate.
3. Review the caller-data contract in `src/services/caller_contract.py` if your transcripts need
   different bounds or screens.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
