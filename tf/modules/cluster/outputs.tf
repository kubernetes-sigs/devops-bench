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

output "cluster_name" {
  value       = var.infra_provider == "gcp" ? try(module.gke[0].cluster_name, "") : (var.infra_provider == "kind" ? try(module.kind[0].cluster_name, "") : try(module.vcluster[0].cluster_name, ""))
  description = "The finalized name of the created cluster"
}

output "location" {
  value       = var.infra_provider == "gcp" ? try(module.gke[0].cluster_location, "") : (var.infra_provider == "kind" ? try(module.kind[0].cluster_location, "") : try(module.vcluster[0].cluster_location, ""))
  description = "The region/zone or 'local'"
}

output "endpoint" {
  value       = var.infra_provider == "gcp" ? try(module.gke[0].endpoint, "") : (var.infra_provider == "kind" ? try(module.kind[0].endpoint, "") : try(module.vcluster[0].external_endpoint, ""))
  description = "Cluster control plane endpoint"
}

# The kind or GKE endpoint without the vcluster fallback that `endpoint` has. A
# root kubernetes provider configured from `endpoint` would depend on
# module.vcluster's resources, which that provider serves, and tofu rejects the
# cycle. Stacks that do not use vcluster read this instead.
output "managed_endpoint" {
  value       = var.infra_provider == "gcp" ? try(module.gke[0].endpoint, "") : try(module.kind[0].endpoint, "")
  description = "Control plane endpoint for the kind/GKE providers (no vcluster fallback)"
}

output "cluster_ca_certificate" {
  value       = var.infra_provider == "gcp" ? try(module.gke[0].cluster_ca_certificate, "") : (var.infra_provider == "kind" ? try(module.kind[0].cluster_ca_certificate, "") : "")
  description = "Cluster CA certificate"
}

output "client_certificate" {
  value       = var.infra_provider == "kind" ? try(module.kind[0].client_certificate, "") : ""
  description = "Client certificate for KinD"
}

output "client_key" {
  value       = var.infra_provider == "kind" ? try(module.kind[0].client_key, "") : ""
  description = "Client key for KinD"
}

output "kubeconfig_path" {
  value       = var.infra_provider == "kind" ? try(module.kind[0].kubeconfig_path, "") : ""
  description = "Local path to the kubeconfig file"
}


output "kubeconfig" {
  value       = var.infra_provider == "vcluster" ? try(module.vcluster[0].kubeconfig, "") : ""
  description = "Raw kubeconfig YAML (vcluster-only)"
  sensitive   = true
}
