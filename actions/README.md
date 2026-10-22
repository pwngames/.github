# Qualified CI actions

`ci-plan` and `ci-record` avoid repeating a full validation only for audited documentation changes or a previously fully qualified, identical Git tree. They never copy a successful check to another SHA. The consumer's real required aggregate job still runs and validates its expected outcomes.

Both actions require the same inputs:

- `profile`: versioned validation/toolchain/platform identity, such as `py314-node20-ubuntu24-arm64-v1`.
- `validation-context`: explicit nonsecret build/environment/mode identity. Distinct test inputs or production flags require distinct contexts. Never pass secret values.
- `expected-jobs`: exhaustive JSON array of heavy job IDs, excluding the reserved `plan` job. Matrix jobs use their aggregate job ID.
- `implementation-ref`: exactly the immutable 40-character SHA used by `uses:`. The action checks its actual execution reference.
- `workflow-path`: validation policy, default `.github/workflows/ci.yml`.
- `trusted-workflows`: optional JSON list of allowed run entrypoints. Default is only `workflow-path`. For a reusable CI invoked by a deploy entrypoint, both must be explicitly listed; policy blobs for every listed entrypoint are bound.
- `gate-name`: default exact source Actions job label, normally `ci-gate`.
- `gate-names`: optional canonical JSON mapping for every trusted entrypoint, e.g. `{".github/workflows/ci.yml":"ci-gate",".github/workflows/deploy.yml":"ci / ci-gate"}`. Use the same complete mapping in every caller so equivalent validation retains a stable identity.

Give both jobs `contents: read` and `actions: read`. Check out the caller repository before the action, with `fetch-depth: 0` in `plan` and the aggregate gate. Missing base history safely forces full validation rather than classifying an incomplete diff.

`ci-plan` outputs `mode` (`full`, `docs`, `reuse`), `tree`, `checkout-sha` and compact bound `plan` JSON. Heavy jobs depend on `plan` and run only when its mode is `full`. The always-reporting aggregate job depends on `plan` and **every** heavy job. It calls `ci-record` with `plan: ${{ needs.plan.outputs.plan }}` and `results: ${{ toJSON(needs) }}`. Full mode requires every expected job to succeed; docs/reuse require them all to be skipped and revalidate the classification/proof. Failed plan, missing/extra jobs, failure/cancellation or contradictory outcomes fail the real gate.

Only full mode uploads `ci-qualification-<tree>-<profile-context-policy-hash>-attempt-<N>`. The bounded single-file receipt attests actual checkout commit/tree, workflow blobs, immutable helper, validation profile/context, source run/attempt/event, PR merge parents and successful expected jobs. Docs/reuse never create receipts, so qualifications cannot chain.

Reuse verifies the own-repository successful CI run, artifact run/head/repository IDs, nonexpired bounded ZIP, exact tree/profile/context/policy, actual Git commit and PR merge parents, and the successful aggregate check from GitHub Actions app15368. Merged PRs can lose their run linkage; the recorded event PR is then corroborated against the exact head-commit association and own-repository PR. Earlier full attempts of a subsequently successful same run are allowed; future/foreign attempts are rejected.

The docs allowlist is only `docs/**` and top-level `README*.md`. Unknown changes, missing history/API/proof, legacy runs without a receipt, unqualified CI/policy/config changes or invalid artifacts select full validation. A new policy requires a first full run; later exact-tree proof with all current workflow blobs and the immutable implementation can qualify that same policy tree across events. A contradictory gate fails closed. The bounded candidate search may miss an older valid receipt, which costs another full run rather than relaxing evidence.

The actions do not authorize a deployment or cross-environment artifact reuse. Consumers must preserve delivery checks and bind any reused build output to its exact build mode and provenance separately.
