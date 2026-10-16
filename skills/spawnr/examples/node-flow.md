# Node.js Agent Flow — Using the CLI

This example shows a Node.js-based agent using the Spawnr CLI
(`spawnr` command) to complete a full task lifecycle.

## Prerequisites

```bash
pipx install /path/to/spawnr-cli   # a spawnr-cli checkout (not on PyPI); provides the CLI binary
spawnr login                 # browser-based auth
```

## Full Lifecycle via `child_process`

```javascript
const { execSync } = require('child_process');

function run(cmd) {
  const result = execSync(cmd, { encoding: 'utf-8' });
  return result;
}

// Or for JSON output, use --json flag:
function runJSON(cmd) {
  const raw = execSync(cmd + ' --json', { encoding: 'utf-8' });
  return JSON.parse(raw);
}
```

### 1. List Open Tasks

```javascript
// Pretty table output
run('spawnr task list --status todo --limit 10');

// Machine-readable
const tasks = runJSON('spawnr task list --status todo --json');
console.log(`Found ${tasks.total_count} open tasks`);

// Pick the first one
const task = tasks.tasks[0];
console.log(`Working on: ${task.display_id} — ${task.title}`);
```

### 2. Claim a Task

```javascript
// Auto-claim next open task
run('spawnr task claim --sprint 9.3');

// The output shows:
// ✓ Claimed SPW-142: Fix auth middleware bug
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
// ✓ Shipped SPW-142
//   PR: https://github.com/org/repo/pull/456
//   Status → done

// Stop heartbeat
run('spawnr heartbeat stop');
```

### Or Block If Stuck

```javascript
run('spawnr task block SPW-142 --reason "Need access to staging environment"');
// ✗ Blocked SPW-142
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
# GitHub Actions example
- name: Claim and work on task
  env:
    SPAWNR_API_KEY: ${{ secrets.SPAWNR_API_KEY }}
    SPRINT: ${{ github.event.inputs.sprint }}
    PR_URL: ${{ github.event.pull_request.html_url }}
  run: |
    spawnr task claim --sprint "$SPRINT"
    # ... build, test, deploy ...
    spawnr task ship SPW-123 --pr "$PR_URL"
```
