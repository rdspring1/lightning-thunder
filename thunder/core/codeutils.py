from typing import List, Dict, Tuple, Set, Deque, Any
from numbers import Number
from collections import deque
from collections.abc import Mapping, Sequence, Iterable
import inspect
from inspect import Parameter
import string
import functools
from functools import partial

import thunder.core.baseutils as baseutils
from thunder.core.baseutils import ProxyInterface
import thunder.core.dtypes as dtypes
import thunder.core.devices as devices
from thunder.core.pytree import tree_flatten, tree_unflatten
from thunder.core.baseutils import *


#
# Functions related to analyzing and printing functions and arguments
#


# TODO Document this class
# TODO This could probably be a frozen dataclass
# TODO Add an is_printable check to not construct an object_ctx in that case
# TODO Maybe merge TrackedObject and ContextObject? But they are conceptually distinct...
class TrackedObject:
    def __init__(self, name: str, obj: Any):
        self.name = name
        self.obj = obj


# TODO This can be a frozen dataclass
class ContextObject:
    def __init__(self, name: str, obj: Any):
        self.name = name
        self.obj = obj


Printable = Union[str, ContextObject, ProxyInterface]

_modules_to_shortnames_map = {
    "thunder.torch": "ltorch",
    "thunder.numpy": "lnp",
    "thunder.core.prims": "prims",
}


def module_shortname(module):
    return _modules_to_shortnames_map.get(module, module)


def indent_string(indent):
    tab = "  "
    return f"{tab * indent}"


# TODO Consider adding a Printable interface or protocol
# TODO Consider not printing devices, which are constructed each time, and instead giving them
#   readable names
# TODO Refine imports so that devices can print as Devices("cuda:0") instead of
#   having to qualify as devices.Devices
def is_printable(x: Any) -> Tuple[bool, Optional[Tuple[str, Any]]]:
    if x is None:
        return True, None
    if isinstance(x, TrackedObject):
        return True, None
    if isinstance(x, ContextObject):
        return True, None
    if isinstance(x, ProxyInterface):
        return True, None
    if is_collection(x):
        flat, _ = tree_flatten(x)
        return True, None
        # return all((is_printable(f) for f in flat)), None
    if isinstance(x, str):
        return True, None
    if isinstance(x, dtypes.dtype):
        return True, ("dtypes", dtypes)
    if isinstance(x, devices.Device):
        return True, ("devices", devices)
    if x in (bool, int, float, complex):
        return True, None
    if isinstance(x, Number):
        return True, None

    return False, None


def to_printable(name_generator: Callable, x: Any) -> Tuple[Any, Optional[Tuple[str, Any]]]:
    can_print, module_info = is_printable(x)
    if can_print:
        return x, module_info

    # NOTE Non-printable objects are serialized in the Python context
    name = name_generator()
    co = ContextObject(name, x)
    return co, None


# NOTE This quote marker allows for removal of quotation marks when printing collections
# TODO Review this
_quote_marker = "_@_"


def _qm(s: str, quote_markers: bool) -> str:
    if not quote_markers:
        return s

    return f"{_quote_marker}{s}{_quote_marker}"


