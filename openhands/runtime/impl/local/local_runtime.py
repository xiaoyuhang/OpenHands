"""This runtime runs the action_execution_server directly on the local machine without Docker."""

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

import httpx
import tenacity

import openhands
from openhands.core.config import OpenHandsConfig
from openhands.core.exceptions import AgentRuntimeDisconnectedError
from openhands.core.logger import openhands_logger as logger
from openhands.events import EventStream
from openhands.events.action import (
    Action,
)
from openhands.events.observation import (
    Observation,
)
from openhands.events.serialization import event_to_dict, observation_from_dict
from openhands.integrations.provider import PROVIDER_TOKEN_TYPE
from openhands.llm.llm_registry import LLMRegistry
from openhands.runtime.impl.action_execution.action_execution_client import (
    ActionExecutionClient,
)
from openhands.runtime.impl.docker.docker_runtime import (
    APP_PORT_RANGE_1,
    APP_PORT_RANGE_2,
    EXECUTION_SERVER_PORT_RANGE,
    VSCODE_PORT_RANGE,
)
from openhands.runtime.plugins import PluginRequirement
from openhands.runtime.plugins.vscode import VSCodeRequirement
from openhands.runtime.runtime_status import RuntimeStatus
from openhands.runtime.utils import find_available_tcp_port
from openhands.runtime.utils.command import get_action_execution_server_startup_command
from openhands.utils.async_utils import call_sync_from_async
from openhands.utils.http_session import httpx_verify_option
from openhands.utils.tenacity_stop import stop_if_should_exit


@dataclass
class ActionExecutionServerInfo:
    """Information about a running server process."""

    process: subprocess.Popen
    execution_server_port: int
    vscode_port: int
    app_ports: list[int]
    log_thread: threading.Thread
    log_thread_exit_event: threading.Event
    temp_workspace: str | None
    workspace_mount_path: str


# Global dictionary to track running server processes by session ID
_RUNNING_SERVERS: dict[str, ActionExecutionServerInfo] = {}

# Global list to track warm servers waiting for use
_WARM_SERVERS: list[ActionExecutionServerInfo] = []


def get_user_info() -> tuple[int, str | None]:
    """Get user ID and username in a cross-platform way."""
    username = os.getenv('USER')
    if sys.platform == 'win32':
        # On Windows, we don't use user IDs the same way
        # Return a default value that won't cause issues
        return 1000, username
    else:
        # On Unix systems, use os.getuid()
        return os.getuid(), username


def check_dependencies(code_repo_path: str, check_browser: bool) -> None:
    ERROR_MESSAGE = 'Please follow the instructions in https://github.com/OpenHands/OpenHands/blob/main/Development.md to install OpenHands.'
    if not os.path.exists(code_repo_path):
        raise ValueError(
            f'Code repo path {code_repo_path} does not exist. ' + ERROR_MESSAGE
        )
    # Check jupyter is installed
    logger.debug('Checking dependencies: Jupyter')
    output = subprocess.check_output(
        [sys.executable, '-m', 'jupyter', '--version'],
        text=True,
        cwd=code_repo_path,
    )
    logger.debug(f'Jupyter output: {output}')
    if 'jupyter' not in output.lower():
        raise ValueError('Jupyter is not properly installed. ' + ERROR_MESSAGE)

    # Check libtmux is installed (skip on Windows)
    if sys.platform != 'win32':
        logger.debug('Checking dependencies: libtmux')
        import libtmux

        server = libtmux.Server()
        try:
            session = server.new_session(session_name='test-session')
        except Exception:
            raise ValueError('tmux is not properly installed or available on the path.')
        pane = session.attached_pane
        pane.send_keys('echo "test"')
        pane_output = '\n'.join(pane.cmd('capture-pane', '-p').stdout)
        session.kill_session()
        if 'test' not in pane_output:
            raise ValueError('libtmux is not properly installed. ' + ERROR_MESSAGE)

    if check_browser:
        logger.debug('Checking dependencies: browser')
        from openhands.runtime.browser.browser_env import BrowserEnv

        browser = BrowserEnv()
        browser.close()


