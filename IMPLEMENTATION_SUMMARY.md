# OpenHands Runtime Logging and Stability Improvements

## Summary

This implementation addresses two key requirements:

1. **Comprehensive Runtime Logging**: Added detailed logging throughout the runtime initialization and execution process using `openhands_logger`
2. **Server Stability Issues**: Investigated and fixed server process stability issues (Note: OpenHands uses uvicorn, not gunicorn)

## Key Changes Made

### 1. Enhanced Logging in LocalRuntime (`openhands/runtime/impl/local/local_runtime.py`)

#### Initialization Logging
- Added detailed logging for runtime initialization process
- Workspace setup and validation logging
- User management and permission setup logging
- Plugin loading and configuration logging
- Component initialization status tracking

#### Connection and Health Check Logging
- Server process creation with PID tracking
- Health check attempts with detailed error reporting
- Process death detection with exit code logging
- Connection retry attempts and failures

#### Action Execution Logging
- Action execution requests with type identification
- Server state validation before execution
- Process recovery from global server dictionary
- Detailed error handling for disconnected servers

#### Process Management Logging
- Server termination with graceful shutdown attempts
- Process cleanup with timeout handling
- Thread management for log output capture
- Resource cleanup validation

### 2. Enhanced Logging in ActionExecutor (`openhands/runtime/action_execution_server.py`)

#### Server Startup Logging
- Detailed server initialization process
- Environment variable configuration logging
- Plugin loading and setup tracking
- Component initialization status

#### Resource Monitoring
- Real-time memory usage tracking
- CPU utilization monitoring
- Thread count monitoring
- Automatic resource warnings for high usage

#### Process Lifecycle Logging
- Server startup with configuration details
- Health endpoint registration
- Graceful shutdown procedures
- Error handling and recovery

### 3. Server Stability Improvements

#### Process Management Enhancements
- Enhanced PID tracking and process monitoring
- Improved process death detection
- Graceful termination with fallback to kill
- Thread-safe server registry management

#### Resource Monitoring
- Added memory usage tracking with warnings
- CPU utilization monitoring
- Thread count monitoring with alerts
- Automatic resource cleanup

#### Error Handling Improvements
- Better error recovery mechanisms
- Detailed error logging with context
- Process state validation
- Connection retry logic improvements

## Technical Details

### Logging Infrastructure
- Uses existing `openhands_logger` from `openhands.core.logger`
- Consistent logging levels: INFO for normal operations, DEBUG for detailed tracing, ERROR for failures, WARNING for concerning conditions
- All logging integrates with existing OpenHands logging infrastructure

### Process Management
- Uses `subprocess.Popen` for server process creation
- Implements threading for log output capture
- Global server registry (`_RUNNING_SERVERS`) for process tracking
- Graceful shutdown with timeout handling

### Resource Monitoring
- Uses `psutil` for system resource monitoring
- 30-second monitoring intervals
- Automatic warnings at 80% memory usage
- Thread count monitoring for stability

## Files Modified

1. **`openhands/runtime/impl/local/local_runtime.py`**
   - Added comprehensive logging throughout LocalRuntime class
   - Enhanced process management and monitoring
   - Improved error handling and recovery

2. **`openhands/runtime/action_execution_server.py`**
   - Added detailed server startup logging
   - Implemented resource monitoring
   - Enhanced process lifecycle management

3. **`server_stability_analysis.md`** (Created)
   - Comprehensive analysis of server stability issues
   - Detailed explanation of fixes implemented
   - Troubleshooting guide for future issues

## Server Technology Clarification

**Important**: OpenHands uses **uvicorn** (not gunicorn) as its ASGI server. The stability issues were related to:
- Process management in local runtime mode
- Resource exhaustion under high thread counts
- Inadequate monitoring and error handling

## Testing and Validation

- All code passes pre-commit hooks (ruff, mypy, formatting)
- Logging integration tested with existing infrastructure
- Process management improvements validated
- Resource monitoring functionality verified

## Usage

The enhanced logging will automatically provide detailed information about:
- Runtime initialization progress
- Server startup and health checks
- Action execution flow
- Process lifecycle events
- Resource usage patterns
- Error conditions and recovery attempts

No configuration changes are required - the logging enhancements are integrated into the existing OpenHands logging system.

## Benefits

1. **Improved Debugging**: Detailed logs make it easier to diagnose runtime issues
2. **Better Monitoring**: Resource usage tracking helps identify performance problems
3. **Enhanced Stability**: Improved process management reduces server crashes
4. **Proactive Alerts**: Resource warnings help prevent system overload
5. **Better Error Recovery**: Enhanced error handling improves system resilience
