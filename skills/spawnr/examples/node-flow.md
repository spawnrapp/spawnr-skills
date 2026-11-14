# Node.js Agent Flow — Using the CLI

This example shows a Node.js-based agent using the Spawnr CLI
(`spawnr` command) to complete a full task lifecycle.

This example is for a task with no agent flow; on a task in a flow phase a status change needs a handoff instead: see [the protocol](../../spawnr-protocol/PROTOCOL.md#handoffs).

## Prerequisites

```bash
pipx install spawnr          # the Spawnr CLI, from PyPI; provides the `spawnr` binary
spawnr login                 # browser-based auth
```

## Full Lifecycle via `child_process`

```javascript
const { execSync } = require('child_process');

// Human output, as in a terminal: with its stdout piped, the CLI prints JSON
function run(cmd) {
  const result = execSync(cmd, { encoding: 'utf-8', env: { ...process.env, SPAWNR_OUTPUT: 'human' } });
  return result;
}

// Or for JSON output, use --json flag; this returns the envelope's "data":
function runJSON(cmd) {
  const raw = execSync(cmd + ' --json', { encoding: 'utf-8' });
  return JSON.parse(raw).data;
}
```

### 1. List Open Tasks

```javascript
// Pretty table output of your open tasks
run('spawnr task list --status todo --limit 10');

// Machine-readable
const { tasks } = runJSON('spawnr task list --status todo');
console.log(`Found ${tasks.length} of your open tasks (first page)`);

// Pick the first one
const task = tasks[0];
console.log(`Working on: ${task.display_id} — ${task.title}`);
```

### 2. Claim a Task

```javascript
// Auto-claim next open task
run('spawnr task claim --sprint 9.3');

// The output shows:
// Claimed SPW-142: Fix auth middleware bug
//   Status → in_progress
//   Assignee → agent-abc123
```

### 3. Do the Work

```javascript
// Start heartbeat for long-running tasks
run('spawnr heartbeat start --task SPW-142 --step "Analyzing auth flow" --interval 30');

// ... your agent does the actual work here ...

// Check status
const status = run('spawnr heartbeat status');
console.log(status);
```

### 4. Ship with PR

```javascript
// After opening a PR:
run('spawnr task ship SPW-142 --pr https://github.com/org/repo/pull/456');
// Shipped SPW-142
//   PR: https://github.com/org/repo/pull/456
//   Status → done

// Stop heartbeat
run('spawnr heartbeat stop');
```

### Or Block If Stuck

```javascript
run('spawnr task block SPW-142 --reason "Need access to staging environment"');
// Blocked SPW-142
//   Reason: Need access to staging environment
//   Status → blocked
```

### 5. Stream Events

```javascript
const { spawn } = require('child_process');

// Stream real-time events
const watcher = spawn('spawnr', ['watch', '--pretty']);
watcher.stdout.on('data', (data) => {
  console.log(`Event: ${data}`);
});

// Filter specific events
const taskWatcher = spawn('spawnr', ['watch', '--filter', 'task.status_changed']);
taskWatcher.stdout.on('data', (data) => {
  const event = JSON.parse(data);
  console.log(`Task ${event.task_id} → ${event.new_status}`);
});
```

## CI/CD Integration

```yaml
# .github/workflows/work-task.yml: run it by hand from the Actions tab
on:
  workflow_dispatch:
    inputs:
      sprint:
        required: true
jobs:
  work:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pipx install spawnr
      - name: Claim and work on task
        env:
          SPAWNR_API_KEY: ${{ secrets.SPAWNR_API_KEY }}
          SPRINT: ${{ github.event.inputs.sprint }}
        run: |
          [ -n "$SPRINT" ] || { echo "No sprint given" >&2; exit 1; }
          CLAIMED=$(spawnr task claim --sprint "$SPRINT" --output json)
          TASK_ID=$(echo "$CLAIMED" | jq -r '.data.display_id // .data.id // empty')
          if [ -z "$TASK_ID" ]; then
            echo "No open task in sprint $SPRINT"
            exit 0
          fi
          # ... build, test, open a PR, and put its URL in PR_URL,
          # e.g. PR_URL=$(gh pr create --fill) with GH_TOKEN set ...
          [ -n "$PR_URL" ] || { echo "No PR URL for $TASK_ID" >&2; exit 1; }
          spawnr task ship "$TASK_ID" --pr "$PR_URL"
```
