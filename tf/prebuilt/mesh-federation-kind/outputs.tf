# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# The harness reads `cluster_name` + `cluster_location` and wires the per-run
# KUBECONFIG to that (primary) cluster. We return cluster-1 (the client); setup.sh
# merges cluster-2's context into the same kubeconfig so the agent has both.
output "cluster_name" {
  value = kind_cluster.c1.name
}

# "local" tells the TF deployer this is a kind cluster (skip gcloud get-credentials).
output "cluster_location" {
  value = "local"
}

output "backend_cluster_name" {
  value = kind_cluster.c2.name
}
