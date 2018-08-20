import warnings
from copy import copy
from pprint import pformat
import requests
import subprocess
import socket
import json
import os
import sys
import threading
import retrying
import atexit

import plotly
from plotly.files import PLOTLY_DIR
from six import string_types
from plotly.optional_imports import get_module

psutil = get_module('psutil')

# Valid image format constants
# ----------------------------
valid_formats = ('png', 'jpeg', 'webp', 'svg', 'pdf', 'eps')
_format_conversions = {fmt: fmt
                       for fmt in valid_formats}
_format_conversions.update({'jpg': 'jpeg'})


# Utility functions
# -----------------
def _raise_format_value_error(val):
    raise ValueError("""
    Invalid value of type {typ} receive as an image format designation.
        Received value: {v}

    An image format must be specified as one of the following string values:
        {valid_formats}""".format(
        typ=type(val),
        v=val,
        valid_formats=sorted(_format_conversions.keys())))


def _validate_coerce_format(fmt):
    """
    Validate / coerce a user specified image format, and raise an informative
    exception if format is invalid.

    Parameters
    ----------
    fmt: str
        A string that may or may not be a valid image format.

    Returns
    -------
    str
        A valid image format string as supported by orca. This may not
        be identical to the input image designation. For example,
        the resulting string will always be lower case and  'jpg' is
        converted to 'jpeg'.

    Raises
    ------
    ValueError
        if the input `fmt` cannot be interpreted as a valid image format.
    """

    # Let None pass through
    if fmt is None:
        return None

    if not isinstance(fmt, string_types) or not fmt:
        _raise_format_value_error(fmt)

    fmt = fmt.lower()
    if fmt[0] == '.':
        fmt = fmt[1:]

    if fmt not in _format_conversions:
        _raise_format_value_error(fmt)

    return _format_conversions[fmt]


def _find_open_port():
    """
    Use the socket module to find an open port.

    Returns
    -------
    int
        An open port
    """
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(('', 0))
    _, port = s.getsockname()
    s.close()

    return port


def which_py2(cmd, mode=os.F_OK | os.X_OK, path=None):
    """
    Backport (unmodified) of shutil.which command from Python 3.6
    Remove this when Python 2 support is dropped

    Given a command, mode, and a PATH string, return the path which
    conforms to the given mode on the PATH, or None if there is no such
    file.

    `mode` defaults to os.F_OK | os.X_OK. `path` defaults to the result
    of os.environ.get("PATH"), or can be overridden with a custom search
    path.
    """
    # Check that a given file can be accessed with the correct mode.
    # Additionally check that `file` is not a directory, as on Windows
    # directories pass the os.access check.
    def _access_check(fn, mode):
        return (os.path.exists(fn) and os.access(fn, mode)
                and not os.path.isdir(fn))

    # If we're given a path with a directory part, look it up directly rather
    # than referring to PATH directories. This includes checking relative to the
    # current directory, e.g. ./script
    if os.path.dirname(cmd):
        if _access_check(cmd, mode):
            return cmd
        return None

    if path is None:
        path = os.environ.get("PATH", os.defpath)
    if not path:
        return None
    path = path.split(os.pathsep)

    if sys.platform == "win32":
        # The current directory takes precedence on Windows.
        if not os.curdir in path:
            path.insert(0, os.curdir)

        # PATHEXT is necessary to check on Windows.
        pathext = os.environ.get("PATHEXT", "").split(os.pathsep)
        # See if the given file matches any of the expected path extensions.
        # This will allow us to short circuit when given "python.exe".
        # If it does match, only test that one, otherwise we have to try
        # others.
        if any(cmd.lower().endswith(ext.lower()) for ext in pathext):
            files = [cmd]
        else:
            files = [cmd + ext for ext in pathext]
    else:
        # On other platforms you don't have things like PATHEXT to tell you
        # what file suffixes are executable, so just pass on cmd as-is.
        files = [cmd]

    seen = set()
    for dir in path:
        normdir = os.path.normcase(dir)
        if not normdir in seen:
            seen.add(normdir)
            for thefile in files:
                name = os.path.join(dir, thefile)
                if _access_check(name, mode):
                    return name
    return None


