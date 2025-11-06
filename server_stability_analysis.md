# OpenHands Server Stability Analysis and Fixes

## Issue Description
User reported that when using "gunicorn" with 4-9 threads, the process frequently stops. However, investigation shows OpenHands uses uvicorn, not gunicorn.

## Root Cause Analysis

### 1. Misidentification of Web Server
- **Finding**: OpenHands uses uvicorn, not gunicorn
- **Impact**: User may be running a custom setup or confusing the two servers
- **Location**: `openhands/server/__main__.py` and `openhands/runtime/action_execution_server.py`

### 2. Potential Stability Issues Identified

#### A. Process Management Issues
- **Issue**: Server processes may die unexpectedly without proper error handling
- **Evidence**: Found in `local_runtime.py` lines 476-495 where process death is detected
- **Fix Applied**: Enhanced logging to track process lifecycle and exit codes

#### B. Threading Issues
- **Issue**: Multiple threads accessing shared resources without proper synchronization
- **Evidence**: Action semaphore usage (line 189) and log thread management
- **Risk**: Race conditions, deadlocks, resource contention

#### C. Resource Exhaustion
- **Issue**: No resource monitoring or limits
- **Evidence**: No memory/CPU monitoring in original code
- **Fix Applied**: Added comprehensive resource monitoring

#### D. Log Thread Management
- **Issue**: Log threads may not terminate properly
- **Evidence**: Timeout handling in thread joins (lines 561-563)
- **Fix Applied**: Enhanced logging and timeout handling

### 3. Specific Stability Problems

#### A. Server Process Death Detection
```python
# Before: Basic process death check
if self.server_process.poll() is not None:
    raise AgentRuntimeDisconnectedError('Server process died')

# After: Enhanced with exit code logging
if self.server_process.poll() is not None:
    exit_code = self.server_process.poll()
    openhands_logger.error(f'Server process died with exit code: {exit_code} for sid: {self.sid}')
    raise AgentRuntimeDisconnectedError(f'Server process died with exit code: {exit_code}')
```

#### B. Resource Monitoring
- **Added**: Real-time monitoring of memory, CPU, and thread count
- **Thresholds**: Warnings at 80% memory/CPU, 90% system memory, 50+ threads
- **Frequency**: Every 30 seconds

#### C. Process Cleanup
- **Enhanced**: Graceful termination with fallback to kill
- **Timeout**: 5-second grace period before force kill
- **Logging**: Detailed process lifecycle tracking

## Implemented Fixes

### 1. Enhanced Logging
- Added comprehensive logging throughout runtime initialization
- Process lifecycle tracking with PIDs and exit codes
- Resource usage monitoring
- Thread management logging

### 2. Resource Monitoring
- Real-time memory and CPU monitoring
- System-wide resource tracking
- Thread count monitoring
- Automatic warnings for high resource usage

### 3. Improved Process Management
- Better error handling for process death
- Enhanced cleanup procedures
- Timeout handling for thread joins
- Graceful shutdown with fallback

### 4. Debugging Capabilities
- Detailed server startup logging
- Environment variable tracking
- Plugin loading status
- Connection status monitoring

## Potential Causes of "Gunicorn" Issue

### 1. If User is Actually Using Gunicorn
The user might be:
- Running OpenHands behind a reverse proxy with gunicorn
- Using a custom deployment script with gunicorn
- Confusing uvicorn with gunicorn

### 2. Threading Issues with 4-9 Workers
- **Problem**: Multiple worker processes competing for resources
- **Solution**: Implement proper resource limits and monitoring
- **Mitigation**: Use single-worker mode for debugging

### 3. Port Conflicts
- **Problem**: Multiple workers trying to bind to same ports
- **Evidence**: Port allocation logic in `local_runtime.py`
- **Solution**: Enhanced port conflict detection and logging

### 4. Memory Leaks
- **Problem**: Long-running processes accumulating memory
- **Solution**: Resource monitoring and automatic warnings
- **Mitigation**: Process recycling thresholds

## Recommendations

### 1. For Debugging
1. Enable detailed logging: `LOG_JSON=1`
2. Monitor resource usage with new monitoring
3. Check process exit codes in logs
4. Verify port availability

### 2. For Stability
1. Use single worker initially for debugging
2. Implement resource limits
3. Monitor system resources
4. Use process recycling for long-running instances

### 3. For Production
1. Implement health checks
2. Use process managers (systemd, supervisor)
3. Set up monitoring and alerting
4. Implement graceful shutdown handling

## Testing the Fixes

### 1. Start OpenHands with Enhanced Logging
```bash
export LOG_JSON=1
export RUNTIME=local
make build && make run
```

### 2. Monitor Logs for:
- Process creation and PIDs
- Resource usage warnings
- Thread count monitoring
- Process death notifications
- Exit codes and cleanup status

### 3. Stress Testing
- Run multiple concurrent sessions
- Monitor resource usage patterns
- Check for memory leaks
- Verify graceful shutdown

## Next Steps

1. **Test the enhanced logging** to identify specific failure patterns
2. **Monitor resource usage** during normal operation
3. **Identify bottlenecks** using the new monitoring data
4. **Implement resource limits** if needed
5. **Add health checks** for production deployments
