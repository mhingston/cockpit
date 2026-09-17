# Workflow Usage

Choose the smallest route that supports the uncertainty and verification
burden of the task. Route choice and model choice are separate, and neither
authorises a mutation.

## Light

Use for direct answers, small read-only checks, straightforward edits, and
well-bounded verification.

The main agent keeps the work local and reports the evidence directly.

## Medium

Use when the task spans several files, needs implementation plus verification,
or has meaningful but bounded design uncertainty.

The main agent owns the implementation, integrated review, and final evidence.
Use one bounded worker only when it materially reduces context or provides
useful independent inspection.

## Heavy

Use when the task is substantive, cross-cutting, or benefits from multiple
independent workstreams. Decompose by outcome and file ownership. Keep workers
bounded and avoid overlapping edits.

Before dispatch, provide each worker with:

- the concrete question or outcome;
- the exact repository, path, revision, and constraints;
- whether the work is read-only or mutation-authorised;
- owned files and required checks; and
- the expected handoff format: findings, file/line references, uncertainty,
  changed files, checks, and unresolved concerns.

## Approval boundary

Preparation, inspection, search, dry runs, and test fixtures are not approval
for external or durable mutation. Before a mutation, show the bounded change,
target, exact command/request, verification, and rollback boundary. Wait for
approval of that specific scope.

## Lifecycle

For long work, keep a short handoff with the current revision, completed
checks, remaining uncertainty, and next action. Capture lifecycle metadata and
review proposals locally; promote durable knowledge only through an explicit
human review step.
