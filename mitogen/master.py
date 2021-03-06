import Queue
import commands
import dis
import errno
import getpass
import imp
import inspect
import itertools
import logging
import os
import pkgutil
import pty
import re
import select
import signal
import socket
import sys
import termios
import textwrap
import threading
import time
import types
import zlib

if not hasattr(pkgutil, 'find_loader'):
    # find_loader() was new in >=2.5, but the modern pkgutil.py syntax has
    # been kept intentionally 2.3 compatible so we can reuse it.
    from mitogen.compat import pkgutil

import mitogen.core


LOG = logging.getLogger('mitogen')
RLOG = logging.getLogger('mitogen.ctx')

DOCSTRING_RE = re.compile(r'""".+?"""', re.M | re.S)
COMMENT_RE = re.compile(r'^[ ]*#[^\n]*$', re.M)
IOLOG_RE = re.compile(r'^[ ]*IOLOG.debug\(.+?\)$', re.M)


def minimize_source(source):
    subber = lambda match: '""' + ('\n' * match.group(0).count('\n'))
    source = DOCSTRING_RE.sub(subber, source)
    source = COMMENT_RE.sub('', source)
    return source.replace('    ', '\t')


def get_child_modules(path, fullname):
    it = pkgutil.iter_modules([os.path.dirname(path)])
    return ['%s.%s' % (fullname, name) for _, name, _ in it]


def format_cmdline(args):
    return ' '.join(
            commands.mkarg(arg).strip()
        if any(s in arg for s in (" '\"$")) else
            arg
        for arg in args
    )


def create_child(*args):
    parentfp, childfp = socket.socketpair()
    pid = os.fork()
    if not pid:
        os.dup2(childfp.fileno(), 0)
        os.dup2(childfp.fileno(), 1)
        childfp.close()
        parentfp.close()
        os.execvp(args[0], args)
        raise SystemExit

    childfp.close()
    LOG.debug('create_child() child %d fd %d, parent %d, cmd: %s',
              pid, parentfp.fileno(), os.getpid(), format_cmdline(args))
    return pid, os.dup(parentfp.fileno())


def flags(names):
    """Return the result of ORing a set of (space separated) :py:mod:`termios`
    module constants together."""
    return sum(getattr(termios, name) for name in names.split())


def cfmakeraw((iflag, oflag, cflag, lflag, ispeed, ospeed, cc)):
    """Given a list returned by :py:func:`termios.tcgetattr`, return a list
    that has been modified in the same manner as the `cfmakeraw()` C library
    function."""
    iflag &= ~flags('IGNBRK BRKINT PARMRK ISTRIP INLCR IGNCR ICRNL IXON')
    oflag &= ~flags('OPOST IXOFF')
    lflag &= ~flags('ECHO ECHOE ECHONL ICANON ISIG IEXTEN')
    cflag &= ~flags('CSIZE PARENB')
    cflag |= flags('CS8')

    iflag = 0
    oflag = 0
    lflag = 0
    return [iflag, oflag, cflag, lflag, ispeed, ospeed, cc]


def disable_echo(fd):
    old = termios.tcgetattr(fd)
    new = cfmakeraw(old)
    flags = (
        termios.TCSAFLUSH |
        getattr(termios, 'TCSASOFT', 0)
    )
    termios.tcsetattr(fd, flags, new)


def close_nonstandard_fds():
    for fd in xrange(3, 1024):
        try:
            os.close(fd)
        except OSError:
            pass


def tty_create_child(*args):
    master_fd, slave_fd = os.openpty()
    disable_echo(master_fd)
    disable_echo(slave_fd)

    pid = os.fork()
    if not pid:
        os.dup2(slave_fd, 0)
        os.dup2(slave_fd, 1)
        os.dup2(slave_fd, 2)
        close_nonstandard_fds()
        os.setsid()
        os.close(os.open(os.ttyname(1), os.O_RDWR))
        os.execvp(args[0], args)
        raise SystemExit

    os.close(slave_fd)
    LOG.debug('tty_create_child() child %d fd %d, parent %d, cmd: %s',
              pid, master_fd, os.getpid(), format_cmdline(args))
    return pid, master_fd


