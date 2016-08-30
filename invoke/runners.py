# -*- coding: utf-8 -*-

import locale
import os
import re
from signal import SIGINT, SIGTERM
import struct
from subprocess import Popen, PIPE
import sys
import threading
import time

# Import some platform-specific things at top level so they can be mocked for
# tests.
try:
    import pty
except ImportError:
    pty = None
try:
    import fcntl
except ImportError:
    fcntl = None
try:
    import termios
except ImportError:
    termios = None

from .exceptions import Failure, ThreadException
from .platform import (
    WINDOWS, pty_size, character_buffered, ready_for_reading, read_byte,
)
from .util import has_fileno, isatty, ExceptionHandlingThread

from .vendor import six


class Runner(object):
    """
    Partially-abstract core command-running API.

    This class is not usable by itself and must be subclassed, implementing a
    number of methods such as `start`, `wait` and `returncode`. For a subclass
    implementation example, see the source code for `.Local`.
    """
    read_chunk_size = 1000
    input_sleep = 0.01

    def __init__(self, context):
        """
        Create a new runner with a handle on some `.Context`.

        :param context:
            a `.Context` instance, used to transmit default options and provide
            access to other contextualized information (e.g. a remote-oriented
            `.Runner` might want a `.Context` subclass holding info about
            hostnames and ports.)

            .. note::
                The `.Context` given to `.Runner` instances **must** contain
                default config values for the `.Runner` class in question. At a
                minimum, this means values for each of the default
                `.Runner.run` keyword arguments such as ``echo`` and ``warn``.

        :raises exceptions.ValueError:
            if not all expected default values are found in ``context``.
        """
        #: The `.Context` given to the same-named argument of `__init__`.
        self.context = context
        #: A `threading.Event` signaling program completion.
        #:
        #: Typically set after `wait` returns. Some IO mechanisms rely on this
        #: to know when to exit an infinite read loop.
        self.program_finished = threading.Event()
        # I wish Sphinx would organize all class/instance attrs in the same
        # place. If I don't do this here, it goes 'class vars -> __init__
        # docstring -> instance vars' :( TODO: consider just merging class and
        # __init__ docstrings, though that's annoying too.
        #: How many bytes (at maximum) to read per iteration of stream reads.
        self.read_chunk_size = self.__class__.read_chunk_size
        # Ditto re: declaring this in 2 places for doc reasons.
        #: How many seconds to sleep on each iteration of the stdin read loop
        #: and other otherwise-fast loops.
        self.input_sleep = self.__class__.input_sleep
        #: Whether pty fallback warning has been emitted.
        self.warned_about_pty_fallback = False
        #: A list of `StreamWatcher` instances for use by `respond`. Is filled
        #: in at runtime by `run`.
        self.watchers = []

    def run(self, command, **kwargs):
        """
        Execute ``command``, returning an instance of `Result`.

        .. note::
            All kwargs will default to the values found in this instance's
            `~.Runner.context` attribute, specifically in its configuration's
            ``run`` subtree (e.g. ``run.echo`` provides the default value for
            the ``echo`` keyword, etc). The base default values are described
            in the parameter list below.

        :param str command: The shell command to execute.

        :param str shell: Which shell binary to use. Default: ``/bin/bash``.

        :param bool warn:
            Whether to warn and continue, instead of raising `.Failure`, when
            the executed command exits with a nonzero status. Default:
            ``False``.

        :param hide:
            Allows the caller to disable ``run``'s default behavior of copying
            the subprocess' stdout and stderr to the controlling terminal.
            Specify ``hide='out'`` (or ``'stdout'``) to hide only the stdout
            stream, ``hide='err'`` (or ``'stderr'``) to hide only stderr, or
            ``hide='both'`` (or ``True``) to hide both streams.

            The default value is ``None``, meaning to print everything;
            ``False`` will also disable hiding.

            .. note::
                Stdout and stderr are always captured and stored in the
                ``Result`` object, regardless of ``hide``'s value.

            .. note::
                ``hide=True`` will also override ``echo=True`` if both are
                given (either as kwargs or via config/CLI).

        :param bool pty:
            By default, ``run`` connects directly to the invoked process and
            reads its stdout/stderr streams. Some programs will buffer (or even
            behave) differently in this situation compared to using an actual
            terminal or pseudoterminal (pty). To use a pty instead of the
            default behavior, specify ``pty=True``.

            .. warning::
                Due to their nature, ptys have a single output stream, so the
                ability to tell stdout apart from stderr is **not possible**
                when ``pty=True``. As such, all output will appear on
                ``out_stream`` (see below) and be captured into the ``stdout``
                result attribute. ``err_stream`` and ``stderr`` will always be
                empty when ``pty=True``.

        :param bool fallback:
            Controls auto-fallback behavior re: problems offering a pty when
            ``pty=True``. Whether this has any effect depends on the specific
            `Runner` subclass being invoked. Default: ``True``.

        :param bool echo:
            Controls whether `.run` prints the command string to local stdout
            prior to executing it. Default: ``False``.

            .. note::
                ``hide=True`` will override ``echo=True`` if both are given.

        :param dict env:
            By default, subprocesses recieve a copy of Invoke's own environment
            (i.e. ``os.environ``). Supply a dict here to update that child
            environment.

            For example, ``run('command', env={'PYTHONPATH':
            '/some/virtual/env/maybe'})`` would modify the ``PYTHONPATH`` env
            var, with the rest of the child's env looking identical to the
            parent.

            .. seealso:: ``replace_env`` for changing 'update' to 'replace'.

        :param bool replace_env:
            When ``True``, causes the subprocess to receive the dictionary
            given to ``env`` as its entire shell environment, instead of
            updating a copy of ``os.environ`` (which is the default behavior).
            Default: ``False``.

        :param str encoding:
            Override auto-detection of which encoding the subprocess is using
            for its stdout/stderr streams (which defaults to the return value
            of `default_encoding`).

        :param out_stream:
            A file-like stream object to which the subprocess' standard error
            should be written. If ``None`` (the default), ``sys.stdout`` will
            be used.

        :param err_stream:
            Same as ``out_stream``, except for standard error, and defaulting
            to ``sys.stderr``.

        :param in_stream:
            A file-like stream object to used as the subprocess' standard
            input. If ``None`` (the default), ``sys.stdin`` will be used.

        :param list watchers:
            A list of `StreamWatcher` instances which will be used to scan the
            program's ``stdout`` or ``stderr`` and may write into its ``stdin``
            (typically ``str`` or ``bytes`` objects depending on Python
            version) in response to patterns or other heuristics.

            See :doc:`/concepts/watchers` for details on this functionality.

            Default: ``[]``.

        :param bool echo_stdin:
            Whether to write data from ``in_stream`` back to ``out_stream``.

            In other words, in normal interactive usage, this parameter
            controls whether Invoke mirrors what you type back to your
            terminal.

            By default (when ``None``), this behavior is triggered by the
            following:

                * Not using a pty to run the subcommand (i.e. ``pty=False``),
                  as ptys natively echo stdin to stdout on their own;
                * And when the controlling terminal of Invoke itself (as per
                  ``in_stream``) appears to be a valid terminal device or TTY.
                  (Specifically, when `~invoke.util.isatty` yields a ``True``
                  result when given ``in_stream``.)

                  .. note::
                      This property tends to be ``False`` when piping another
                      program's output into an Invoke session, or when running
                      Invoke within another program (e.g. running Invoke from
                      itself).

            If both of those properties are true, echoing will occur; if either
            is false, no echoing will be performed.

            When not ``None``, this parameter will override that auto-detection
            and force, or disable, echoing.

        :returns:
            `Result`, or a subclass thereof.

        :raises: `.Failure`, if the command exited nonzero & ``warn=False``.

        :raises:
            `.ThreadException` (if the background I/O threads encounter
            exceptions).

        :raises:
            ``KeyboardInterrupt``, if the user generates one during command
            execution by pressing Ctrl-C.

            .. note::
                In normal usage, Invoke's top-level CLI tooling will catch
                these & exit with return code ``130`` (typical POSIX behavior)
                instead of printing a traceback and exiting ``1`` (which is
                what Python normally does).
        """
        try:
            return self._run_body(command, **kwargs)
        finally:
            self.stop()

    def _run_body(self, command, **kwargs):
        # Normalize kwargs w/ config
        opts, out_stream, err_stream, in_stream = self._run_opts(kwargs)
        shell = opts['shell']
        # Environment setup
        env = self.generate_env(opts['env'], opts['replace_env'])
        # Echo running command
        if opts['echo']:
            print("\033[1;37m{0}\033[0m".format(command))
        # Start executing the actual command (runs in background)
        self.start(command, shell, env)
        # Arrive at final encoding if neither config nor kwargs had one
        self.encoding = opts['encoding'] or self.default_encoding()
        # Set up IO thread parameters (format - body_func: {kwargs})
        stdout, stderr = [], []
        thread_args = {
            self.handle_stdout: {
                'buffer_': stdout,
                'hide': 'out' in opts['hide'],
                'output': out_stream,
            },
            # TODO: make this & related functionality optional, for users who
            # don't care about autoresponding & are encountering issues with
            # the stdin mirroring? Downside is it fragments expected behavior &
            # puts folks with true interactive use cases in a different support
            # class.
            self.handle_stdin: {
                'input_': in_stream,
                'output': out_stream,
                'echo': opts['echo_stdin'],
            }
        }
        if not self.using_pty:
            thread_args[self.handle_stderr] = {
                'buffer_': stderr,
                'hide': 'err' in opts['hide'],
                'output': err_stream,
            }
        # Kick off IO threads
        self.threads, exceptions = [], []
        for target, kwargs in six.iteritems(thread_args):
            t = ExceptionHandlingThread(target=target, kwargs=kwargs)
            self.threads.append(t)
            t.start()
        # Wait for completion, then tie things off & obtain result
        # And make sure we perform that tying off even if things asplode.
        exception = None
        try:
            self.wait()
        except BaseException as e: # Make sure we nab ^C etc
            exception = e
            # TODO: consider consuming the KeyboardInterrupt instead of storing
            # it for later raise; this would allow for subprocesses which don't
            # actually exit on Ctrl-C (e.g. vim). NOTE: but this would make it
            # harder to correctly detect it and exit 130 once everything wraps
            # up...
            # TODO: generally, but especially if we do ignore
            # KeyboardInterrupt, honor other signals sent to our own process
            # and transmit them to the subprocess before handling 'normally'.
            # TODO: we should probably re-raise anything that's not
            # KeyboardInterrupt? This is quite possibly swallowing things.
            # NOTE: we handle this now instead of at actual-exception-handling
            # time because otherwise the stdout/err reader threads may block
            # until the subprocess exits.
            if isinstance(exception, KeyboardInterrupt):
                self.send_interrupt(exception)
        self.program_finished.set()
        for t in self.threads:
            # NOTE: using a join timeout for corner case from #351 (one pipe
            # excepts, fills up, prevents subproc from exiting, and other pipe
            # then has a blocking read() call, causing its thread to block on
            # join). In normal, non-#351 situations this should function
            # similarly to a non-timeout'd join.
            # NOTE: but we avoid a timeout for the stdin handler as it has its
            # own termination conditions & isn't subject to this corner case.
            timeout = None
            if t.kwargs['target'] != self.handle_stdin:
                # TODO: make the timeout configurable
                timeout = 1
            t.join(timeout)
            e = t.exception()
            if e is not None:
                exceptions.append(e)
        # If we got a main-thread exception while wait()ing, raise it now that
        # we've closed our worker threads.
        if exception is not None:
            raise exception
        # If any exceptions appeared inside the threads, raise them now as an
        # aggregate exception object.
        if exceptions:
            raise ThreadException(exceptions)
        stdout = ''.join(stdout)
        stderr = ''.join(stderr)
        if WINDOWS:
            # "Universal newlines" - replace all standard forms of
            # newline with \n. This is not technically Windows related
            # (\r as newline is an old Mac convention) but we only apply
            # the translation for Windows as that's the only platform
            # it is likely to matter for these days.
            stdout = stdout.replace("\r\n", "\n").replace("\r", "\n")
            stderr = stderr.replace("\r\n", "\n").replace("\r", "\n")
        # Get return/exit code
        exited = self.returncode()
        # Return, or raise as failure, our final result
        result = self.generate_result(
            command=command,
            shell=shell,
            env=env,
            stdout=stdout,
            stderr=stderr,
            exited=exited,
            pty=self.using_pty,
        )
        if not (result or opts['warn']):
            raise Failure(result)
        return result

    def _run_opts(self, kwargs):
        """
        Unify `run` kwargs with config options to arrive at local options.

        :returns:
            Four-tuple of ``(opts_dict, stdout_stream, stderr_stream,
            stdin_stream)``.
        """
        opts = {}
        for key, value in six.iteritems(self.context.config.run):
            runtime = kwargs.pop(key, None)
            opts[key] = value if runtime is None else runtime
        # TODO: handle invalid kwarg keys (anything left in kwargs)
        # If hide was True, turn off echoing
        if opts['hide'] is True:
            opts['echo'] = False
        # Then normalize 'hide' from one of the various valid input values,
        # into a stream-names tuple.
        opts['hide'] = normalize_hide(opts['hide'])
        # Derive stream objects
        out_stream = opts['out_stream']
        if out_stream is None:
            out_stream = sys.stdout
        err_stream = opts['err_stream']
        if err_stream is None:
            err_stream = sys.stderr
        in_stream = opts['in_stream']
        if in_stream is None:
            in_stream = sys.stdin
        # Determine pty or no
        self.using_pty = self.should_use_pty(opts['pty'], opts['fallback'])
        if opts['watchers']:
            self.watchers = opts['watchers']
        return opts, out_stream, err_stream, in_stream

    def generate_result(self, **kwargs):
        """
        Create & return a suitable `Result` instance from the given ``kwargs``.

        Subclasses may wish to override this in order to manipulate things or
        generate a `Result` subclass (e.g. ones containing additional metadata
        besides the default).
        """
        return Result(**kwargs)

    def read_proc_output(self, reader):
        """
        Iteratively read & decode bytes from a subprocess' out/err stream.

        :param reader:
            A literal reader function/partial, wrapping the actual stream
            object in question, which takes a number of bytes to read, and
            returns that many bytes (or ``None``).

            ``reader`` should be a reference to either `read_proc_stdout` or
            `read_proc_stderr`, which perform the actual, platform/library
            specific read calls.

        :returns:
            A generator yielding Unicode strings (`unicode` on Python 2; `str`
            on Python 3).

            Specifically, each resulting string is the result of decoding
            `read_chunk_size` bytes read from the subprocess' out/err stream.
        """
        # NOTE: Typically, reading from any stdout/err (local, remote or
        # otherwise) can be thought of as "read until you get nothing back".
        # This is preferable over "wait until an out-of-band signal claims the
        # process is done running" because sometimes that signal will appear
        # before we've actually read all the data in the stream (i.e.: a race
        # condition).
        while True:
            data = reader(self.read_chunk_size)
            if not data:
                break
            yield self.decode(data)

    def write_our_output(self, stream, string):
        """
        Write ``string`` to ``stream``.

        Also calls ``.flush()`` on ``stream`` to ensure that real terminal
        streams don't buffer.

        :param stream:
            A file-like stream object, mapping to the ``out_stream`` or
            ``err_stream`` parameters of `run`.

        :param string: A Unicode string object.

        :returns: ``None``.
        """
        # Encode under Python 2 only, because of the common problem where
        # sys.stdout/err on Python 2 end up using sys.getdefaultencoding(),
        # which is frequently NOT the same thing as the real local terminal
        # encoding (reflected as sys.stdout.encoding). I.e. even when
        # sys.stdout.encoding is UTF-8, ascii is still actually used, and
        # explodes.
        # Python 3 doesn't have this problem, so we delegate encoding to the
        # io.*Writer classes involved.
        if six.PY2:
            # TODO: split up self.encoding, only use the one for 'local
            # encoding' here.
            string = string.encode(self.encoding)
        stream.write(string)
        stream.flush()

    def _handle_output(self, buffer_, hide, output, reader, indices):
        # TODO: store un-decoded/raw bytes somewhere as well...
        for data in self.read_proc_output(reader):
            # Echo to local stdout if necessary
            # TODO: should we rephrase this as "if you want to hide, give me a
            # dummy output stream, e.g. something like /dev/null"? Otherwise, a
            # combo of 'hide=stdout' + 'here is an explicit out_stream' means
            # out_stream is never written to, and that seems...odd.
            if not hide:
                self.write_our_output(stream=output, string=data)
            # Store in shared buffer so main thread can do things with the
            # result after execution completes.
            # NOTE: this is threadsafe insofar as no reading occurs until after
            # the thread is join()'d.
            buffer_.append(data)
            # Run our specific buffer & indices through the autoresponder
            self.respond(buffer_, indices)

    def handle_stdout(self, buffer_, hide, output):
        """
        Read process' stdout, storing into a buffer & printing/parsing.

        Intended for use as a thread target. Only terminates when all stdout
        from the subprocess has been read.

        :param list buffer_: The capture buffer shared with the main thread.
        :param bool hide: Whether or not to replay data into ``output``.
        :param output:
            Output stream (file-like object) to write data into when not
            hiding.

        :returns: ``None``.
        """
        self._handle_output(
            buffer_,
            hide,
            output,
            reader=self.read_proc_stdout,
            indices=threading.local(),
        )

    def handle_stderr(self, buffer_, hide, output):
        """
        Read process' stderr, storing into a buffer & printing/parsing.

        Identical to `handle_stdout` except for the stream read from; see its
        docstring for API details.
        """
        self._handle_output(
            buffer_,
            hide,
            output,
            reader=self.read_proc_stderr,
            indices=threading.local(),
        )

    def read_our_stdin(self, input_):
        """
        Read & decode one byte from a local stdin stream.

        :param input_:
            Actual stream object to read from. Maps to ``in_stream`` in `run`,
            so will often be ``sys.stdin``, but might be any stream-like
            object.

        :returns:
            A Unicode string, the result of decoding the read byte (this might
            be the empty string if the pipe has closed/reached EOF); or
            ``None`` if stdin wasn't ready for reading yet.
        """
        # TODO: consider moving the character_buffered contextmanager call in
        # here? Downside is it would be flipping those switches for every byte
        # read instead of once per session, which could be costly (?).
        byte = None
        if ready_for_reading(input_):
            byte = read_byte(input_)
            # Decode if it appears to be binary-type. (From real terminal
            # streams, usually yes; from file-like objects, often no.)
            if byte and isinstance(byte, six.binary_type):
                # TODO: will decoding 1 byte at a time break multibyte
                # character encodings? How to square interactivity with that?
                byte = self.decode(byte)
        return byte

    def handle_stdin(self, input_, output, echo):
        """
        Read local stdin, copying into process' stdin as necessary.

        Intended for use as a thread target.

        .. note::
            Because real terminal stdin streams have no well-defined "end", if
            such a stream is detected (based on existence of a callable
            ``.fileno()``) this method will wait until `program_finished` is
            set, before terminating.

            When the stream doesn't appear to be from a terminal, the same
            semantics as `handle_stdout` are used - the stream is simply
            ``read()`` from until it returns an empty value.

        :param input_: Stream (file-like object) from which to read.
        :param output: Stream (file-like object) to which echoing may occur.
        :param bool echo: User override option for stdin-stdout echoing.

        :returns: ``None``.
        """
        with character_buffered(input_):
            while True:
                # Read 1 byte at a time for interactivity's sake.
                char = self.read_our_stdin(input_)
                if char:
                    # Mirror what we just read to process' stdin.
                    # We perform an encode so Python 3 gets bytes (streams +
                    # str's in Python 3 == no bueno) but skip the decode step,
                    # since there's presumably no need (nobody's interacting
                    # with this data programmatically).
                    self.write_proc_stdin(char)
                    # Also echo it back to local stdout (or whatever
                    # out_stream is set to) when necessary.
                    if echo is None:
                        echo = self.should_echo_stdin(input_, output)
                    if echo:
                        self.write_our_output(stream=output, string=char)
                # Empty string/char/byte != None. Can't just use 'else' here.
                elif char is not None:
                    # When reading from file-like objects that aren't "real"
                    # terminal streams, an empty byte signals EOF.
                    break
                # Dual all-done signals: program being executed is done
                # running, *and* we don't seem to be reading anything out of
                # stdin. (NOTE: If we only test the former, we may encounter
                # race conditions re: unread stdin.)
                if self.program_finished.is_set() and not char:
                    break
                # Take a nap so we're not chewing CPU.
                time.sleep(self.input_sleep)

        # while not self.program_finished.is_set():
        #    # TODO: reinstate lock/whatever thread logic from fab v1 which
        #    # prevents reading from stdin while other parts of the code are
        #    # prompting for runtime passwords? (search for 'input_enabled')
        #    if have_char and chan.input_enabled:
        #        # Send all local stdin to remote end's stdin
        #        #byte = msvcrt.getch() if WINDOWS else sys.stdin.read(1)
        #        yield self.encode(sys.stdin.read(1))
        #        # Optionally echo locally, if needed.
        #        # TODO: how to truly do this? access the out_stream which
        #        # isn't currently visible to us? if we just skip this part,
        #        # interactive users may not have their input echoed...ISTR we
        #        # used to assume remote would send it back down stdout/err...
        #        # clearly not?
        #        #if not using_pty and env.echo_stdin:
        #            # Not using fastprint() here -- it prints as 'user'
        #            # output level, don't want it to be accidentally hidden
        #        #    sys.stdout.write(byte)
        #        #    sys.stdout.flush()

    def should_echo_stdin(self, input_, output):
        """
        Determine whether data read from ``input_`` should echo to ``output``.

        Used by `handle_stdin`; tests attributes of ``input_`` and ``output``.

        :param input_: Input stream (file-like object).
        :param output: Output stream (file-like object).
        :returns: A ``bool``.
        """
        return (not self.using_pty) and isatty(input_)

    def respond(self, buffer_, indices):
        """
        Write to the program's stdin in response to patterns in ``buffer_``.

        The patterns and responses are driven by the key/value pairs in the
        ``responses`` kwarg of `run` - see its documentation for format
        details, and :doc:`/concepts/responses` for a conceptual overview.

        :param list buffer:
            The capture buffer for this thread's particular IO stream.

        :param indices:
            A `threading.local` object upon which is (or will be) stored the
            last-seen index for each key in ``responses``. Allows the responder
            functionality to be used by multiple threads (typically, one each
            for stdout and stderr) without conflicting.

        :returns: ``None``.
        """
        # Join buffer contents into a single string; without this,
        # StreamWatcher subclasses can't do things like iteratively scan for
        # pattern matches.
        # NOTE: using string.join should be "efficient enough" for now, re:
        # speed and memory use. Should that become false, consider using
        # StringIO or cStringIO (tho the latter doesn't do Unicode well?) which
        # is apparently even more efficient.
        stream = u''.join(buffer_)
        for watcher in self.watchers:
            for response in watcher.submit(stream):
                self.write_proc_stdin(response)

    def generate_env(self, env, replace_env):
        """
        Return a suitable environment dict based on user input & behavior.

        :param dict env: Dict supplying overrides or full env, depending.
        :param bool replace_env:
            Whether ``env`` updates, or is used in place of, the value of
            `os.environ`.

        :returns: A dictionary of shell environment vars.
        """
        return env if replace_env else dict(os.environ, **env)

    def should_use_pty(self, pty, fallback):
        """
        Should execution attempt to use a pseudo-terminal?

        :param bool pty:
            Whether the user explicitly asked for a pty.
        :param bool fallback:
            Whether falling back to non-pty execution should be allowed, in
            situations where ``pty=True`` but a pty could not be allocated.
        """
        # NOTE: fallback not used: no falling back implemented by default.
        return pty

    @property
    def has_dead_threads(self):
        """
        Detect whether any IO threads appear to have terminated unexpectedly.

        Used during process-completion waiting (in `wait`) to ensure we don't
        deadlock our child process if our IO processing threads have
        errored/died.

        :returns:
            ``True`` if any threads appear to have terminated with an
            exception, ``False`` otherwise.
        """
        return any(x.is_dead for x in self.threads)

    def wait(self):
        """
        Block until the running command appears to have exited.

        :returns: ``None``.
        """
        while True:
            proc_finished = self.process_is_finished
            dead_threads = self.has_dead_threads
            if proc_finished or dead_threads:
                break
            time.sleep(self.input_sleep)

    def write_proc_stdin(self, data):
        """
        Write encoded ``data`` to the running process' stdin.

        :param data: A Unicode string.

        :returns: ``None``.
        """
        # Encode always, then request implementing subclass to perform the
        # actual write to subprocess' stdin.
        self._write_proc_stdin(data.encode(self.encoding))

    def decode(self, data):
        """
        Decode some ``data`` bytes, returning Unicode.
        """
        # NOTE: yes, this is a 1-liner. The point is to make it much harder to
        # forget to use 'replace' when decoding :)
        return data.decode(self.encoding, 'replace')

    @property
    def process_is_finished(self):
        """
        Determine whether our subprocess has terminated.

        .. note::
            The implementation of this method should be nonblocking, as it is
            used within a query/poll loop.

        :returns:
            ``True`` if the subprocess has finished running, ``False``
            otherwise.
        """
        raise NotImplementedError

    def start(self, command, shell, env):
        """
        Initiate execution of ``command`` (via ``shell``, with ``env``).

        Typically this means use of a forked subprocess or requesting start of
        execution on a remote system.

        In most cases, this method will also set subclass-specific member
        variables used in other methods such as `wait` and/or `returncode`.
        """
        raise NotImplementedError

    def read_proc_stdout(self, num_bytes):
        """
        Read ``num_bytes`` from the running process' stdout stream.

        :param int num_bytes: Number of bytes to read at maximum.

        :returns: A string/bytes object.
        """
        raise NotImplementedError

    def read_proc_stderr(self, num_bytes):
        """
        Read ``num_bytes`` from the running process' stderr stream.

        :param int num_bytes: Number of bytes to read at maximum.

        :returns: A string/bytes object.
        """
        raise NotImplementedError

    def _write_proc_stdin(self, data):
        """
        Write ``data`` to running process' stdin.

        This should never be called directly; it's for subclasses to implement.
        See `write_proc_stdin` for the public API call.

        :param data: Already-encoded byte data suitable for writing.

        :returns: ``None``.
        """
        raise NotImplementedError

    def default_encoding(self):
        """
        Return a string naming the expected encoding of subprocess streams.

        This return value should be suitable for use by encode/decode methods.
        """
        # TODO: probably wants to be 2 methods, one for local and one for
        # subprocess. For now, good enough to assume both are the same.
        #
        # Based on some experiments there is an issue with
        # `locale.getpreferredencoding(do_setlocale=False)` in Python 2.x on
        # Linux and OS X, and `locale.getpreferredencoding(do_setlocale=True)`
        # triggers some global state changes. (See #274 for discussion.)
        encoding = locale.getpreferredencoding(False)
        if six.PY2 and not WINDOWS:
            default = locale.getdefaultlocale()[1]
            if default is not None:
                encoding = default
        return encoding

    def send_interrupt(self, interrupt):
        """
        Submit an interrupt signal to the running subprocess.

        :param interrupt:
            The locally-sourced ``KeyboardInterrupt`` causing the method call.

        :returns: ``None``.
        """
        raise NotImplementedError

    def returncode(self):
        """
        Return the numeric return/exit code resulting from command execution.

        :returns: `int`
        """
        raise NotImplementedError

    def stop(self):
        """
        Perform final cleanup, if necessary.

        This method is called within a ``finally`` clause inside the main `run`
        method. Depending on the subclass, it may be a no-op, or it may do
        things such as close network connections or open files.

        :returns: ``None``
        """
        raise NotImplementedError


