# PyInstaller runtime hook.
#
# Some optional voice dependencies decorate classes/functions
# with @typeguard.typechecked. That decorator calls inspect.getsource() at
# import time to rewrite the function with runtime type checks inserted.
# In a frozen PyInstaller app there is no .py source available (only
# compiled bytecode in the PYZ archive), so inspect.getsource() raises
# OSError: could not get source code, and the import of inflect (and
# therefore the whole TTS stack) crashes.
#
# This hook runs before any of the app's own code, so it replaces
# typeguard.typechecked with a harmless no-op *before* inflect (or
# anything else) does `from typeguard import typechecked`. The decorated
# functions/classes just run without the extra runtime type checks --
# which is fine, since those checks are a dev-time safety net, not
# something your app's behavior depends on.

try:
    import typeguard

    def _typechecked_noop(*args, **kwargs):
        # Supports both @typechecked and @typechecked(...) usage.
        if len(args) == 1 and callable(args[0]) and not kwargs:
            return args[0]

        def _decorator(func_or_class):
            return func_or_class

        return _decorator

    typeguard.typechecked = _typechecked_noop
except ImportError:
    # typeguard not installed / not on the import path yet -- nothing to
    # patch, and inflect will simply fail later with its own error if it
    # actually needs it.
    pass


# Older TTS paths decorated some functions with @torch.jit.script (TorchScript),
# which -- like typeguard above -- needs inspect.getsource() to read the
# function's actual .py source in order to JIT-compile it. That source
# doesn't exist in a frozen app, so it crashes with:
#   OSError: Can't get source for <function ...>. TorchScript requires
#   source access in order to carry out compilation.
#
# Patching torch.jit.script to a no-op means decorated functions just run
# as plain eager-mode Python instead of being compiled -- slightly slower
# per call, but produces identical results, and avoids the crash.
try:
    import torch.jit

    def _script_noop(obj, *args, **kwargs):
        return obj

    torch.jit.script = _script_noop
except ImportError:
    pass
