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

Give qualification plan/gate jobs `contents: read`, `actions: read`, `checks: read` and `pull-requests: read`. Reusable-workflow caller permissions must also allow these read-only scopes. Checks read corroborates the trusted aggregate app; pull-request reads corroborate exact merged-PR source association when run linkage disappears. No write/admin scope is required. Check out the caller repository before the action, with `fetch-depth: 0` in `plan` and the aggregate gate. Missing base history safely forces full validation rather than classifying an incomplete diff.

`ci-plan` outputs `mode` (`full`, `docs`, `reuse`), `tree`, `checkout-sha` and compact bound `plan` JSON. Heavy jobs depend on `plan` and run only when its mode is `full`. The always-reporting aggregate job depends on `plan` and **every** heavy job. It calls `ci-record` with `plan: ${{ needs.plan.outputs.plan }}` and `results: ${{ toJSON(needs) }}`. Full mode requires every expected job to succeed; docs/reuse require them all to be skipped and revalidate the classification/proof. Failed plan, missing/extra jobs, failure/cancellation or contradictory outcomes fail the real gate.

Only full mode uploads `ci-qualification-<tree>-<profile-context-policy-hash>-attempt-<N>`. The bounded single-file receipt attests actual checkout commit/tree, workflow blobs, immutable helper, validation profile/context, source run/attempt/event, PR merge parents and successful expected jobs. Docs/reuse never create receipts, so qualifications cannot chain.

Reuse verifies the own-repository successful CI run, artifact run/head/repository IDs, nonexpired bounded ZIP, exact tree/profile/context/policy, actual Git commit and PR merge parents, and the successful aggregate check from GitHub Actions app15368. Merged PRs can lose their run linkage; the recorded event PR is then corroborated against the exact head-commit association and own-repository PR. Earlier full attempts of a subsequently successful same run are allowed; future/foreign attempts are rejected.

The docs allowlist is only `docs/**` and top-level `README*.md`. Unknown changes, missing history/API/proof, legacy runs without a receipt, unqualified CI/policy/config changes or invalid artifacts select full validation. A new policy requires a first full run; later exact-tree proof with all current workflow blobs and the immutable implementation can qualify that same policy tree across events. A contradictory gate fails closed. The bounded candidate search may miss an older valid receipt, which costs another full run rather than relaxing evidence.

The actions do not authorize a deployment or cross-environment artifact reuse. Consumers must preserve delivery checks and bind any reused build output to its exact build mode and provenance separately.

## Bound build output reuse

`required-artifacts` is an optional JSON array of output artifact names. A full qualification is reusable only while every named artifact is present, nonexpired and bound to the same own source run/head. Missing output selects full validation. `ci-plan` additionally exports `qualified-run`, `qualified-attempt`, and `qualified-checkout`: the verified full producer identity, or the current run identity in full mode. A consumer such as Website can expose these through its reusable workflow, download the source run's output, and verify its own build-mode/content receipt. The shared receipt alone never proves build-input or output-content equivalence.

## Downstream docs delivery verdict

`ci-record` also emits a **separate non-reusable** bounded `ci-verdict-attempt-N` artifact in every successfully validated mode and exposes its `mode` output. This is not a full qualification and cannot enter reuse search.

`ci-verdict` accepts the same canonical policy inputs plus `source-run` and `source-sha`. A workflow-run consumer checks out that exact source and provides values directly from its trigger. The action verifies the exact trigger, own successful push workflow, source checkout, immutable policy/profile/context, source attempt, artifact run/head/repository IDs, and successful aggregate Actions gate. A docs verdict must additionally match a complete Git API comparison of the recorded source base and actual checkout; runtime paths, renames out of runtime paths, missing/mismatched/truncated source evidence fail closed.

Its outputs are `mode` and `outcome`. Only verified docs produces `outcome=no_runtime_change`. Other modes return `runtime_validation_required`; environment-specific delivery remains the consumer's responsibility. Missing or bad downstream evidence rejects classification and must not authorize a docs no-op. Consumers can skip builds/publication/Sentry deployment for verified docs and emit a distinct bound production receipt with `outcome=no_runtime_change`; that is history-synchronization evidence, not a claim that a runtime was deployed.

Both `ci-record` and `ci-verdict` offer `record-runtime-noop: 'true'`, default false. Opt in only when the consumer actually gates its runtime delivery off in verified docs mode. On release-branch docs pushes, the centralized writer emits the distinct bounded outcome receipt: main uses `production-sync-receipt` with `environment: production`; develop uses `runtime-noop` with `environment: development`. It binds the current producer run, actual source checkout and qualified source CI run/attempt, with `success: true` and `outcome: no_runtime_change`. It never sets a deployed field. Full/reuse modes do not emit this no-op evidence. External build systems, such as the AI application's direct Cloudflare autobuild integration, still need their own path/build controls; skipping GitHub validation does not disable those external builds.


Permission/API lookup failures conservatively select full validation and emit only fixed stage/status codes such as `pull_requests_read_http_403` or `checks_read_http_403`. Raw gh stderr, response bodies, signed URLs and credential data are never included in these diagnostics. Gate revalidation failures still fail closed. GitHub documents [read access for associated pull requests](https://docs.github.com/en/rest/commits/commits#list-pull-requests-associated-with-a-commit) and [checks read for a check run](https://docs.github.com/en/rest/checks/runs#get-a-check-run).