def write_all(fd, s, deadline=None):
    timeout = None
    written = 0

    while written < len(s):
        if deadline is not None:
            timeout = max(0, deadline - time.time())
        if timeout == 0:
            raise mitogen.core.TimeoutError('write timed out')

        _, wfds, _ = select.select([], [fd], [], timeout)
        if not wfds:
            continue

        n, disconnected = mitogen.core.io_op(os.write, fd, buffer(s, written))
        if disconnected:
            raise mitogen.core.StreamError('EOF on stream during write')

        written += n


def iter_read(fd, deadline=None):
    bits = []
    timeout = None

    while True:
        if deadline is not None:
            timeout = max(0, deadline - time.time())
            if timeout == 0:
                break

        rfds, _, _ = select.select([fd], [], [], timeout)
        if not rfds:
            continue

        s, disconnected = mitogen.core.io_op(os.read, fd, 4096)
        if disconnected:
            raise mitogen.core.StreamError(
                'EOF on stream; last 300 bytes received: %r' %
                (''.join(bits)[-300:],)
            )

        bits.append(s)
        yield s

    raise mitogen.core.TimeoutError('read timed out')


def discard_until(fd, s, deadline):
    for buf in iter_read(fd, deadline):
        if buf.endswith(s):
            return


def scan_code_imports(co, LOAD_CONST=dis.opname.index('LOAD_CONST'),
                          IMPORT_NAME=dis.opname.index('IMPORT_NAME')):
    """Given a code object `co`, scan its bytecode yielding any
    ``IMPORT_NAME`` and associated prior ``LOAD_CONST`` instructions
    representing an `Import` statement or `ImportFrom` statement.

    :return:
        Generator producing `(level, modname, namelist)` tuples, where:

        * `level`: -1 for normal import, 0, for absolute import, and >0 for
          relative import.
        * `modname`: Name of module to import, or from where `namelist` names
          are imported.
        * `namelist`: for `ImportFrom`, the list of names to be imported from
          `modname`.
    """
    # Yield `(op, oparg)` tuples from the code object `co`.
    ordit = itertools.imap(ord, co.co_code)
    nextb = ordit.next

    opit = ((c, (None
                 if c < dis.HAVE_ARGUMENT else
                 (nextb() | (nextb() << 8))))
            for c in ordit)

    opit, opit2, opit3 = itertools.tee(opit, 3)
    next(opit2)
    next(opit3)
    next(opit3)

    for oparg1, oparg2, (op3, arg3) in itertools.izip(opit, opit2, opit3):
        if op3 == IMPORT_NAME:
            op2, arg2 = oparg2
            op1, arg1 = oparg1
            if op1 == op2 == LOAD_CONST:
                yield (co.co_consts[arg1],
                       co.co_names[arg3],
                       co.co_consts[arg2] or ())


class SelectError(mitogen.core.Error):
    pass


class Select(object):
    notify = None

    def __init__(self, receivers=(), oneshot=True):
        self._receivers = []
        self._oneshot = oneshot
        self._queue = Queue.Queue()
        for recv in receivers:
            self.add(recv)

    def _put(self, value):
        self._queue.put(value)
        if self.notify:
            self.notify(self)

    def __bool__(self):
        return bool(self._receivers)

    def __enter__(self):
        return self

    def __exit__(self, e_type, e_val, e_tb):
        self.close()

    def __iter__(self):
        while self._receivers:
            recv, msg = self.get()
            yield recv, msg

    loop_msg = 'Adding this Select instance would create a Select cycle'

    def _check_no_loop(self, recv):
        if recv is self:
            raise SelectError(self.loop_msg)

        for recv_ in self._receivers:
            if recv_ == recv:
                raise SelectError(self.loop_msg)
            if isinstance(recv_, Select):
                recv_._check_no_loop(recv)

    owned_msg = 'Cannot add: Receiver is already owned by another Select'

    def add(self, recv):
        if isinstance(recv, Select):
            recv._check_no_loop(self)

        self._receivers.append(recv)
        if recv.notify is not None:
            raise SelectError(self.owned_msg)

        recv.notify = self._put
        # Avoid race by polling once after installation.
        if not recv.empty():
            self._put(recv)

    not_present_msg = 'Instance is not a member of this Select'

    def remove(self, recv):
        try:
            if recv.notify != self._put:
                raise ValueError
            self._receivers.remove(recv)
            recv.notify = None
        except (IndexError, ValueError):
            raise SelectError(self.not_present_msg)

    def close(self):
        for recv in self._receivers[:]:
            self.remove(recv)

    def empty(self):
        return self._queue.empty()

    empty_msg = 'Cannot got(), Select instance is empty'

    def get(self, timeout=None):
        if not self._receivers:
            raise SelectError(self.empty_msg)

        while True:
            recv = mitogen.core._queue_interruptible_get(self._queue, timeout)
            try:
                msg = recv.get(False)
                if self._oneshot:
                    self.remove(recv)
                return recv, msg
            except mitogen.core.TimeoutError:
                # A receiver may have been queued with no result if another
                # thread drained it before we woke up, or because another
                # thread drained it between add() calling recv.empty() and
                # self._put(). In this case just sleep again.
                continue