def which(cmd):
    """
    Return the absolute path of the input executable string, based on the
    user's current PATH variable.

    This is a wrapper for shutil.which that is compatible with Python 2.

    Parameters
    ----------
    cmd: str
        String containing the name of an executable on the user's path.

    Returns
    -------
    str or None
        String containing the absolute path of the executable, or None if
        the executable was not found.

    """
    if sys.version_info > (3, 0):
        # Python 3 code in this block
        import shutil
        return shutil.which(cmd)
    else:
        return which_py2(cmd)


# Orca configuration class
# ------------------------
class OrcaConfig(object):
    """
    Singleton object containing the current user defined configuration
    properties for orca.

    These parameters may optionally be saved to the user's ~/.plotly
    directory using the `save` method, in which case they are automatically
    restored in future sessions.
    """
    def __init__(self):
        self._props = {}
        root_dir = os.path.dirname(os.path.abspath(plotly.__file__))
        self.package_dir = os.path.join(root_dir, 'package_data')

        self.restore_defaults(reset_server=False)

        # Constants
        plotlyjs = os.path.join(self.package_dir, 'plotly.min.js')
        self._constants = {
            'plotlyjs': plotlyjs,
            'config_file': os.path.join(PLOTLY_DIR, ".orca")
        }

    def restore_defaults(self, reset_server=True):
        """
        Reset all orca configuration properties to their default values
        """
        self._props = {}

        if reset_server:
            # Server must restart before setting is active
            reset_orca_status()

    def update(self, d={}, **kwargs):
        """
        Update one or more properties from a dict or from input keyword
        arguments.

        Parameters
        ----------
        d: dict
            Dictionary from property names to new property values.

        kwargs
            Named argument value pairs where the name is a configuration
            property name and the value is the new property value.

        Returns
        -------
        None

        Examples
        --------
        Update configuration properties using a dictionary

        >>> import plotly.io as pio
        >>> pio.orca.config.update({'timeout': 30, 'default_format': 'svg'})

        Update configuration properties using keyword arguments

        >>> pio.orca.config.update(timeout=30, default_format=svg})
        """
        # Combine d and kwargs
        if not isinstance(d, dict):
            raise ValueError("""
The first argument to update must be a dict, \
but received value of type {typ}l
    Received value: {val}""".format(typ=type(d), val=d))

        updates = copy(d)
        updates.update(kwargs)

        # Validate keys
        for k in updates:
            if k not in self._props:
                raise ValueError('Invalid property name: {k}'.format(k=k))

        # Apply keys
        for k, v in updates.items():
            setattr(self, k, v)

    @property
    def port(self):
        """
        The specific port to use to communicate with the orca server, or
        None if the port is to be chosen automatically.

        If an orca server is active, the port in use is stored in the
        plotly.io.orca.status.port property.

        Returns
        -------
        int or None
        """
        return self._props.get('port', None)

    @port.setter
    def port(self, val):
        if val is not None and not isinstance(val, int):
            raise ValueError("""
The port value must be an integer, but received value of type {typ}.
    Received value: {val}""".format(typ=type(val), val=val))

        self._props['port'] = val

    @property
    def executable(self):
        """
        The name or full path of the orca executable.

         - If a name (e.g. 'orca'), then it should be the name of an orca
           executable on the PATH. The directories on the PATH can be
           displayed by running the following command:

           >>> import os
           >>> print(os.environ.get('PATH').replace(':', os.linesep))

         - If a full path (e.g. '/path/to/orca'), then
           is should be the full path to an orca executable. In this case
           the executable does not need to reside on the PATH.

        If an orca server has been validated, then the full path to the
        validated orca executable is stored in the
        plotly.io.orca.status.executable property.

        Returns
        -------
        str
        """
        return self._props.get('executable', 'orca')

    @executable.setter
    def executable(self, val):

        # Use default value if val is None or empty
        # -----------------------------------------
        if not val:
            val = 'orca'

        # Validate val
        # ------------
        if not isinstance(val, string_types):
            raise ValueError("""
The executable property must be a string, but received value of type {typ}.
    Received value: {val}""".format(typ=type(val), val=val))
        self._props['executable'] = val

        # Server must restart before setting is active
        shutdown_orca_server()

    @property
    def timeout(self):
        """
        The number of seconds of inactivity required before the orca server
        is shut down.

        For example, if timeout is set to 20, then the orca
        server will shutdown once is has not been used for at least
        20 seconds. If timeout is set to None, then the server will not be
        automatically shut down due to inactivity.

        Regardless of the value of timeout, a running orca server may be
        manually shut down like this:

        >>> import plotly.io as pio
        >>> pio.orca.shutdown_orca_server()

        Returns
        -------
        int or float or None
        """
        return self._props.get('timeout', None)

    @timeout.setter
    def timeout(self, val):
        if val is not None and not isinstance(val, (int, float)):
            raise ValueError("""
The timeout property must be a number, but received value of type {typ}.
    Received value: {val}""".format(typ=type(val), val=val))
        self._props['timeout'] = val

    @property
    def default_width(self):
        """
        The default width to use on image export. This value is only
        applied if the no width value is supplied to the plotly.io
        to_image or write_image functions.

        Returns
        -------
        int or None
        """
        return self._props.get('default_width', None)

    @default_width.setter
    def default_width(self, val):
        if val is not None and not isinstance(val, int):
            raise ValueError("""
The default_width property must be an int, but received value of type {typ}.
    Received value: {val}""".format(typ=type(val), val=val))
        self._props['default_width'] = val

    @property
    def default_height(self):
        """
        The default height to use on image export. This value is only
        applied if the no height value is supplied to the plotly.io
        to_image or write_image functions.

        Returns
        -------
        int or None
        """
        return self._props.get('default_height', None)

    @default_height.setter
    def default_height(self, val):
        if val is not None and not isinstance(val, int):
            raise ValueError("""
The default_height property must be an int, but received value of type {typ}.
    Received value: {val}""".format(typ=type(val), val=val))
        self._props['default_height'] = val

    @property
    def default_format(self):
        """
        The default image format to use on image export.

        Valid image formats strings are:
          - 'png'
          - 'jpg' or 'jpeg'
          - 'webp'
          - 'svg'
          - 'pdf'
          - 'eps' (Requires the poppler library to be installed)

        This value is only applied if no format value is supplied to the
        plotly.io to_image or write_image functions.

        Returns
        -------
        str or None
        """
        return self._props.get('default_format', 'png')

    @default_format.setter
    def default_format(self, val):
        val = _validate_coerce_format(val)
        self._props['default_format'] = val

    @property
    def default_scale(self):
        """
        The default image scaling factor to use on image export.
        This value is only applied if the no scale value is supplied to the
        plotly.io to_image or write_image functions.

        Returns
        -------
        int or None
        """
        return self._props.get('default_scale', 1)

    @default_scale.setter
    def default_scale(self, val):
        if val is not None and not isinstance(val, (int, float)):
            raise ValueError("""
The default_scale property must be a number, but received value of type {typ}.
    Received value: {val}""".format(typ=type(val), val=val))
        self._props['default_scale'] = val


    @property
    def plotlyjs(self):
        """
        The plotly.js bundle being used for image rendering.

        Returns
        -------
        str
        """
        return self._constants.get('plotlyjs', None)


    @property
    def topojson(self):
        """
        Path to the topojson files needed to render choropleth traces.

        If None, topojson files from the plot.ly CDN are used.

        Returns
        -------
        str
        """
        return self._props.get('topojson',
                               os.path.join(self.package_dir, 'topojson'))

    @topojson.setter
    def topojson(self, val):
        # Validate val
        # ------------
        if val is not None and not isinstance(val, string_types):
            raise ValueError("""
The topojson property must be a string, but received value of type {typ}.
    Received value: {val}""".format(typ=type(val), val=val))
        self._props['topojson'] = val

        # Server must restart before setting is active
        shutdown_orca_server()

    @property
    def mathjax(self):
        """
        Path to the MathJax bundle needed to render LaTeX characters

        Returns
        -------
        str
        """
        return self._props.get('mathjax',
                               ('https://cdnjs.cloudflare.com'
                                '/ajax/libs/mathjax/2.7.5/MathJax.js'))

    @mathjax.setter
    def mathjax(self, val):

        # Validate val
        # ------------
        if val is not None and not isinstance(val, string_types):
            raise ValueError("""
The mathjax property must be a string, but received value of type {typ}.
    Received value: {val}""".format(typ=type(val), val=val))
        self._props['mathjax'] = val

        # Server must restart before setting is active
        shutdown_orca_server()

    @property
    def mapbox_access_token(self):
        """
        Mapbox access token required to render mapbox traces.

        Returns
        -------
        str
        """
        return self._props.get('mapbox_access_token', None)

    @mapbox_access_token.setter
    def mapbox_access_token(self, val):
        # Validate val
        # ------------
        if val is not None and not isinstance(val, string_types):
            raise ValueError("""
    The mapbox_access_token property must be a string, \
but received value of type {typ}.
        Received value: {val}""".format(typ=type(val), val=val))
        self._props['mapbox_access_token'] = val

        # Server must restart before setting is active
        shutdown_orca_server()

    @property
    def config_file(self):
        """
        Path to orca configuration file

        Using the `plotly.io.config.save()` method will save the current
        configuration settings to this file. Settings in this file are
        restored at the beginning of each sessions.

        Returns
        -------
        str
        """
        return os.path.join(PLOTLY_DIR, ".orca")

    def reload(self, warn=True):
        """
        Reload orca settings from .plotly/.orca, if any.

        Note: Settings are loaded automatically when plotly is imported.
        This method is only needed if the setting are changed by some outside
        process (e.g. a text editor) during an interactive session.

        Parameters
        ----------
        warn

        Returns
        -------

        """
        if os.path.exists(self.config_file):

            # ### Load file into a string ###
            try:
                with open(self.config_file, 'r') as f:
                    orca_str = f.read()
            except:
                if warn:
                    warnings.warn("""\
        Unable to read orca configuration file at {path}""".format(
                        path=self.config_file
                    ))
                return

            # ### Parse as JSON ###
            try:
                orca_props = json.loads(orca_str)
            except ValueError:
                if warn:
                    warnings.warn("""\
        Orca configuration file at {path} is not valid JSON""".format(
                        path=self.config_file
                    ))
                return

            # ### Update _props ###
            for k, v in orca_props.items():
                # Only keep properties that we understand
                if k in self._props:
                    self._props[k] = v

        elif warn:
            warnings.warn("""\
        Orca configuration file at {path} not found""".format(
                path=self.config_file))

    def save(self):
        """
        Attempt to save current settings to disk, so that they are
        automatically restored for future sessions.

        This operation requires write access to the path returned by
        in the `config_file` property.

        Returns
        -------
        None
        """
        ## Make smarter
        ## Only save set-able properties with non-default values
        with open(self.config_file, 'w', encoding='utf-8') as f:
            json.dump(self._props, f, indent=4)

    def __repr__(self):
        """
        Display a nice representation of the current orca server status.
        """
        return """\
orca configuration
------------------
""" + pformat(self._props, width=40)


