mock_provider "oci" {}

run "full_public_iam_contract" {
  command = plan

  variables {
    tenancy_ocid           = "ocid1.tenancy.oc1..publiciamtest"
    compartment_ocid       = "ocid1.compartment.oc1..publiciamtest"
    prefix                 = "jetuse-spike-iam01"
    enable_semantic_store  = true
    create_deployer_policy = true
    deployer_group_subject = "Default/JetUseDeployers"
  }

  assert {
    condition     = oci_identity_dynamic_group.runtime[0].name == "jetuse-spike-iam01-runtime-dg"
    error_message = "Runtime dynamic group name must use the configured prefix."
  }

  assert {
    condition = (
      strcontains(oci_identity_dynamic_group.runtime[0].matching_rule, "resource.type='computecontainerinstance'") &&
      strcontains(oci_identity_dynamic_group.runtime[0].matching_rule, "resource.type='fnfunc'") &&
      !strcontains(oci_identity_dynamic_group.runtime[0].matching_rule, "resource.type='autonomousdatabase'")
    )
    error_message = "Runtime dynamic group must contain only Container Instances and Functions principals."
  }

  assert {
    condition     = oci_identity_dynamic_group.adb[0].matching_rule == "All {resource.type='autonomousdatabase', resource.compartment.id='ocid1.compartment.oc1..publiciamtest'}"
    error_message = "ADB must have an isolated resource-principal dynamic group."
  }

  assert {
    condition     = length(oci_identity_dynamic_group.semantic_store) == 1 && strcontains(oci_identity_dynamic_group.semantic_store[0].matching_rule, "resource.type='generativeaisemanticstore'")
    error_message = "Semantic Store dynamic group must be created when enabled."
  }

  assert {
    condition     = length(oci_identity_policy.runtime[0].statements) == 22
    error_message = "Full Public runtime policy must contain the reviewed 22 statements."
  }

  assert {
    condition     = contains(oci_identity_policy.runtime[0].statements, "Allow dynamic-group jetuse-spike-iam01-runtime-dg to manage generative-ai-vectorstore in compartment id ocid1.compartment.oc1..publiciamtest")
    error_message = "Runtime policy must allow application-managed Vector Stores."
  }

  assert {
    condition     = contains(oci_identity_policy.runtime[0].statements, "Allow dynamic-group jetuse-spike-iam01-runtime-dg to manage ai-service-speech-family in compartment id ocid1.compartment.oc1..publiciamtest")
    error_message = "Runtime policy must allow Speech jobs and TTS."
  }

  assert {
    condition     = contains(oci_identity_policy.runtime[0].statements, "Allow any-user to use functions-family in compartment id ocid1.compartment.oc1..publiciamtest where ALL {request.principal.type = 'ApiGateway', request.resource.compartment.id = 'ocid1.compartment.oc1..publiciamtest'}")
    error_message = "API Gateway must be allowed to invoke the Functions router."
  }

  assert {
    condition     = oci_identity_policy.runtime_tenancy[0].statements == tolist(["Allow dynamic-group jetuse-spike-iam01-runtime-dg to read objectstorage-namespaces in tenancy"])
    error_message = "The runtime tenancy policy must remain read-only and namespace-only."
  }

  assert {
    condition     = length(oci_identity_policy.deployer) == 1 && length(oci_identity_policy.deployer[0].statements) == 6
    error_message = "The deployer policy must contain the reviewed six statements."
  }

  assert {
    condition     = contains(oci_identity_policy.deployer[0].statements, "Allow group Default/JetUseDeployers to manage all-resources in compartment id ocid1.compartment.oc1..publiciamtest")
    error_message = "Deployer all-resources permission must be restricted to the dedicated compartment."
  }

  assert {
    condition     = alltrue([for statement in oci_identity_policy.deployer[0].statements : !strcontains(lower(statement), "manage all-resources in tenancy")])
    error_message = "The deployer group must never receive tenancy-wide all-resources permission."
  }
}

run "minimal_without_semantic_store_or_deployer_policy" {
  command = plan

  variables {
    tenancy_ocid           = "ocid1.tenancy.oc1..publiciamtest"
    compartment_ocid       = "ocid1.compartment.oc1..publiciamtest"
    prefix                 = "jetuse-spike-iam02"
    enable_semantic_store  = false
    create_deployer_policy = false
  }

  assert {
    condition     = length(oci_identity_dynamic_group.semantic_store) == 0
    error_message = "Semantic Store dynamic group must be omitted when disabled."
  }

  assert {
    condition     = length(oci_identity_policy.runtime[0].statements) == 17
    error_message = "Minimal runtime policy must contain runtime and ADB statements only."
  }

  assert {
    condition     = length(oci_identity_policy.deployer) == 0
    error_message = "Deployer policy must be omitted when disabled."
  }
}