class LogForwarder(object):
    def __init__(self, router):
        self._router = router
        self._cache = {}
        router.add_handler(self._on_forward_log, mitogen.core.FORWARD_LOG)

    def _on_forward_log(self, msg):
        if msg == mitogen.core._DEAD:
            return

        logger = self._cache.get(msg.src_id)
        if logger is None:
            context = self._router.context_by_id(msg.src_id)
            if context is None:
                LOG.error('FORWARD_LOG received from src_id %d', msg.src_id)
                return

            name = '%s.%s' % (RLOG.name, context.name)
            self._cache[msg.src_id] = logger = logging.getLogger(name)

        name, level_s, s = msg.data.split('\x00', 2)
        logger.log(int(level_s), '%s: %s', name, s)

    def __repr__(self):
        return 'LogForwarder(%r)' % (self._router,)


class ModuleFinder(object):
    STDLIB_DIRS = [
        # virtualenv on OS X does some weird half-ass job of symlinking the
        # stdlib into the virtualenv directory. So pick two modules at random
        # that represent both places the stdlib seems to come from.
        os.path.dirname(os.path.dirname(logging.__file__)),
        os.path.dirname(os.path.dirname(os.__file__)),
    ]

    def __init__(self):
        #: Import machinery is expensive, keep :py:meth`:get_module_source`
        #: results around.
        self._found_cache = {}

        #: Avoid repeated dependency scanning, which is expensive.
        self._related_cache = {}

    def __repr__(self):
        return 'ModuleFinder()'

    def is_stdlib_name(self, modname):
        """Return ``True`` if `modname` appears to come from the standard
        library."""
        if imp.is_builtin(modname) != 0:
            return True

        module = sys.modules.get(modname)
        if module is None:
            return False

        # six installs crap with no __file__
        modpath = getattr(module, '__file__', '')
        if 'site-packages' in modpath:
            return False

        for dirname in self.STDLIB_DIRS:
            if os.path.commonprefix((dirname, modpath)) == dirname:
                return True

        return False

    def _get_module_via_pkgutil(self, fullname):
        """Attempt to fetch source code via pkgutil. In an ideal world, this
        would be the only required implementation of get_module()."""
        loader = pkgutil.find_loader(fullname)
        LOG.debug('pkgutil.find_loader(%r) -> %r', fullname, loader)
        if not loader:
            return

        try:
            path = loader.get_filename(fullname)
            source = loader.get_source(fullname)
            if path is not None and source is not None:
                return path, source, loader.is_package(fullname)
        except AttributeError:
            return

    def _get_module_via_sys_modules(self, fullname):
        """Attempt to fetch source code via sys.modules. This is specifically
        to support __main__, but it may catch a few more cases."""
        if fullname not in sys.modules:
            LOG.debug('%r does not appear in sys.modules', fullname)
            return

        if 'six.moves' in fullname:
            # TODO: causes inspect.getsource() to explode.
            return None, None, None

        modpath = getattr(sys.modules[fullname], '__file__', '')
        if not modpath.rstrip('co').endswith('.py'):
            # Probably a native module.
            return None, None, None

        is_pkg = hasattr(sys.modules[fullname], '__path__')
        try:
            source = inspect.getsource(sys.modules[fullname])
        except IOError:
            # Work around inspect.getsourcelines() bug.
            if not is_pkg:
                raise
            source = '\n'

        return (sys.modules[fullname].__file__.rstrip('co'),
                source,
                hasattr(sys.modules[fullname], '__path__'))

    def _get_module_via_parent_enumeration(self, fullname):
        """Attempt to fetch source code by examining the module's (hopefully
        less insane) parent package. Required for ansible.compat.six."""
        pkgname, _, modname = fullname.rpartition('.')
        pkg = sys.modules.get(pkgname)
        if pkg is None or not hasattr(pkg, '__file__'):
            return

        pkg_path = os.path.dirname(pkg.__file__)
        try:
            fp, path, ext = imp.find_module(modname, [pkg_path])
            LOG.error('%r', (fp, path, ext))
            return path, fp.read(), False
        except ImportError, e:
            LOG.debug('imp.find_module(%r, %r) -> %s', modname, [pkg_path], e)

    get_module_methods = [_get_module_via_pkgutil,
                          _get_module_via_sys_modules,
                          _get_module_via_parent_enumeration]

    def get_module_source(self, fullname):
        """Given the name of a loaded module `fullname`, attempt to find its
        source code.

        :returns:
            Tuple of `(module path, source text, is package?)`, or ``None`` if
            the source cannot be found.
        """
        tup = self._found_cache.get(fullname)
        if tup:
            return tup

        for method in self.get_module_methods:
            tup = method(self, fullname)
            if tup:
                return tup

        return None, None, None

    def resolve_relpath(self, fullname, level):
        """Given an ImportFrom AST node, guess the prefix that should be tacked
        on to an alias name to produce a canonical name. `fullname` is the name
        of the module in which the ImportFrom appears."""
        if level == 0:
            return ''

        bits = fullname.split('.')
        if len(bits) < level:
            # This would be an ImportError in real code.
            return ''

        return '.'.join(bits[:-level]) + '.'

    def generate_parent_names(self, fullname):
        while '.' in fullname:
            fullname = fullname[:fullname.rindex('.')]
            yield fullname

    def find_related_imports(self, fullname):
        """
        Given the `fullname` of a currently loaded module, and a copy of its
        source code, examine :py:data:`sys.modules` to determine which of the
        ``import`` statements from the source code caused a corresponding
        module to be loaded that is not part of the standard library.
        """
        related = self._related_cache.get(fullname)
        if related is not None:
            return related

        modpath, src, _ = self.get_module_source(fullname)
        if src is None:
            LOG.warning('%r: cannot find source for %r', self, fullname)
            return []

        maybe_names = list(self.generate_parent_names(fullname))

        co = compile(src, modpath, 'exec')
        for level, modname, namelist in scan_code_imports(co):
            if level == -1:
                modnames = [modname, '%s.%s' % (fullname, modname)]
            else:
                modnames = [self.resolve_relpath(fullname, level) + modname]

            maybe_names.extend(modnames)
            maybe_names.extend(
                '%s.%s' % (mname, name)
                for mname in modnames
                for name in namelist
            )

        return self._related_cache.setdefault(fullname, [
            name
            for name in maybe_names
            if sys.modules.get(name) is not None
            and not self.is_stdlib_name(name)
            and 'six.moves' not in name  # TODO: crap
        ])

    def find_related(self, fullname):
        stack = [fullname]
        found = set()

        while stack:
            fullname = stack.pop(0)
            fullnames = self.find_related_imports(fullname)
            stack.extend(set(fullnames).difference(found, stack, [fullname]))
            found.update(fullnames)

        return found