# Make config a singleton object
# ------------------------------
config = OrcaConfig()
del OrcaConfig


# Orca status class
# ------------------------
class OrcaStatus(object):
    """
    Class to store information about the current status of the orca server.
    """
    _props = {
        'state': 'unvalidated', # or 'validated' or 'running'
        'executable': None,
        'version': None,
        'pid': None,
        'port': None,
        'command': None
    }

    @property
    def state(self):
        """
        A string representing the state of the orca server process

        One of:
          - unvalidated: The orca executable has not yet been searched for or
            tested to make sure its valid.
          - validated: The orca executable has been located and tested for
            validity, but it is not running.
          - running: The orca server process is currently running.
        """
        return self._props['state']

    @property
    def executable(self):
        """
        If the `state` property is 'validated' or 'running', this property
        contains the full path to the orca executable.

        This path can be specified explicitly by setting the `executable`
        property of the `plotly.io.orca.config` object.

        This property will be None if the `state` is 'unvalidated'.
        """
        return self._props['executable']

    @property
    def version(self):
        """
        If the `state` property is 'validated' or 'running', this property
        contains the version of the validated orca executable.

        This property will be None if the `state` is 'unvalidated'.
        """
        return self._props['version']


    @property
    def pid(self):
        """
        The process id of the orca server process, if any. This property
        will be None if the `state` is not 'running'.
        """
        return self._props['pid']


    @property
    def port(self):
        """
        The port number that the orca server process is listening to, if any.
        This property will be None if the `state` is not 'running'.

        This port can be specified explicitly by setting the `port`
        property of the `plotly.io.orca.config` object.
        """
        return self._props['port']

    @property
    def command(self):
        """
        The command arguments used to launch the running orca server, if any.
        This property will be None if the `state` is not 'running'.
        """
        return self._props['command']

    def __repr__(self):
        """
        Display a nice representation of the current orca server status.
        """
        return """\
    orca status
    -----------
""" + pformat(self._props, width=40)


