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

output "sa_email" {
  description = "Email of the service account the bastion runs as."
  value       = google_service_account.bastion.email
}

output "name" {
  description = "Name of the bastion VM."
  value       = google_compute_instance.bastion.name
}

output "zone" {
  description = "Zone of the bastion VM."
  value       = google_compute_instance.bastion.zone
}

output "iap_ssh_command" {
  description = "Command to SSH into the bastion over IAP."
  value       = "gcloud compute ssh ${google_compute_instance.bastion.name} --zone ${google_compute_instance.bastion.zone} --project ${var.project_id} --tunnel-through-iap"
}
