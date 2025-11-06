# Gunicorn Multi-Worker Issues with LocalRuntime

## Problem Analysis

When running OpenHands with gunicorn using multiple workers (4-9 threads), the LocalRuntime can experience process stops and conflicts. This document analyzes the root causes and implemented fixes.

## Root Causes

### 1. Process Isolation Issues
- Each gunicorn worker runs in its own process with separate memory space
- Global dictionaries (`_RUNNING_SERVERS`, `_WARM_SERVERS`) are per-process, not shared between workers
- Workers cannot see servers created by other workers, leading to resource conflicts

### 2. Port Conflicts
- Multiple workers attempt to allocate ports from the same ranges simultaneously
- Race conditions occur when workers try to bind to the same ports
- No coordination between workers for port allocation

### 3. Orphaned Processes
- When a gunicorn worker dies or restarts, its LocalRuntime servers become orphaned
- Orphaned servers continue consuming resources (ports, processes, disk space)
- No cleanup mechanism for orphaned servers across worker boundaries

### 4. Resource Contention
- Multiple workers creating temporary workspaces simultaneously
- File system locks and permission issues
- Process management conflicts

## Implemented Fixes

### 1. Enhanced Logging and Monitoring
- **Process Chain Detection**: Logs the full process hierarchy to detect gunicorn environments
- **Multi-Worker Warnings**: Explicit warnings when running under gunicorn
- **Server Lifecycle Tracking**: Detailed logging of server creation, registration, and cleanup
- **Port Allocation Logging**: Comprehensive logging of port allocation and conflicts
- **Health Monitoring**: Regular health checks of server processes

### 2. Worker-Specific Port Offsets
- **Dynamic Port Ranges**: Each worker gets a unique port offset based on process ID
- **Conflict Prevention**: Reduces likelihood of port conflicts between workers
- **Deterministic Allocation**: Uses `(PID % 100) * 10` for consistent offsets

### 3. Orphaned Process Detection and Cleanup
- **Startup Cleanup**: Checks for and cleans up orphaned servers during initialization
- **Process Validation**: Verifies server processes are still running
- **Workspace Cleanup**: Removes temporary workspaces from dead processes

### 4. Enhanced Error Handling
- **Graceful Degradation**: Better error handling when servers fail to start
- **Resource Cleanup**: Proper cleanup of resources when errors occur
- **Timeout Management**: Appropriate timeouts for process operations

## Code Changes

### LocalRuntime Initialization (`__init__`)
```python
# Added process chain detection
process_chain = []
proc = current_process
while proc and len(process_chain) < 5:
    process_chain.append(f'{proc.name()}({proc.pid})')
    proc = proc.parent()

# Gunicorn detection and warnings
if any('gunicorn' in name.lower() for name in process_chain):
    logger.warning('Running under gunicorn - potential multi-worker scenario detected')

# Orphaned server cleanup
for sid, server_info in list(_RUNNING_SERVERS.items()):
    if server_info.process.poll() is not None:
        logger.warning(f'Detected orphaned server for session {sid}')
        # Cleanup logic...
```

### Port Allocation (`_create_server`)
```python
# Worker-specific port offset
worker_offset = (current_process.pid % 100) * 10

# Apply offset to port ranges
execution_range = (EXECUTION_SERVER_PORT_RANGE[0] + worker_offset,
                  EXECUTION_SERVER_PORT_RANGE[1] + worker_offset)
# Similar for other port ranges...

# Enhanced conflict detection
existing_ports = []
for sid, server_info in _RUNNING_SERVERS.items():
    existing_ports.extend([server_info.execution_server_port, ...])

conflicts = set(all_allocated_ports) & set(existing_ports)
if conflicts:
    logger.error(f'Port conflicts detected: {conflicts}')
```

### Server Process Management
```python
# Enhanced process creation with validation
server_process = subprocess.Popen(cmd, ...)
time.sleep(0.1)  # Give process time to start
if server_process.poll() is not None:
    raise RuntimeError(f'Server process failed to start')

# Improved log monitoring with health checks
def log_output():
    last_health_check = time.time()
    while server_process.poll() is None:
        if current_time - last_health_check > 10:
            logger.debug(f'Server health check - PID: {server_process.pid}')
```

### Server Cleanup (`close`)
```python
# Enhanced cleanup with detailed logging
if self.server_process.poll() is None:
    logger.info('Server process is still running, sending terminate signal')
    self.server_process.terminate()
    # Graceful termination with fallback to kill...
else:
    logger.info(f'Server process was already terminated')
```

## Recommendations

### 1. Avoid Multi-Worker Mode for LocalRuntime
- Use single-worker mode (`--workers=1`) when using LocalRuntime
- Multi-worker mode is better suited for stateless applications
- LocalRuntime maintains stateful server processes that don't work well with worker isolation

### 2. Alternative Architectures
- **Separate Runtime Service**: Run LocalRuntime servers in a separate service
- **Shared State Management**: Use external state management (Redis, database) for server coordination
- **Container-Based Isolation**: Use Docker runtime instead of LocalRuntime for better isolation

### 3. Configuration Options
```bash
# Recommended gunicorn configuration for LocalRuntime
gunicorn --workers=1 --threads=4-9 app:application

# Alternative: Use async workers
gunicorn --worker-class=uvicorn.workers.UvicornWorker --workers=1 app:application
```

### 4. Monitoring and Alerting
- Monitor for orphaned processes: `ps aux | grep action_execution_server`
- Check for port conflicts in logs
- Monitor temporary workspace disk usage
- Set up alerts for server process failures

## Testing Multi-Worker Scenarios

### Test Setup
```bash
# Start with multiple workers
gunicorn --workers=4 --bind=0.0.0.0:8000 app:application

# Monitor processes
watch 'ps aux | grep -E "(gunicorn|action_execution_server)"'

# Check port usage
netstat -tlnp | grep -E "(8000|42[0-9]{3})"
```

### Expected Behavior with Fixes
- Workers should get different port ranges (offset by PID)
- Orphaned servers should be detected and cleaned up
- Detailed logging should show worker coordination issues
- Graceful degradation when conflicts occur

## Future Improvements

1. **Shared State Backend**: Implement Redis-based server registry
2. **Port Coordination Service**: Centralized port allocation service
3. **Health Check Endpoints**: HTTP endpoints for server health monitoring
4. **Automatic Recovery**: Automatic restart of failed servers
5. **Resource Limits**: Per-worker resource limits and quotas

## Conclusion

The implemented fixes provide better visibility into multi-worker issues and reduce the likelihood of conflicts. However, the fundamental architecture of LocalRuntime is not well-suited for multi-worker environments. For production deployments requiring high concurrency, consider using Docker runtime or implementing a separate runtime service architecture.