# Make config a singleton object
# ------------------------------
status = OrcaStatus()
del OrcaStatus


# Public orca server interactino functions
# ----------------------------------------
def validate_orca_executable():
    """
    Attempt to find and validate the orca executable specified by the
    `plotly.io.orca.config.executable` property.

    If the `plotly.io.orca.status.state` property is 'validated' or 'running'
    then this function does nothing.

    How it works:
      - First, it searches the system PATH for an executable that matches the
      name or path specified in the `plotly.io.orca.config.executable`
      property.
      - Then it runs the executable with the `--help` flag to make sure
      it's the plotly orca executable
      - Then it runs the executable with the `--version` flag to check the
      orca version.

    If all of these steps are successful then the `status.state` property
    is set to 'validated' and the `status.executable` and `status.version`
    properties are populated

    Returns
    -------
    None
    """
    # Check state
    # -----------
    if status.state != 'unvalidated':
        # Nothing more to do
        return

    # Initialize error messages
    # -------------------------
    install_location_instructions = """\
If you haven't installed orca yet, you can do so using conda as follows:

    $ conda install -c plotly plotly-orca

After installation is complete, no further configuration should be needed. 
For other approaches to installing orca, see the orca project README at
https://github.com/plotly/orca.

If you have installed orca, then for some reason plotly.py was unable to
locate it. In this case, set the `plotly.io.orca.config.executable`
property to the full path to your orca executable. For example:

    >>> plotly.io.orca.config.executable = '/path/to/orca'

If you're still having trouble, feel free to ask for help on the forums at
https://community.plot.ly/c/api/python"""

    # Try to find an executable
    # -------------------------
    # Search for executable name or path in config.executable
    executable = which(config.executable)

    # If searching for default ('orca') and none was found,
    # Try searching for orca.js in case orca was installed using npm
    if executable is None and config.executable == 'orca':
        executable = which('orca.js')

    if executable is None:
        path = os.environ.get("PATH", os.defpath)
        formatted_path = path.replace(':', '\n    ')

        raise ValueError("""
The orca executable is required in order to export figures as static images,
but it could not be found on the system path.

Searched for executable '{executable}' on the following path:
    {formatted_path}

{instructions}""".format(
            executable=config.executable,
            formatted_path=formatted_path,
            instructions=install_location_instructions))

    # Run executable with --help and see if it's our orca
    # ---------------------------------------------------
    invalid_executable_msg = """
The orca executable is required in order to export figures as static images,
but the executable that was found at '{executable}' does not seem to be a
valid plotly orca executable.

{instructions}""".format(
        executable=executable,
        instructions=install_location_instructions)

    try:
        help_result = subprocess.check_output([executable, '--help'])
    except subprocess.CalledProcessError:
        raise ValueError(invalid_executable_msg)

    if not help_result:
        raise ValueError(invalid_executable_msg)

    if 'plotly' not in help_result.decode('utf-8').lower():
        raise ValueError(invalid_executable_msg)

    # Get orca version
    # ----------------
    try:
        orca_version = subprocess.check_output([executable, '--version'])
    except subprocess.CalledProcessError:
        raise ValueError("""
An error occurred while trying to get the version of the orca executable.
Here is the command that plotly.py ran to request the version:

    $ {executable} --version
""")

    if not orca_version:
        raise ValueError("""
No version was reported by the orca executable.      

Here is the command that plotly.py ran to request the version:

    $ {executable} --version  
""")
    else:
        orca_version = orca_version.decode()

    # Check version >= 1.1.0 so we have --graph-only support.
    status._props['executable'] = executable
    status._props['version'] = orca_version.strip()
    status._props['state'] = 'validated'


