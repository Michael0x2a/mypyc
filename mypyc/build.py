"""Support for building extensions using mypyc with distutils or setuptools

The main entry points are mypycify, which produces a list of extension
modules to be passed to setup, and MypycifyBuildExt, which must be
registered as a BuildExt command. A trivial setup.py for a mypyc built
project, then, looks like:

    from distutils.core import setup
    from mypyc.build import mypycify, MypycifyBuildExt

    setup(name='test_module',
          ext_modules=mypycify(['foo.py']),
          cmdclass={{'build_ext': MypycifyBuildExt}},

See the mypycify docs for additional arguments.

Because MypycifyBuildExt needs to inherit from the
distutils/setuputils build_ext, we need to know at import-time whether
we are using distutils or setuputils. We hackily decide based on
whether setuptools has been imported already.
"""

import glob
import sys
import os.path
import subprocess
import hashlib
import time
import shutil

from typing import List, Tuple, Any, Optional, Union, Dict, cast
MYPY = False
if MYPY:
    from typing import NoReturn

from mypy.main import process_options
from mypy.errors import CompileError
from mypy.options import Options
from mypy.build import BuildSource
from mypyc.namegen import exported_name
from mypyc.options import CompilerOptions

from mypyc import emitmodule


# We can work with either setuptools or distutils, and pick setuptools
# if it has been imported.
assert 'setuptools' in sys.modules or 'distutils' in sys.modules, (
    "'setuptools' or 'distutils' must be imported before mypyc.build")
USE_SETUPTOOLS = 'setuptools' in sys.modules

if USE_SETUPTOOLS:
    from setuptools import setup, Extension  # type: ignore
    from setuptools.command.build_ext import build_ext  # type: ignore
else:
    from distutils.core import setup, Extension
    from distutils.command.build_ext import build_ext  # type: ignore

from distutils import sysconfig, ccompiler


def setup_mypycify_vars() -> None:
    """Rewrite a bunch of config vars in pretty dubious ways."""
    # There has to be a better approach to this.

    # The vars can contain ints but we only work with str ones
    vars = cast(Dict[str, str], sysconfig.get_config_vars())
    if sys.platform == 'darwin':
        # On OS X, force the creation of dynamic libraries instead of bundles so that
        # we can link against multi-module shared libraries.
        # From https://stackoverflow.com/a/32765319
        vars['LDSHARED'] = vars['LDSHARED'].replace('-bundle', '-dynamiclib')
        # Also disable building 32-bit binaries, since we generate too much code
        # for a 32-bit Mach-O object. There has to be a better way to do this.
        vars['LDFLAGS'] = vars['LDFLAGS'].replace('-arch i386', '')
        vars['CFLAGS'] = vars['CFLAGS'].replace('-arch i386', '')