class LocalRuntime(ActionExecutionClient):
    """This runtime will run the action_execution_server directly on the local machine.
    When receiving an event, it will send the event to the server via HTTP.

    Args:
        config (OpenHandsConfig): The application configuration.
        event_stream (EventStream): The event stream to subscribe to.
        sid (str, optional): The session ID. Defaults to 'default'.
        plugins (list[PluginRequirement] | None, optional): list of plugin requirements. Defaults to None.
        env_vars (dict[str, str] | None, optional): Environment variables to set. Defaults to None.
    """

    def __init__(
        self,
        config: OpenHandsConfig,
        event_stream: EventStream,
        llm_registry: LLMRegistry,
        sid: str = 'default',
        plugins: list[PluginRequirement] | None = None,
        env_vars: dict[str, str] | None = None,
        status_callback: Callable[[str, RuntimeStatus, str], None] | None = None,
        attach_to_existing: bool = False,
        headless_mode: bool = True,
        user_id: str | None = None,
        git_provider_tokens: PROVIDER_TOKEN_TYPE | None = None,
    ) -> None:
        logger.info(f'Initializing LocalRuntime with session ID: {sid}')
        logger.info(
            f'Runtime configuration - headless_mode: {headless_mode}, attach_to_existing: {attach_to_existing}'
        )
        logger.info(
            f'Plugins requested: {[p.name for p in plugins] if plugins else "None"}'
        )
        logger.info(
            f'Environment variables provided: {len(env_vars) if env_vars else 0} variables'
        )

        self.is_windows = sys.platform == 'win32'
        logger.info(
            f'Platform detected: {"Windows" if self.is_windows else "Unix-like"}'
        )

        if self.is_windows:
            logger.warning(
                'Running on Windows - some features that require tmux will be limited. '
                'For full functionality, please consider using WSL or Docker runtime.'
            )

        self.config = config
        self._user_id, self._username = get_user_info()
        logger.info(
            f'User information - ID: {self._user_id}, Username: {self._username}'
        )

        logger.warning(
            'Initializing LocalRuntime. WARNING: NO SANDBOX IS USED. '
            'This is an experimental feature, please report issues to https://github.com/OpenHands/OpenHands/issues. '
            '`run_as_openhands` will be ignored since the current user will be used to launch the server. '
            'We highly recommend using a sandbox (eg. DockerRuntime) unless you '
            'are running in a controlled environment.\n'
            f'User ID: {self._user_id}. '
            f'Username: {self._username}.'
        )

        # Initialize these values to be set in connect()
        self._temp_workspace: str | None = None
        self._execution_server_port = -1
        self._vscode_port = -1
        self._app_ports: list[int] = []

        self.api_url = (
            f'{self.config.sandbox.local_runtime_url}:{self._execution_server_port}'
        )
        self.status_callback = status_callback
        self.server_process: subprocess.Popen[str] | None = None
        self.action_semaphore = threading.Semaphore(1)  # Ensure one action at a time
        self._log_thread_exit_event = threading.Event()  # Add exit event

        # Update env vars
        if self.config.sandbox.runtime_startup_env_vars:
            logger.info(
                f'Updating environment with {len(self.config.sandbox.runtime_startup_env_vars)} startup variables'
            )
            os.environ.update(self.config.sandbox.runtime_startup_env_vars)
        else:
            logger.debug('No runtime startup environment variables to set')

        # Initialize the action_execution_server
        logger.info('Initializing ActionExecutionClient parent class')
        super().__init__(
            config,
            event_stream,
            llm_registry,
            sid,
            plugins,
            env_vars,
            status_callback,
            attach_to_existing,
            headless_mode,
            user_id,
            git_provider_tokens,
        )

        # If there is an API key in the environment we use this in requests to the runtime
        session_api_key = os.getenv('SESSION_API_KEY')
        self._session_api_key: str | None = None
        if session_api_key:
            logger.info(
                'Session API key found in environment, configuring authentication'
            )
            self.session.headers['X-Session-API-Key'] = session_api_key
            self._session_api_key = session_api_key
        else:
            logger.debug('No session API key found in environment')

        # Log process information for debugging multi-worker scenarios
        import psutil

        current_process = psutil.Process()
        parent_process = current_process.parent()

        logger.info(
            f'LocalRuntime process info - PID: {current_process.pid}, PPID: {parent_process.pid if parent_process else "None"}'
        )
        if parent_process:
            logger.info(
                f'Parent process: {parent_process.name()} (PID: {parent_process.pid})'
            )

        # Check for gunicorn/multi-worker environment
        process_chain: list[str] = []
        proc = current_process
        while proc and len(process_chain) < 5:  # Limit chain length
            try:
                process_chain.append(f'{proc.name()}({proc.pid})')
                proc = proc.parent()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                break

        logger.info(f'Process chain: {" -> ".join(reversed(process_chain))}')

        # Check for gunicorn master/worker scenario
        if any('gunicorn' in name.lower() for name in process_chain):
            logger.warning(
                'Running under gunicorn - potential multi-worker scenario detected'
            )
            logger.warning(
                'Multiple gunicorn workers may cause LocalRuntime server conflicts'
            )

        # Log current running servers count and check for orphaned processes
        logger.info(f'Current running servers count: {len(_RUNNING_SERVERS)}')
        logger.info(f'Current warm servers count: {len(_WARM_SERVERS)}')

        # Check for orphaned servers (processes that are no longer running)
        orphaned_sessions = []
        for sid, server_info in list(_RUNNING_SERVERS.items()):
            try:
                if server_info.process.poll() is not None:
                    logger.warning(
                        f'Detected orphaned server for session {sid} (process exited with code {server_info.process.poll()})'
                    )
                    orphaned_sessions.append(sid)
            except Exception as e:
                logger.error(f'Error checking server process for session {sid}: {e}')
                orphaned_sessions.append(sid)

        # Clean up orphaned servers
        for sid in orphaned_sessions:
            logger.info(f'Cleaning up orphaned server for session {sid}')
            try:
                server_info = _RUNNING_SERVERS[sid]
                if server_info.temp_workspace and os.path.exists(
                    server_info.temp_workspace
                ):
                    shutil.rmtree(server_info.temp_workspace)
                    logger.info(
                        f'Cleaned up orphaned workspace: {server_info.temp_workspace}'
                    )
                del _RUNNING_SERVERS[sid]
            except Exception as e:
                logger.error(f'Error cleaning up orphaned server {sid}: {e}')

        if orphaned_sessions:
            logger.info(f'Cleaned up {len(orphaned_sessions)} orphaned servers')
        else:
            logger.info('No orphaned servers detected')

        logger.info('LocalRuntime initialization completed successfully')

    @property
    def session_api_key(self) -> str | None:
        return self._session_api_key

    @property
    def action_execution_server_url(self) -> str:
        return self.api_url

    async def connect(self) -> None:
        """Start the action_execution_server on the local machine or connect to an existing one."""
        logger.info(f'Starting connection process for session {self.sid}')
        self.set_runtime_status(RuntimeStatus.STARTING_RUNTIME)

        # Get environment variables for warm server configuration
        desired_num_warm_servers = int(os.getenv('DESIRED_NUM_WARM_SERVERS', '0'))
        logger.info(
            f'Warm server configuration - desired count: {desired_num_warm_servers}, available: {len(_WARM_SERVERS)}'
        )

        # Check if there's already a server running for this session ID
        if self.sid in _RUNNING_SERVERS:
            logger.info(f'Found existing server for session {self.sid}')
            self.log('info', f'Connecting to existing server for session {self.sid}')
            server_info = _RUNNING_SERVERS[self.sid]
            self.server_process = server_info.process
            self._execution_server_port = server_info.execution_server_port
            self._log_thread = server_info.log_thread
            self._log_thread_exit_event = server_info.log_thread_exit_event
            self._vscode_port = server_info.vscode_port
            self._app_ports = server_info.app_ports
            self._temp_workspace = server_info.temp_workspace
            self.config.workspace_mount_path_in_sandbox = (
                server_info.workspace_mount_path
            )
            self.api_url = (
                f'{self.config.sandbox.local_runtime_url}:{self._execution_server_port}'
            )
            logger.info(
                f'Reusing existing server - port: {self._execution_server_port}, workspace: {server_info.workspace_mount_path}'
            )
        elif self.attach_to_existing:
            # If we're supposed to attach to an existing server but none exists, raise an error
            self.log('error', f'No existing server found for session {self.sid}')
            raise AgentRuntimeDisconnectedError(
                f'No existing server found for session {self.sid}'
            )
        else:
            logger.info('Setting up new server and workspace')
            # Set up workspace directory
            if self.config.workspace_base is not None:
                logger.info(
                    f'Using configured workspace base: {self.config.workspace_base}'
                )
                logger.warning(
                    f'Workspace base path is set to {self.config.workspace_base}. '
                    'It will be used as the path for the agent to run in. '
                    'Be careful, the agent can EDIT files in this directory!'
                )
                self.config.workspace_mount_path_in_sandbox = self.config.workspace_base
                self._temp_workspace = None
            else:
                # A temporary directory is created for the agent to run in
                logger.info(
                    'No workspace base configured, creating temporary directory'
                )
                logger.warning(
                    'Workspace base path is NOT set. Agent will run in a temporary directory.'
                )
                self._temp_workspace = tempfile.mkdtemp(
                    prefix=f'openhands_workspace_{self.sid}',
                )
                self.config.workspace_mount_path_in_sandbox = self._temp_workspace
                logger.info(f'Created temporary workspace: {self._temp_workspace}')

            logger.info(
                f'Using workspace directory: {self.config.workspace_mount_path_in_sandbox}'
            )

            # Check if we have a warm server available
            warm_server_available = False
            if _WARM_SERVERS and not self.attach_to_existing:
                logger.info(
                    f'Attempting to use warm server from pool of {len(_WARM_SERVERS)} available'
                )
                try:
                    # Pop a warm server from the list
                    self.log('info', 'Using a warm server')
                    server_info = _WARM_SERVERS.pop(0)
                    logger.info(
                        f'Retrieved warm server - port: {server_info.execution_server_port}, PID: {server_info.process.pid}'
                    )

                    # Use the warm server
                    self.server_process = server_info.process
                    self._execution_server_port = server_info.execution_server_port
                    self._log_thread = server_info.log_thread
                    self._log_thread_exit_event = server_info.log_thread_exit_event
                    self._vscode_port = server_info.vscode_port
                    self._app_ports = server_info.app_ports

                    # We need to clean up the warm server's temp workspace and create a new one
                    if server_info.temp_workspace:
                        shutil.rmtree(server_info.temp_workspace)

                    # Create a new temp workspace for this session
                    if (
                        self._temp_workspace is None
                        and self.config.workspace_base is None
                    ):
                        self._temp_workspace = tempfile.mkdtemp(
                            prefix=f'openhands_workspace_{self.sid}',
                        )
                        self.config.workspace_mount_path_in_sandbox = (
                            self._temp_workspace
                        )

                    self.api_url = f'{self.config.sandbox.local_runtime_url}:{self._execution_server_port}'

                    # Store the server process in the global dictionary with the new workspace
                    logger.info(
                        f'Registering warm server for session {self.sid} in global registry'
                    )
                    logger.info(
                        f'Server details - PID: {self.server_process.pid}, execution port: {self._execution_server_port}'
                    )
                    _RUNNING_SERVERS[self.sid] = ActionExecutionServerInfo(
                        process=self.server_process,
                        execution_server_port=self._execution_server_port,
                        vscode_port=self._vscode_port,
                        app_ports=self._app_ports,
                        log_thread=self._log_thread,
                        log_thread_exit_event=self._log_thread_exit_event,
                        temp_workspace=self._temp_workspace,
                        workspace_mount_path=self.config.workspace_mount_path_in_sandbox,
                    )
                    logger.info(
                        f'Successfully registered warm server for session {self.sid}'
                    )

                    warm_server_available = True
                except IndexError:
                    # No warm servers available
                    self.log('info', 'No warm servers available, starting a new server')
                    warm_server_available = False
                except Exception as e:
                    # Error using warm server
                    self.log('error', f'Error using warm server: {e}')
                    warm_server_available = False

            # If no warm server is available, start a new one
            if not warm_server_available:
                # Create a new server
                server_info, api_url = _create_server(
                    config=self.config,
                    plugins=self.plugins,
                    workspace_prefix=self.sid,
                )

                # Set instance variables
                self.server_process = server_info.process
                self._execution_server_port = server_info.execution_server_port
                self._vscode_port = server_info.vscode_port
                self._app_ports = server_info.app_ports
                self._log_thread = server_info.log_thread
                self._log_thread_exit_event = server_info.log_thread_exit_event

                # We need to use the existing temp workspace, not the one created by _create_server
                if (
                    server_info.temp_workspace
                    and server_info.temp_workspace != self._temp_workspace
                ):
                    shutil.rmtree(server_info.temp_workspace)

                self.api_url = api_url

                # Store the server process in the global dictionary with the correct workspace
                logger.info(
                    f'Registering new server for session {self.sid} in global registry'
                )
                logger.info(
                    f'Server details - PID: {self.server_process.pid}, execution port: {self._execution_server_port}'
                )
                _RUNNING_SERVERS[self.sid] = ActionExecutionServerInfo(
                    process=self.server_process,
                    execution_server_port=self._execution_server_port,
                    vscode_port=self._vscode_port,
                    app_ports=self._app_ports,
                    log_thread=self._log_thread,
                    log_thread_exit_event=self._log_thread_exit_event,
                    temp_workspace=self._temp_workspace,
                    workspace_mount_path=self.config.workspace_mount_path_in_sandbox,
                )
                logger.info(
                    f'Successfully registered new server for session {self.sid}'
                )
                logger.info(
                    f'Total running servers after registration: {len(_RUNNING_SERVERS)}'
                )

        self.log('info', f'Waiting for server to become ready at {self.api_url}...')
        self.set_runtime_status(RuntimeStatus.STARTING_RUNTIME)

        await call_sync_from_async(self._wait_until_alive)

        if not self.attach_to_existing:
            await call_sync_from_async(self.setup_initial_env)

        self.log(
            'debug',
            f'Server initialized with plugins: {[plugin.name for plugin in self.plugins]}',
        )
        if not self.attach_to_existing:
            self.set_runtime_status(RuntimeStatus.READY)
        self._runtime_initialized = True

        # Check if we need to create more warm servers after connecting
        if (
            desired_num_warm_servers > 0
            and len(_WARM_SERVERS) < desired_num_warm_servers
        ):
            num_to_create = desired_num_warm_servers - len(_WARM_SERVERS)
            self.log(
                'info',
                f'Creating {num_to_create} additional warm servers to reach desired count',
            )
            for _ in range(num_to_create):
                _create_warm_server_in_background(self.config, self.plugins)

    @classmethod
    def setup(cls, config: OpenHandsConfig, headless_mode: bool = False):
        should_check_dependencies = os.getenv('SKIP_DEPENDENCY_CHECK', '') != '1'
        if should_check_dependencies:
            code_repo_path = os.path.dirname(os.path.dirname(openhands.__file__))
            check_browser = config.enable_browser and sys.platform != 'win32'
            check_dependencies(code_repo_path, check_browser)

        initial_num_warm_servers = int(os.getenv('INITIAL_NUM_WARM_SERVERS', '0'))
        # Initialize warm servers if needed
        if initial_num_warm_servers > 0 and len(_WARM_SERVERS) == 0:
            plugins = _get_plugins(config)

            # Copy the logic from Runtime where we add a VSCodePlugin on init if missing
            if not headless_mode:
                plugins.append(VSCodeRequirement())

            for _ in range(initial_num_warm_servers):
                _create_warm_server(config, plugins)

    @tenacity.retry(
        wait=tenacity.wait_fixed(2),
        stop=tenacity.stop_after_delay(120) | stop_if_should_exit(),
        before_sleep=lambda retry_state: logger.debug(
            f'Waiting for server to be ready... (attempt {retry_state.attempt_number})'
        ),
    )
    def _wait_until_alive(self) -> bool:
        """Wait until the server is ready to accept requests."""
        logger.info(f'Checking if server is alive at {self.api_url}/alive')

        if self.server_process and self.server_process.poll() is not None:
            exit_code = self.server_process.poll()
            logger.error(f'Server process died with exit code: {exit_code}')
            raise RuntimeError(f'Server process died with exit code: {exit_code}')

        try:
            response = self.session.get(f'{self.api_url}/alive')
            response.raise_for_status()
            logger.info(f'Server is alive and responding at {self.api_url}')
            return True
        except Exception as e:
            logger.debug(f'Server not ready yet: {e}')
            self.log('debug', f'Server not ready yet: {e}')
            raise

    async def execute_action(self, action: Action) -> Observation:
        """Execute an action by sending it to the server."""
        logger.debug(f'Executing action: {action.__class__.__name__}')

        if not self.runtime_initialized:
            logger.error('Runtime not initialized when trying to execute action')
            raise AgentRuntimeDisconnectedError('Runtime not initialized')

        # Check if our server process is still valid
        if self.server_process is None:
            logger.warning('Server process is None, checking global dictionary')
            # Check if there's a server in the global dictionary
            if self.sid in _RUNNING_SERVERS:
                self.server_process = _RUNNING_SERVERS[self.sid].process
                logger.info(
                    f'Recovered server process from global dictionary for sid: {self.sid}'
                )
            else:
                logger.error(
                    f'Server process not found in global dictionary for sid: {self.sid}'
                )
                raise AgentRuntimeDisconnectedError('Server process not found')

        # Check if the server process is still running
        if self.server_process.poll() is not None:
            exit_code = self.server_process.poll()
            logger.error(
                f'Server process died with exit code: {exit_code} for sid: {self.sid}'
            )
            # If the process died, remove it from the global dictionary
            if self.sid in _RUNNING_SERVERS:
                del _RUNNING_SERVERS[self.sid]
                logger.info(
                    f'Removed dead server from global dictionary for sid: {self.sid}'
                )
            raise AgentRuntimeDisconnectedError(
                f'Server process died with exit code: {exit_code}'
            )

        with self.action_semaphore:
            try:
                response = await call_sync_from_async(
                    lambda: self.session.post(
                        f'{self.api_url}/execute_action',
                        json={'action': event_to_dict(action)},
                    )
                )

                # After executing the action, check if we need to create more warm servers
                desired_num_warm_servers = int(
                    os.getenv('DESIRED_NUM_WARM_SERVERS', '0')
                )
                if (
                    desired_num_warm_servers > 0
                    and len(_WARM_SERVERS) < desired_num_warm_servers
                ):
                    self.log(
                        'info',
                        f'Creating a new warm server to maintain desired count of {desired_num_warm_servers}',
                    )
                    _create_warm_server_in_background(self.config, self.plugins)

                return observation_from_dict(response.json())
            except httpx.NetworkError:
                raise AgentRuntimeDisconnectedError('Server connection lost')

    def close(self) -> None:
        """Stop the server process if not in attach_to_existing mode."""
        logger.info(f'Closing LocalRuntime for session {self.sid}')

        # If we're in attach_to_existing mode, don't close the server
        if self.attach_to_existing:
            logger.info(
                f'Not closing server for session {self.sid} (attach_to_existing=True)'
            )
            self.log(
                'info',
                f'Not closing server for session {self.sid} (attach_to_existing=True)',
            )
            # Just clean up our reference to the process, but leave it running
            self.server_process = None
            # Don't clean up temp workspace when attach_to_existing=True
            super().close()
            return

        # Signal the log thread to exit
        logger.info('Signaling log thread to exit')
        self._log_thread_exit_event.set()

        # Remove from global dictionary
        if self.sid in _RUNNING_SERVERS:
            logger.info(f'Removing session {self.sid} from running servers registry')
            del _RUNNING_SERVERS[self.sid]
        else:
            logger.warning(f'Session {self.sid} not found in running servers registry')

        if self.server_process:
            logger.info(f'Terminating server process PID: {self.server_process.pid}')

            # Check if process is still alive before terminating
            if self.server_process.poll() is None:
                logger.info('Server process is still running, sending terminate signal')
                self.server_process.terminate()
                try:
                    exit_code = self.server_process.wait(timeout=5)
                    logger.info(
                        f'Server process terminated gracefully with exit code: {exit_code}'
                    )
                except subprocess.TimeoutExpired:
                    logger.warning(
                        f'Server process PID {self.server_process.pid} did not terminate gracefully, killing it'
                    )
                    self.server_process.kill()
                    try:
                        exit_code = self.server_process.wait(timeout=2)
                        logger.info(
                            f'Server process killed with exit code: {exit_code}'
                        )
                    except subprocess.TimeoutExpired:
                        logger.error(
                            f'Failed to kill server process PID {self.server_process.pid}'
                        )
            else:
                exit_code = self.server_process.poll() or 0
                logger.info(
                    f'Server process was already terminated with exit code: {exit_code}'
                )

            self.server_process = None

            logger.info('Waiting for log thread to finish...')
            if self._log_thread and self._log_thread.is_alive():
                self._log_thread.join(timeout=5)  # Add timeout to join
                if self._log_thread.is_alive():
                    logger.warning('Log thread did not finish within timeout')
                else:
                    logger.info('Log thread finished successfully')
            else:
                logger.info('Log thread was not running or already finished')
        else:
            logger.info('No server process to terminate')

        # Clean up temp workspace if it exists and we created it
        if self._temp_workspace and not self.attach_to_existing:
            logger.info(f'Cleaning up temporary workspace: {self._temp_workspace}')
            try:
                shutil.rmtree(self._temp_workspace)
                logger.info('Temporary workspace cleaned up successfully')
            except Exception as e:
                logger.error(f'Error cleaning up temporary workspace: {e}')
            self._temp_workspace = None
        else:
            logger.info('No temporary workspace to clean up')

        logger.info(f'LocalRuntime close completed for session {self.sid}')
        super().close()

    @classmethod
    async def delete(cls, conversation_id: str) -> None:
        """Delete the runtime for a conversation."""
        if conversation_id in _RUNNING_SERVERS:
            logger.info(f'Deleting LocalRuntime for conversation {conversation_id}')
            server_info = _RUNNING_SERVERS[conversation_id]

            # Signal the log thread to exit
            server_info.log_thread_exit_event.set()

            # Terminate the server process
            if server_info.process:
                server_info.process.terminate()
                try:
                    server_info.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server_info.process.kill()

            # Wait for the log thread to finish
            server_info.log_thread.join(timeout=5)

            # Remove from global dictionary
            del _RUNNING_SERVERS[conversation_id]
            logger.info(f'LocalRuntime for conversation {conversation_id} deleted')

        # Also clean up any warm servers if this is the last conversation being deleted
        if not _RUNNING_SERVERS:
            logger.info('No active conversations, cleaning up warm servers')
            for server_info in _WARM_SERVERS[:]:
                # Signal the log thread to exit
                server_info.log_thread_exit_event.set()

                # Terminate the server process
                if server_info.process:
                    server_info.process.terminate()
                    try:
                        server_info.process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        server_info.process.kill()

                # Wait for the log thread to finish
                server_info.log_thread.join(timeout=5)

                # Clean up temp workspace
                if server_info.temp_workspace:
                    shutil.rmtree(server_info.temp_workspace)

                # Remove from warm servers list
                _WARM_SERVERS.remove(server_info)

            logger.info('All warm servers cleaned up')

    @property
    def runtime_url(self) -> str:
        runtime_url = os.getenv('RUNTIME_URL')
        if runtime_url:
            return runtime_url

        # TODO: This could be removed if we had a straightforward variable containing the RUNTIME_URL in the K8 env.
        runtime_url_pattern = os.getenv('RUNTIME_URL_PATTERN')
        runtime_id = os.getenv('RUNTIME_ID')
        if runtime_url_pattern and runtime_id:
            runtime_url = runtime_url_pattern.format(runtime_id=runtime_id)
            return runtime_url

        # Fallback to localhost
        return self.config.sandbox.local_runtime_url

    def _create_url(self, prefix: str, port: int) -> str:
        runtime_url = self.runtime_url
        logger.debug(f'runtime_url is {runtime_url}')
        if 'localhost' in runtime_url:
            url = f'{self.runtime_url}:{self._vscode_port}'
        else:
            runtime_id = os.getenv('RUNTIME_ID')
            parsed = urlparse(self.runtime_url)
            scheme, netloc, path = parsed.scheme, parsed.netloc, parsed.path or '/'
            path_mode = path.startswith(f'/{runtime_id}') if runtime_id else False
            if path_mode:
                url = f'{scheme}://{netloc}/{runtime_id}/{prefix}'
            else:
                url = f'{scheme}://{prefix}-{netloc}'
        logger.debug(f'_create_url url is {url}')
        return url

    @property
    def vscode_url(self) -> str | None:
        token = super().get_vscode_token()
        if not token:
            return None
        vscode_url = self._create_url('vscode', self._vscode_port)
        return f'{vscode_url}/?tkn={token}&folder={self.config.workspace_mount_path_in_sandbox}'

    @property
    def web_hosts(self) -> dict[str, int]:
        hosts: dict[str, int] = {}
        for index, port in enumerate(self._app_ports):
            url = self._create_url(f'work-{index + 1}', port)
            hosts[url] = port
        return hosts


