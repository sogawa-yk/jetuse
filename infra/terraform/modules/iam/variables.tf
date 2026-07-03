variable "tenancy_ocid" {
  description = "Tenancy OCID. Dynamic groups and tenancy-level policies are created here."
  type        = string
}

variable "compartment_ocid" {
  description = "Dedicated compartment in which JetUse runs."
  type        = string
}

variable "prefix" {
  description = "Prefix for dynamic group and policy names."
  type        = string
}

variable "enable_dynamic_group" {
  description = "Create the tenancy-level JetUse runtime dynamic groups and their tenancy-scoped namespace policy."
  type        = bool
  default     = true
}

variable "enable_runtime_policy" {
  description = "Create the JetUse runtime policy in the dedicated compartment. Existing dynamic groups are referenced when enable_dynamic_group is false."
  type        = bool
  default     = true
}

variable "enable_semantic_store" {
  description = "Create the dynamic group and policies required by OCI Generative AI semantic stores (SQL Search)."
  type        = bool
  default     = true
}

variable "existing_dynamic_group" {
  description = "Name of the pre-existing dynamic group referenced by the runtime policy when enable_dynamic_group is false. Its matching rule must cover Container Instances, Functions and ADB (and semantic stores when enable_semantic_store is true)."
  type        = string
  default     = ""
}

variable "create_deployer_policy" {
  description = "Grant an existing group permission to deploy JetUse into the dedicated compartment with Resource Manager."
  type        = bool
  default     = false
}

variable "deployer_group_subject" {
  description = "OCI policy group subject after 'Allow group', for example Default/JetUseDeployers or id ocid1.group..."
  type        = string
  default     = ""
}
