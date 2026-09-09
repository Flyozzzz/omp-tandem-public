# Contributing to OMP Tandem

Thanks for helping improve independent AI collaboration. Start with the [README](README.md) and [full guide](docs/guide.md).

## Before opening an issue or pull request

- Search existing issues and describe the user-visible problem, not only a proposed implementation.
- For security-sensitive reports, follow [SECURITY.md](SECURITY.md). Do not publish exploit details or credentials in an ordinary issue.
- Use synthetic examples. Never attach provider tokens, native session histories, runtime SQLite databases, transfer bundles, or private client configuration.
- Keep changes focused. Discuss public MCP contract changes and data migrations before implementing a broad redesign.

## Development setup

Use Python 3.12+ and uv. From the repository root:

```sh
uv sync --frozen --group dev
uv run pytest -q
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv build --wheel
```

`pytest` belongs to the development environment. Do not fix import errors by relying on a global pytest executable or manually adding the runtime source tree to `PYTHONPATH`.

Tests use temporary data and local RPC/HTTP peers. A normal test run must not require OMP provider credentials, paid model calls, or changes to global client configuration. Real client smoke checks are separate, explicit checks using your own authorized accounts and disposable project state.

## Behavioral requirements

Changes must preserve these boundaries unless an explicit design change is agreed:

- Client-origin workspace binding happens before project data access. Model-supplied task arguments do not choose a namespace.
- Product snapshots and transferred evidence do not grant permissions or automatically approve claims.
- Legacy migration copies eligible data; it never rewrites the original store or interrupts active owners.
- Task status and durable events remain transactionally consistent.
- Questions, cancellation, worker slots and native RPC processes have bounded, observable lifecycles.
- Bootstrap subprocesses do not consume MCP stdin, pollute protocol stdout, copy credentials, or overwrite another live runtime generation.
- Failure is not reported as success merely because an agent produced confident text.

Prefer small services with explicit dependencies over mixins or helpers that receive an entire coordinator object. Keep MCP handlers, persistence, worker execution, and presentation separate. Preserve eager annotations where the pinned FastMCP Context wrappers require them.

## Tests and documentation

Add a regression when a plausible behavior, race, or trust-boundary bug warrants it. Assert consumer-visible outcomes; avoid tests that only inspect source text or repeat mocked field forwarding.

Update the English, Russian and Simplified Chinese landing pages/guides when changing documented behavior or commands. Keep executable examples aligned across languages. Detailed material belongs in `docs/`, not an ever-growing landing README.

After changing distribution inputs, regenerate and verify the manifest:

```sh
uv run --frozen python scripts/package.py
uv run --frozen python scripts/package.py --check
```

Do not commit generated `dist/` artifacts or runtime caches. CI checks the committed manifest, pytest, Ruff and wheel building on Linux and macOS.

## Review and license

Explain what changed, why it is needed, which scenarios were exercised, and any remaining limitations. Be respectful, engage with evidence, and preserve other contributors' work.

By submitting a contribution, you agree to provide it under the project's [MIT License](LICENSE). Only contribute material you have the right to license. Third-party dependencies retain their own terms; do not remove their notices.