def reset_orca_status():
    """
    Shutdown the running orca server, if any, and reset the orca status
    to unvalidated.

    This command is only needed if the desired orca executable is changed
    during an interactive session.

    Returns
    -------
    None
    """
    shutdown_orca_server()
    status._props['executable'] = None
    status._props['version'] = None
    status._props['state'] = 'unvalidated'


# Initialze process control variables
# -----------------------------------
__orca_lock = threading.Lock()
__orca_state = {'proc': None,
                'shutdown_timer': None}


# Shutdown
# --------
# The @atexit.register annotation ensures that the shutdown function is
# is run when the Python process is terminated
@atexit.register
def cleanup():
    shutdown_orca_server()


def shutdown_orca_server():
    """
    Shutdown the running orca server process, if any

    Returns
    -------
    None
    """
    # Use double-check locking to make sure the properties of __orca_state
    # are updated consistently across threads.
    if __orca_state['proc'] is not None:
        with __orca_lock:
            if __orca_state['proc'] is not None:

                # We use psutil to kill all child processes of the main orca
                # process. This prevents any zombie processes from being
                # left over, and it saves us from needing to write
                # OS-specific process management code here.
                parent = psutil.Process(__orca_state['proc'].pid)
                for child in parent.children(
                        recursive=True):
                    child.terminate()

                __orca_state['proc'].terminate()  # Unix

                output, err = __orca_state['proc'].communicate()

                # Wait for the process to shutdown
                child_status = __orca_state['proc'].wait()

                # Update our internal process management state
                __orca_state['proc'] = None

                if __orca_state['shutdown_timer'] is not None:
                    __orca_state['shutdown_timer'].cancel()
                    __orca_state['shutdown_timer'] = None

                __orca_state['port'] = None

                # Update orca.status so the user has an accurate view
                # of the state of the orca server
                status._props['state'] = 'validated'
                status._props['pid'] = None
                status._props['port'] = None
                status._props['command'] = None


