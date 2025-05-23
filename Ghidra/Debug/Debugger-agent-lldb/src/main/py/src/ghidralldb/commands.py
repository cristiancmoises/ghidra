## ###
# IP: GHIDRA
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
##
from concurrent.futures import Future
from contextlib import contextmanager
import functools
import inspect
import optparse
import os.path
import shlex
import socket
import sys
import time
from typing import (Any, Callable, Dict, Generator, List, Literal,
                    Optional, Tuple, TypeVar, Union, cast)

try:
    import psutil
except ImportError:
    print("Unable to import 'psutil' - check that it has been installed")

from ghidratrace import sch
from ghidratrace.client import (Client, Address, AddressRange, Trace, Schedule,
                                TraceObject, Transaction)
from ghidratrace.display import print_tabular_values, wait
import lldb

from . import arch, hooks, methods, util


PAGE_SIZE = 4096

DEFAULT_REGISTER_BANK = "General Purpose Registers"

AVAILABLES_PATH = 'Available'
AVAILABLE_KEY_PATTERN = '[{pid}]'
AVAILABLE_PATTERN = AVAILABLES_PATH + AVAILABLE_KEY_PATTERN
BREAKPOINT_KEY_PATTERN = '[{breaknum}]'
WATCHPOINT_KEY_PATTERN = '[{watchnum}]'
BREAK_LOC_KEY_PATTERN = '[{locnum}]'
PROCESSES_PATH = 'Processes'
PROCESS_KEY_PATTERN = '[{procnum}]'
PROCESS_PATTERN = PROCESSES_PATH + PROCESS_KEY_PATTERN
PROC_WATCHES_PATTERN = PROCESS_PATTERN + '.Watchpoints'
PROC_WATCH_PATTERN = PROC_WATCHES_PATTERN + WATCHPOINT_KEY_PATTERN
PROC_BREAKS_PATTERN = PROCESS_PATTERN + '.Breakpoints'
PROC_BREAK_PATTERN = PROC_BREAKS_PATTERN + BREAKPOINT_KEY_PATTERN
ENV_PATTERN = PROCESS_PATTERN + '.Environment'
THREADS_PATTERN = PROCESS_PATTERN + '.Threads'
THREAD_KEY_PATTERN = '[{tnum}]'
THREAD_PATTERN = THREADS_PATTERN + THREAD_KEY_PATTERN
STACK_PATTERN = THREAD_PATTERN + '.Stack'
FRAME_KEY_PATTERN = '[{level}]'
FRAME_PATTERN = STACK_PATTERN + FRAME_KEY_PATTERN
REGS_PATTERN = FRAME_PATTERN + '.Registers'
BANK_PATTERN = REGS_PATTERN + '.{bank}'
MEMORY_PATTERN = PROCESS_PATTERN + '.Memory'
REGION_KEY_PATTERN = '[{start:08x}]'
REGION_PATTERN = MEMORY_PATTERN + REGION_KEY_PATTERN
MODULES_PATTERN = PROCESS_PATTERN + '.Modules'
MODULE_KEY_PATTERN = '[{modpath}]'
MODULE_PATTERN = MODULES_PATTERN + MODULE_KEY_PATTERN
SECTIONS_ADD_PATTERN = '.Sections'
SECTION_KEY_PATTERN = '[{secname}]'
SECTION_ADD_PATTERN = SECTIONS_ADD_PATTERN + SECTION_KEY_PATTERN


class Extra(object):
    def __init__(self) -> None:
        self.memory_mapper: Optional[arch.DefaultMemoryMapper] = None
        self.register_mapper: Optional[arch.DefaultRegisterMapper] = None

    def require_mm(self) -> arch.DefaultMemoryMapper:
        if self.memory_mapper is None:
            raise RuntimeError("No memory mapper")
        return self.memory_mapper

    def require_rm(self) -> arch.DefaultRegisterMapper:
        if self.register_mapper is None:
            raise RuntimeError("No register mapper")
        return self.register_mapper


class State(object):

    def __init__(self) -> None:
        self.reset_client()

    def require_client(self) -> Client:
        if self.client is None:
            raise RuntimeError("Not connected")
        return self.client

    def require_no_client(self) -> None:
        if self.client is not None:
            raise RuntimeError("Already connected")

    def reset_client(self) -> None:
        self.client: Optional[Client] = None
        self.reset_trace()

    def require_trace(self) -> Trace[Extra]:
        if self.trace is None:
            raise RuntimeError("No trace active")
        return self.trace

    def require_no_trace(self) -> None:
        if self.trace is not None:
            raise RuntimeError("Trace already started")

    def reset_trace(self) -> None:
        self.trace: Optional[Trace[Extra]] = None
        util.set_convenience_variable('_ghidra_tracing', "false")
        self.reset_tx()

    def require_tx(self) -> Tuple[Trace[Extra], Transaction]:
        trace = self.require_trace()
        if self.tx is None:
            raise RuntimeError("No transaction")
        return trace, self.tx

    def require_no_tx(self) -> None:
        if self.tx is not None:
            raise RuntimeError("Transaction already started")

    def reset_tx(self) -> None:
        self.tx: Optional[Transaction] = None


STATE = State()

if __name__ == '__main__':
    lldb.SBDebugger.InitializeWithErrorHandling()
    lldb.debugger = lldb.SBDebugger.Create()
elif lldb.debugger:
    lldb.debugger.HandleCommand(
        'command container add -h "Commands for connecting to Ghidra" ghidra')
    lldb.debugger.HandleCommand(
        'command container add -h "Commands for exporting data to a Ghidra trace" ghidra trace')
    lldb.debugger.HandleCommand(
        'command container add -h "Utility commands for testing with Ghidra" ghidra util')

    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_trace_connect         ghidra trace connect')
    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_trace_listen          ghidra trace listen')
    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_trace_disconnect      ghidra trace disconnect')
    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_trace_start           ghidra trace start')
    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_trace_stop            ghidra trace stop')
    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_trace_restart         ghidra trace restart')
    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_trace_info            ghidra trace info')
    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_trace_info_lcsp       ghidra trace info-lcsp')
    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_trace_txstart         ghidra trace tx-start')
    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_trace_txcommit        ghidra trace tx-commit')
    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_trace_txabort         ghidra trace tx-abort')
    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_trace_txopen          ghidra trace tx-open')
    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_trace_save            ghidra trace save')
    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_trace_new_snap        ghidra trace new-snap')
    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_trace_putmem          ghidra trace putmem')
    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_trace_putval          ghidra trace putval')
    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_trace_putmem_state    ghidra trace putmem-state')
    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_trace_delmem          ghidra trace delmem')
    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_trace_putreg          ghidra trace putreg')
    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_trace_delreg          ghidra trace delreg')
    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_trace_create_obj      ghidra trace create-obj')
    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_trace_insert_obj      ghidra trace insert-obj')
    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_trace_remove_obj      ghidra trace remove-obj')
    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_trace_set_value       ghidra trace set-value')
    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_trace_retain_values   ghidra trace retain-values')
    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_trace_get_obj         ghidra trace get-obj')
    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_trace_get_values      ghidra trace get-values')
    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_trace_get_values_rng  ghidra trace get-values-rng')
    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_trace_activate        ghidra trace activate')
    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_trace_disassemble     ghidra trace disassemble')
    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_trace_put_processes   ghidra trace put-processes')
    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_trace_put_available   ghidra trace put-available')
    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_trace_put_breakpoints ghidra trace put-breakpoints')
    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_trace_put_watchpoints ghidra trace put-watchpoints')
    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_trace_put_environment ghidra trace put-environment')
    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_trace_put_regions     ghidra trace put-regions')
    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_trace_put_modules     ghidra trace put-modules')
    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_trace_put_threads     ghidra trace put-threads')
    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_trace_put_frames      ghidra trace put-frames')
    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_trace_put_all         ghidra trace put-all')
    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_trace_install_hooks   ghidra trace install-hooks')
    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_trace_remove_hooks    ghidra trace remove-hooks')
    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_trace_sync_enable     ghidra trace sync-enable')
    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_trace_sync_disable    ghidra trace sync-disable')
    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_trace_sync_synth_stopped    ghidra trace sync-synth-stopped')
    lldb.debugger.HandleCommand(
        'command script add -f ghidralldb.commands.ghidra_util_wait_stopped     ghidra util wait-stopped')
    # lldb.debugger.HandleCommand('target stop-hook add -P ghidralldb.hooks.StopHook')
    lldb.debugger.SetAsync(True)
    print("Commands loaded.")


C = TypeVar('C', bound=Callable)


def convert_errors(func: C) -> C:
    @functools.wraps(func)
    def _func(debugger: lldb.SBDebugger, command: str,
              result: lldb.SBCommandReturnObject,
              internal_dict: Dict[str, Any]) -> None:
        result.Clear()
        try:
            func(debugger, command, result, internal_dict)
            result.SetStatus(lldb.eReturnStatusSuccessFinishNoResult)
        except BaseException as e:
            result.SetError(str(e))
    return cast(C, _func)


