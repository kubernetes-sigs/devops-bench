# tasks/

A task is `tasks/<provider>/<name>/task.yaml` — the typed eval contract.

- **Grade on outcome.** Accept every valid path to the goal; never require a specific method.
- **Parallel-safety is mandatory.** Many runs of the same task execute at once, so every
  globally-unique cloud resource name must be run-scoped (via `var.cluster_name` or a random
  suffix) and swept at teardown. Shared, fixed names cause collisions and flakes.
- Keep namespaces, placeholders, and `{{...}}` substitutions consistent across the task and
  its verification/chaos specs.
- Run the **task-review** skill before submitting.

Deeper guide: `../docs/how-to/add-a-task.md`.