class Local(Runner):
    """
    Execute a command on the local system in a subprocess.

    .. note::
        When Invoke itself is executed without a controlling terminal (e.g.
        when ``sys.stdin`` lacks a useful ``fileno``), it's not possible to
        present a handle on our PTY to local subprocesses. In such situations,
        `Local` will fallback to behaving as if ``pty=False`` (on the theory
        that degraded execution is better than none at all) as well as printing
        a warning to stderr.

        To disable this behavior, say ``fallback=False``.
    """
    def __init__(self, context):
        super(Local, self).__init__(context)
        # Bookkeeping var for pty use case
        self.status = None

    def should_use_pty(self, pty=False, fallback=True):
        use_pty = False
        if pty:
            use_pty = True
            # TODO: pass in & test in_stream, not sys.stdin
            if not has_fileno(sys.stdin) and fallback:
                if not self.warned_about_pty_fallback:
                    sys.stderr.write("WARNING: stdin has no fileno; falling back to non-pty execution!\n") # noqa
                    self.warned_about_pty_fallback = True
                use_pty = False
        return use_pty

    def read_proc_stdout(self, num_bytes):
        # Obtain useful read-some-bytes function
        if self.using_pty:
            # Need to handle spurious OSErrors on some Linux platforms.
            try:
                data = os.read(self.parent_fd, num_bytes)
            except OSError as e:
                # Only eat this specific OSError so we don't hide others
                if "Input/output error" not in str(e):
                    raise
                # The bad OSErrors happen after all expected output has
                # appeared, so we return a falsey value, which triggers the
                # "end of output" logic in code using reader functions.
                data = None
        else:
            data = os.read(self.process.stdout.fileno(), num_bytes)
        return data

    def read_proc_stderr(self, num_bytes):
        # NOTE: when using a pty, this will never be called.
        # TODO: do we ever get those OSErrors on stderr? Feels like we could?
        return os.read(self.process.stderr.fileno(), num_bytes)

    def _write_proc_stdin(self, data):
        # NOTE: parent_fd from os.fork() is a read/write pipe attached to our
        # forked process' stdout/stdin, respectively.
        fd = self.parent_fd if self.using_pty else self.process.stdin.fileno()
        # Try to write, ignoring broken pipes if encountered (implies child
        # process exited before the process piping stdin to us finished;
        # there's nothing we can do about that!)
        try:
            return os.write(fd, data)
        except OSError as e:
            if 'Broken pipe' not in str(e):
                raise

    def start(self, command, shell, env):
        if self.using_pty:
            if pty is None: # Encountered ImportError
                sys.exit("You indicated pty=True, but your platform doesn't support the 'pty' module!") # noqa
            cols, rows = pty_size()
            self.pid, self.parent_fd = pty.fork()
            # If we're the child process, load up the actual command in a
            # shell, just as subprocess does; this replaces our process - whose
            # pipes are all hooked up to the PTY - with the "real" one.
            if self.pid == 0:
                # TODO: both pty.spawn() and pexpect.spawn() do a lot of
                # setup/teardown involving tty.setraw, getrlimit, signal.
                # Ostensibly we'll want some of that eventually, but if
                # possible write tests - integration-level if necessary -
                # before adding it!
                #
                # Set pty window size based on what our own controlling
                # terminal's window size appears to be.
                # TODO: make subroutine?
                winsize = struct.pack('HHHH', rows, cols, 0, 0)
                fcntl.ioctl(sys.stdout.fileno(), termios.TIOCSWINSZ, winsize)
                # Use execve for bare-minimum "exec w/ variable # args + env"
                # behavior. No need for the 'p' (use PATH to find executable)
                # for now.
                # TODO: see if subprocess is using equivalent of execvp...
                os.execve(shell, [shell, '-c', command], env)
        else:
            self.process = Popen(
                command,
                shell=True,
                executable=shell,
                env=env,
                stdout=PIPE,
                stderr=PIPE,
                stdin=PIPE,
            )

    @property
    def process_is_finished(self):
        if self.using_pty:
            # NOTE:
            # https://github.com/pexpect/ptyprocess/blob/4058faa05e2940662ab6da1330aa0586c6f9cd9c/ptyprocess/ptyprocess.py#L680-L687
            # implies that Linux "requires" use of the blocking, non-WNOHANG
            # version of this call. Our testing doesn't verify this, however,
            # so...
            # NOTE: It does appear to be totally blocking on Windows, so our
            # issue #351 may be totally unsolvable there. Unclear.
            pid_val, self.status = os.waitpid(self.pid, os.WNOHANG)
            return pid_val != 0
        else:
            return self.process.poll() is not None

    def send_interrupt(self, interrupt):
        # NOTE: No need to reraise the interrupt since we have full control
        # over the local process and can kill it.
        if self.using_pty:
            os.kill(self.pid, SIGINT)
        else:
            # Use send_signal with platform-appropriate signal (Windows doesn't
            # support SIGINT unfortunately, only SIGTERM).
            # NOTE: could use subprocess.terminate() (which is cross-platform)
            # but feels best to use SIGINT as much as we possibly can as it's
            # most appropriate. terminate() always sends SIGTERM.
            # NOTE: in interactive POSIX terminals, this is technically
            # unnecessary as Ctrl-C submits the INT to the entire foreground
            # process group (which will be both Invoke and its spawned
            # subprocess). However, it doesn't seem to hurt, & ensures that a
            # *non-interactive* SIGINT is forwarded correctly.
            self.process.send_signal(SIGINT if not WINDOWS else SIGTERM)

    def returncode(self):
        if self.using_pty:
            return os.WEXITSTATUS(self.status)
        else:
            return self.process.returncode

    def stop(self):
        # No explicit close-out required (so far).
        pass


