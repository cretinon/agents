---
name: bats
description: Manage and maintain BATS (Bash Automated Testing System) test suites — mapping shell code to behavioral tests, writing idiomatic bats tests with bats-support/bats-assert, mocking external dependencies, and keeping 100% test-to-code synchronicity. Use when writing, refactoring, or reviewing .bats test files.
---

# Agent Skill Profile: BATS Test Suite Manager

## 1. Skill Objective & Core Capability
The agent possesses advanced capabilities to systematically parse POSIX/Bash shell scripts, map code logic to behavioral tests, and maintain an automated **Bash Automated Testing System (BATS)** ecosystem with 100% test-to-code synchronicity.

---

## 2. Context Analysis & Execution Workflow
When a code change occurs or a test updates, the agent MUST execute the following cognitive workflow before writing any code:


### Step 1: Code Component Mapping
- Parse target scripts located 
- Extract functions, global variables, exit codes, and environmental side-effects (e.g., file creation, network mutations).
- Identify mocking targets (e.g., blocking `curl`, `rm -rf`, or proprietary binaries).

### Step 2: Test Case Matrix Formulation
For every function or logical branch modified, generate a mental or explicit test matrix accounting for:
- **Happy Path:** Valid inputs yielding expected exit code `0` and structural standard output (`stdout`).
- **Edge Cases:** Empty strings, out-of-bounds parameters, null bytes.
- **Failure Handling:** Invalid permissions, missing dependencies, non-zero exits (`stderr` verification).

---

## 3. Code Generation Patterns & Syntax Reference
The agent must generate idiomatic BATS syntax leveraging modern ecosystem standards (including `bats-support` and `bats-assert` if present).

### Standard BATS Test Template
```bats
#!/usr/bin/env bats

# Load helper libraries if available in the workspace environment
setup() {
    load 'test_helper/bats-support/load'
    load 'test_helper/bats-assert/load'
    
    # Source the target script to expose functions directly if applicable
    # Otherwise, rely on direct path execution
    SRC_DIR="$(dirname "${BATS_TEST_DIRNAME}")/src"
    source "${SRC_DIR}/target_script.sh"
    
    # Establish isolated environment per test
    TEST_SANDBOX="$(mktemp -d)"
    export TEST_SANDBOX
}

teardown() {
    # Clean up isolated state
    rm -rf "${TEST_SANDBOX}"
}

@test "successful execution of function_name with valid input" {
    run function_name "valid_argument"
    
    # Assertions
    assert_success
    assert_output "Expected Output String"
}

@test "graceful failure when required file is missing" {
    # Execution using BATS native run framework
    run target_script.sh --file "${TEST_SANDBOX}/non_existent.txt"
    
    assert_failure
    # Partial string match within stdout/stderr
    assert_output --partial "Error: File not found"
}
```

### Advanced Mocking Pattern
When external binaries are called within the target scripts, the agent must intercept them using local function overriding or path pollution within the test scope:

```bats
@test "mocks external curl network dependency" {
    # Inject temporary mock binary
    curl() {
        echo '{"status": "success", "mocked": true}'
        return 0
    }
    export -f curl

    run fetch_data_function
    assert_success
    assert_output --partial '"mocked": true'
}
```

---

## 4. Execution, Validation & Debugging Loop
The agent cannot assume a test passes based on generation alone. It must run validation tasks directly in the host/container environment.

### Command Execution Sequences

> **⚠️ NEVER run raw `bats`, `shellcheck`, or `kcov` binaries directly.** The project's
> sanctioned quality gate is the orchestrator wrapper (`shell/AGENTS.md` → Pre-Commit
> Verification Gate). It applies the project's custom lint rules and runtime setup that a
> direct binary invocation bypasses. ALWAYS use:
>
> ```shell
> # Full BATS suite for a library (LIB = shell, mcp, storm, ...)
> ${MY_GIT_DIR}/shell/my_warp.sh --lib "$LIB" -b
> # ShellCheck (syntax + project lint rules)
> ${MY_GIT_DIR}/shell/my_warp.sh --lib "$LIB" -s
> # kcov code coverage (must stay above the project threshold)
> ${MY_GIT_DIR}/shell/my_warp.sh --lib "$LIB" -k AI
> ```

1. **Run Full Suite (mandatory):** `${MY_GIT_DIR}/shell/my_warp.sh --lib "$LIB" -b`
2. **Run Single File (debugging only):** a raw `bats bats/specific_feature.bats` may be used
   *locally* to iterate quickly, but it bypasses the wrapper's custom lint/setup — the final
   verification before finishing a task MUST always be the full wrapper gate
   (`my_warp.sh --lib "$LIB" -s -b -k`, exit code `0` each).
3. **Run with Pretty Formatting:** the wrapper handles formatting; do not add raw
   `bats --formatter ...` invocations to scripts or CI.

### Error Interpretation & Remediation Matrix
If BATS returns failing statuses, the agent must triage using the following patterns:

| Error Symptom | Common Root Cause | Agent Remediation Step |
| :--- | :--- | :--- |
| `Status 127` encountered | Script or helper command not found in `$PATH`. | Verify relative path routing inside `setup()`. Check file execution permissions (`chmod +x`). |
| Multi-line string match failing | `[ "$output" = "val" ]` does not natively support newlines well. | Switch to looping array checks `"${lines[0]}"` or utilize `assert_output` from `bats-assert`. |
| Unintended environment spillover | Test leakage from previous tests causing erratic suite behavior. | Audit `teardown()` routines. Force variable scoping using `local` inside target source scripts. |

---

## 5. Security & Isolation Safeguards
To comply with host security rules, the agent's BATS orchestration capabilities are limited by these defensive mechanics:
- **Sandbox Bound:** Never run a `.bats` file that performs raw destructive operations (`rm -rf /`, `chown`) without a verified `$TEST_SANDBOX` prefix directory guard loop.
- **No Credentials Hardcoding:** If a test requires API tokens, the agent must parse them from pre-existing environment variables or mock the processing function. Dummy values must be clear (e.g., `export API_KEY="mock_test_key_abc123"`).
