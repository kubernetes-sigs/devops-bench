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

# The harness reads `cluster_name` + `cluster_location` and runs
# `gcloud container clusters get-credentials` for that single cluster. We return the
# PRIMARY (east) cluster here; the setup script additionally merges the WEST context
# into the kubeconfig (as `east`/`west`) so the agent has both regions available.
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

# Standalone west-only kubeconfig written by setup.sh, for verifiers that must
# read the standby region. The task's verification_spec spells the same path
# with the {{CLUSTER_NAME}} placeholder; if you change the layout in
# locals.west_kubeconfig, change the task too.
output "west_kubeconfig_path" {
  value = local.west_kubeconfig
}

# Global anycast IP that fronts the storefront. Users hit this; the agent discovers it
# (e.g. `gcloud compute forwarding-rules list`) and re-points the URL map behind it.
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
