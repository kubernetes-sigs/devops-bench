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
  description = "Name of the provisioned virtual cluster"
  value       = var.cluster_name
}

output "cluster_location" {
  description = "Location of the provisioned virtual cluster"
  value       = var.location
}

output "kubeconfig" {
  description = "Raw virtual cluster kubeconfig YAML string"
  value       = data.kubernetes_secret.vcluster_kubeconfig.data["config"]
  sensitive   = true
}

output "external_endpoint" {
  description = "Reachable external endpoint of the virtual cluster API server"
  value       = var.service_type == "NodePort" ? "${local.host_node_ip}:${kubernetes_service.vcluster_exposure.spec[0].port[0].node_port}" : try(coalesce(kubernetes_service.vcluster_exposure.status[0].load_balancer[0].ingress[0].ip, kubernetes_service.vcluster_exposure.status[0].load_balancer[0].ingress[0].hostname), "")
}