def _python_bin_path():
    # Derive environment paths using sys.executable
    interpreter_path = sys.executable
    python_bin_path = os.path.dirname(interpreter_path)
    return python_bin_path


def _create_server(
    config: OpenHandsConfig,
    plugins: list[PluginRequirement],
    workspace_prefix: str,
) -> tuple[ActionExecutionServerInfo, str]:
    logger.info('Creating a server')

    # Set up workspace directory
    temp_workspace = tempfile.mkdtemp(
        prefix=f'openhands_workspace_{workspace_prefix}',
    )
    workspace_mount_path = temp_workspace

    # Find available ports with detailed logging
    logger.info(f'Allocating ports for workspace prefix: {workspace_prefix}')
    logger.info(
        f'Port ranges - Execution: {EXECUTION_SERVER_PORT_RANGE}, VSCode: {VSCODE_PORT_RANGE}'
    )
    logger.info(f'App port ranges - 1: {APP_PORT_RANGE_1}, 2: {APP_PORT_RANGE_2}')

    # Add worker-specific port offset to avoid conflicts in multi-worker scenarios
    worker_offset = 0
    try:
        import psutil

        current_process = psutil.Process()
        # Use process ID modulo to create a deterministic offset
        worker_offset = (
            current_process.pid % 100
        ) * 10  # Offset by 10 ports per worker
        if worker_offset > 0:
            logger.info(
                f'Applying worker-specific port offset: {worker_offset} (PID: {current_process.pid})'
            )
    except Exception as e:
        logger.debug(f'Could not calculate worker offset: {e}')

    # Apply offset to port ranges
    execution_range = (
        EXECUTION_SERVER_PORT_RANGE[0] + worker_offset,
        EXECUTION_SERVER_PORT_RANGE[1] + worker_offset,
    )
    vscode_range = (
        VSCODE_PORT_RANGE[0] + worker_offset,
        VSCODE_PORT_RANGE[1] + worker_offset,
    )
    app_range_1 = (
        APP_PORT_RANGE_1[0] + worker_offset,
        APP_PORT_RANGE_1[1] + worker_offset,
    )
    app_range_2 = (
        APP_PORT_RANGE_2[0] + worker_offset,
        APP_PORT_RANGE_2[1] + worker_offset,
    )

    if worker_offset > 0:
        logger.info(
            f'Adjusted port ranges - Execution: {execution_range}, VSCode: {vscode_range}'
        )
        logger.info(f'Adjusted app ranges - 1: {app_range_1}, 2: {app_range_2}')

    execution_server_port = find_available_tcp_port(*execution_range)
    logger.info(f'Allocated execution server port: {execution_server_port}')

    vscode_port_env = os.getenv('VSCODE_PORT')
    if vscode_port_env:
        vscode_port = int(vscode_port_env)
        logger.info(f'Using VSCode port from environment: {vscode_port}')
    else:
        vscode_port = find_available_tcp_port(*vscode_range)
        logger.info(f'Allocated VSCode port: {vscode_port}')

    app_port_1_env = os.getenv('WORK_PORT_1') or os.getenv('APP_PORT_1')
    if app_port_1_env:
        app_port_1 = int(app_port_1_env)
        logger.info(f'Using app port 1 from environment: {app_port_1}')
    else:
        app_port_1 = find_available_tcp_port(*app_range_1)
        logger.info(f'Allocated app port 1: {app_port_1}')

    app_port_2_env = os.getenv('WORK_PORT_2') or os.getenv('APP_PORT_2')
    if app_port_2_env:
        app_port_2 = int(app_port_2_env)
        logger.info(f'Using app port 2 from environment: {app_port_2}')
    else:
        app_port_2 = find_available_tcp_port(*app_range_2)
        logger.info(f'Allocated app port 2: {app_port_2}')

    app_ports = [app_port_1, app_port_2]

    # Check for port conflicts with existing servers
    all_allocated_ports = [execution_server_port, vscode_port] + app_ports
    logger.info(f'All allocated ports: {all_allocated_ports}')

    # Check if any ports are already in use by existing servers
    existing_ports = []
    for sid, server_info in _RUNNING_SERVERS.items():
        existing_ports.extend(
            [
                server_info.execution_server_port,
                server_info.vscode_port,
                *server_info.app_ports,
            ]
        )

    if existing_ports:
        logger.info(f'Existing server ports: {existing_ports}')
        conflicts = set(all_allocated_ports) & set(existing_ports)
        if conflicts:
            logger.error(f'Port conflicts detected: {conflicts}')
            logger.error(
                'This may indicate a race condition in multi-worker environment'
            )
        else:
            logger.info('No port conflicts with existing servers')
    else:
        logger.info('No existing servers, no port conflicts possible')

    # Get user info
    user_id, username = get_user_info()

    # Start the server process
    cmd = get_action_execution_server_startup_command(
        server_port=execution_server_port,
        plugins=plugins,
        app_config=config,
        python_prefix=[],
        python_executable=sys.executable,
        override_user_id=user_id,
        override_username=username,
    )

    logger.info(f'Starting server with command: {cmd}')

    env = os.environ.copy()
    # Get the code repo path
    code_repo_path = os.path.dirname(os.path.dirname(openhands.__file__))
    env['PYTHONPATH'] = os.pathsep.join([code_repo_path, env.get('PYTHONPATH', '')])
    env['OPENHANDS_REPO_PATH'] = code_repo_path
    env['LOCAL_RUNTIME_MODE'] = '1'
    env['VSCODE_PORT'] = str(vscode_port)

    # Prepend the interpreter's bin directory to PATH for subprocesses
    env['PATH'] = f'{_python_bin_path()}{os.pathsep}{env.get("PATH", "")}'

    logger.debug(f'Updated PATH for subprocesses: {env["PATH"]}')

    logger.info('Creating subprocess with PID tracking enabled')
    logger.info(f'Working directory: {code_repo_path}')
    logger.info(
        'Environment variables set: PYTHONPATH, OPENHANDS_REPO_PATH, LOCAL_RUNTIME_MODE, VSCODE_PORT'
    )
    logger.debug(f'Full environment: {env}')

    try:
        server_process = subprocess.Popen(  # noqa: S603
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=1,
            env=env,
            cwd=code_repo_path,
        )
        logger.info(
            f'Server process created successfully with PID: {server_process.pid}'
        )
        logger.info(f'Server process command: {" ".join(cmd)}')

        # Check if process started successfully
        time.sleep(0.1)  # Give process a moment to start
        if server_process.poll() is not None:
            logger.error(
                f'Server process exited immediately with code: {server_process.poll()}'
            )
            raise RuntimeError(
                f'Server process failed to start, exit code: {server_process.poll()}'
            )
        else:
            logger.info('Server process is running successfully')

    except Exception as e:
        logger.error(f'Failed to create server process: {e}')
        raise

    log_thread_exit_event = threading.Event()

    # Start a thread to read and log server output
    def log_output() -> None:
        logger.info(f'Starting log output thread for server PID: {server_process.pid}')
        if not server_process or not server_process.stdout:
            logger.error('server process or stdout not available for logging.')
            return

        line_count = 0
        last_health_check = time.time()

        try:
            # Read lines while the process is running and stdout is available
            while server_process.poll() is None:
                if log_thread_exit_event.is_set():
                    logger.info('server log thread received exit signal.')
                    break

                line = server_process.stdout.readline()
                if not line:
                    # No output available, check if process is still alive
                    current_time = time.time()
                    if current_time - last_health_check > 10:  # Check every 10 seconds
                        logger.debug(
                            f'Server health check - PID: {server_process.pid}, lines read: {line_count}'
                        )
                        last_health_check = current_time
                    time.sleep(0.1)  # Small delay to prevent busy waiting
                    continue

                line_count += 1
                stripped_line = line.strip()

                # Log server output with different levels based on content
                if 'ERROR' in stripped_line or 'CRITICAL' in stripped_line:
                    logger.error(f'server: {stripped_line}')
                elif 'WARNING' in stripped_line:
                    logger.warning(f'server: {stripped_line}')
                elif (
                    'Starting' in stripped_line
                    or 'Initialized' in stripped_line
                    or 'Ready' in stripped_line
                ):
                    logger.info(f'server: {stripped_line}')
                else:
                    logger.debug(f'server: {stripped_line}')

            # Check final process status
            exit_code = server_process.poll()
            logger.info(
                f'Server process monitoring ended - exit code: {exit_code}, lines processed: {line_count}'
            )

            if exit_code is not None and exit_code != 0:
                logger.error(f'Server process exited with non-zero code: {exit_code}')

            # Capture any remaining output
            if not log_thread_exit_event.is_set():
                logger.info('server process exited, reading remaining output.')
                remaining_lines = 0
                for line in server_process.stdout:
                    if log_thread_exit_event.is_set():
                        break
                    remaining_lines += 1
                    logger.info(f'server (remaining): {line.strip()}')
                logger.info(f'Captured {remaining_lines} remaining output lines')

        except Exception as e:
            logger.error(f'Error reading server output: {e}', exc_info=True)
        finally:
            logger.info(
                f'server log output thread finished. Total lines processed: {line_count}'
            )

    log_thread = threading.Thread(target=log_output, daemon=True)
    log_thread.start()

    # Create server info object
    server_info = ActionExecutionServerInfo(
        process=server_process,
        execution_server_port=execution_server_port,
        vscode_port=vscode_port,
        app_ports=app_ports,
        log_thread=log_thread,
        log_thread_exit_event=log_thread_exit_event,
        temp_workspace=temp_workspace,
        workspace_mount_path=workspace_mount_path,
    )

    # API URL for the server
    api_url = f'{config.sandbox.local_runtime_url}:{execution_server_port}'

    return server_info, api_url


