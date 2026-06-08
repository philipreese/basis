---
trigger: always_on
---

# 🗺️ Rule 01: Orchestration & Agent Routing

## 1. Goal
To manage complex development requests by decomposing them into structured task plans (DAGs), executing tasks concurrently using specialized subagents, and validating results systematically.

## 2. Planning Protocol
Before modifying code:
1.  **Deconstruct Requirements**: Break down the user's prompt into atomic components.
2.  **Define Dependency DAG**: Map dependencies between tasks. (e.g., database schema changes must occur before service implementations).
3.  **Assign Specialists**: Determine which agent role (Architect, Developer, QA, Security, IV&V) is responsible for each node in the DAG.

## 3. Subagent Management & Execution
- **Default to Self-Perform**: The Orchestrator should execute atomic, linear, or straightforward tasks directly within the main session workflow to optimize token efficiency and minimize orchestration latency.
- **Conditional Delegation**: ONLY invoke specialized subagents via `invoke_subagent` if the task meets at least one of the following criteria:
  1. It requires massive, parallelisable execution paths that can run concurrently.
  2. It demands a hard context pivot or distinct role-based perspective (e.g., an objective black-box verification pass by the IV&V Verifier).
  3. The complexity or token size of the sub-task threatens to pollute the main orchestrator’s active context window.
- Keep subagent prompts highly specific, bounded, and outcome-oriented.
- Always provide the subagent with the local project rules and specific context files needed.

## 4. Conflict & Error Handling
- If a subagent reports a failure or block, stop dependent paths.
- Analyze logs, compile errors, or test outcomes.
- Feed the error details back to the agent or launch a debugger subagent to isolate the issue before proceeding.
