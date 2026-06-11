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