# TODO Review prettyprinting other map types like dict -- these need to print strings in a particular way
# TODO Add None support
def prettyprint(x: Any, *, with_type: bool = False, _quote_markers: bool = False) -> str:
    m = partial(_qm, quote_markers=_quote_markers)
    if x is None:
        return m("None")
    if isinstance(x, TrackedObject):
        return m(x.name)
    if isinstance(x, ContextObject):
        return m(x.name)
    if isinstance(x, ProxyInterface):
        # NOTE This doesn't need quote markers because it can't
        #   occur in a collection
        if with_type:
            return f'{x.name}: "{x.type_string()}"'

        return m(x.name)
    if is_collection(x):
        flat, spec = tree_flatten(x)
        printed = tuple(prettyprint(x, with_type=False, _quote_markers=True) for x in flat)
        unflattened = tree_unflatten(printed, spec)
        unflattened_str = str(unflattened)
        # NOTE Collections of strings (so collections of names) print like this --
        #   ('a', 'b') -- but we want them to print like this -- (a, b) --
        #   so this just removes all the single quotes -- this seems super hacky
        unflattened_str = unflattened_str.replace(f"{_quote_marker}'", "")
        unflattened_str = unflattened_str.replace(f"'{_quote_marker}", "")
        return unflattened_str
    if isinstance(x, str):
        return f'"{x}"'
    if isinstance(x, dtypes.dtype):
        return m(f"dtypes.{str(x)})")
    if isinstance(x, devices.Device):
        return m(f'devices.Device("{str(x)}")')
    if x is bool:
        return m("bool")
    if x is int:
        return m("int")
    if x is float:
        return m("float")
    if x is complex:
        return m("complex")
    # TODO Handle complex infinities and nans
    if isinstance(x, Number):
        s: str
        if x == float("inf"):
            s = m("float('inf')")
        elif x == -float("inf"):
            s = m("-float('inf')")
        elif x != x:
            s = m("float('NaN')")
        else:
            s = m(str(x))
        return s

    baseutils.check(
        False, lambda: f"prettyprint doesn't support object {x} of type {type(x)}", exception_type=NotImplementedError
    )


# TODO Review collection handling (along with constraints and tracking)
# TODO Review collection printing
def prettyprint_arg(x, *, with_type=False, meta_lookup=None):
    if is_collection(x):
        flat, tree = tree_flatten(x)
        printed = tuple(prettyprint_arg(f, with_type=False, meta_lookup=meta_lookup) for f in flat)
        coll_str = str(tree_unflatten(printed, tree))

        # NOTE Collections of strings (so collections of names) print like this --
        #   ('a', 'b') -- but we want them to print like this -- (a, b) --
        #   so this just removes all the single quotes -- this is almost certainly hacky
        coll_str = coll_str.replace("'", "")
        return coll_str

    if isinstance(x, ProxyInterface):
        if with_type:
            return f'{x.name}: "{x.type_string()}"'
        else:
            return x.name

    if meta_lookup is not None:
        om: ObjectMeta = meta_lookup(x)
        baseutils.check(om is not None, lambda: f"Could not find metadata for {x}")

        if with_type:
            return f'{om.name}: "{baseutils.print_type(om.type)}"'
        return om.name

    if with_type:
        return f'{x}: "{baseutils.print_type(type(x))}"'

    return str(x)


def prettyprint_args(*args, with_type=False, meta_lookup=None):
    return ", ".join(prettyprint_arg(arg, with_type=with_type, meta_lookup=meta_lookup) for arg in args)


def prettyprint_kwarg(key: str, value: Any, *, meta_lookup: Callable) -> str:
    # TODO Refactor this in common with prettyprint_arg
    if is_collection(value):
        flat, tree = tree_flatten(value)
        printed = tuple(prettyprint_arg(f, with_type=False, meta_lookup=meta_lookup) for f in flat)
        coll_str = str(tree_unflatten(printed, tree))

        # NOTE Collections of strings (so collections of names) print like this --
        #   ('a', 'b') -- but we want them to print like this -- (a, b) --
        #   so this just removes all the single quotes -- this is almost certainly hacky
        coll_str = coll_str.replace("'", "")
        return f"{key}={coll_str}"

    om: ObjectMeta = meta_lookup(value)
    return f"{key}={om.name}"


def prettyprint_kwargs(kwargs: dict, *, meta_lookup: Callable) -> str:
    return ", ".join(prettyprint_kwarg(k, v, meta_lookup=meta_lookup) for k, v in kwargs.items())


