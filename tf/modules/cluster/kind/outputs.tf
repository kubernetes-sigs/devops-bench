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
  value = kind_cluster.default.name
}

output "cluster_location" {
  value = "local"
}

output "kubeconfig_path" {
  value = kind_cluster.default.kubeconfig_path
}

output "endpoint" {
  value = kind_cluster.default.endpoint
}

output "client_certificate" {
  value = kind_cluster.default.client_certificate
}

output "client_key" {
  value = kind_cluster.default.client_key
}

output "cluster_ca_certificate" {
  value = kind_cluster.default.cluster_ca_certificate
}