class ModuleResponder(object):
    def __init__(self, router):
        self._router = router
        self._finder = ModuleFinder()
        router.add_handler(self._on_get_module, mitogen.core.GET_MODULE)

    def __repr__(self):
        return 'ModuleResponder(%r)' % (self._router,)

    def _on_get_module(self, msg):
        LOG.debug('%r.get_module(%r)', self, msg)
        if msg == mitogen.core._DEAD:
            return

        fullname = msg.data
        try:
            path, source, is_pkg = self._finder.get_module_source(fullname)
            if source is None:
                raise ImportError('could not find %r' % (fullname,))

            if is_pkg:
                pkg_present = get_child_modules(path, fullname)
                LOG.debug('get_child_modules(%r, %r) -> %r',
                          path, fullname, pkg_present)
            else:
                pkg_present = None

            compressed = zlib.compress(source)
            self._router.route(
                mitogen.core.Message.pickled(
                    (pkg_present, path, compressed),
                    dst_id=msg.src_id,
                    handle=msg.reply_to,
                )
            )
        except Exception:
            LOG.debug('While importing %r', fullname, exc_info=True)
            self._router.route(
                mitogen.core.Message.pickled(
                    None,
                    dst_id=msg.src_id,
                    handle=msg.reply_to,
                )
            )