@convert_errors
def ghidra_trace_connect(debugger: lldb.SBDebugger, command: str,
                         result: lldb.SBCommandReturnObject,
                         internal_dict: Dict[str, Any]) -> None:
    """Connect LLDB to Ghidra for tracing.

    Usage: ghidra trace connect ADDRESS
        ADDRESS must be HOST:PORT

    The required address must be of the form 'host:port'
    """

    args = shlex.split(command)

    STATE.require_no_client()
    if len(args) != 1:
        raise RuntimeError("Usage: ghidra trace connect ADDRESS")
    address = args[0]

    parts = address.split(':')
    if len(parts) != 2:
        raise RuntimeError("ADDRESS must be HOST:PORT")
    host, port = parts
    try:
        c = socket.socket()
        c.connect((host, int(port)))
        STATE.client = Client(
            c, "lldb-" + util.LLDB_VERSION.full, methods.REGISTRY)
    except ValueError:
        raise RuntimeError("port must be numeric")


@convert_errors
def ghidra_trace_listen(debugger: lldb.SBDebugger, command: str,
                        result: lldb.SBCommandReturnObject,
                        internal_dict: Dict[str, Any]) -> None:
    """Listen for Ghidra to connect for tracing.

    Usage: ghidra trace listen [ADDRESS]
        ADDRESS must be PORT or HOST:PORT

    Takes an optional address for the host and port on which to listen. Either
    the form 'host:port' or just 'port'. If omitted, it will bind to an
    ephemeral port on all interfaces. If only the port is given, it will bind to
    that port on all interfaces. This command will block until the connection is
    established.
    """

    args = shlex.split(command)
    host: str
    port: Union[str, int]
    if len(args) == 0:
        host, port = '0.0.0.0', 0
    elif len(args) == 1:
        address = args[0]
        parts = address.split(':')
        if len(parts) == 1:
            host, port = '0.0.0.0', parts[0]
        elif len(parts) == 2:
            host, port = parts
        else:
            raise RuntimeError("ADDRESS must be PORT or HOST:PORT")
    else:
        raise RuntimeError("Usage: ghidra trace listen [ADDRESS]")

    STATE.require_no_client()
    try:
        s = socket.socket()
        s.bind((host, int(port)))
        host, port = s.getsockname()
        s.listen(1)
        print(f"Listening at {host}:{port}...")
        c, (chost, cport) = s.accept()
        s.close()
        print(f"Connection from {chost}:{cport}")
        STATE.client = Client(
            c, util.LLDB_VERSION.display, methods.REGISTRY)
    except ValueError:
        raise RuntimeError("PORT must be numeric")


@convert_errors
def ghidra_trace_disconnect(debugger: lldb.SBDebugger, command: str,
                            result: lldb.SBCommandReturnObject,
                            internal_dict: Dict[str, Any]) -> None:
    """Disconnect LLDB from Ghidra for tracing.

    Usage: ghidra trace disconnect
    """

    args = shlex.split(command)
    if len(args) != 0:
        raise RuntimeError("Usage: ghidra trace disconnect")

    STATE.require_client().close()
    STATE.reset_client()


def compute_name() -> str:
    target = lldb.debugger.GetTargetAtIndex(0)
    progname = target.executable.basename
    if progname is None:
        return 'lldb/noname'
    else:
        return 'lldb/' + progname.split('/')[-1]


def start_trace(name: str) -> None:
    language, compiler = arch.compute_ghidra_lcsp()
    STATE.trace = STATE.require_client().create_trace(
        name, language, compiler, extra=Extra())
    # TODO: Is adding an attribute like this recommended in Python?
    STATE.trace.extra.memory_mapper = arch.compute_memory_mapper(language)
    STATE.trace.extra.register_mapper = arch.compute_register_mapper(language)

    frame = inspect.currentframe()
    if frame is None:
        raise AssertionError("cannot locate schema.xml")
    parent = os.path.dirname(inspect.getfile(frame))
    schema_fn = os.path.join(parent, 'schema.xml')
    with open(schema_fn, 'r') as schema_file:
        schema_xml = schema_file.read()
    with STATE.trace.open_tx("Create Root Object"):
        root = STATE.trace.create_root_object(schema_xml, 'LldbSession')
        root.set_value('_display', util.LLDB_VERSION.display)
        STATE.trace.create_object(AVAILABLES_PATH).insert()
        STATE.trace.create_object(PROCESSES_PATH).insert()
    util.set_convenience_variable('_ghidra_tracing', "true")


@convert_errors
def ghidra_trace_start(debugger: lldb.SBDebugger, command: str,
                       result: lldb.SBCommandReturnObject,
                       internal_dict: Dict[str, Any]) -> None:
    """Start a Trace in Ghidra.

    Usage: ghidra trace start [NAME]

    Takes an optional name for the trace. If omitted, it tries to derive the
    name from the target image.
    """

    args = shlex.split(command)
    if len(args) == 0:
        name = compute_name()
    elif len(args) == 1:
        name = args[0]
    else:
        raise RuntimeError("Usage: ghidra trace start [NAME]")

    STATE.require_client()
    STATE.require_no_trace()
    start_trace(name)


@convert_errors
def ghidra_trace_stop(debugger: lldb.SBDebugger, command: str,
                      result: lldb.SBCommandReturnObject,
                      internal_dict: Dict[str, Any]) -> None:
    """Stop the Trace in Ghidra.

    Usage: ghidra trace stop
    """

    args = shlex.split(command)
    if len(args) != 0:
        raise RuntimeError("Usage: ghidra trace stop")

    STATE.require_trace().close()
    STATE.reset_trace()


@convert_errors
def ghidra_trace_restart(debugger: lldb.SBDebugger, command: str,
                         result: lldb.SBCommandReturnObject,
                         internal_dict: Dict[str, Any]) -> None:
    """Restart or start the Trace in Ghidra.

    Usage: ghidra trace restart [NAME]

    Takes an optional name for the trace. If omitted, it tries to derive the
    name from the target image.
    """

    args = shlex.split(command)
    if len(args) == 0:
        name = compute_name()
    elif len(args) == 1:
        name = args[0]
    else:
        raise RuntimeError("Usage: ghidra trace restart [NAME]")

    STATE.require_client()
    if STATE.trace is not None:
        STATE.trace.close()
        STATE.reset_trace()
    start_trace(name)


@convert_errors
def ghidra_trace_info(debugger: lldb.SBDebugger, command: str,
                      result: lldb.SBCommandReturnObject,
                      internal_dict: Dict[str, Any]) -> None:
    """Get info about the Ghidra connection.

    Usage: ghidra trace info
    """

    args = shlex.split(command)
    if len(args) != 0:
        raise RuntimeError("Usage: ghidra trace info")

    result = {}
    if STATE.client is None:
        print("Not connected to Ghidra")
        return
    host, port = STATE.client.s.getpeername()
    print(f"Connected to Ghidra at {host}:{port}")
    if STATE.trace is None:
        print("No trace")
        return
    print("Trace active")
    return result


@convert_errors
def ghidra_trace_info_lcsp(debugger: lldb.SBDebugger, command: str,
                           result: lldb.SBCommandReturnObject,
                           internal_dict: Dict[str, Any]) -> None:
    """Get the selected Ghidra language-compiler-spec pair.

    Usage: ghidra trace info-lcsp

    For diagnostics, shows the Ghidra language-compiler-spec pair that will be
    selected when starting the trace.
    """

    args = shlex.split(command)
    if len(args) != 0:
        raise RuntimeError("Usage: ghidra trace info-lcsp")

    language, compiler = arch.compute_ghidra_lcsp()
    print(f"Selected Ghidra language: {language}")
    print(f"Selected Ghidra compiler: {compiler}")


@convert_errors
def ghidra_trace_txstart(debugger: lldb.SBDebugger, command: str,
                         result: lldb.SBCommandReturnObject,
                         internal_dict: Dict[str, Any]) -> None:
    """Start a transaction on the trace.

    Usage: ghidra trace tx-start DESCRIPTION
        DESCRIPTION must be in quotes if it contains spaces
    """

    args = shlex.split(command)
    if len(args) != 1:
        raise RuntimeError("Usage: ghidra trace tx-start DESCRIPTION")
    description = args[0]

    STATE.require_no_tx()
    STATE.tx = STATE.require_trace().start_tx(description, undoable=False)


@convert_errors
def ghidra_trace_txcommit(debugger: lldb.SBDebugger, command: str,
                          result: lldb.SBCommandReturnObject,
                          internal_dict: Dict[str, Any]) -> None:
    """Commit the current transaction.

    Usage: ghidra trace tx-commit
    """

    args = shlex.split(command)
    if len(args) != 0:
        raise RuntimeError("Usage: ghidra trace tx-commit")

    STATE.require_tx()[1].commit()
    STATE.reset_tx()


