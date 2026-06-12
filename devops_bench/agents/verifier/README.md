# Verifier Agent

This directory houses the modular, type-safe verification engine used by `ScenarioManager` and task evaluators to validate cluster state during and after chaos disruptions.


## 1. Input API: `VerificationSpec`

The `VerificationSpec` is a Pydantic v2 `RootModel` that parses a dictionary mapping checker names to their specifications:
* **Dictionary Check**: Runs multiple named specifications in sequence, mapping results back to their respective keys.

### Discriminated Union: `SingleVerificationSpec`
Individual verification condition specs are discriminated using their literal `"type"` field:

#### 1. Pod Healthy Condition (`pod_healthy`)
Verifies that pods matched by a label selector are in the `Running` and `Ready` state.
```jsonc
{
  "type": "pod_healthy",
  "selector": "app=my-app",
  "namespace": "production" // Optional, defaults to active namespace
}
```

#### 2. Scaling Complete Condition (`scaling_complete`)
Verifies that a deployment has successfully converged to at least a minimum number of ready replicas.
```jsonc
{
  "type": "scaling_complete",
  "deployment": "my-deployment",
  "min_replicas": 3,       // Optional, defaults to 1
  "namespace": "production" // Optional, defaults to active namespace
}
```

---

## 2. Output API: `VerificationResult`

Every check (single or compound) returns a structured recursive `VerificationResult` report:

```python
class VerificationResult(BaseModel):
    success: bool                                                 # True if all conditions were met
    elapsed_time: float                                           # Time elapsed during execution (seconds)
    reason: str                                                   # Readable summary of outcomes/failures
    details: Optional[Union[Dict[str, 'VerificationResult'], List['VerificationResult'], dict]] # Recursive child results
```

---

## 3. Code Examples

### YAML Task Configuration Specification
```yaml
chaos_spec: |
  [
    {
      "name": "Planned Load Spike",
      "trigger": { "type": "time", "delay_seconds": 5 },
      "action": { "type": "generate_load", ... },
      "verification": {
        "pod_spec": {
          "type": "pod_healthy",
          "selector": "app=hello-app",
          "namespace": "production"
        },
        "scaling_spec": {
          "type": "scaling_complete",
          "deployment": "hello-app",
          "min_replicas": 2,
          "namespace": "production"
        }
      }
    }
  ]
```

## 4. Extending with New Checks

Adding a new check is extremely simple and fully adheres to the **Open-Closed Principle (OCP)**:
1. Create a new file under `devops_bench/agents/verifier/verifiers/<new_check_name>_verifier.py`.
2. Define a verifier class inheriting from `BaseVerifier` with `type: Literal["<new_check_name>"]` and implement `verify(self, timeout_sec: int) -> VerificationResult`.
3. Register your new class in the `SingleVerificationSpec` union inside `devops_bench/agents/verifier/spec.py`.

---

## 5. Event-Driven Watcher Architecture (kubewatch)

The verifier agent uses an event-driven architecture powered by **[robusta-dev/kubewatch](https://github.com/robusta-dev/kubewatch)** instead of active polling loops.

### How it Works
1. **Local Webhook Receiver**: When checking a condition, `KubeWatchService` starts a temporary local HTTP server on an ephemeral random port.
2. **Isolated Daemon Instance**: It constructs an isolated, temporary `.kubewatch.yaml` configuration in a temporary directory and starts the local `kubewatch` daemon process. This ensures your global `~/.kubewatch.yaml` is never modified.
3. **Structured JSON Events**: `kubewatch` tracks cluster state changes and posts structured JSON payloads (representing creations, updates, or deletions of pods and deployments) directly to our local HTTP server.
4. **Instant Event Dispatch**: Upon receiving an event, the HTTP handler sets a thread-safe `threading.Event`, immediately waking up the verifier thread to run a precise check via `kubectl get`.
5. **Teardown**: When the verification succeeds or times out, the local daemon process is terminated, the web server is closed, and all temporary configurations are cleaned up.

### Installation & Prerequisites

To use the event-driven watcher, the `kubewatch` binary must be installed.

1. **Install kubewatch**:
   ```bash
   # Build and install the latest binary using the Makefile target
   make install-kubewatch
   ```
2. **Add to Path or Environment**:
   - Ensure your `$GOPATH/bin` (typically `~/go/bin`) is in your shell `PATH`.
   - Alternatively, you can configure the exact path to the binary using the `KUBEWATCH_BIN` environment variable:
     ```bash
     export KUBEWATCH_BIN="/path/to/go/bin/kubewatch"
     ```