class ModuleForwarder(object):
    """
    Respond to GET_MODULE requests in a slave by forwarding the request to our
    parent context, or satisfying the request from our local Importer cache.
    """
    def __init__(self, router, parent_context, importer):
        self.router = router
        self.parent_context = parent_context
        self.importer = importer
        router.add_handler(self._on_get_module, mitogen.core.GET_MODULE)

    def __repr__(self):
        return 'ModuleForwarder(%r)' % (self.router,)

    def _on_get_module(self, msg):
        LOG.debug('%r._on_get_module(%r)', self, msg)
        if msg == mitogen.core._DEAD:
            return

        fullname = msg.data
        cached = self.importer._cache.get(fullname)
        if cached:
            LOG.debug('%r._on_get_module(): using cached %r', self, fullname)
            self.router.route(
                mitogen.core.Message.pickled(
                    cached,
                    dst_id=msg.src_id,
                    handle=msg.reply_to,
                )
            )
        else:
            LOG.debug('%r._on_get_module(): requesting %r', self, fullname)
            self.parent_context.send(
                mitogen.core.Message(
                    data=msg.data,
                    handle=mitogen.core.GET_MODULE,
                    reply_to=self.router.add_handler(
                        lambda m: self._on_got_source(m, msg),
                        persist=False
                    )
                )
            )

    def _on_got_source(self, msg, original_msg):
        LOG.debug('%r._on_got_source(%r, %r)', self, msg, original_msg)
        fullname = original_msg.data
        self.importer._cache[fullname] = msg.unpickle()
        self.router.route(
            mitogen.core.Message(
                data=msg.data,
                dst_id=original_msg.src_id,
                handle=original_msg.reply_to,
            )
        )


class Stream(mitogen.core.Stream):
    """
    Base for streams capable of starting new slaves.
    """
    #: The path to the remote Python interpreter.
    python_path = 'python2.7'

    #: True to cause context to write verbose /tmp/mitogen.<pid>.log.
    debug = False

    #: True to cause context to write /tmp/mitogen.stats.<pid>.<thread>.log.
    profiling = False

    def construct(self, remote_name=None, python_path=None, debug=False,
                  profiling=False, **kwargs):
        """Get the named context running on the local machine, creating it if
        it does not exist."""
        super(Stream, self).construct(**kwargs)
        if python_path:
            self.python_path = python_path

        if remote_name is None:
            remote_name = '%s@%s:%d'
            remote_name %= (getpass.getuser(), socket.gethostname(), os.getpid())
        self.remote_name = remote_name
        self.debug = debug
        self.profiling = profiling

    def on_shutdown(self, broker):
        """Request the slave gracefully shut itself down."""
        LOG.debug('%r closing CALL_FUNCTION channel', self)
        self.send(
            mitogen.core.Message.pickled(
                mitogen.core._DEAD,
                src_id=mitogen.context_id,
                dst_id=self.remote_id,
                handle=mitogen.core.CALL_FUNCTION
            )
        )

    # base64'd and passed to 'python -c'. It forks, dups 0->100, creates a
    # pipe, then execs a new interpreter with a custom argv. 'CONTEXT_NAME' is
    # replaced with the context name. Optimized for size.
    @staticmethod
    def _first_stage():
        import os,sys,zlib
        R,W=os.pipe()
        R2,W2=os.pipe()
        if os.fork():
            os.dup2(0,100)
            os.dup2(R,0)
            os.dup2(R2,101)
            for f in R,R2,W,W2: os.close(f)
            os.environ['ARGV0'] = sys.executable
            os.execv(sys.executable,['mitogen:CONTEXT_NAME'])
        else:
            os.write(1, 'EC0\n')
            C = zlib.decompress(sys.stdin.read(input()))
            os.fdopen(W,'w',0).write(C)
            os.fdopen(W2,'w',0).write('%s\n%s' % (len(C),C))
            os.write(1, 'EC1\n')
            sys.exit(0)

    def get_boot_command(self):
        source = inspect.getsource(self._first_stage)
        source = textwrap.dedent('\n'.join(source.strip().split('\n')[2:]))
        source = source.replace('    ', '\t')
        source = source.replace('CONTEXT_NAME', self.remote_name)
        encoded = source.encode('base64').replace('\n', '')
        return [self.python_path, '-c',
                'exec("%s".decode("base64"))' % (encoded,)]

    def get_preamble(self):
        parent_ids = mitogen.parent_ids[:]
        parent_ids.insert(0, mitogen.context_id)
        source = inspect.getsource(mitogen.core)
        source += '\nExternalContext().main%r\n' % ((
            parent_ids,                # parent_ids
            self.remote_id,            # context_id
            self.debug,
            self.profiling,
            LOG.level or logging.getLogger().level or logging.INFO,
        ),)

        compressed = zlib.compress(minimize_source(source))
        return str(len(compressed)) + '\n' + compressed

    create_child = staticmethod(create_child)

    def connect(self):
        LOG.debug('%r.connect()', self)
        pid, fd = self.create_child(*self.get_boot_command())
        self.name = 'local.%s' % (pid,)
        self.receive_side = mitogen.core.Side(self, fd)
        self.transmit_side = mitogen.core.Side(self, os.dup(fd))
        LOG.debug('%r.connect(): child process stdin/stdout=%r',
                  self, self.receive_side.fd)

        self._connect_bootstrap()

    def _ec0_received(self):
        LOG.debug('%r._ec0_received()', self)
        write_all(self.transmit_side.fd, self.get_preamble())
        discard_until(self.receive_side.fd, 'EC1\n', time.time() + 10.0)

    def _connect_bootstrap(self):
        discard_until(self.receive_side.fd, 'EC0\n', time.time() + 10.0)
        self._ec0_received()