class MypycifyExtension(Extension):
    """Represents an Extension generated by mypyc.

    Stores a little bit of extra metadata to support that.
    Arguments:
      * is_mypyc_shared: True if this is a shared library generated to implement
          multiple modules
      * mypyc_shared_target: If this is a shim library, a reference to the shared library
          that actually contains the implementation of the module
    """
    def __init__(self, *args: Any,
                 is_mypyc_shared: bool = False,
                 mypyc_shared_target: Optional['MypycifyExtension'] = None,
                 **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.is_mypyc_shared = is_mypyc_shared
        self.mypyc_shared_target = mypyc_shared_target


def fail(message: str) -> 'NoReturn':
    # TODO: Is there something else we should do to fail?
    sys.exit(message)


def get_mypy_config(paths: List[str],
                    mypy_options: Optional[List[str]]) -> Tuple[List[BuildSource], Options]:
    """Construct mypy BuildSources and Options from file and options lists"""
    # It is kind of silly to do this but oh well
    mypy_options = mypy_options or []
    mypy_options.append('--')
    mypy_options.extend(paths)

    sources, options = process_options(mypy_options)
    if options.python_version[0] == 2:
        fail('Python 2 not supported')
    if not options.strict_optional:
        fail('Disabling strict optional checking not supported')
    options.show_traceback = True
    # Needed to get types for all AST nodes
    options.export_types = True
    # TODO: Support incremental checking
    options.incremental = False

    for source in sources:
        options.per_module_options.setdefault(source.module, {})['mypyc'] = True

    return sources, options


shim_template_unix = """\
#include <Python.h>

PyObject *CPyInit_{full_modname}(void);

PyMODINIT_FUNC
PyInit_{modname}(void)
{{
    return CPyInit_{full_modname}();
}}
"""

# As far as I could tell, Windows lacks the rpath style features we
# would need in automatically load the shared library (located
# relative to the module library) when a module library is loaded,
# which means that instead we get to do it dynamically.
#
# We do this by, at module initialization time, finding the location
# of the module dll and using it to compute the location of the shared
# library. We then load the shared library with LoadLibrary, find the
# appropriate CPyInit_ routine using GetProcAddress, and call it.
#
# The relative path of the shared library (from the shim library) is provided
# as the preprocessor define MYPYC_LIBRARY.
shim_template_windows = r"""\
#include <Python.h>
#include <windows.h>
#include <stdlib.h>
#include <stdio.h>

EXTERN_C IMAGE_DOS_HEADER __ImageBase;

typedef PyObject *(__cdecl *INITPROC)();

PyMODINIT_FUNC
PyInit_{modname}(void)
{{
    char path[MAX_PATH];
    char drive[MAX_PATH];
    char directory[MAX_PATH];
    HINSTANCE hinstLib;
    INITPROC proc;

    // get the file name of this dll
    DWORD res = GetModuleFileName((HINSTANCE)&__ImageBase, path, sizeof(path));
    if (res == 0 || res == sizeof(path)) {{
        PyErr_SetString(PyExc_RuntimeError, "GetModuleFileName failed");
        return NULL;
    }}

    // find the directory this dll is in
    _splitpath(path, drive, directory, NULL, NULL);
    // and use it to construct a path to the shared library
    snprintf(path, sizeof(path), "%s%s%s", drive, directory, MYPYC_LIBRARY);

    hinstLib = LoadLibrary(path);
    if (!hinstLib) {{
        PyErr_SetString(PyExc_RuntimeError, "LoadLibrary failed");
        return NULL;
    }}
    proc = (INITPROC)GetProcAddress(hinstLib, "CPyInit_{full_modname}");
    if (!proc) {{
        PyErr_SetString(PyExc_RuntimeError, "GetProcAddress failed");
        return NULL;
    }}

    return proc();
}}

// distutils sometimes spuriously tells cl to export CPyInit___init__,
// so provide that so it chills out
PyMODINIT_FUNC PyInit___init__(void) {{ return PyInit_{modname}(); }}
"""


def generate_c_extension_shim(full_module_name: str, module_name: str, dirname: str) -> str:
    """Create a C extension shim with a passthrough PyInit function."""
    cname = '%s.c' % full_module_name.replace('.', '___')  # XXX
    cpath = os.path.join(dirname, cname)

    with open(cpath, 'w') as f:
        shim_template = shim_template_windows if sys.platform == 'win32' else shim_template_unix
        f.write(shim_template.format(modname=module_name,
                                     full_modname=exported_name(full_module_name)))

    return cpath


def shared_lib_name(modules: List[str]) -> str:
    """Produce a probably unique name for a library from a list of module names."""
    h = hashlib.sha1()
    h.update(','.join(modules).encode())
    return 'mypyc_%s' % h.hexdigest()[:20]


def include_dir() -> str:
    """Find the path of the lib-rt dir that needs to be included"""
    return os.path.join(os.path.abspath(os.path.dirname(__file__)), 'lib-rt')


def generate_c(sources: List[BuildSource], options: Options,
               shared_lib_name: Optional[str],
               compiler_options: Optional[CompilerOptions] = None
               ) -> Tuple[List[Tuple[str, str]], str]:
    """Drive the actual core compilation step.

    Returns the C source code and (for debugging) the pretty printed IR.
    """
    module_names = [source.module for source in sources]
    compiler_options = compiler_options or CompilerOptions()

    # Do the actual work now
    t0 = time.time()
    try:
        result = emitmodule.parse_and_typecheck(sources, options)
    except CompileError as e:
        for line in e.messages:
            print(line)
        fail('Typechecking failure')

    t1 = time.time()
    if compiler_options.verbose:
        print("Parsed and typechecked in {:.3f}s".format(t1 - t0))

    ops = []  # type: List[str]
    ctext = emitmodule.compile_modules_to_c(result, module_names, shared_lib_name,
                                            compiler_options=compiler_options, ops=ops)

    t2 = time.time()
    if compiler_options.verbose:
        print("Compiled to C in {:.3f}s".format(t2 - t1))

    return ctext, '\n'.join(ops)


def build_using_shared_lib(sources: List[BuildSource],
                           lib_name: str,
                           cfiles: List[str],
                           build_dir: str,
                           extra_compile_args: List[str],
                           ) -> List[MypycifyExtension]:
    """Produce the list of extension modules when a shared library is needed.

    This creates one shared library extension module that all of the
    others link against and then one shim extension module for each
    module in the build, that simply calls an initialization function
    in the shared library.

    We treat the shared library as a python extension so that it is
    cleanly processed by setuptools, but it isn't *really* a python C
    extension module on its own.
    """
    shared_lib = MypycifyExtension(
        'lib' + lib_name,
        is_mypyc_shared=True,
        sources=cfiles,
        include_dirs=[include_dir()],
        extra_compile_args=extra_compile_args,
    )
    extensions = [shared_lib]

    for source in sources:
        module_name = source.module.split('.')[-1]
        shim_file = generate_c_extension_shim(source.module, module_name, build_dir)

        # We include the __init__ in the "module name" we stick in the Extension,
        # since this seems to be needed for it to end up in the right place.
        full_module_name = source.module
        assert source.path
        if os.path.split(source.path)[1] == '__init__.py':
            full_module_name += '.__init__'
        extensions.append(MypycifyExtension(
            full_module_name,
            mypyc_shared_target=shared_lib,
            sources=[shim_file],
            extra_compile_args=extra_compile_args,
        ))

    return extensions


def build_single_module(sources: List[BuildSource],
                        cfiles: List[str],
                        extra_compile_args: List[str],
                        ) -> List[MypycifyExtension]:
    """Produce the list of extension modules for a standalone extension.

    This contains just one module, since there is no need for a shared module.
    """
    return [MypycifyExtension(
        sources[0].module,
        sources=cfiles,
        include_dirs=[include_dir()],
        extra_compile_args=extra_compile_args,
    )]


def mypycify(paths: List[str],
             mypy_options: Optional[List[str]] = None,
             opt_level: str = '3',
             multi_file: bool = False,
             skip_cgen: bool = False,
             verbose: bool = False,
             strip_asserts: bool = False) -> List[MypycifyExtension]:
    """Main entry point to building using mypyc.

    This produces a list of Extension objects that should be passed as the
    ext_modules parameter to setup.

    Arguments:
      * paths: A list of file paths to build. It may contain globs.
      * mypy_options: Optionally, a list of command line flags to pass to mypy.
                      (This can also contain additional files, for compatibility reasons.)
      * opt_level: The optimization level, as a string. Defaults to '3' (meaning '-O3').
    """

    setup_mypycify_vars()
    compiler_options = CompilerOptions(strip_asserts=strip_asserts,
                                       multi_file=multi_file, verbose=verbose)

    # Create a compiler object so we can make decisions based on what
    # compiler is being used. typeshed is missing some attribues on the
    # compiler object so we give it type Any
    compiler = ccompiler.new_compiler()  # type: Any
    sysconfig.customize_compiler(compiler)

    expanded_paths = []
    for path in paths:
        expanded_paths.extend(glob.glob(path))

    build_dir = 'build'  # TODO: can this be overridden??
    try:
        os.mkdir(build_dir)
    except FileExistsError:
        pass

    sources, options = get_mypy_config(expanded_paths, mypy_options)
    # We generate a shared lib if there are multiple modules or if any
    # of the modules are in package. (Because I didn't want to fuss
    # around with making the single module code handle packages.)
    use_shared_lib = len(sources) > 1 or any('.' in x.module for x in sources)

    lib_name = shared_lib_name([source.module for source in sources]) if use_shared_lib else None

    # We let the test harness make us skip doing the full compilation
    # so that it can do a corner-cutting version without full stubs.
    # TODO: Be able to do this based on file mtimes?
    if not skip_cgen:
        cfiles, ops_text = generate_c(sources, options, lib_name,
                                      compiler_options=compiler_options)
        # TODO: unique names?
        with open(os.path.join(build_dir, 'ops.txt'), 'w') as f:
            f.write(ops_text)
        cfilenames = []
        for cfile, ctext in cfiles:
            cfile = os.path.join(build_dir, cfile)
            with open(cfile, 'w', encoding='utf-8') as f:
                f.write(ctext)
            if os.path.splitext(cfile)[1] == '.c':
                cfilenames.append(cfile)
    else:
        cfilenames = glob.glob(os.path.join(build_dir, '*.c'))

    cflags = []  # type: List[str]
    if compiler.compiler_type == 'unix':
        cflags += [
            '-O{}'.format(opt_level), '-Werror', '-Wno-unused-function', '-Wno-unused-label',
            '-Wno-unreachable-code', '-Wno-unused-variable', '-Wno-trigraphs',
            '-Wno-unused-command-line-argument'
        ]
        if 'gcc' in compiler.compiler[0]:
            # This flag is needed for gcc but does not exist on clang.
            cflags += ['-Wno-unused-but-set-variable']
    elif compiler.compiler_type == 'msvc':
        if opt_level == '3':
            opt_level = '2'
        cflags += [
            '/O{}'.format(opt_level),
            '/wd4102',  # unreferenced label
            '/wd4101',  # unreferenced local variable
            '/wd4146',  # negating unsigned int
        ]
        if multi_file:
            # Disable whole program optimization in multi-file mode so
            # that we actually get the compilation speed and memory
            # use wins that multi-file mode is intended for.
            cflags += [
                '/GL-',
                '/wd9025',  # warning about overriding /GL
            ]

    # Copy the runtime library in
    rt_file = os.path.join(build_dir, 'CPy.c')
    shutil.copyfile(os.path.join(include_dir(), 'CPy.c'), rt_file)
    cfilenames.append(rt_file)

    if use_shared_lib:
        assert lib_name
        extensions = build_using_shared_lib(sources, lib_name, cfilenames, build_dir, cflags)
    else:
        extensions = build_single_module(sources, cfilenames, cflags)

    return extensions


class MypycifyBuildExt(build_ext):
    """Custom setuptools/distutils build_ext command.

    This overrides the build_extension method so that we can hook in
    before and after the actual compilation.

    The key thing here is that we need to hook in after compilation on
    OS X, because we need to use `install_name_tool` to fix up the
    libraries to use relative paths.

    We hook in before compilation to update library paths to include
    where the built shared library is placed. (We probably could have
    hacked this together without hooking in here, but we were hooking
    in already and build_ext makes it easy to get that information)
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # parallel is busted because the shared library needs to go first.
        # We could override build_extensions to arrange for that to happen
        # and to parallelize the rest but it wouldn't help much.
        self.parallel = 0

    def _get_rt_lib_path(self, ext: MypycifyExtension) -> str:
        module_parts = ext.name.split('.')
        if len(module_parts) > 1:
            relative_lib_path = os.path.join(*(['..'] * (len(module_parts) - 1)))
        else:
            relative_lib_path = '.'
        return relative_lib_path

    def build_extension(self, ext: MypycifyExtension) -> None:
        # First we need to figure out what the real library names are
        # so that we can set them up properly.
        if isinstance(ext, MypycifyExtension) and ext.mypyc_shared_target:
            relative_lib_path = self._get_rt_lib_path(ext)
            shared_dir, shared_file = os.path.split(
                self.get_ext_fullpath(ext.mypyc_shared_target.name))
            shared_name = os.path.splitext(shared_file)[0][3:]
            if sys.platform == 'win32':
                # On windows, instead of linking against the shared library,
                # we dynamically load it at runtime. We generate our C shims
                # before we have found out what the library filename is, so
                # pass it in as a preprocessor define.
                path = os.path.join(relative_lib_path, shared_file)
                ext.extra_compile_args = ext.extra_compile_args + [
                    '/DMYPYC_LIBRARY=\\"{}\\"'.format(path.replace('\\', '\\\\'))]
            else:
                # On other platforms we link against the library normally
                ext.libraries.append(shared_name)
                ext.library_dirs.append(shared_dir)
            if sys.platform == 'linux':
                ext.runtime_library_dirs.append('$ORIGIN/{}'.format(
                    relative_lib_path))

        # Run the actual C build
        super().build_extension(ext)

        # On OS X, we need to patch up these paths post-hoc, tragically
        if sys.platform == 'darwin':
            out_path = self.get_ext_fullpath(ext.name)
            # After compiling the shared library, drop the path part from its name.
            if isinstance(ext, MypycifyExtension) and ext.is_mypyc_shared:
                subprocess.check_call(['install_name_tool', '-id',
                                       os.path.basename(out_path),
                                       out_path])
            # For libraries that link against a shared one, update the path to
            # the shared library to be relative to @loader_path.
            if isinstance(ext, MypycifyExtension) and ext.mypyc_shared_target:
                new_path = os.path.join('@loader_path', relative_lib_path,
                                        shared_file)
                subprocess.check_call(['install_name_tool', '-change',
                                       shared_file, new_path, out_path])
