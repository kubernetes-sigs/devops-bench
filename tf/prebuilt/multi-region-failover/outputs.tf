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

# The harness runs get-credentials for the single cluster these two outputs
# name, so they return the primary (east).
output "cluster_name" {
  value = module.east.cluster_name
}

output "cluster_location" {
  value = module.east.location
}

output "west_cluster_name" {
  value = module.west.cluster_name
}

output "west_cluster_location" {
  value = module.west.location
}

# The task's verification_spec spells this path with {{CLUSTER_NAME}}; change
# both together.
output "west_kubeconfig_path" {
  value = local.west_kubeconfig
}

output "lb_ip" {
  value = google_compute_global_address.lb_ip.address
}

output "primary_static_ip" {
  value = google_compute_address.east_ip.address
}

output "standby_static_ip" {
  value = google_compute_address.west_ip.address
}

output "sql_primary_instance" {
  value = google_sql_database_instance.primary.name
}

output "sql_replica_instance" {
  value = google_sql_database_instance.replica.name
}
