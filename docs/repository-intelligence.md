# Repository Intelligence

Install and configure [Repowise](https://docs.repowise.dev/) for repository
understanding and [Sentrux](https://sentrux.dev/docs/mcp/) for structural
feedback only when the workflow needs those capabilities. The base hook tests
do not require either integration.

## Complementary roles

Repowise provides understand-before-acting intelligence: repository context,
symbols, dependencies, history, rationale, likely impact, and affected tests.
Its output is derived and must be checked against the current checkout.

Sentrux provides structural feedback and regression gates: module boundaries,
cycles, coupling, depth, complexity distribution, redundancy, test gaps, and
explicit architecture rules. It is a sensor and gate, not the source of truth
for behaviour or runtime evidence.

## Evidence flow

1. Identify the exact checkout and current revision.
2. Check Repowise status before relying on indexed context.
3. Run Sentrux `scan` with an absolute repository path when structural analysis
   is relevant.
4. Read the files being changed directly.
5. Implement the smallest viable change.
6. Run compiler, build, analyzer, test, or evaluation checks.
7. Re-check Repowise impact/risk if the integration supports it.
8. Compare Sentrux results only when an authorised baseline exists.

Do not refresh indexes, create baselines, or update architecture rules as an
incidental side effect. Treat score changes as signals requiring concrete
regression evidence, not as optimisation targets.

## Useful command shapes

```bash
repowise status --no-workspace --format json .
repowise context --no-workspace --format json path/to/file
repowise risk --no-workspace --target path/to/file
repowise health --no-workspace --format json .
```

Sentrux is normally invoked through its configured integration. Start with
`scan`, then use only the analysis surfaces relevant to the task.
