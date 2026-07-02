mock_provider "oci" {}

run "full_public_bootstrap_contract" {
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
    condition     = oci_identity_dynamic_group.runtime.name == "jetuse-spike-iam01-runtime-dg"
    error_message = "Runtime dynamic group name must use the configured prefix."
  }

  assert {
    condition = (
      strcontains(oci_identity_dynamic_group.runtime.matching_rule, "resource.type='computecontainerinstance'") &&
      strcontains(oci_identity_dynamic_group.runtime.matching_rule, "resource.type='fnfunc'") &&
      !strcontains(oci_identity_dynamic_group.runtime.matching_rule, "resource.type='autonomousdatabase'")
    )
    error_message = "Runtime dynamic group must contain only Container Instances and Functions principals."
  }

  assert {
    condition     = oci_identity_dynamic_group.adb.matching_rule == "All {resource.type='autonomousdatabase', resource.compartment.id='ocid1.compartment.oc1..publiciamtest'}"
    error_message = "ADB must have an isolated resource-principal dynamic group."
  }

  assert {
    condition     = length(oci_identity_dynamic_group.semantic_store) == 1 && strcontains(oci_identity_dynamic_group.semantic_store[0].matching_rule, "resource.type='generativeaisemanticstore'")
    error_message = "Semantic Store dynamic group must be created when enabled."
  }

  assert {
    condition     = length(oci_identity_policy.runtime.statements) == 22
    error_message = "Full Public runtime policy must contain the reviewed 22 statements."
  }

  assert {
    condition     = contains(oci_identity_policy.runtime.statements, "Allow dynamic-group jetuse-spike-iam01-runtime-dg to manage generative-ai-vectorstore in compartment id ocid1.compartment.oc1..publiciamtest")
    error_message = "Runtime policy must allow application-managed Vector Stores."
  }

  assert {
    condition     = contains(oci_identity_policy.runtime.statements, "Allow dynamic-group jetuse-spike-iam01-runtime-dg to manage ai-service-speech-family in compartment id ocid1.compartment.oc1..publiciamtest")
    error_message = "Runtime policy must allow Speech jobs and TTS."
  }

  assert {
    condition     = contains(oci_identity_policy.runtime.statements, "Allow any-user to use functions-family in compartment id ocid1.compartment.oc1..publiciamtest where ALL {request.principal.type = 'ApiGateway', request.resource.compartment.id = 'ocid1.compartment.oc1..publiciamtest'}")
    error_message = "API Gateway must be allowed to invoke the Functions router."
  }

  assert {
    condition     = oci_identity_policy.runtime_tenancy.statements == tolist(["Allow dynamic-group jetuse-spike-iam01-runtime-dg to read objectstorage-namespaces in tenancy"])
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
    condition     = length(oci_identity_policy.runtime.statements) == 17
    error_message = "Minimal runtime policy must contain runtime and ADB statements only."
  }

  assert {
    condition     = length(oci_identity_policy.deployer) == 0
    error_message = "Deployer policy must be omitted when disabled."
  }
}