@convert_errors
def ghidra_trace_txabort(debugger: lldb.SBDebugger, command: str,
                         result: lldb.SBCommandReturnObject,
                         internal_dict: Dict[str, Any]) -> None:
    """Abort the current transaction.

    Usage: ghidra trace tx-abort

    Use only in emergencies. This may not always succeed, and it may leave the
    trace in an inconsistent state.
    """

    args = shlex.split(command)
    if len(args) != 0:
        raise RuntimeError("Usage: ghidra trace tx-abort")

    trace, tx = STATE.require_tx()
    print("Aborting trace transaction!")
    tx.abort()
    STATE.reset_tx()


@contextmanager
def open_tracked_tx(description: str) -> Generator[Transaction, None, None]:
    with STATE.require_trace().open_tx(description) as tx:
        STATE.tx = tx
        yield tx
    STATE.reset_tx()


@convert_errors
def ghidra_trace_txopen(debugger: lldb.SBDebugger, command: str,
                        result: lldb.SBCommandReturnObject,
                        internal_dict: Dict[str, Any]) -> None:
    """Run a command with an open transaction.

    Usage: ghidra trace tx-open DESCRIPTION COMMAND

    Execute the given command with an open transaction. This is generally
    useful only when executing a single 'put' command, or when executing a
    custom command that performs several puts.
    """

    args = shlex.split(command)
    if len(args) != 2:
        raise RuntimeError("Usage: ghidra trace tx-open DESCRIPTION COMMAND")

    description = args[0]
    cmd = args[1]
    with open_tracked_tx(description):
        lldb.debugger.GetCommandInterpreter().HandleCommand(cmd, result)


@convert_errors
def ghidra_trace_save(debugger: lldb.SBDebugger, command: str,
                      result: lldb.SBCommandReturnObject,
                      internal_dict: Dict[str, Any]) -> None:
    """Save the current trace.

    Usage: ghidra trace save
    """

    args = shlex.split(command)
    if len(args) != 0:
        raise RuntimeError("Usage: ghidra trace save")

    STATE.require_trace().save()


@convert_errors
def ghidra_trace_new_snap(debugger: lldb.SBDebugger, command: str,
                          result: lldb.SBCommandReturnObject,
                          internal_dict: Dict[str, Any]) -> None:
    """Create a new snapshot.

    Usage: ghidra trace new-snap [SNAP] DESCRIPTION

    Subsequent modifications to machine state will affect the new snapshot.
    """

    args = shlex.split(command)
    if len(args) == 1:
        time = None
        description = args[0]
    elif len(args) == 2:
        time = Schedule(int(args[0]))
        description = args[1]
    else:
        raise RuntimeError("Usage: ghidra trace new-snap [SNAP] DESCRIPTION")
    STATE.require_trace().snapshot(description, time=time)