class Broker(mitogen.core.Broker):
    shutdown_timeout = 5.0


class Context(mitogen.core.Context):
    via = None

    def on_disconnect(self, broker):
        """
        Override base behaviour of triggering Broker shutdown on parent stream
        disconnection.
        """
        mitogen.core.fire(self, 'disconnect')

    def call_async(self, fn, *args, **kwargs):
        LOG.debug('%r.call_async(%r, *%r, **%r)',
                  self, fn, args, kwargs)

        if isinstance(fn, types.MethodType) and \
           isinstance(fn.im_self, (type, types.ClassType)):
            klass = fn.im_self.__name__
        else:
            klass = None

        return self.send_async(
            mitogen.core.Message.pickled(
                (fn.__module__, klass, fn.__name__, args, kwargs),
                handle=mitogen.core.CALL_FUNCTION,
            )
        )

    def call(self, fn, *args, **kwargs):
        """Invoke `fn(*args, **kwargs)` in the external context."""
        return self.call_async(fn, *args, **kwargs).get_data()


def _local_method():
    return Stream

def _ssh_method():
    import mitogen.ssh
    return mitogen.ssh.Stream

def _sudo_method():
    import mitogen.sudo
    return mitogen.sudo.Stream


METHOD_NAMES = {
    'local': _local_method,
    'ssh': _ssh_method,
    'sudo': _sudo_method,
}


def upgrade_router(econtext):
    if not isinstance(econtext.router, Router):  # TODO
        econtext.router.__class__ = Router  # TODO
        econtext.router.id_allocator = ChildIdAllocator(econtext.router)
        LOG.debug('_proxy_connect(): constructing ModuleForwarder')
        ModuleForwarder(econtext.router, econtext.parent, econtext.importer)


@mitogen.core.takes_econtext
def _proxy_connect(name, context_id, method_name, kwargs, econtext):
    upgrade_router(econtext)
    context = econtext.router._connect(
        context_id,
        METHOD_NAMES[method_name](),
        name=name,
        **kwargs
    )
    return context.name


class IdAllocator(object):
    def __init__(self, router):
        self.router = router
        self.next_id = 1
        self.lock = threading.Lock()
        router.add_handler(self.on_allocate_id, mitogen.core.ALLOCATE_ID)

    def __repr__(self):
        return 'IdAllocator(%r)' % (self.router,)

    def allocate(self):
        self.lock.acquire()
        try:
            id_ = self.next_id
            self.next_id += 1
            return id_
        finally:
            self.lock.release()

    def on_allocate_id(self, msg):
        id_ = self.allocate()
        requestee = self.router.context_by_id(msg.src_id)
        allocated = self.router.context_by_id(id_, msg.src_id)

        LOG.debug('%r: allocating %r to %r', self, allocated, requestee)
        self.router.route(
            mitogen.core.Message.pickled(
                id_,
                dst_id=msg.src_id,
                handle=msg.reply_to,
            )
        )

        LOG.debug('%r: publishing route to %r via %r', self,
                  allocated, requestee)
        self.router.propagate_route(allocated, requestee)


