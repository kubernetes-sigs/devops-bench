output "cluster_name" {
  value       = module.cluster.cluster_name
  description = "The finalized name of the created cluster"
}

output "cluster_location" {
  value       = module.cluster.location
  description = "The region/zone or 'local'"
}