# Launch or get server
def ensure_orca_server():
    """
    Start an orca server if none is running. If a server is already running,
    then reset the timeout countdown

    Returns
    -------
    None
    """

    # Validate psutil
    if psutil is None:
        raise ValueError("""\
Image generation requires the psutil package.

Install using pip:
    $ pip install psutil
    
Install using conda:
    $ pip install psutil
""")

    # Validate orca executable
    if status.state == 'unvalidated':
        validate_orca_executable()

    # Acquire lock to make sure that we keep the properties of __orca_state
    # consistent across threads
    with __orca_lock:
        # Cancel the current shutdown timer, if any
        if __orca_state['shutdown_timer'] is not None:
            __orca_state['shutdown_timer'].cancel()

        # Start a new server process if none is active
        if __orca_state['proc'] is None:

            # Determine server port
            if config.port is None:
                __orca_state['port'] = _find_open_port()
            else:
                __orca_state['port'] = config.port

            # Build orca command list
            cmd_list = [config.executable, 'serve',
                        '-p', str(__orca_state['port']),
                        '--graph-only']

            if config.plotlyjs:
                cmd_list.extend(['--plotly', config.plotlyjs])

            if config.topojson:
                cmd_list.extend(['--topojson', config.topojson])

            if config.mathjax:
                cmd_list.extend(['--mathjax', config.mathjax])

            if config.mapbox_access_token:
                cmd_list.extend(['--mapbox-access-token',
                                 config.mapbox_access_token])

            # Create subprocess that launches the orca server on the
            # specified port.
            __orca_state['proc'] = subprocess.Popen(cmd_list,
                                                    stdout=subprocess.PIPE)

            # Update orca.status so the user has an accurate view
            # of the state of the orca server
            status._props['state'] = 'running'
            status._props['pid'] = __orca_state['proc'].pid
            status._props['port'] = __orca_state['port']
            status._props['command'] = cmd_list

        # Create new shutdown timer if a timeout was specified
        if config.timeout is not None:
            t = threading.Timer(config.timeout, shutdown_orca_server)
            # Make t a daemon thread so that exit won't wait for timer to
            # complete
            t.daemon = True
            t.start()
            __orca_state['shutdown_timer'] = t


@retrying.retry(wait_random_min=5, wait_random_max=10, stop_max_delay=10000)
def _request_image_with_retrying(**kwargs):
    """
    Helper method to perform an image request to a running orca server process
    with retrying logic.
    """
    server_url = 'http://{hostname}:{port}'.format(
        hostname='localhost', port=__orca_state['port'])

    request_params = {k: v for k, v, in kwargs.items() if v is not None}
    json_str = json.dumps(request_params, cls=plotly.utils.PlotlyJSONEncoder)
    r = requests.post(server_url + '/', data=json_str)
    return r.content