run "dynamic_groups_only" {
  command = plan

  variables {
    tenancy_ocid           = "ocid1.tenancy.oc1..publiciamtest"
    compartment_ocid       = "ocid1.compartment.oc1..publiciamtest"
    prefix                 = "jetuse-spike-iam03"
    enable_dynamic_group   = true
    enable_runtime_policy  = false
    enable_semantic_store  = true
    create_deployer_policy = false
  }

  assert {
    condition = (
      length(oci_identity_dynamic_group.runtime) == 1 &&
      length(oci_identity_dynamic_group.adb) == 1 &&
      length(oci_identity_dynamic_group.semantic_store) == 1
    )
    error_message = "All requested dynamic groups must be created independently of the compartment runtime policy."
  }

  assert {
    condition     = length(oci_identity_policy.runtime) == 0
    error_message = "The compartment runtime policy must be omitted when disabled."
  }

  assert {
    condition     = length(oci_identity_policy.runtime_tenancy) == 1
    error_message = "The tenancy-scoped namespace policy must be created with the dynamic groups."
  }
}

run "runtime_policy_only_with_existing_dynamic_groups" {
  command = plan

  variables {
    tenancy_ocid           = "ocid1.tenancy.oc1..publiciamtest"
    compartment_ocid       = "ocid1.compartment.oc1..publiciamtest"
    prefix                 = "jetuse-spike-iam04"
    enable_dynamic_group   = false
    enable_runtime_policy  = true
    enable_semantic_store  = true
    create_deployer_policy = false
    existing_dynamic_group = "preexisting-dg"
  }

  assert {
    condition = (
      length(oci_identity_dynamic_group.runtime) == 0 &&
      length(oci_identity_dynamic_group.adb) == 0 &&
      length(oci_identity_dynamic_group.semantic_store) == 0
    )
    error_message = "No tenancy-level dynamic groups may be planned when their creation is disabled."
  }

  assert {
    condition     = length(oci_identity_policy.runtime) == 1
    error_message = "The compartment runtime policy must still be created when enabled independently."
  }

  assert {
    condition     = contains(oci_identity_policy.runtime[0].statements, "Allow dynamic-group preexisting-dg to use generative-ai-family in compartment id ocid1.compartment.oc1..publiciamtest")
    error_message = "The runtime policy must reference the explicitly named pre-existing dynamic group."
  }

  assert {
    condition     = contains(oci_identity_policy.runtime[0].statements, "Allow dynamic-group preexisting-dg to use database-tools-family in compartment id ocid1.compartment.oc1..publiciamtest")
    error_message = "The semantic store statements must reference the single pre-existing dynamic group."
  }

  assert {
    condition = alltrue([
      for statement in oci_identity_policy.runtime[0].statements :
      strcontains(statement, "preexisting-dg") || strcontains(statement, "any-user")
    ])
    error_message = "Every statement must reference the single pre-existing dynamic group (except the API Gateway any-user grant)."
  }

  assert {
    condition     = length(oci_identity_policy.runtime[0].statements) == length(distinct(oci_identity_policy.runtime[0].statements))
    error_message = "Statements that collapse to the same grant on the single dynamic group must be deduplicated."
  }

  assert {
    condition     = length(oci_identity_policy.runtime_tenancy) == 0
    error_message = "No tenancy-scoped policy may be planned for a compartment-only runtime-policy deployment."
  }
}

run "runtime_policy_requires_existing_dynamic_group_names" {
  command = plan

  variables {
    tenancy_ocid           = "ocid1.tenancy.oc1..publiciamtest"
    compartment_ocid       = "ocid1.compartment.oc1..publiciamtest"
    prefix                 = "jetuse-spike-iam06"
    enable_dynamic_group   = false
    enable_runtime_policy  = true
    enable_semantic_store  = true
    create_deployer_policy = false
  }

  expect_failures = [oci_identity_policy.runtime]
}

run "runtime_iam_fully_disabled" {
  command = plan

  variables {
    tenancy_ocid           = "ocid1.tenancy.oc1..publiciamtest"
    compartment_ocid       = "ocid1.compartment.oc1..publiciamtest"
    prefix                 = "jetuse-spike-iam05"
    enable_dynamic_group   = false
    enable_runtime_policy  = false
    enable_semantic_store  = true
    create_deployer_policy = false
  }

  assert {
    condition = (
      length(oci_identity_dynamic_group.runtime) == 0 &&
      length(oci_identity_dynamic_group.adb) == 0 &&
      length(oci_identity_dynamic_group.semantic_store) == 0 &&
      length(oci_identity_policy.runtime) == 0 &&
      length(oci_identity_policy.runtime_tenancy) == 0
    )
    error_message = "All runtime IAM resources must be omitted when both controls are disabled."
  }
}