class StreamWatcher(threading.local):
    """
    A class whose subclasses may act on seen stream data from subprocesses.

    Subclasses must exhibit the following API; see `Responder` for a concrete
    example.

    * `__init__` is simply the parameterization & state initialization vector.
      Subclasses may do whatever they need here, as long as they remember to
      call `super`, which does some basic bookkeeping.
    * `submit` must accept the entire current contents of the stream being
      watched, as a Unicode string, and may optionally return an iterable of
      Unicode strings (or act as a generator iterator, i.e. multiple calls to
      ``yield <unicode string>``), which will each be written to the
      subprocess' standard input.

    .. note::
        `StreamWatcher` subclasses exist in part to enable state tracking, such
        as detecting when a submitted password didn't work & erroring (or
        prompting a user, or etc). Such bookkeeping isn't easily achievable
        with simple callback functions.

    .. note::
        `StreamWatcher` subclasses `threading.local` so that its instances can
        be used to 'watch' both subprocess stdout and stderr in separate
        threads.
    """
    def submit(self, stream):
        """
        Act on ``stream`` data, potentially returning responses.

        :param unicode stream:
            All data read on this stream since the beginning of the session.

        :returns:
            An iterable of Unicode strings (which may be empty).
        """
        raise NotImplementedError


class Responder(StreamWatcher):
    """
    A parameterizable object that submits responses to specific patterns.

    Commonly used to implement password auto-responds for things like ``sudo``.
    """

    def __init__(self, pattern, response):
        """
        Imprint this `Responder` with necessary parameters.

        :param pattern:
            A raw string (e.g. ``r"\[sudo\] password for .*:"``) which will be
            turned into a regular expression.

        :param response:
            The string to submit to the subprocess' stdin when ``pattern`` is
            detected.
        """
        # TODO: precompile the keys into regex objects
        self.pattern = pattern
        self.response = response
        self.index = 0

    def pattern_matches(self, stream, pattern, index_attr):
        """
        Generic "search for pattern in stream, using index" behavior.

        Used here and in some subclasses that want to track multiple patterns
        concurrently.

        :param unicode stream: The same data passed to `submit`.
        :param unicode pattern: The pattern to search for.
        :param unicode index_attr: The name of the index attribute to use.
        :returns: An iterable of string matches.
        """
        # NOTE: generifies scanning so it can be used to scan for >1 pattern at
        # once, e.g. in FailingResponder.
        # Only look at stream contents we haven't seen yet, to avoid dupes.
        index = getattr(self, index_attr)
        new_ = stream[index:]
        # Search, across lines if necessary
        matches = re.findall(pattern, new_, re.S)
        # Update seek index if we've matched
        if matches:
            setattr(self, index_attr, index + len(new_))
        return matches

    def submit(self, stream):
        # Iterate over findall() response in case >1 match occurred.
        for _ in self.pattern_matches(stream, self.pattern, 'index'):
            yield self.response