def _create_warm_server(
    config: OpenHandsConfig,
    plugins: list[PluginRequirement],
) -> None:
    """Create a warm server in the background."""
    try:
        server_info, api_url = _create_server(
            config=config,
            plugins=plugins,
            workspace_prefix='warm',
        )

        # Wait for the server to be ready
        session = httpx.Client(timeout=30, verify=httpx_verify_option())

        # Use tenacity to retry the connection
        @tenacity.retry(
            wait=tenacity.wait_fixed(2),
            stop=tenacity.stop_after_delay(120) | stop_if_should_exit(),
            before_sleep=lambda retry_state: logger.debug(
                f'Waiting for warm server to be ready... (attempt {retry_state.attempt_number})'
            ),
        )
        def wait_until_alive() -> bool:
            if server_info.process.poll() is not None:
                raise RuntimeError('Warm server process died')

            try:
                response = session.get(f'{api_url}/alive')
                response.raise_for_status()
                return True
            except Exception as e:
                logger.debug(f'Warm server not ready yet: {e}')
                raise

        wait_until_alive()
        logger.info(f'Warm server ready at port {server_info.execution_server_port}')

        # Add to the warm servers list
        _WARM_SERVERS.append(server_info)
    except Exception as e:
        logger.error(f'Failed to create warm server: {e}')
        # Clean up resources
        if 'server_info' in locals():
            server_info.log_thread_exit_event.set()
            if server_info.process:
                server_info.process.terminate()
                try:
                    server_info.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server_info.process.kill()
            server_info.log_thread.join(timeout=5)
            if server_info.temp_workspace:
                shutil.rmtree(server_info.temp_workspace)


def _create_warm_server_in_background(
    config: OpenHandsConfig,
    plugins: list[PluginRequirement],
) -> None:
    """Start a new thread to create a warm server."""
    thread = threading.Thread(
        target=_create_warm_server, daemon=True, args=(config, plugins)
    )
    thread.start()


def _get_plugins(config: OpenHandsConfig) -> list[PluginRequirement]:
    from openhands.controller.agent import Agent

    plugins = Agent.get_cls(config.default_agent).sandbox_plugins
    return plugins
