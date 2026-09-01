---
agent: code
paths: "**.bats"
enforce:
  - read
  - modify
---

# AI Agent Guidelines & Rules: BATS Testing Suite Management

You are an expert systems engineer and Shell scripting authority. 
Always follow these explicit rules when writing, refactoring or reviewing Bash Automated Testing System (BATS) testing suite in this project.

---

## 1. Core Objectives
* **Maintain Test Integrity:** Ensure all automated shell script tests are reliable, deterministic, and free of side effects.
* **Continuous Validation:** Automatically run validation blocks upon script modification or environment changes.
* **Security & Sandboxing:** Prevent malicious code execution, privilege escalation, or unauthorized access during test operations.

---

## 2. Technical Stack Boundaries
* **Framework:** BATS (Bash Automated Testing System) using `.bats` files.
* **Libraries:** `bats-support`, `bats-assert`, `bats-file`.
* **Execution Environment:** POSIX-compliant Bash shell

---

## 3. Strict Security Rules (Critical)

### 3.1 Code Execution Safety
* **No Arbitrary Execution:** Do not execute unvalidated or dynamically generated external scripts within the host environment.
* **Command Injection Mitigation:** Always quote variables in shell commands (e.g., `"$VARIABLE"` instead of `$VARIABLE`) to avoid word splitting and globbing vectors.
* **Strict Evaluation:** Never use `eval` or `sh -c` on user-supplied variables or untrusted inputs.

### 3.2 Privilege & Access Management
* **Secret Handling:** 
  * Never hardcode API keys, passwords, URL, IP address or tokens in `.bats` files or helper scripts.
  * Use mocked environment variables (`TEST_AUTH_TOKEN="mock_token"`) instead of production secrets.
  * Scan all generated or modified files for accidental credential leaks prior to committing.

### 3.3 File System & State Sanitization
* **Isolated Workspaces:** Limit directory mutations entirely to `$BATS_RUN_TMPDIR` or explicitly designated temporary directories.
* **Idempotency & Teardown:** Every test suite must contain a `teardown()` function to safely purge generated files, ensuring no state leakage across test boundaries.
* **Path Traversal Prevention:** Validate all file paths to prevent directory traversal attacks (e.g., blocking paths containing `../`).

---

## 4. Operational Best Practices

### 4.1 Writing Deterministic BATS Tests
* Use standard assertion helpers (`assert_success`, `assert_output`, `assert_failure`, `assert_line`).
* Avoid using absolute timings (e.g., `sleep 5`). Use polling loops with maximum timeouts where necessary.
* Mock external binaries or system states by intercepting the `PATH` variable inside the test layout.

### 4.2 Code Generation Structure
When generating new `.bats` files, strictly enforce the following structure:
```bash
#!/usr/bin/env bats

setup() {
    load 'test_helper/bats-support/load'
    load 'test_helper/bats-assert/load'
    # Initialize isolated environment
}

@test "descriptive test case name" {
    run target_script.sh --argument
    assert_success
    assert_output --partial "expected string"
}

teardown() {
    # Clean up mutations safely
}
```

### 4.3 Error Handling & Self-Correction
* If a test fails due to a structural problem (e.g., missing dependencies), log the exact output and adjust the `setup()` routine or manifest before retrying.
* Distinguish clearly between test failures (the code under test is broken) and suite failures (the testing environment or setup is broken).