def quantize_pages(start: int, end: int) -> Tuple[int, int]:
    return (start // PAGE_SIZE * PAGE_SIZE, (end+PAGE_SIZE-1) // PAGE_SIZE*PAGE_SIZE)


def put_bytes(start: int, end: int, result: lldb.SBCommandReturnObject,
              pages: bool) -> None:
    trace = STATE.require_trace()
    if pages:
        start, end = quantize_pages(start, end)
    proc = util.get_process()
    error = lldb.SBError()
    if end - start <= 0:
        return
    buf = proc.ReadMemory(start, end - start, error)

    if error.Success() and buf is not None:
        base, addr = trace.extra.require_mm().map(proc, start)
        if base != addr.space:
            trace.create_overlay_space(base, addr.space)
        count = trace.put_bytes(addr, buf)
        if result is None:
            pass
        elif isinstance(count, Future):
            if count.done():
                result.PutCString(f"Wrote {count.result()} bytes")
            else:
                count.add_done_callback(lambda c: print(f"Wrong {c} bytes"))
                result.PutCString(
                    f"Wrong {len(buf)} bytes, perhaps in the future")
        else:
            result.PutCString(f"Wrote {count} bytes")
    else:
        raise RuntimeError(f"Cannot read memory at {start:x}")


def eval_address(address: str) -> int:
    try:
        return util.parse_and_eval(address)
    except BaseException as e:
        raise RuntimeError(f"Cannot convert '{address}' to address: {e}")


def eval_range(address: str, length: str) -> Tuple[int, int]:
    start = eval_address(address)
    try:
        end = start + util.parse_and_eval(length)
    except BaseException as e:
        raise RuntimeError(f"Cannot convert '{length}' to length: {e}")
    return start, end


def putmem(address: str, length: str, result: lldb.SBCommandReturnObject,
           pages: bool = True) -> None:
    start, end = eval_range(address, length)
    put_bytes(start, end, result, pages)


@convert_errors
def ghidra_trace_putmem(debugger: lldb.SBDebugger, command: str,
                        result: lldb.SBCommandReturnObject,
                        internal_dict: Dict[str, Any]) -> None:
    """Record the given block of memory into the Ghidra trace.

    Usage: ghidra trace putmem ADDRESS LENGTH [PAGES]

    Writes the given range of bytes from memory into the Ghidra trace. ADDRESS
    is a memory address. It is expression evaluated within the current target.
    LENGTH is the number of bytes to write, also evaluated within the current
    target. PAGES is a boolean value. If true, the range will be quantized to
    page boundaries that include the requested range.
    """

    args = shlex.split(command)
    if len(args) == 2:
        address = args[0]
        length = args[1]
        pages = True
    elif len(args) == 3:
        address = args[0]
        length = args[1]
        pages = (util.get_eval(args[2]).unsigned != 0)
    else:
        raise RuntimeError("Usage: ghidra trace putmem ADDRESS LENGTH [PAGES]")

    STATE.require_tx()
    putmem(address, length, result, pages)


@convert_errors
def ghidra_trace_putval(debugger: lldb.SBDebugger, command: str,
                        result: lldb.SBCommandReturnObject,
                        internal_dict: Dict[str, Any]) -> None:
    """Record the given value into the Ghidra trace, if it's in memory.

    Usage: ghidra trace putval EXPRESSION [PAGES]

    Evaluates the given expression within the current target. If the resulting
    value has an address in the target's memory, the bytes comprising that value
    are written into the Ghidra trace. If PAGES is true, then the full page(s)
    containing those bytes are written. If the resulting value has no address,
    or its address is not in memory, an error results.

    Please note, register and value aliases, e.g., '$pc' or '$1' may be assigned
    to a temporary memory address by LLDB. Thus, a command like

       ghidra trace putval $1

    may result in undefined behavior.
    """

    args = shlex.split(command)
    if len(args) == 1:
        expression = args[0]
        pages = True
    elif len(args) == 2:
        expression = args[0]
        pages = (util.get_eval(args[2]).unsigned != 0)
    else:
        raise RuntimeError("Usage: ghidra trace putval EXPRESSION [PAGES]")

    STATE.require_tx()
    try:
        value = util.get_eval(expression)
        address = value.addr
    except BaseException as e:
        raise RuntimeError(f"Could not evaluate {expression}: {e}")
    if not address.IsValid():
        raise RuntimeError(f"Expression {expression} does not have an address")
    start = int(address)
    end = start + start + value.size
    return put_bytes(start, end, result, pages)


def putmem_state(address: str, length: str, state: str,
                 pages: bool = True) -> None:
    trace = STATE.require_trace()
    trace.validate_state(state)
    start, end = eval_range(address, length)
    if pages:
        start, end = quantize_pages(start, end)
    proc = util.get_process()
    base, addr = trace.extra.require_mm().map(proc, start)
    if base != addr.space:
        trace.create_overlay_space(base, addr.space)
    trace.set_memory_state(addr.extend(end - start), state)


@convert_errors
def ghidra_trace_putmem_state(debugger: lldb.SBDebugger, command: str,
                              result: lldb.SBCommandReturnObject,
                              internal_dict: Dict[str, Any]) -> None:
    """Set the state of the given range of memory in the Ghidra trace.

    Usage: ghidra trace putmem-state ADDRESS LENGTH STATE [PAGES]
        STATE is one of known, unknown, or error

    Marks the given range of memory in the Ghidra trace with the given state.
    By default, all addresses in the trace are marked 'unknown'. Writing bytes
    to the trace, e.g., via putmem, implicitly marks the affected bytes as
    'known'. The interpretation of START, LENGTH, and PAGES is the same as in
    'ghidra trace putmem'.
    """

    args = shlex.split(command)
    if len(args) == 3:
        address = args[0]
        length = args[1]
        state = args[2]
        pages = True
    elif len(args) == 4:
        address = args[0]
        length = args[1]
        state = args[2]
        pages = (util.get_eval(args[2]).unsigned != 0)
    else:
        raise RuntimeError(
            "Usage: ghidra trace putmem ADDRESS LENGTH STATE [PAGES]")

    STATE.require_tx()
    putmem_state(address, length, state, pages)


@convert_errors
def ghidra_trace_delmem(debugger: lldb.SBDebugger, command: str,
                        result: lldb.SBCommandReturnObject,
                        internal_dict: Dict[str, Any]) -> None:
    """Delete the given range of memory from the Ghidra trace.

    Usage: ghidra trace delmem ADDRESS LENGTH

    Why would you do this? There are probably good reasons, but please consider
    that deleting information is typically not helping the user.

    Note there is no PAGES argument. This is to prevent accidental deletion of
    more bytes than intended. Expand the range manually, if you must.
    """

    trace = STATE.require_trace()
    args = shlex.split(command)
    if len(args) != 2:
        raise RuntimeError("Usage: ghidra trace delmem ADDRESS LENGTH")
    address = args[0]
    length = args[1]

    STATE.require_tx()
    start, end = eval_range(address, length)
    proc = util.get_process()
    base, addr = trace.extra.require_mm().map(proc, start)
    # Do not create the space. We're deleting stuff.
    trace.delete_bytes(addr.extend(end - start))


# Yes, lldb puts each full bank in a "value", with chilren for each reg
def putreg(frame: lldb.SBFrame, bank: lldb.SBValue) -> None:
    proc = util.get_process()
    space = REGS_PATTERN.format(procnum=proc.GetProcessID(),
                                tnum=util.selected_thread().GetThreadID(),
                                level=frame.GetFrameID())
    bank_path = BANK_PATTERN.format(procnum=proc.GetProcessID(),
                                    tnum=util.selected_thread().GetThreadID(),
                                    level=frame.GetFrameID(), bank=bank.name)
    trace = STATE.require_trace()
    trace.create_overlay_space('register', space)
    robj = trace.create_object(space)
    robj.insert()
    bobj = trace.create_object(bank_path)
    bobj.insert()
    mapper = trace.extra.require_rm()
    values = []
    for i in range(bank.GetNumChildren()):
        item = bank.GetChildAtIndex(i, lldb.eDynamicCanRunTarget, True)
        values.append(mapper.map_value(
            proc, item.GetName(), data_to_reg_bytes(item.data)))
        # In the tree, just use the human-friendly display value
        bobj.set_value(item.GetName(), item.value)
    # TODO: Memorize registers that failed for this arch, and omit later.
    trace.put_registers(space, values)


@convert_errors
def ghidra_trace_putreg(debugger: lldb.SBDebugger, command: str,
                        result: lldb.SBCommandReturnObject,
                        internal_dict: Dict[str, Any]) -> None:
    """Record the given register group for the current frame into the Ghidra
    trace.

    Usage: ghidra trace putreg [GROUP]

    If no group is specified, 'all' is assumed.
    """

    trace, tx = STATE.require_tx()
    args = shlex.split(command)
    if len(args) == 0:
        group = 'all'
    elif len(args) == 1:
        group = args[0]
    else:
        raise RuntimeError("Usage: ghidra trace putreg [GROUP]")

    frame = util.selected_frame()
    regs = frame.GetRegisters()
    with trace.client.batch() as b:
        if group != 'all':
            bank = regs.GetFirstValueByName(group)
            putreg(frame, bank)
            return

        for i in range(0, regs.GetSize()):
            bank = regs.GetValueAtIndex(i)
            putreg(frame, bank)


def collect_mapped_names(names: List[str], proc: lldb.SBProcess,
                         bank: lldb.SBValue) -> None:
    trace = STATE.require_trace()
    mapper = trace.extra.require_rm()
    for i in range(bank.GetNumChildren()):
        item = bank.GetChildAtIndex(i, lldb.eDynamicCanRunTarget, True)
        names.append(mapper.map_name(proc, item.GetName()))


@convert_errors
def ghidra_trace_delreg(debugger: lldb.SBDebugger, command: str,
                        result: lldb.SBCommandReturnObject,
                        internal_dict: Dict[str, Any]) -> None:
    """Delete the given register group for the current frame from the Ghidra
    trace.

    Usage: ghidra trace delreg [GROUP]

    If no group is specified, 'all' is assumed.

    Why would you do this? There are probably good reasons, but please consider
    that deleting information is typically not helping the user.
    """

    args = shlex.split(command)
    if len(args) == 0:
        group = 'all'
    elif len(args) == 1:
        group = args[0]
    else:
        raise RuntimeError("Usage: ghidra trace delreg [GROUP]")

    trace, tx = STATE.require_tx()
    proc = util.get_process()
    frame = util.selected_frame()
    regs = frame.GetRegisters()
    space = REGS_PATTERN.format(procnum=proc.GetProcessID(), tnum=util.selected_thread().GetThreadID(),
                                level=frame.GetFrameID())
    names: List[str] = []
    if group != 'all':
        bank = regs.GetFirstValueByName(group)
        collect_mapped_names(names, proc, bank)
    else:
        for i in range(regs.GetSize()):
            bank = regs.GetValueAtIndex(i)
            collect_mapped_names(names, proc, bank)
    trace.delete_registers(space, names)


@convert_errors
def ghidra_trace_create_obj(debugger: lldb.SBDebugger, command: str,
                            result: lldb.SBCommandReturnObject,
                            internal_dict: Dict[str, Any]) -> None:
    """Create an object in the Ghidra trace.

    Usage: ghidra trace create-obj PATH

    PATH gives the objects fully-qualified name, e.g., Processes[0].Threads[1],
    which often denotes the second thread of the first target process.

    The new object is in a detached state, so it may not be immediately
    recognized by the Debugger GUI. Use 'ghidra trace insert-obj' to finish the
    object, after all its required attributes are set.
    """

    args = shlex.split(command)
    if len(args) != 1:
        raise RuntimeError("Usage: ghidra trace create-obj PATH")
    path = args[0]

    trace, tx = STATE.require_tx()
    obj = trace.create_object(path)
    obj.insert()
    result.PutCString(f"Created object: id={obj.id}, path='{obj.path}'")


@convert_errors
def ghidra_trace_insert_obj(debugger: lldb.SBDebugger, command: str,
                            result: lldb.SBCommandReturnObject,
                            internal_dict: Dict[str, Any]) -> None:
    """Insert an object into the Ghidra trace.

    Usage: ghidra trace insert-obj PATH

    See 'ghidra trace create-obj'. An object in a detached state is missing
    some or all of its ancestry for its lifespan. Inserting the object creates
    its ancestry for its whole lifespan.
    """

    args = shlex.split(command)
    if len(args) != 1:
        raise RuntimeError("Usage: ghidra trace insert-obj PATH")
    path = args[0]

    # NOTE: id parameter is probably not necessary, since this command is for
    # humans.
    trace, tx = STATE.require_tx()
    span = trace.proxy_object_path(path).insert()
    result.PutCString(f"Inserted object: lifespan={span}")


@convert_errors
def ghidra_trace_remove_obj(debugger: lldb.SBDebugger, command: str,
                            result: lldb.SBCommandReturnObject,
                            internal_dict: Dict[str, Any]) -> None:
    """Remove an object from the Ghidra trace.

    Usage: ghidra trace remove-obj PATH

    This does not delete the object. It just removes it from the tree for the
    current snap and onwards.
    """

    args = shlex.split(command)
    if len(args) != 1:
        raise RuntimeError("Usage: ghidra trace remove-obj PATH")
    path = args[0]

    # NOTE: id parameter is probably not necessary, since this command is for
    # humans.
    trace, tx = STATE.require_tx()
    trace.proxy_object_path(path).remove()


def to_bytes(value: lldb.SBValue) -> bytes:
    n = value.GetNumChildren()
    return bytes(int(value.GetChildAtIndex(i).GetValueAsUnsigned())
                 for i in range(0, n))


def to_string(value: lldb.SBValue, encoding: str) -> str:
    n = value.GetNumChildren()
    b = bytes(int(value.GetChildAtIndex(i).GetValueAsUnsigned())
              for i in range(0, n))
    return str(b, encoding)


def to_bool_list(value: lldb.SBValue) -> List[bool]:
    n = value.GetNumChildren()
    return [bool(int(value.GetChildAtIndex(i).GetValueAsUnsigned()))
            for i in range(0, n)]


def to_int_list(value: lldb.SBValue) -> List[int]:
    n = value.GetNumChildren()
    return [int(value.GetChildAtIndex(i).GetValueAsUnsigned())
            for i in range(0, n)]


def to_short_list(value: lldb.SBValue) -> List[int]:
    n = value.GetNumChildren()
    return [int(value.GetChildAtIndex(i).GetValueAsUnsigned())
            for i in range(0, n)]


def get_byte_order(order: int) -> Literal['big', 'little']:
    if order == lldb.eByteOrderBig:
        return 'big'
    elif order == lldb.eByteOrderLittle:
        return 'little'
    elif order == lldb.eByteOrderPDP:
        raise ValueError("PDP byte order unsupported")
    else:
        raise ValueError(f"Unrecognized order: {order}")


def data_to_int(data: lldb.SBData) -> int:
    order = get_byte_order(data.byte_order)
    return int.from_bytes(data.uint8s, order)


def data_to_reg_bytes(data: lldb.SBData) -> bytes:
    order = get_byte_order(data.byte_order)
    if order == 'little':
        return bytes(reversed(data.uint8s))
    return bytes(data.uint8s)


def eval_value(expr: str, schema: Optional[sch.Schema] = None) -> Tuple[Union[
        bool, int, float, bytes, Tuple[str, Address], List[bool], List[int],
        str, None], Optional[sch.Schema]]:
    return convert_value(expr, util.get_eval(expr), schema)


def convert_value(expr: str, val: lldb.SBValue,
                  schema: Optional[sch.Schema] = None) -> Tuple[Union[
        bool, int, float, bytes, Tuple[str, Address], List[bool], List[int],
        str, None], Optional[sch.Schema]]:
    type = val.GetType()
    while type.IsTypedefType():
        type = type.GetTypedefedType()

    code = type.GetBasicType()
    if code == lldb.eBasicTypeVoid:
        return None, sch.VOID
    if (code == lldb.eBasicTypeChar or
        code == lldb.eBasicTypeSignedChar or
            code == lldb.eBasicTypeUnsignedChar):
        if not "\\x" in val.GetValue():
            return int(val.GetValueAsUnsigned()), sch.CHAR
        return int(val.GetValueAsUnsigned()), sch.BYTE
    if code == lldb.eBasicTypeShort or code == lldb.eBasicTypeUnsignedShort:
        return data_to_int(val.data), sch.SHORT
    if code == lldb.eBasicTypeInt or code == lldb.eBasicTypeUnsignedInt:
        return data_to_int(val.data), sch.INT
    if code == lldb.eBasicTypeLong or code == lldb.eBasicTypeUnsignedLong:
        return data_to_int(val.data), sch.LONG
    if code == lldb.eBasicTypeLongLong or code == lldb.eBasicTypeUnsignedLongLong:
        return data_to_int(val.data), sch.LONG
    if code == lldb.eBasicTypeBool:
        return bool(val.GetValue()), sch.BOOL

    # TODO: This seems like a bit of a hack
    type_name = type.GetName()
    if type_name.startswith("const char["):
        return val.GetSummary(), sch.STRING
    if type_name.startswith("const wchar_t["):
        return val.GetSummary(), sch.STRING

    if type.IsArrayType():
        etype = type.GetArrayElementType()
        while etype.IsTypedefType():
            etype = etype.GetTypedefedType()
        ecode = etype.GetBasicType()
        if ecode == lldb.eBasicTypeBool:
            return to_bool_list(val), sch.BOOL_ARR
        elif (ecode == lldb.eBasicTypeChar or
              ecode == lldb.eBasicTypeSignedChar or
              ecode == lldb.eBasicTypeUnsignedChar):
            if schema == sch.BYTE_ARR:
                return to_bytes(val), schema
            elif schema == sch.CHAR_ARR:
                return to_string(val, 'utf-8'), schema
            return to_string(val, 'utf-8'), sch.STRING
        elif (ecode == lldb.eBasicTypeShort or
              ecode == lldb.eBasicTypeUnsignedShort):
            if schema is None:
                if etype.name == 'wchar_t':
                    return to_string(val, 'utf-16'), sch.STRING
                schema = sch.SHORT_ARR
            elif schema == sch.CHAR_ARR:
                return to_string(val, 'utf-16'), schema
            return to_int_list(val), schema
        elif (ecode == lldb.eBasicTypeSignedWChar or
              ecode == lldb.eBasicTypeUnsignedWChar):
            if schema is not None and schema != sch.CHAR_ARR:
                return to_short_list(val), schema
            else:
                return to_string(val, 'utf-16'), sch.STRING
        elif ecode == lldb.eBasicTypeInt or ecode == lldb.eBasicTypeUnsignedInt:
            if schema is None:
                if etype.name == 'wchar_t':
                    return to_string(val, 'utf-32'), sch.STRING
                schema = sch.INT_ARR
            elif schema == sch.CHAR_ARR:
                return to_string(val, 'utf-32'), schema
            return to_int_list(val), schema
        elif (ecode == lldb.eBasicTypeLong or
              ecode == lldb.eBasicTypeUnsignedLong or
              ecode == lldb.eBasicTypeLongLong or
              ecode == lldb.eBasicTypeUnsignedLongLong):
            if schema is not None:
                return to_int_list(val), schema
            else:
                return to_int_list(val), sch.LONG_ARR
    elif type.IsPointerType():
        offset = data_to_int(val.data)
        proc = util.get_process()
        base, addr = STATE.require_trace().extra.require_mm().map(proc, offset)
        return (base, addr), sch.ADDRESS
    raise ValueError(f"Cannot convert ({schema}): '{expr}', value='{val}'")


@convert_errors
def ghidra_trace_set_value(debugger: lldb.SBDebugger, command: str,
                           result: lldb.SBCommandReturnObject,
                           internal_dict: Dict[str, Any]) -> None:
    """Set a value (attribute or element) in the Ghidra trace's object tree.

    Usage: ghidra trace set-value PATH KEY VALUE [SCHEMA]

    The object at PATH must exist, though it need not be inserted, yet. To
    denote an element, KEY must be in the form [INDEX]. VALUE is an expression
    evaluated within the current target. This command will attempt to convert
    the value according to its type, into a type recordable by the Ghidra trace.

    A void or null value implies removal. NOTE: The type of an expression may be
    subject to LLDB's current language. To explicitly specify the type, include
    the SCHEMA argument.
    """

    # NOTE: id parameter is probably not necessary, since this command is for
    # humans.
    # TODO: path and key are two separate parameters.... This is mostly to
    # spare me from porting path parsing to Python, but it may also be useful
    # if we ever allow ids here, since the id would be for the object, not the
    # complete value path.

    args = shlex.split(command)
    if len(args) == 3:
        path = args[0]
        key = args[1]
        value = args[2]
        schema = None
    elif len(args) == 4:
        path = args[0]
        key = args[1]
        value = args[2]
        schema = sch.Schema(args[3])
    else:
        raise RuntimeError(
            "Usage: ghidra trace set-value PATH KEY VALUE [SCHEMA]")

    trace, tx = STATE.require_tx()
    if schema == sch.OBJECT:
        val: Union[bool, int, float, bytes, Tuple[str, Address], List[bool],
                   List[int], str, TraceObject, Address,
                   None] = trace.proxy_object_path(value)
    else:
        val, schema = eval_value(value, schema)
        if schema == sch.ADDRESS and isinstance(val, tuple):
            base, addr = val
            val = addr
            if base != addr.space:
                trace.create_overlay_space(base, addr.space)
    trace.proxy_object_path(path).set_value(key, val, schema)


retain_values_parser = optparse.OptionParser(prog='ghidra trace retain-values',
                                             usage="""
Usage: %prog [OPTIONS] [KEYS...]""")
retain_values_parser.add_option(
    '-e', '--elements', action='store_const', dest='kinds', default='elements',
    const='elements', help="Remove all other elements")
retain_values_parser.add_option(
    '-a', '--attributes', action='store_const', dest='kinds',
    const='attributes', help="Remove all other attributes")
retain_values_parser.add_option(
    '-b', '--both', action='store_const', dest='kinds',
    const='both', help="Remove all other elements and attributes")


@convert_errors
def ghidra_trace_retain_values(debugger: lldb.SBDebugger, command: str,
                               result: lldb.SBCommandReturnObject,
                               internal_dict: Dict[str, Any]) -> None:
    """Retain only those keys listed, setting all others to null.

    Usage: ghidra trace retain-values [OPTIONS] PATH [KEYS...]

    OPTIONS may be one of:

        --elements To set all other elements to null (default)
        --attributes To set all other attributes to null
        --both To set all other values (elements and attributes) to null

    KEYS is a space-separated list of keys to keep. This list may be empty, in
    which case, all keys of the specified kind are removed.
    """

    options, args = retain_values_parser.parse_args(shlex.split(command))
    if len(args) < 1:
        raise RuntimeError(
            "Usage: ghidra trace retain-values [OPTIONS] PATH [KEYS...]")
    path = args[0]
    keys = args[1:]

    trace, tx = STATE.require_tx()
    trace.proxy_object_path(path).retain_values(keys, kinds=options.kinds)


@convert_errors
def ghidra_trace_get_obj(debugger: lldb.SBDebugger, command: str,
                         result: lldb.SBCommandReturnObject,
                         internal_dict: Dict[str, Any]) -> None:
    """Get an object descriptor by its canonical path.

    Usage: ghidra trace get-obj PATH

    This isn't the most informative, but it will at least confirm whether an
    object exists and provide its id.
    """

    args = shlex.split(command)
    if len(args) != 1:
        raise RuntimeError("Usage: ghidra trace get-obj PATH")
    path = args[0]

    trace = STATE.require_trace()
    object = trace.get_object(path)
    result.PutCString(f"{object.id}\t{object.path}")


@convert_errors
def ghidra_trace_get_values(debugger: lldb.SBDebugger, command: str,
                            result: lldb.SBCommandReturnObject,
                            internal_dict: Dict[str, Any]) -> None:
    """List all values matching a given path pattern.

    Usage: ghidra trace get-values PATTERN

    PATTERN is a path where blanks indicate wild cards. Beware, this may seem a
    little odd, esp., when the final key is a wild card. Here are some examples:

       Processes[]             To get all processes
       Processes[0].Threads[]  To get all threads in the first process
       Processes[].Threads[]   To get all threads from all processes
       Processes[0].           (Note the trailing period) to get all attributes
                               of the first process
    """

    args = shlex.split(command)
    if len(args) != 1:
        raise RuntimeError("Usage: ghidra trace get-values PATTERN")
    pattern = args[0]

    trace = STATE.require_trace()
    values = wait(trace.get_values(pattern))
    print_tabular_values(values, result.PutCString)


@convert_errors
def ghidra_trace_get_values_rng(debugger: lldb.SBDebugger, command: str,
                                result: lldb.SBCommandReturnObject,
                                internal_dict: Dict[str, Any]) -> None:
    """List all values intersecting a given address range.

    Usage: ghidra trace get-values-rng ADDRESS LENGTH

    This can only retrieve values of type ADDRESS or RANGE.
    NOTE: Even in batch mode, this request will block for the result.
    """

    args = shlex.split(command)
    if len(args) != 2:
        raise RuntimeError("Usage: ghidra trace get-values-rng ADDRESS LENGTH")
    address = args[0]
    length = args[1]

    trace = STATE.require_trace()
    start, end = eval_range(address, length)
    proc = util.get_process()
    base, addr = trace.extra.require_mm().map(proc, start)
    # Do not create the space. We're querying. No tx.
    values = wait(trace.get_values_intersecting(addr.extend(end - start)))
    print_tabular_values(values, result.PutCString)


def activate(path: Optional[str] = None) -> None:
    trace = STATE.require_trace()
    if path is None:
        proc = util.get_process()
        t = util.selected_thread()
        frame = util.selected_frame()
        if frame is not None:
            path = FRAME_PATTERN.format(
                procnum=proc.GetProcessID(), tnum=t.GetThreadID(),
                level=frame.GetFrameID())
        elif t is not None:
            path = THREAD_PATTERN.format(
                procnum=proc.GetProcessID(), tnum=t.GetThreadID())
        else:
            path = PROCESS_PATTERN.format(procnum=proc.GetProcessID())
        trace.proxy_object_path(path).activate()


@convert_errors
def ghidra_trace_activate(debugger: lldb.SBDebugger, command: str,
                          result: lldb.SBCommandReturnObject,
                          internal_dict: Dict[str, Any]) -> None:
    """Activate an object in Ghidra's GUI.

    Usage: ghidra trace activate [PATH]

    This has no effect if the current trace is not current in Ghidra. If PATH is
    omitted, this will activate the current frame.
    """

    args = shlex.split(command)
    if len(args) == 0:
        path = None
    elif len(args) == 1:
        path = args[0]
    else:
        raise RuntimeError("Usage: ghidra trace putmem ADDRESS LENGTH [PAGES]")

    activate(path)


@convert_errors
def ghidra_trace_disassemble(debugger: lldb.SBDebugger, command: str,
                             result: lldb.SBCommandReturnObject,
                             internal_dict: Dict[str, Any]) -> None:
    """Disassemble starting at the given seed.

    Usage: ghidra trace disassemble ADDRESS

    Disassembly proceeds linearly and terminates at the first branch or unknown
    memory encountered.
    """

    args = shlex.split(command)
    if len(args) != 1:
        raise RuntimeError("Usage: ghidra trace disassemble ADDRESS")
    address = args[0]

    trace, tx = STATE.require_tx()
    start = eval_address(address)
    proc = util.get_process()
    base, addr = trace.extra.require_mm().map(proc, start)
    if base != addr.space:
        trace.create_overlay_space(base, addr.space)

    length = trace.disassemble(addr)
    result.PutCString(f"Disassembled {length} bytes")


def compute_proc_state(proc: lldb.SBProcess) -> str:
    if proc.is_running:
        return 'RUNNING'
    return 'STOPPED'


def put_processes() -> None:
    trace = STATE.require_trace()
    keys = []
    proc = util.get_process()
    ipath = PROCESS_PATTERN.format(procnum=proc.GetProcessID())
    keys.append(PROCESS_KEY_PATTERN.format(procnum=proc.GetProcessID()))
    procobj = trace.create_object(ipath)
    istate = compute_proc_state(proc)
    procobj.set_value('State', istate)
    procobj.insert()
    trace.proxy_object_path(PROCESSES_PATH).retain_values(keys)


def put_state(event_process: lldb.SBProcess) -> None:
    ipath = PROCESS_PATTERN.format(procnum=event_process.GetProcessID())
    trace = STATE.require_trace()
    with trace.client.batch():
        with trace.open_tx('State'):
            procobj = trace.create_object(ipath)
            state = "STOPPED" if event_process.is_stopped else "RUNNING"
            procobj.set_value('State', state)
            procobj.insert()


@convert_errors
def ghidra_trace_put_processes(debugger: lldb.SBDebugger, command: str,
                               result: lldb.SBCommandReturnObject,
                               internal_dict: Dict[str, Any]) -> None:
    """Put the list of processes into the trace's Processes list.

    Usage: ghidra trace put-processes
    """

    args = shlex.split(command)
    if len(args) != 0:
        raise RuntimeError("Usage: ghidra trace put-processes")

    trace, tx = STATE.require_tx()
    with trace.client.batch() as b:
        put_processes()


def put_available() -> None:
    trace = STATE.require_trace()
    keys = []
    for proc in psutil.process_iter():
        ppath = AVAILABLE_PATTERN.format(pid=proc.pid)
        procobj = trace.create_object(ppath)
        keys.append(AVAILABLE_KEY_PATTERN.format(pid=proc.pid))
        procobj.set_value('PID', proc.pid)
        procobj.set_value('_display', f'{proc.pid} {proc.name()}')
        procobj.insert()
    trace.proxy_object_path(AVAILABLES_PATH).retain_values(keys)


@convert_errors
def ghidra_trace_put_available(debugger: lldb.SBDebugger, command: str,
                               result: lldb.SBCommandReturnObject,
                               internal_dict: Dict[str, Any]) -> None:
    """Put the list of available processes into the trace's Available list.

    Usage: ghidra trace put-available
    """

    args = shlex.split(command)
    if len(args) != 0:
        raise RuntimeError("Usage: ghidra trace put-available")

    trace, tx = STATE.require_tx()
    with trace.client.batch() as b:
        put_available()


def put_single_breakpoint(b: lldb.SBBreakpoint, proc: lldb.SBProcess) -> None:
    trace = STATE.require_trace()
    mapper = trace.extra.require_mm()
    bpt_path = PROC_BREAK_PATTERN.format(
        procnum=proc.GetProcessID(), breaknum=b.GetID())
    bpt_obj = trace.create_object(bpt_path)
    if b.IsHardware():
        bpt_obj.set_value('Expression', util.get_description(b))
        bpt_obj.set_value('Kinds', 'HW_EXECUTE')
    else:
        bpt_obj.set_value('Expression', util.get_description(b))
        bpt_obj.set_value('Kinds', 'SW_EXECUTE')
    cmdList = lldb.SBStringList()
    if b.GetCommandLineCommands(cmdList):
        list = []
        for i in range(0, cmdList.GetSize()):
            list.append(cmdList.GetStringAtIndex(i))
        bpt_obj.set_value('Commands', list)
    if b.GetCondition():
        bpt_obj.set_value('Condition', b.GetCondition())
    bpt_obj.set_value('Hit Count', b.GetHitCount())
    bpt_obj.set_value('Ignore Count', b.GetIgnoreCount())
    bpt_obj.set_value('Temporary', b.IsOneShot())
    bpt_obj.set_value('Enabled', b.IsEnabled())
    loc_keys = []
    locs = util.BREAKPOINT_LOCATION_INFO_READER.get_locations(b)
    for i, l in enumerate(locs):
        # Retain the key, even if not for this process
        k = BREAK_LOC_KEY_PATTERN.format(locnum=i+1)
        loc_keys.append(k)
        loc_obj = trace.create_object(bpt_path + k)
        if b.location is not None:  # Implies execution break
            base, addr = mapper.map(proc, l.GetLoadAddress())
            if base != addr.space:
                trace.create_overlay_space(base, addr.space)
            loc_obj.set_value('Range', addr.extend(1))
            loc_obj.set_value('Enabled', l.IsEnabled())
        else:  # I guess it's a catchpoint
            pass
        loc_obj.insert()
    bpt_obj.retain_values(loc_keys)
    bpt_obj.insert()


def put_single_watchpoint(w: lldb.SBWatchpoint, proc: lldb.SBProcess) -> None:
    trace = STATE.require_trace()
    mapper = trace.extra.require_mm()
    wpt_path = PROC_WATCH_PATTERN.format(
        procnum=proc.GetProcessID(), watchnum=w.GetID())
    wpt_obj = trace.create_object(wpt_path)
    desc = util.get_description(w, level=0)
    wpt_obj.set_value('Expression', desc)
    wpt_obj.set_value('Kinds', 'WRITE')
    if "type = r" in desc:
        wpt_obj.set_value('Kinds', 'READ')
    if "type = rw" in desc:
        wpt_obj.set_value('Kinds', 'READ,WRITE')
    base, addr = mapper.map(proc, w.GetWatchAddress())
    if base != addr.space:
        trace.create_overlay_space(base, addr.space)
    wpt_obj.set_value('Range', addr.extend(w.GetWatchSize()))
    if w.GetCondition():
        wpt_obj.set_value('Condition', w.GetCondition())
    wpt_obj.set_value('Hit Count', w.GetHitCount())
    wpt_obj.set_value('Ignore Count', w.GetIgnoreCount())
    wpt_obj.set_value('Hardware Index', w.GetHardwareIndex())
    wpt_obj.set_value('Watch Address', hex(w.GetWatchAddress()))
    wpt_obj.set_value('Watch Size', w.GetWatchSize())
    wpt_obj.set_value('Enabled', w.IsEnabled())
    wpt_obj.insert()


def put_breakpoints() -> None:
    target = util.get_target()
    proc = util.get_process()
    cont_path = PROC_BREAKS_PATTERN.format(procnum=proc.GetProcessID())
    cont_obj = STATE.require_trace().create_object(cont_path)
    keys = []
    for i in range(0, target.GetNumBreakpoints()):
        b = target.GetBreakpointAtIndex(i)
        keys.append(BREAKPOINT_KEY_PATTERN.format(breaknum=b.GetID()))
        put_single_breakpoint(b, proc)
    cont_obj.insert()
    cont_obj.retain_values(keys)


def put_watchpoints() -> None:
    target = util.get_target()
    proc = util.get_process()
    cont_path = PROC_WATCHES_PATTERN.format(procnum=proc.GetProcessID())
    cont_obj = STATE.require_trace().create_object(cont_path)
    keys = []
    for i in range(0, target.GetNumWatchpoints()):
        b = target.GetWatchpointAtIndex(i)
        keys.append(WATCHPOINT_KEY_PATTERN.format(watchnum=b.GetID()))
        put_single_watchpoint(b, proc)
    cont_obj.insert()
    cont_obj.retain_values(keys)


@convert_errors
def ghidra_trace_put_breakpoints(debugger: lldb.SBDebugger, command: str,
                                 result: lldb.SBCommandReturnObject,
                                 internal_dict: Dict[str, Any]) -> None:
    """Put the current process's breakpoints into the trace.

    Usage: ghidra trace put-breakpoints
    """

    args = shlex.split(command)
    if len(args) != 0:
        raise RuntimeError("Usage: ghidra trace put-breakpoints")

    trace, tx = STATE.require_tx()
    with trace.client.batch() as b:
        put_breakpoints()


@convert_errors
def ghidra_trace_put_watchpoints(debugger: lldb.SBDebugger, command: str,
                                 result: lldb.SBCommandReturnObject,
                                 internal_dict: Dict[str, Any]) -> None:
    """Put the current process's watchpoints into the trace.

    Usage: ghidra trace put-watchpoints
    """

    args = shlex.split(command)
    if len(args) != 0:
        raise RuntimeError("Usage: ghidra trace put-watchpoints")

    trace, tx = STATE.require_tx()
    with trace.client.batch() as b:
        put_watchpoints()


def put_environment() -> None:
    proc = util.get_process()
    epath = ENV_PATTERN.format(procnum=proc.GetProcessID())
    envobj = STATE.require_trace().create_object(epath)
    envobj.set_value('Debugger', 'lldb')
    envobj.set_value('Arch', arch.get_arch())
    envobj.set_value('OS', arch.get_osabi())
    envobj.set_value('Endian', arch.get_endian())
    envobj.insert()


@convert_errors
def ghidra_trace_put_environment(debugger: lldb.SBDebugger, command: str,
                                 result: lldb.SBCommandReturnObject,
                                 internal_dict: Dict[str, Any]) -> None:
    """Put some environment indicators into the Ghidra trace.

    Usage: ghidra trace put-environment
    """

    args = shlex.split(command)
    if len(args) != 0:
        raise RuntimeError("Usage: ghidra trace put-environment")

    trace, tx = STATE.require_tx()
    with trace.client.batch() as b:
        put_environment()


def should_query_regions() -> bool:
    """It's possible some targets don't support regions.

    There is also a bug in LLDB that can cause its gdb-remote client to
    drop support. We need to account for this second case while still
    ensuring we populate the full range for targets that genuinely don't
    support it.
    """
    # somewhat crappy heuristic to distinguish remote from local
    tgt = util.get_target()
    if tgt.GetNumModules() == 0:
        # Target genuinely doesn't support regions.
        # Will update with full_mem
        return True
    # Target does support it, but bug might be in play
    # probe address 0. Should get the invalid region
    proc = util.get_process()
    info = lldb.SBMemoryRegionInfo()
    result = proc.GetMemoryRegionInfo(0, info)
    return result.Success()


def put_regions() -> None:
    regions = []
    proc = util.get_process()
    if should_query_regions():
        try:
            regions = util.REGION_INFO_READER.get_regions()
        except Exception:
            pass
    if len(regions) == 0 and util.selected_thread() is not None:
        regions = [util.REGION_INFO_READER.full_mem()]
    trace = STATE.require_trace()
    mapper = trace.extra.require_mm()
    keys = []
    for r in regions:
        rpath = REGION_PATTERN.format(
            procnum=proc.GetProcessID(), start=r.start)
        keys.append(REGION_KEY_PATTERN.format(start=r.start))
        regobj = trace.create_object(rpath)
        start_base, start_addr = mapper.map(proc, r.start)
        if start_base != start_addr.space:
            trace.create_overlay_space(start_base, start_addr.space)
        regobj.set_value('Range', start_addr.extend(r.end - r.start))
        if r.perms != None:
            regobj.set_value('Permissions', r.perms)
        regobj.set_value('_readable', r.perms == None or 'r' in r.perms)
        regobj.set_value('_writable', r.perms == None or 'w' in r.perms)
        regobj.set_value('_executable', r.perms == None or 'x' in r.perms)
        regobj.set_value('Offset', hex(r.offset))
        regobj.set_value('Object File', r.objfile)
        regobj.insert()
    trace.proxy_object_path(
        MEMORY_PATTERN.format(procnum=proc.GetProcessID())).retain_values(keys)


@convert_errors
def ghidra_trace_put_regions(debugger: lldb.SBDebugger, command: str,
                             result: lldb.SBCommandReturnObject,
                             internal_dict: Dict[str, Any]) -> None:
    """Read the memory map, if applicable, and write to the trace's Regions.

    Usage: ghidra trace put-regions
    """

    args = shlex.split(command)
    if len(args) != 0:
        raise RuntimeError("Usage: ghidra trace put-regions")

    trace, tx = STATE.require_tx()
    with trace.client.batch() as b:
        put_regions()


def put_modules() -> None:
    target = util.get_target()
    proc = util.get_process()
    modules = util.MODULE_INFO_READER.get_modules()
    trace = STATE.require_trace()
    mapper = trace.extra.require_mm()
    mod_keys = []
    for mk, m in modules.items():
        mpath = MODULE_PATTERN.format(procnum=proc.GetProcessID(), modpath=mk)
        modobj = trace.create_object(mpath)
        mod_keys.append(MODULE_KEY_PATTERN.format(modpath=mk))
        modobj.set_value('Name', m.name)
        base_base, base_addr = mapper.map(proc, m.base)
        if base_base != base_addr.space:
            trace.create_overlay_space(base_base, base_addr.space)
        if m.max > m.base:
            modobj.set_value('Range', base_addr.extend(m.max - m.base + 1))
        sec_keys = []
        for sk, s in m.sections.items():
            spath = mpath + SECTION_ADD_PATTERN.format(secname=sk)
            secobj = trace.create_object(spath)
            sec_keys.append(SECTION_KEY_PATTERN.format(secname=sk))
            start_base, start_addr = mapper.map(proc, s.start)
            if start_base != start_addr.space:
                trace.create_overlay_space(
                    start_base, start_addr.space)
            secobj.set_value('Range', start_addr.extend(s.end - s.start + 1))
            secobj.set_value('Offset', hex(s.offset))
            secobj.set_value('Attrs', s.attrs)
            secobj.insert()
        # In case there are no sections, we must still insert the module
        modobj.insert()
        trace.proxy_object_path(
            mpath + SECTIONS_ADD_PATTERN).retain_values(sec_keys)
    trace.proxy_object_path(MODULES_PATTERN.format(
        procnum=proc.GetProcessID())).retain_values(mod_keys)


@convert_errors
def ghidra_trace_put_modules(debugger: lldb.SBDebugger, command: str,
                             result: lldb.SBCommandReturnObject,
                             internal_dict: Dict[str, Any]) -> None:
    """Gather object files, if applicable, and write to the trace's Modules.

    Usage: ghidra trace put-modules
    """

    args = shlex.split(command)
    if len(args) != 0:
        raise RuntimeError("Usage: ghidra trace put-modules")

    trace, tx = STATE.require_tx()
    with trace.client.batch() as b:
        put_modules()


def convert_state(t: lldb.SBThread) -> str:
    # TODO: This does not seem to work - currently supplanted by proc.is_running
    if t.IsSuspended():
        return 'SUSPENDED'
    if t.IsStopped():
        return 'STOPPED'
    return 'RUNNING'


def compute_thread_display(t: lldb.SBThread) -> str:
    return util.get_description(t)


def put_threads() -> None:
    radix_raw = util.get_convenience_variable('output-radix')
    radix = 16 if radix_raw == 'auto' else int(radix_raw)
    proc = util.get_process()
    keys = []
    trace = STATE.require_trace()
    for t in proc.threads:
        tpath = THREAD_PATTERN.format(
            procnum=proc.GetProcessID(), tnum=t.GetThreadID())
        tobj = trace.create_object(tpath)
        keys.append(THREAD_KEY_PATTERN.format(tnum=t.GetThreadID()))
        tobj.set_value('State', compute_proc_state(proc))
        tobj.set_value('Name', t.GetName())
        tid = t.GetThreadID()
        tobj.set_value('TID', tid)
        tidstr = f'0x{tid:x}' if radix == 16 else f'0{tid:o}' if radix == 8 else f'{tid}'
        tobj.set_value('_short_display',
                       f'[{proc.GetProcessID()}.{t.GetThreadID()}:{tidstr}]')
        tobj.set_value('_display', compute_thread_display(t))
        tobj.insert()
    trace.proxy_object_path(
        THREADS_PATTERN.format(procnum=proc.GetProcessID())).retain_values(keys)


def put_event_thread() -> None:
    proc = util.get_process()
    # Assumption: Event thread is selected by lldb upon stopping
    t = util.selected_thread()
    trace = STATE.require_trace()
    if t is not None:
        tpath = THREAD_PATTERN.format(
            procnum=proc.GetProcessID(), tnum=t.GetThreadID())
        tobj = trace.proxy_object_path(tpath)
    else:
        tobj = None
    trace.proxy_object_path('').set_value('_event_thread', tobj)


@convert_errors
def ghidra_trace_put_threads(debugger: lldb.SBDebugger, command: str,
                             result: lldb.SBCommandReturnObject,
                             internal_dict: Dict[str, Any]) -> None:
    """Put the current process's threads into the Ghidra trace.

    Usage: ghidra trace put-threads
    """

    args = shlex.split(command)
    if len(args) != 0:
        raise RuntimeError("Usage: ghidra trace put-threads")

    trace, tx = STATE.require_tx()
    with trace.client.batch() as b:
        put_threads()


def put_frames() -> None:
    proc = util.get_process()
    trace = STATE.require_trace()
    mapper = trace.extra.require_mm()
    t = util.selected_thread()
    if t is None:
        return
    keys = []
    for i in range(0, t.GetNumFrames()):
        f = t.GetFrameAtIndex(i)
        fpath = FRAME_PATTERN.format(
            procnum=proc.GetProcessID(), tnum=t.GetThreadID(), level=f.GetFrameID())
        fobj = trace.create_object(fpath)
        keys.append(FRAME_KEY_PATTERN.format(level=f.GetFrameID()))
        base, pc = mapper.map(proc, f.GetPC())
        if base != pc.space:
            trace.create_overlay_space(base, pc.space)
        fobj.set_value('PC', pc)
        fobj.set_value('Function', str(f.GetFunctionName()))
        fobj.set_value('_display', util.get_description(f))
        fobj.insert()
        robj = trace.create_object(fpath+".Registers")
        robj.insert()
    trace.proxy_object_path(STACK_PATTERN.format(
        procnum=proc.GetProcessID(), tnum=t.GetThreadID())).retain_values(keys)


@convert_errors
def ghidra_trace_put_frames(debugger: lldb.SBDebugger, command: str,
                            result: lldb.SBCommandReturnObject,
                            internal_dict: Dict[str, Any]) -> None:
    """Put the current thread's frames into the Ghidra trace.

    Usage: ghidra trace put-frames
    """

    args = shlex.split(command)
    if len(args) != 0:
        raise RuntimeError("Usage: ghidra trace put-frames")

    trace, tx = STATE.require_tx()
    with trace.client.batch() as b:
        put_frames()


@convert_errors
def ghidra_trace_put_all(debugger: lldb.SBDebugger, command: str,
                         result: lldb.SBCommandReturnObject,
                         internal_dict: Dict[str, Any]) -> None:
    """Put everything currently selected into the Ghidra trace.

    Usage: ghidra trace put-all
    """

    args = shlex.split(command)
    if len(args) != 0:
        raise RuntimeError("Usage: ghidra trace put-all")

    trace, tx = STATE.require_tx()
    with trace.client.batch() as b:
        ghidra_trace_putreg(debugger, DEFAULT_REGISTER_BANK,
                            result, internal_dict)
        ghidra_trace_putmem(debugger, "$pc 1", result, internal_dict)
        ghidra_trace_putmem(debugger, "$sp 1", result, internal_dict)
        put_processes()
        put_environment()
        put_regions()
        put_modules()
        put_threads()
        put_frames()
        put_breakpoints()
        put_watchpoints()
        put_available()


@convert_errors
def ghidra_trace_install_hooks(debugger: lldb.SBDebugger, command: str,
                               result: lldb.SBCommandReturnObject,
                               internal_dict: Dict[str, Any]) -> None:
    """Install hooks to trace in Ghidra.

    Usage: ghidra trace install-hooks
    """

    args = shlex.split(command)
    if len(args) != 0:
        raise RuntimeError("Usage: ghidra trace install-hooks")

    hooks.install_hooks()


@convert_errors
def ghidra_trace_remove_hooks(debugger: lldb.SBDebugger, command: str,
                              result: lldb.SBCommandReturnObject,
                              internal_dict: Dict[str, Any]) -> None:
    """Remove hooks to trace in Ghidra.

    Usage: ghidra trace remove-hooks

    Using this directly is not recommended, unless it seems the hooks are
    preventing lldb or other extensions from operating. Removing hooks will break
    trace synchronization until they are replaced.
    """

    args = shlex.split(command)
    if len(args) != 0:
        raise RuntimeError("Usage: ghidra trace remove-hooks")

    hooks.remove_hooks()


@convert_errors
def ghidra_trace_sync_enable(debugger: lldb.SBDebugger, command: str,
                             result: lldb.SBCommandReturnObject,
                             internal_dict: Dict[str, Any]) -> None:
    """Synchronize the current process with the Ghidra trace.

    Usage: ghidra trace sync-enable

    This will automatically install hooks if necessary. The goal is to record
    the current frame, thread, and process into the trace immediately, and then
    to append the trace upon stopping and/or selecting new frames. This action
    is effective only for the current process. This command must be executed
    for each individual process you'd like to synchronize.

    This will have no effect unless or until you start a trace.
    """

    args = shlex.split(command)
    if len(args) != 0:
        raise RuntimeError("Usage: ghidra trace sync-enable")

    hooks.install_hooks()
    hooks.enable_current_process()


@convert_errors
def ghidra_trace_sync_disable(debugger: lldb.SBDebugger, command: str,
                              result: lldb.SBCommandReturnObject,
                              internal_dict: Dict[str, Any]) -> None:
    """Cease synchronizing the current process with the Ghidra trace.

    Usage: ghidra trace sync-disable

    This is the opposite of 'ghidra trace sync-enable', except it will not
    automatically remove hooks.
    """

    args = shlex.split(command)
    if len(args) != 0:
        raise RuntimeError("Usage: ghidra trace sync-disable")

    hooks.disable_current_process()


@convert_errors
def ghidra_trace_sync_synth_stopped(debugger: lldb.SBDebugger, command: str,
                                    result: lldb.SBCommandReturnObject,
                                    internal_dict: Dict[str, Any]) -> None:
    """Act as though the target has just stopped.

    This may need to be invoked immediately after 'ghidra trace sync-
    enable', to ensure the first snapshot displays the initial/current
    target state.
    """

    hooks.on_stop(None)  # Pass a fake event


@convert_errors
def ghidra_util_wait_stopped(debugger: lldb.SBDebugger, command: str,
                             result: lldb.SBCommandReturnObject,
                             internal_dict: Dict[str, Any]) -> None:
    """Spin wait until the selected thread is stopped.

    Usage: ghidra util wait-stopped [SECONDS]

    An optional timeout may be given in seconds. If omitted, the timeout is 1
    second.
    """

    args = shlex.split(command)
    if len(args) == 0:
        timeout = 1
    elif len(args) == 1:
        timeout = int(args[0])
    else:
        raise RuntimeError("Usage: ghidra util wait-stopped [SECONDS]")

    start = time.time()
    p = util.get_process()
    while p is not None and p.state == lldb.eStateRunnig:
        time.sleep(0.1)
        p = util.get_process()  # I suppose it could change
        if time.time() - start > timeout:
            raise RuntimeError('Timed out waiting for thread to stop')
    print(f"Finished wait. State={p.state}")