# TODO Make this a frozen dataclass?
class SigInfo:
    def __init__(self, name):
        self.name = name
        self.args = []
        self.varargs = None
        self.kwargs = {}
        self.varkwargs = None
        self.defaultdict = {}

    def __repr__(self):
        return f"[SigInfo args={self.args}, varargs={self.varargs}, kwargs={self.kwargs}, varkwargs={self.varkwargs}]"

    # NOTE This prints the original signature, not the bound signature
    # TODO Maybe be clear about what inputs are const and what aren't?
    # TODO Add type annotations
    # TODO Better support default arguments that can't be serialized as simple Python objects like strings and
    #   numbers -- this may require including a Python ctx to support arbitrary object defaults
    def prettyprint(self):
        def _arg_printer(name: str, has_default: bool, default: Any = None) -> str:
            if has_default:
                can_print, _ = is_printable(default)
                check(
                    can_print,
                    lambda: f"Only signatures with printable defaults are currently supported, but found {default=} that is not serializable as a string",
                )
                return f"{name}={default}"
            return name

        args = []

        for name, _ in self.args:
            printed = _arg_printer(name, name in self.defaultdict, self.defaultdict.get(name, None))
            args.append(printed)

        if self.varargs is not None:
            varargs_name, _ = self.varargs
            args.append(f"*{varargs_name}")

        # Writes the keyword-only marker
        if self.varargs is None and len(self.kwargs.items()) > 0:
            args.append("*")

        for name, _ in self.kwargs.items():
            printed = _arg_printer(name, name in self.defaultdict, self.defaultdict.get(name, None))
            args.append(printed)

        if self.varkwargs is not None:
            varkwargs_name, _ = self.varkwargs
            args.append(f"**{varkwargs_name}")

        arg_str = ", ".join(args)

        return f"def {self.name}({arg_str}):"


# Creates a SigInfo object from a function and the inputs to it
# The SigInfo object contains name and value information for the args, varargs, kwargs, and varkwargs
#   given to a function.
# To call a function foo from its SigInfo, you can do the following:
#
# arg_values = tuple(x[1] for x in si.args)
# if si.varargs is not None:
#     arg_values = arg_values + si.varargs[1]
# kwarg_values = si.kwargs
# if si.varkwargs is not None:
#     kwarg_values.update(si.varkwargs[1])
# foo(*arg_values, **kwarg_values)
#
# This removes the name information and combines the args and varargs into arg_values,
#   and the kwargs and varkwargs into kwarg_values


# TODO Review errors and improve message quality (ex. too many arguments error)
def get_siginfo(fn, args, kwargs):
    # TODO Hacky way to extract meta function from Symbol objects
    #   This should probably use a SymbolInterface, or Symbol should define __name__
    if hasattr(fn, "meta"):
        fn = fn.meta

    # Binds args and kwargs to signature
    sig = inspect.signature(fn)

    # print(f"{sig=}")
    # print(f"{len(args)=}")
    # print(f"{args=}")
    # print(f"{kwargs=}")

    ba = sig.bind(*args, **kwargs)

    # Augments arguments with default values
    # NOTE: for example, alpha=1., if alpha is not specified
    #   explicitly then ba above will not contain it
    default_dict = {}

    args_dict = {k: v.default for k, v in sig.parameters.items() if v.default is not Parameter.empty}

    default_dict.update(args_dict)
    args_dict.update(ba.arguments)

    # Augments the parameters with positional information
    params_with_indices = {k: (v, idx) for idx, (k, v) in enumerate(sig.parameters.items())}

    # Constructs signature information

    # TODO Is there a better way to do this?
    # TODO Consider refactoring name extraction
    # Acquires the name of the function
    # NOTE Not all callables define __name__, including objects that define __call__ and
    #   objects created with functools.partial
    #   This "unwraps" partial objects until the original function is found, and the
    #   name is taken from it

    fn_ = fn
    while isinstance(fn_, functools.partial):
        fn_ = fn_.func
    name = fn_.__name__

    si = SigInfo(name)

    for name, x in args_dict.items():
        p, idx = params_with_indices[name]
        pkind = p.kind

        if pkind in (Parameter.POSITIONAL_ONLY, Parameter.POSITIONAL_OR_KEYWORD):
            si.args.append((x, idx, name))
        elif pkind is Parameter.VAR_POSITIONAL:
            si.varargs = (name, x)
        elif pkind is Parameter.KEYWORD_ONLY:
            si.kwargs[name] = x
        elif pkind is Parameter.VAR_KEYWORD:
            si.varkwargs = (name, x)
        else:
            raise ValueError(f"Unexpected parameter kind {pkind}")

    si.args = sorted(si.args, key=lambda x: x[1])
    si.args = tuple((x[2], x[0]) for x in si.args)

    si.defaultdict = default_dict

    return si
