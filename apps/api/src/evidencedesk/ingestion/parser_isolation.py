"""Linux child restrictions layered on a read-only, network-none Docker container."""

import ctypes
import errno
import os
import platform
import resource
import sys


class Filter(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_ushort),
        ("jt", ctypes.c_ubyte),
        ("jf", ctypes.c_ubyte),
        ("k", ctypes.c_uint32),
    ]


class Program(ctypes.Structure):
    _fields_ = [("length", ctypes.c_ushort), ("filters", ctypes.POINTER(Filter))]


def install_filter() -> None:
    """Fail closed, including x32 ABI; deny networking, inspection and new processes."""
    machine = platform.machine()
    if sys.platform != "linux" or machine not in {"x86_64", "aarch64"}:
        raise RuntimeError("Parser service requires supported Linux syscall ABI")
    if machine == "x86_64":
        arch = 0xC000003E
        # socket/connect/accept/sendto/recvfrom/sendmsg/recvmsg/shutdown/bind/listen/
        # names/socketpair/options, clone/fork/vfork/execve, ptrace, accept4,
        # process_vm_readv/writev, execveat, io_uring, clone3.
        denied = [*range(41, 56), 56, 57, 58, 59, 101, 288, 310, 311, 322, 425, 426, 427, 435]
        denied += [29, 30, 31, *range(64, 72), 220, *range(240, 246), 248, 249, 250]
        denied += [
            76,
            77,
            *range(82, 89),
            *range(90, 95),
            132,
            188,
            189,
            190,
            197,
            198,
            199,
            258,
            259,
            260,
            261,
            263,
            264,
            265,
            266,
            268,
            280,
            304,
            316,
            437,
            438,
        ]
        open_calls = [(2, 24), (257, 32)]
    else:
        arch = 0xC00000B7
        denied = [117, *range(198, 213), 220, 221, 242, 270, 271, 281, 425, 426, 427, 435]
        denied += [*range(180, 198), 217, 218, 219]
        denied += [
            5,
            6,
            7,
            14,
            15,
            16,
            *range(33, 39),
            45,
            46,
            52,
            53,
            54,
            55,
            88,
            265,
            276,
            437,
            438,
        ]
        open_calls = [(56, 32)]
    # Check seccomp_data.arch before interpreting syscall numbers; disallow x32/unknown ABI.
    instructions = [
        Filter(0x20, 0, 0, 4),
        Filter(0x15, 1, 0, arch),
        Filter(0x06, 0, 0, 0x80000000),
        Filter(0x20, 0, 0, 0),
        Filter(0x35, 0, 1, 0x40000000),
        Filter(0x06, 0, 0, 0x80000000),
    ]
    # Parser libraries only read files. Permit ordinary read-only opens; stdout is an
    # inherited bounded descriptor. Block every write access/create/truncate/append/tmpfile
    # open plus openat2 and path mutation syscalls, including on /dev/shm or /dev/mqueue.
    write_flags = 3 | 64 | 512 | 1024 | 0x400000
    for syscall, flags_offset in open_calls:
        instructions.extend(
            [
                Filter(0x15, 0, 4, syscall),
                Filter(0x20, 0, 0, flags_offset),
                Filter(0x45, 0, 1, write_flags),
                Filter(0x06, 0, 0, 0x00050000 | errno.EPERM),
                Filter(0x20, 0, 0, 0),
            ]
        )
    for syscall in denied:
        instructions.extend(
            [Filter(0x15, 0, 1, syscall), Filter(0x06, 0, 0, 0x00050000 | errno.EPERM)]
        )
    instructions.append(Filter(0x06, 0, 0, 0x7FFF0000))
    array = (Filter * len(instructions))(*instructions)
    program = Program(len(instructions), array)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) != 0 or libc.prctl(22, 2, ctypes.byref(program), 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "Cannot isolate parser syscalls")


def isolate() -> None:
    if os.getuid() != 0:
        raise RuntimeError("Parser supervisor must drop a distinct child identity")
    os.setgroups([])
    os.setgid(65532)
    os.setuid(65532)
    resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    resource.setrlimit(resource.RLIMIT_AS, (1024**3, 1024**3))
    resource.setrlimit(resource.RLIMIT_FSIZE, (256 * 1024**2, 256 * 1024**2))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_NPROC, (1, 1))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.umask(0o077)
    os.chdir("/")
    install_filter()