class ChildIdAllocator(object):
    def __init__(self, router):
        self.router = router

    def allocate(self):
        master = Context(self.router, 0)
        return master.send_await(
            mitogen.core.Message(dst_id=0, handle=mitogen.core.ALLOCATE_ID)
        )


class Router(mitogen.core.Router):
    debug = False

    #: When enabled, causes the broker thread and any subsequent broker and
    #: main threads existing in any child to write
    #: ``/tmp/mitogen.stats.<pid>.<thread_name>.log`` containing a
    #: :py:mod:`cProfile` dump on graceful exit. Must be set prior to any
    #: :py:class:`Broker` being constructed, e.g. via:
    #:
    #: .. code::
    #:
    #:      mitogen.master.Router.profiling = True
    #:
    profiling = False

    def __init__(self, *args, **kwargs):
        super(Router, self).__init__(*args, **kwargs)
        self.id_allocator = IdAllocator(self)
        self.responder = ModuleResponder(self)
        self.log_forwarder = LogForwarder(self)

    def enable_debug(self):
        """
        Cause this context and any descendant child contexts to write debug
        logs to /tmp/mitogen.<pid>.log.
        """
        mitogen.core.enable_debug_logging()
        self.debug = True

    def __enter__(self):
        return self

    def __exit__(self, e_type, e_val, tb):
        self.broker.shutdown()
        self.broker.join()

    def allocate_id(self):
        return self.id_allocator.allocate()

    def context_by_id(self, context_id, via_id=None):
        context = self._context_by_id.get(context_id)
        if context is None:
            context = Context(self, context_id)
            if via_id is not None:
                context.via = self.context_by_id(via_id)
            self._context_by_id[context_id] = context
        return context

    def local(self, **kwargs):
        return self.connect('local', **kwargs)

    def sudo(self, **kwargs):
        return self.connect('sudo', **kwargs)

    def ssh(self, **kwargs):
        return self.connect('ssh', **kwargs)

    def _connect(self, context_id, klass, name=None, **kwargs):
        context = Context(self, context_id)
        stream = klass(self, context.context_id, **kwargs)
        if name is not None:
            stream.name = name
        stream.connect()
        context.name = stream.name
        self.register(context, stream)
        return context

    def connect(self, method_name, name=None, **kwargs):
        klass = METHOD_NAMES[method_name]()
        kwargs.setdefault('debug', self.debug)
        kwargs.setdefault('profiling', self.profiling)

        via = kwargs.pop('via', None)
        if via is not None:
            return self.proxy_connect(via, method_name, name=name, **kwargs)
        context_id = self.allocate_id()
        return self._connect(context_id, klass, name=name, **kwargs)

    def propagate_route(self, target, via):
        self.add_route(target.context_id, via.context_id)
        child = via
        parent = via.via

        while parent is not None:
            LOG.debug('Adding route to %r for %r via %r', parent, target, child)
            parent.send(
                mitogen.core.Message(
                    data='%s\x00%s' % (target.context_id, child.context_id),
                    handle=mitogen.core.ADD_ROUTE,
                )
            )
            child = parent
            parent = parent.via

    def proxy_connect(self, via_context, method_name, name=None, **kwargs):
        context_id = self.allocate_id()
        # Must be added prior to _proxy_connect() to avoid a race.
        self.add_route(context_id, via_context.context_id)
        name = via_context.call(_proxy_connect,
            name, context_id, method_name, kwargs
        )

        # name = '%s.%s' % (via_context.name, name)
        context = Context(self, context_id, name=name)
        context.via = via_context
        self._context_by_id[context.context_id] = context

        self.propagate_route(context, via_context)
        return context


class ProcessMonitor(object):
    def __init__(self):
        # pid -> callback()
        self.callback_by_pid = {}
        signal.signal(signal.SIGCHLD, self._on_sigchld)

    def _on_sigchld(self, _signum, _frame):
        for pid, callback in self.callback_by_pid.items():
            pid, status = os.waitpid(pid, os.WNOHANG)
            if pid:
                callback(status)
                del self.callback_by_pid[pid]

    def add(self, pid, callback):
        self.callback_by_pid[pid] = callback

    _instance = None

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