def to_image(fig, format=None, width=None, height=None, scale=None, ):
    """
    Convert a figure to a static image bytes string

    Parameters
    ----------
    fig:
        Figure object or dict representing a figure

    format: str or None
        The desired image format. One of
          - 'png'
          - 'jpg' or 'jpeg'
          - 'webp'
          - 'svg'
          - 'pdf'
          - 'eps' (Requires the poppler library to be installed)

        If not specified, will default to `plotly.io.config.default_format`

    width: int or None
        The width of the exported image in layout pixels. If the `scale`
        property is 1.0, this will also be the width of the exported image
        in physical pixels.

        If not specified, will default to `plotly.io.config.default_width`

    height: int or None
        The height of the exported image in layout pixels. If the `scale`
        property is 1.0, this will also be the height of the exported image
        in physical pixels.

        If not specified, will default to `plotly.io.config.default_height`

    scale: int or float or None
        The scale factor to use when exporting the figure. A scale factor
        larger than 1.0 will increase the image resolution with respect
        to the figure's layout pixel dimensions. Whereas as scale factor of
        less than 1.0 will decrease the image resolution.

        If not specified, will default to `plotly.io.config.default_scale`

    Returns
    -------
    bytes
        The image data
    """
    # Make sure orca sever is running
    # -------------------------------
    ensure_orca_server()

    # Handle defaults
    # ---------------
    # Apply configuration defaults to unspecified arguments
    if format is None:
        format = config.default_format

    format = _validate_coerce_format(format)

    if scale is None:
        scale = config.default_scale

    if width is None:
        width = config.default_width

    if height is None:
        height = config.default_height

    # Request image from server
    # -------------------------
    img_data = _request_image_with_retrying(
        figure=fig, format=format, scale=scale, width=width, height=height)

    return img_data


def write_image(fig, file, format=None, scale=None, width=None, height=None):
    """
    Convert a figure to a static image and write it to a file or writeable
    object

    Parameters
    ----------
    fig:
        Figure object or dict representing a figure

    file: str or writeable
        A string representing a local file path or a writeable object
        (e.g. an open file descriptor)

    format: str or None
        The desired image format. One of
          - 'png'
          - 'jpg' or 'jpeg'
          - 'webp'
          - 'svg'
          - 'pdf'
          - 'eps' (Requires the poppler library to be installed)

        If not specified, will default to `plotly.io.config.default_format`

    width: int or None
        The width of the exported image in layout pixels. If the `scale`
        property is 1.0, this will also be the width of the exported image
        in physical pixels.

        If not specified, will default to `plotly.io.config.default_width`

    height: int or None
        The height of the exported image in layout pixels. If the `scale`
        property is 1.0, this will also be the height of the exported image
        in physical pixels.

        If not specified, will default to `plotly.io.config.default_height`

    scale: int or float or None
        The scale factor to use when exporting the figure. A scale factor
        larger than 1.0 will increase the image resolution with respect
        to the figure's layout pixel dimensions. Whereas as scale factor of
        less than 1.0 will decrease the image resolution.

        If not specified, will default to `plotly.io.config.default_scale`

    Returns
    -------
    None"""

    # Check if file is a string
    # -------------------------
    file_is_str = isinstance(file, string_types)

    # Infer format if not specified
    # -----------------------------
    if file_is_str and format is None:
        _, ext = os.path.splitext(file)
        if ext:
            format = _validate_coerce_format(ext)
        else:
            raise ValueError("""
Cannot infer image type from output path '{file}'.
Please add a file extension or specify the type using the format parameter.
For example:

>>> import plotly.io as pio
>>> pio.write_image(fig, file_path, format='png') 
""".format(file=file))

    # Request image
    # -------------
    # Do this first so we don't create a file if image conversion fails
    img_data = to_image(fig,
                        format=format,
                        scale=scale,
                        width=width,
                        height=height)

    # Open file
    # ---------
    if file_is_str:
        with open(file, 'wb') as f:
            f.write(img_data)
    else:
        file.write(img_data)