class FailingResponder(Responder):
    """
    Variant of `Responder` which is capable of detecting incorrect responses.

    This class adds a ``failure_sentinel`` parameter to `__init__`, and its
    `submit` will raise `ResponseFailure` if it detects that sentinel value in
    the stream.
    """
    def __init__(self, pattern, response, failure_sentinel):
        super(FailingResponder, self).__init__(pattern, response)
        self.failure_sentinel = failure_sentinel
        self.failure_index = 0
        self.tried = False

    def submit(self, stream):
        # Behave like regular Responder initially
        response = super(FailingResponder, self).submit(stream)
        # Also check stream for our failure sentinel
        failed = self.pattern_matches(
            stream, self.failure_sentinel, 'failure_index'
        )
        # Error out if we seem to have failed after a previous response.
        # TODO: write tests for other cases!!!
        if self.tried and failed:
            raise ResponseFailure(self)
        # Once we see that we had a response, take note
        # TODO: will super.submit return a generator that always appears true?
        if response:
            self.tried = True
        # Again, behave regularly by default.
        return response


class ResponseFailure(Exception):
    """
    Signals that an autoresponse encountered a failure.
    """
    def __init__(self, responder):
        self.responder = responder

    def __str__(self):
        # TODO: test
        # NOTE: not repr'ing the pattern as that doubles up backslashes. shrug
        return "Auto-response to r\"{0}\" failed with {1!r}!".format(
            self.responder.pattern, self.responder.failure_sentinel
        )


class Result(object):
    """
    A container for information about the result of a command execution.

    See individual attribute/method documentation below for details.

    .. note::
        `Result` objects' truth evaluation is equivalent to their `.ok`
        attribute's value. Therefore, quick-and-dirty expressions like the
        following are possible::

            if run("some shell command"):
                do_something()
            else:
                handle_problem()
    """
    # TODO: inherit from namedtuple instead? heh (or: use attrs from pypi)
    def __init__(self, command, shell, env, stdout, stderr, exited, pty):
        #: The command which was executed.
        self.command = command
        #: The shell binary used for execution.
        self.shell = shell
        #: The shell environment used for execution.
        self.env = env
        #: An integer representing the subprocess' exit/return code.
        self.exited = exited
        #: An alias for `.exited`.
        self.return_code = exited
        #: The subprocess' standard output, as a multiline string.
        self.stdout = stdout
        #: Same as `.stdout` but containing standard error (unless the process
        #: was invoked via a pty; see `.Runner.run`.)
        self.stderr = stderr
        #: A boolean describing whether the subprocess was invoked with a pty
        #: or not; see `.Runner.run`.
        self.pty = pty

    def __nonzero__(self):
        # NOTE: This is the method that (under Python 2) determines Boolean
        # behavior for objects.
        return self.ok

    def __bool__(self):
        # NOTE: And this is the Python 3 equivalent of __nonzero__. Much better
        # name...
        return self.__nonzero__()

    def __str__(self):
        ret = ["Command exited with status {0}.".format(self.exited)]
        for x in ('stdout', 'stderr'):
            val = getattr(self, x)
            ret.append(u"""=== {0} ===
{1}
""".format(x, val.rstrip()) if val else u"(no {0})".format(x))
        return u"\n".join(ret)

    @property
    def ok(self):
        """
        A boolean equivalent to ``exited == 0``.
        """
        return self.exited == 0

    @property
    def failed(self):
        """
        The inverse of ``ok``.

        I.e., ``True`` if the program exited with a nonzero return code, and
        ``False`` otherwise.
        """
        return not self.ok


def normalize_hide(val):
    hide_vals = (None, False, 'out', 'stdout', 'err', 'stderr', 'both', True)
    if val not in hide_vals:
        err = "'hide' got {0!r} which is not in {1!r}"
        raise ValueError(err.format(val, hide_vals))
    if val in (None, False):
        hide = ()
    elif val in ('both', True):
        hide = ('out', 'err')
    elif val == 'stdout':
        hide = ('out',)
    elif val == 'stderr':
        hide = ('err',)
    else:
        hide = (val,)
    return hide
