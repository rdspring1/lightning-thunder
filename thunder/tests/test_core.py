import operator
import traceback
from functools import partial, reduce
from itertools import product

import pytest
import torch
from looseversion import LooseVersion
from torch.testing import assert_close, make_tensor
from types import FunctionType

import thunder
from thunder import last_traces, cache_mode, cache_hits, cache_misses
import thunder.examine as examine
import thunder.clang as clang
import thunder.core.proxies as proxies
import thunder.torch as ltorch
import thunder.executors.torchex as torchex
import thunder.core.codeutils as codeutils
from thunder.core.pytree import tree_flatten_only, tree_unflatten
from thunder.tests.framework import instantiate, NOTHING, TorchExecutor, nvFuserExecutor, requiresCUDA
import thunder.core.dtypes as dtypes
import thunder.core.devices as devices
import thunder.core.prims as prims
from thunder.core.trace import TraceCtx, set_tracectx, reset_tracectx, tracectx

#
# Tests related to running valid Python programs
#


# Tests that traces don't generate duplicate names
#   (at least not within the first 10k names tested below)
def test_name_generation():
    # NOTE This function is just because trace's currently require a function to
    #   construct them
    def foo():
        pass

    trace = TraceCtx(foo)

    names = set()
    for ctr in range(10000):
        name = trace._gen_name(ctr)
        assert name not in names, f"Found duplicate name {name}"

        names.add(name)


@instantiate(dtypes=(thunder.float32,))
def test_integer_isinstance_mimicry(executor, device: str, dtype: dtypes.dtype):
    # isinstance() works as expected
    def foo(a, b, c):
        if isinstance(a, int):
            return clang.add(a, b)

        return clang.add(b, c)

    traced_foo = executor.make_callable(foo)

    tdtype = ltorch.to_torch_dtype(dtype)
    a = make_tensor((2, 1), device=device, dtype=tdtype)
    b = make_tensor((2, 2), device=device, dtype=tdtype)
    c = make_tensor((1, 2), device=device, dtype=tdtype)

    thunder_result = traced_foo(a, b, c)
    torch_result = b + c
    assert_close(thunder_result, torch_result)

    thunder_result = traced_foo(2, b, c)
    torch_result = 2 + b
    assert_close(thunder_result, torch_result)

    # type() doesn't work (it returns the actual type)
    def bar(a, b, c):
        if type(a) is int:
            return clang.add(a, b)

        return clang.add(b, c)

    traced_bar = executor.make_callable(bar)

    try:
        thunder_result = traced_bar(a, b, c)
        torch_result = b + c
        assert_close(thunder_result, torch_result)
        pytest.fail()
    except BaseException:
        pass

    try:
        thunder_result = traced_bar(2, b, c)
        torch_result = 2 + b
        assert_close(thunder_result, torch_result)
        pytest.fail()
    except BaseException:
        pass


# TODO Subsume this by test_elementwise when sample inputs are expanded to include more numbers
@instantiate(dtypes=NOTHING)
def test_integer_return(executor, device, _):
    if executor == nvFuserExecutor:
        pytest.xfail("nvFuser does not support only scalar outputs")

    def foo(a, b):
        return clang.add(a, b)

    traced_foo = executor.make_callable(foo)

    thunder_result = traced_foo(3, 4)
    python_result = 3 + 4
    assert_close(thunder_result, python_result)


@instantiate(dtypes=(thunder.float32,))
def test_crazy_collections_in_and_out(executor, device, dtype):
    def foo(a, b, c, *, ka, kb, kc):
        d = {
            5: 2,
            7: 9,
            "a": [a, b],
            "b": {"a": a, "b": b, "c": [b, (a, c)]},
            "x": (a, [a, a, a], (b, (a, a, c, b))),
        }

        e = a["a"]["a"] + b[0]
        f = c[1]["c"] + b[1]
        g = e + f
        h = f + ka + kb
        i = ka + ka  # NOTE: not returned (ignored computation)
        j = kc[0] + kc[1]

        d["j"] = j

        return (
            a,
            (g,),
            (((j,),),),
            g,
            g,
            b,
            e,
            d["j"],
            (f, d, c, (d,), c, {"a": a, 5: f, "b": h}),
            (5,),
            (),
            (a,),
            [5, a, (b,), (), {}],
            {},
        )

    traced_foo = executor.make_callable(foo)
    tdtype = ltorch.to_torch_dtype(dtype)

    a = make_tensor((2,), device=device, dtype=tdtype)
    b = make_tensor((2, 2, 2), device=device, dtype=tdtype)
    c = make_tensor((2, 2), device=device, dtype=tdtype)

    args = ({"a": {"a": a}}, (b, c), (3, {"c": c}))
    kwargs = {"ka": b, "kb": 3.0, "kc": (a, 2)}
    thunder_result = traced_foo(*args, **kwargs)
    torch_result = foo(*args, **kwargs)

    assert_close(thunder_result, torch_result)


@instantiate(dtypes=(thunder.float32,))
def test_nested_empty_tuple_unpack(executor, device, dtype):
    def foo(a):
        pass

    cfoo = executor.make_callable(foo)
    torch_dtype = ltorch.to_torch_dtype(dtype)

    a = make_tensor((2, 2), device=device, dtype=torch_dtype)

    inp = {
        0: (
            (
                a,
                a,
            ),
            [a, (a, a), {}],
            {},
            (),
        )
    }

    cfoo(inp)


@instantiate(dtypes=(thunder.float32,))
def test_varargs(executor, device, dtype):
    def foo(*args):
        return reduce(operator.add, args)

    traced_foo = executor.make_callable(foo)
    tdtype = ltorch.to_torch_dtype(dtype)

    a = make_tensor((2,), device=device, dtype=tdtype)
    packed = (a, a, a, a, a)

    thunder_result = traced_foo(*packed)
    torch_result = foo(*packed)

    assert_close(thunder_result, torch_result)


@instantiate(dtypes=(thunder.float32,))
def test_kwargs(executor, device, dtype):
    def foo(**kwargs):
        return kwargs["a"] + kwargs["b"]

    traced_foo = executor.make_callable(foo)
    tdtype = ltorch.to_torch_dtype(dtype)

    a = make_tensor((2,), device=device, dtype=tdtype)
    b = make_tensor((2,), device=device, dtype=tdtype)

    thunder_result = traced_foo(a=a, b=b)
    torch_result = foo(a=a, b=b)

    assert_close(thunder_result, torch_result)


@instantiate(dtypes=(thunder.float32,))
def test_varargs_and_kwargs(executor, device, dtype):
    def foo(a, b, *posargs, e, **kwargs):
        accum = a
        for x in posargs:
            accum = a + x

        d = b + e + kwargs["f"]

        return accum, d, kwargs["g"]

    traced_foo = executor.make_callable(foo)
    tdtype = ltorch.to_torch_dtype(dtype)

    a = make_tensor((2,), device=device, dtype=tdtype)
    b = make_tensor((2, 2, 2), device=device, dtype=tdtype)
    c = make_tensor((2, 2), device=device, dtype=tdtype)
    d = make_tensor((2,), device=device, dtype=tdtype)
    e = make_tensor((2,), device=device, dtype=tdtype)
    f = make_tensor((2,), device=device, dtype=tdtype)
    g = make_tensor((2,), device=device, dtype=tdtype)

    thunder_result = traced_foo(a, b, c, d, e=e, f=f, g=g)
    torch_result = foo(a, b, c, d, e=e, f=f, g=g)

    assert_close(thunder_result, torch_result)


@instantiate(dtypes=(thunder.float32,))
def test_no_return(executor, device, dtype):
    def foo(a, b):
        c = a + b
        pass

    traced_foo = executor.make_callable(foo)
    tdtype = ltorch.to_torch_dtype(dtype)

    a = make_tensor((2,), device=device, dtype=tdtype)
    b = make_tensor((2, 2, 2), device=device, dtype=tdtype)

    thunder_result = traced_foo(a, b=b)
    torch_result = foo(a, b)

    assert_close(thunder_result, torch_result)


@instantiate(dtypes=NOTHING)
def test_no_input(executor, device, dtype):
    def foo():
        return 3, ()

    traced_foo = executor.make_callable(foo)

    thunder_result = traced_foo()
    torch_result = foo()

    assert_close(thunder_result, torch_result)


@instantiate(dtypes=(thunder.float32,))
def test_no_compute(executor, device, dtype):
    def foo(a, b):
        return a, 3.0

    traced_foo = executor.make_callable(foo)
    tdtype = ltorch.to_torch_dtype(dtype)

    a = make_tensor((2,), device=device, dtype=tdtype)
    b = make_tensor((2, 2, 2), device=device, dtype=tdtype)

    thunder_result = traced_foo(a, b=b)
    torch_result = foo(a, b)

    assert_close(thunder_result, torch_result)


@instantiate(dtypes=(thunder.float32,))
def test_strings_in_and_out(executor, device, dtype):
    def foo(a, b, c="ok"):
        return a, b, "hello"

    cfoo = executor.make_callable(foo)

    lc_result = cfoo("a", b="b")
    assert lc_result == ("a", "b", "hello")


@instantiate(dtypes=(thunder.float32,))
def test_objects_in_and_out(executor, device, dtype):
    a = object()
    b = object()
    c = object()

    def foo(a, b, c=c):
        return a, b, object()

    cfoo = executor.make_callable(foo)

    lc_result = cfoo(a, b=b)
    a, b, c = lc_result

    assert type(a) is object
    assert type(b) is object
    assert type(c) is object


@instantiate(dtypes=(thunder.float32,))
def test_devices_in_and_out(executor, device, dtype):
    dev = thunder.devices.Device(device)

    def foo(a, dev=dev):
        return a, dev

    cfoo = executor.make_callable(foo)

    lc_result = cfoo(1, dev)

    x, y = lc_result

    assert x == 1
    assert y == dev


@instantiate(dtypes=(thunder.float32,))
def test_partial(executor, device, dtype):
    def foo(a, *, b, c=2):
        return a, b, c

    pfoo = partial(foo, b=3, c=4)
    cpfoo = executor.make_callable(pfoo)

    lc_result = cpfoo(1)
    py_result = pfoo(1)

    assert_close(lc_result, py_result)

    # Tests that later partials override earlier partials correctly
    ppfoo = partial(pfoo, b=2, c=8)
    cppfoo = executor.make_callable(ppfoo)

    lc_result = cppfoo(1)
    py_result = ppfoo(1)

    assert_close(lc_result, py_result)


# Tests that partials that specify default args are not supported (yet)
@instantiate(dtypes=(thunder.float32,))
def test_partial_args(executor, device, dtype):
    def foo(a, b):
        return a + b

    torch_dtype = ltorch.to_torch_dtype(dtype)
    a = make_tensor((2, 2), device=device, dtype=torch_dtype)
    b = make_tensor((2, 2), device=device, dtype=torch_dtype)

    pfoo = partial(foo, a)

    with pytest.raises(NotImplementedError):
        cpfoo = executor.make_callable(pfoo)
        cpfoo(b)


@instantiate(dtypes=(thunder.float32,))
def test_constant_creation(executor, device, dtype):
    def foo(a):
        x = prims.convert_element_type(1, float)
        return a + x

    cfoo = thunder.compile_with_info(foo, executors_list=executor.executors_list())

    torch_dtype = ltorch.to_torch_dtype(dtype)
    a = make_tensor((2, 2), device=device, dtype=torch_dtype)

    lc_result, traces = cfoo(a)
    python_result = foo(a)

    assert_close(lc_result, python_result)

    for trace in traces:
        fn = trace.python_callable()
        lc_result = fn(a)
        assert_close(lc_result, python_result)


#
# Tests related to printing signatures and bound symbols
#


def test_siginfo_printing():
    def foo(a=object(), b=torch.float32, *, c=(object(), object())):
        return a, b, c[0], a, c

    siginfo = codeutils.get_siginfo(foo, (), {})

    s0 = siginfo.prettyprint()
    s1 = siginfo.prettyprint()

    assert s0 == s1

    trace = TraceCtx(foo)
    with tracectx(trace):
        s0 = trace.python()
        s1 = trace.python()

        assert s0 == s1


def test_consistent_trace_and_boundsymbol_printing():
    def foo(a=object(), b=(torch.float32, object())):
        return a, b, b[1]

    cfoo = thunder.compile_with_info(foo)
    result, traces = cfoo()

    # Tests consistent printing of traces
    s0 = str(traces[0])
    s1 = str(traces[0])

    assert s0 == s1

    # Tests consistent printing of bound symbols outside the trace context
    for bsym in traces[0].bound_symbols:
        s0 = str(bsym)
        s1 = str(bsym)
        assert s0 == s1


def test_consistent_boundsymbol_collection_printing():
    def foo(tup, d):
        (a, b), c = tup
        e = c + d["dict"]["val"]
        return a + b, e

    cfoo = thunder.compile_with_info(foo)
    result, traces = cfoo(((2, 3), 4), {"dict": {"val": 2}})

    # Tests consistent printing of bound symbols outside the trace context
    for bsym in traces[0].bound_symbols:
        s0 = str(bsym)
        s1 = str(bsym)
        assert s0 == s1


def test_consistent_boundsymbol_collection_hard_printing():
    def foo(tup):
        (a, b), c = tup
        d = b["dict"]["val"]
        return a + d, c

    cfoo = thunder.compile_with_info(foo)
    result, traces = cfoo(((2, {"dict": {"val": 2}}), 4))

    # Tests consistent printing of bound symbols outside the trace context
    for bsym in traces[0].bound_symbols:
        s0 = str(bsym)
        s1 = str(bsym)
        assert s0 == s1


#
# Tests of operator -> executor bindings
#


# This test verifies that all torch operators have direct lowerings to the torch executor
#   (or it's OK that they don't)
def test_direct_torch_lowerings():
    torch_symbols = set(sym.id for sym in ltorch._torch_to_thunder_function_map.values())
    direct_torchex_lowerings = set(torchex._ops_map.keys())

    # These operators need additional scrutiny for how to directly lower them or have been
    #   grandfathered in and just need straightforward lowerings added
    # This is tracked by https://github.com/Lightning-AI/lightning-thunder/issues/581
    allowed_exceptions = {
        # These reduction operators need additional scrutiny because PyTorch has not always been
        #   consistent in handling dims
        "torch.amax",
        "torch.amin",
        "torch.mean",
        "torch.prod",
        "torch.sum",
        "torch.var_mean",
        # These operators just need their lowerings added
        "torch.nn.functional.silu",
        "torch.true_divide",
        "torch.bmm",
        "torch.matmul",
        "torch.ones_like",
        "torch.nn.functional.linear",
        "torch.Tensor.view",
        "torch.Tensor.type_as",
        "torch.ops.aten.embedding_backward",
        "torch.index_select",
        "torch.stack",
        "torch.index_add",
        "torch.scatter_add",
        "torch.full",
        "torch.flatten",
        "torch.zeros",
        "torch.ones",
        "torch.outer",
        "torch.zeros_like",
        "torch.take_along_dim",
    }

    missing_lowerings = torch_symbols - direct_torchex_lowerings - allowed_exceptions

    assert (
        len(missing_lowerings) == 0
    ), f"the following torch symbols are missing direct torchex lowerings {missing_lowerings}"

    mislabeled = allowed_exceptions & direct_torchex_lowerings
    assert len(mislabeled) == 0, f"the following allowed exceptions actually have direct lowerings {mislabeled}"


#
# Type promotion tests
#
# TODO Maybe move to test_type_promotion.py?


# TODO This test just spot-checks type promotion -- it could probably be better
@instantiate(dtypes=NOTHING)
def test_type_promotion_tensors(executor, device, _):
    if executor == TorchExecutor:
        pytest.xfail("https://github.com/Lightning-AI/lightning-thunder/issues/406")

    def foo(a, b):
        return a + b

    traced_foo = executor.make_callable(foo)

    b1 = make_tensor((2, 2), device=device, dtype=torch.bool)
    i64 = make_tensor((2, 2), device=device, dtype=torch.int64)
    bf16 = make_tensor((2, 2), device=device, dtype=torch.bfloat16)
    f16 = make_tensor((2, 2), device=device, dtype=torch.float16)
    f32 = make_tensor((2, 2), device=device, dtype=torch.float32)

    # float16 x float16 type promotion -- float16 result dtype
    result = traced_foo(f16, f16)
    assert result.dtype is torch.float16

    # float16 x float32 type promotion -- float32 result dtype
    result = traced_foo(f16, f32)
    assert result.dtype is torch.float32

    # float16 x bfloat16 type promotion -- float32 result dtype
    result = traced_foo(f16, bf16)
    assert result.dtype is torch.float32

    # int64 x float16 type promotion -- float16 result dtype
    result = traced_foo(f16, i64)
    assert result.dtype is torch.float16

    # bool x int64 type promotion -- int64 result dtype
    result = traced_foo(b1, i64)
    assert result.dtype is torch.int64

    # f x int64 type promotion -- float result dtype
    result = traced_foo(2.0, i64)
    assert result.dtype is torch.float32

    # b1 x int64 type promotion -- int64 result dtype
    result = traced_foo(b1, i64)
    assert result.dtype is torch.int64

    def bar(a, b, c):
        return a - b + c

    traced_bar = executor.make_callable(bar)

    # float x int64 x float16 type promotion -- float16 result dtype
    result = traced_bar(2.0, i64, f16)
    assert result.dtype is torch.float16

    # float x int x int64 -- float32 result dtype
    result = traced_bar(2.1, -1, i64)
    assert result.dtype is torch.float32


@instantiate(dtypes=NOTHING)
def test_type_promotion_numbers_and_tensors(executor, device, _):
    if executor == TorchExecutor:
        pytest.xfail("https://github.com/Lightning-AI/lightning-thunder/issues/406")

    def foo(a, b, c):
        return a + b + c

    cfoo = executor.make_callable(foo)

    f16 = make_tensor((2, 2), device=device, dtype=torch.float16)
    f32 = make_tensor((2, 2), device=device, dtype=torch.float32)
    i64 = make_tensor((2, 2), device=device, dtype=torch.int64)

    result = cfoo(5, f32, 2)
    assert result.dtype is torch.float32

    result = cfoo(f32, 1, f32)
    assert result.dtype is torch.float32

    result = cfoo(i64, 3.0, f16)
    assert result.dtype is torch.float16

    result = cfoo(i64, 3.0, i64)
    assert result.dtype is torch.float32


@instantiate(dtypes=NOTHING)
def test_int_to_float_type_promotion(executor, device, _):
    def foo(a, b):
        return a / b

    cfoo = executor.make_callable(foo)

    i64 = make_tensor((2, 2), device=device, dtype=torch.int64)
    f16 = make_tensor((2, 2), device=device, dtype=torch.float16)

    # int64 x int64 -- float32 result dtype
    result = cfoo(i64, i64)
    assert result.dtype is torch.float32

    # int x int64 -- float32 result dtype
    result = cfoo(2, i64)
    assert result.dtype is torch.float32

    # int64 x bool -- float32 result dtype
    result = cfoo(i64, True)
    assert result.dtype is torch.float32

    # int64 x float16 -- float16 result dtype
    result = cfoo(i64, f16)
    assert result.dtype is torch.float16


#
# Caching tests
#


def test_static_caching_errors():
    def foo():
        pass

    # Verifies that multiple cache types cannot be specified simultaneously
    with pytest.raises(RuntimeError):
        thunder.compile(foo, use_static_caching=True, use_last_executed=True)


@instantiate(dtypes=(thunder.float32,))
def test_static_caching(executor, device: str, dtype: dtypes.dtype):
    torch_dtype = ltorch.to_torch_dtype(dtype)
    a = make_tensor((2, 2), device=device, dtype=torch_dtype)
    b = make_tensor((2, 2), device=device, dtype=torch_dtype)
    c = make_tensor((2, 2), device=device, dtype=torch_dtype)
    d = make_tensor((2, 1), device=device, dtype=torch_dtype)
    e = make_tensor((2, 2), device=device, dtype=torch.bool)

    for disable_preprocessing in (True, False):

        def foo(a, b):
            return a + b

        cfoo = thunder.compile(foo, disable_preprocessing=disable_preprocessing, use_static_caching=True)

        assert cache_mode(cfoo) == thunder.CACHE_MODES.STATIC

        # Tensor x tensor
        result = cfoo(a, b)
        assert cache_misses(cfoo) == 1
        assert cache_hits(cfoo) == 0
        assert_close(result, a + b)

        # Same tensors -- cache hit
        result = cfoo(a, b)
        assert cache_misses(cfoo) == 1
        assert cache_hits(cfoo) == 1
        assert_close(result, a + b)

        # Different tensor, same metadata -- cache hit
        result = cfoo(a, c)
        assert cache_misses(cfoo) == 1
        assert cache_hits(cfoo) == 2
        assert_close(result, a + c)

        # Different tensor, different shape -- cache miss
        result = cfoo(a, d)
        assert cache_misses(cfoo) == 2
        assert cache_hits(cfoo) == 2
        assert_close(result, a + d)

        # Different tensor, different dtype -- cache miss
        result = cfoo(a, e)
        assert cache_misses(cfoo) == 3
        assert cache_hits(cfoo) == 2
        assert_close(result, a + e)

        # Tensor x float number -- cache miss
        result = cfoo(a, 1.0)
        assert cache_misses(cfoo) == 4
        assert cache_hits(cfoo) == 2
        assert_close(result, a + 1.0)

        # Tensor x float number, different tensor data -- cache hit
        result = cfoo(b, 1.0)
        assert cache_misses(cfoo) == 4
        assert cache_hits(cfoo) == 3
        assert_close(result, b + 1.0)

        # Tensor x float number, different number value -- cache miss
        result = cfoo(b, 3.0)
        assert cache_misses(cfoo) == 5
        assert cache_hits(cfoo) == 3
        assert_close(result, b + 3.0)

        # Tensor x int number, different number type -- cache miss
        result = cfoo(b, 3)
        assert cache_misses(cfoo) == 6
        assert cache_hits(cfoo) == 3
        assert_close(result, b + 3)

        # Tensor x int number -- cache hit
        result = cfoo(b, 3)
        assert cache_misses(cfoo) == 6
        assert cache_hits(cfoo) == 4
        assert_close(result, b + 3)

    def bar(a, b):
        return a, b

    cbar = thunder.compile(bar, use_static_caching=True)

    astr = "a"
    bstr = "b"

    # String x string -- cache miss
    cbar(astr, bstr)
    assert cache_misses(cbar) == 1
    assert cache_hits(cbar) == 0

    # Same strings -- cache hit
    cbar(astr, bstr)
    assert cache_misses(cbar) == 1
    assert cache_hits(cbar) == 1

    # Same string values -- different strings
    bother_str = "b"
    cbar(astr, bother_str)
    assert cache_misses(cbar) == 1
    assert cache_hits(cbar) == 2

    # Object x string -- cache miss
    cbar(object(), bother_str)
    assert cache_misses(cbar) == 2
    assert cache_hits(cbar) == 2

    # object() != object() -- cache miss
    cbar(object(), bother_str)
    assert cache_misses(cbar) == 3
    assert cache_hits(cbar) == 2

    # Module tests
    m = torch.nn.Linear(5, 5, device=device, dtype=torch_dtype)
    cm = thunder.compile(m, use_static_caching=True)

    inp = make_tensor((5, 5), device=device, dtype=torch_dtype)

    result = cm(inp)
    torch_result = m(inp)

    assert_close(result, torch_result)

    assert cache_misses(cm) == 1
    assert cache_hits(cm) == 0

    # Same input -- cache hit

    result = cm(inp)
    torch_result = m(inp)

    assert_close(result, torch_result)

    assert cache_misses(cm) == 1
    assert cache_hits(cm) == 1

    # Different input, same metadata -- cache hit
    inp = make_tensor((5, 5), device=device, dtype=torch_dtype)
    result = cm(inp)
    torch_result = m(inp)

    assert_close(result, torch_result)

    assert cache_misses(cm) == 1
    assert cache_hits(cm) == 2

    # Different input, different metadata -- cache miss
    inp = make_tensor((6, 5), device=device, dtype=torch_dtype)
    result = cm(inp)
    torch_result = m(inp)

    assert_close(result, torch_result)

    assert cache_misses(cm) == 2
    assert cache_hits(cm) == 2


#
# Tests related to optimizing passes
#
# TODO Maybe move to test_passes.py? test_nvfuser.py?


@instantiate(executors=(nvFuserExecutor,), dtypes=(thunder.float32,))
def test_redundant_cast_basic(executor, device: str, dtype: dtypes.dtype):
    torch_dtype = ltorch.to_torch_dtype(dtype)
    a = make_tensor((2, 2), device=device, dtype=torch_dtype)

    def foo(a):
        b = a.to(torch.float16)
        c = b.to(torch.float64)
        return c

    cfoo = thunder.compile(foo)
    cfoo(a)

    traces = thunder.last_traces(cfoo)
    extrace = traces[-1]
    fusions = examine.get_fusion_symbols(extrace)

    # Verifies that there is a single fusion with only one operation
    assert len(fusions) == 1
    fusion = fusions[0]
    assert len(fusion.subsymbols) == 1

    # Tests a longer chain of operations
    def bar(a):
        b = a.to(torch.float16)
        c = b.to(torch.float64)
        d = c.to(torch.float32)
        e = d.to(torch.float16)
        return e

    cbar = thunder.compile(bar)
    cbar(a)

    traces = thunder.last_traces(cbar)
    extrace = traces[-1]
    fusions = examine.get_fusion_symbols(extrace)

    # Verifies that there is a single fusion with only one operation
    assert len(fusions) == 1
    fusion = fusions[0]
    assert len(fusion.subsymbols) == 1


@instantiate(executors=(nvFuserExecutor,), dtypes=(thunder.float32,))
def test_redundant_intermediate_consumers(executor, device: str, dtype: dtypes.dtype):
    torch_dtype = ltorch.to_torch_dtype(dtype)
    a = make_tensor((2, 2), device=device, dtype=torch_dtype)

    def foo(a):
        b = a.to(torch.float64)
        c = b + 5
        d = b.to(torch.float16)
        return c, d

    cfoo = thunder.compile(foo)
    cfoo(a)

    traces = thunder.last_traces(cfoo)
    extrace = traces[-1]
    fusions = examine.get_fusion_symbols(extrace)

    # Verifies that there is a single fusion with three each operation
    assert len(fusions) == 1
    fusion = fusions[0]
    assert len(fusion.subsymbols) == 3

    # Verifies that the second conversion consumes the output of the first conversion
    #   (because the first conversion's output is used in an intermediate operation)
    assert fusion.subsymbols[-1].args[0].name == "a"


@instantiate(executors=(nvFuserExecutor,), dtypes=(thunder.float32,))
def test_redundant_no_op(executor, device: str, dtype: dtypes.dtype):
    torch_dtype = ltorch.to_torch_dtype(dtype)
    a = make_tensor((2, 2), device=device, dtype=torch_dtype)

    def foo(a):
        return a.to(torch.float32)

    cfoo = thunder.compile(foo)
    cfoo(a)

    traces = thunder.last_traces(cfoo)
    extrace = traces[-1]
    fusions = examine.get_fusion_symbols(extrace)

    # Verifies that no operations are performed
    assert len(fusions) == 0

    def bar(a):
        b = a.to(torch.float32)
        c = b.to(torch.float64)
        d = c.to(torch.float16)
        e = c.to(torch.float16)
        f = b.to(torch.float32)
        g = d.to(torch.float32)
        return d, e, f, g

    cbar = thunder.compile(bar)
    cbar(a)

    traces = thunder.last_traces(cbar)
    extrace = traces[-1]
    fusions = examine.get_fusion_symbols(extrace)

    # Verifies a single fusion of two operations
    assert len(fusions) == 1
    fusion = fusions[0]
    assert len(fusion.subsymbols) == 1

    # Verifies that the trace outputs are updated properly
    t1, t2, a0, a1 = extrace.output
    assert t1.name == "t1"
    assert t2.name == "t1"
    assert a0.name == a1.name == "a"


# Tests that two separated nvFuser regions can be merged when they don't depend
#   on an intermediate PyTorch region
# TODO Create a testing operator that can only be executed by PyTorch so that
#   these tests don't rely on matmul not being executable by nvFuser
# TODO Explicitly use the nvFuserExecutor in these tests
#   (by creating executor.make_callable_with_info?)
@instantiate(executors=(nvFuserExecutor,), dtypes=(thunder.float32,))
def test_nvfuser_toposort_basic(executor, device: str, dtype: dtypes.dtype):
    torch_dtype = ltorch.to_torch_dtype(dtype)
    a = make_tensor((2, 2), device=device, dtype=torch_dtype)
    b = make_tensor((2, 2), device=device, dtype=torch_dtype)

    def foo(a, b):
        c = a + b
        d = a @ b
        e = a - b

        return c, d, e

    cfoo = thunder.compile_with_info(foo)

    result, traces = cfoo(a, b)

    fusions = examine.get_fusions(traces[-1])

    assert len(fusions) == 1


# Tests that three separated nvFuser regions can be merged when they have no
#   dependencies
@instantiate(executors=(nvFuserExecutor,), dtypes=(thunder.float32,))
def test_nvfuser_toposort_independent(executor, device: str, dtype: dtypes.dtype):
    torch_dtype = ltorch.to_torch_dtype(dtype)
    a = make_tensor((2, 2), device=device, dtype=torch_dtype)
    b = make_tensor((2, 2), device=device, dtype=torch_dtype)

    def foo(a, b):
        c = a + b
        d = a @ b
        e = a - b
        f = b @ a
        g = a * b

        return c, d, e, f, g

    cfoo = thunder.compile_with_info(foo)

    result, traces = cfoo(a, b)

    fusions = examine.get_fusions(traces[-1])

    assert len(fusions) == 1


# Tests that three separated nvFuser regions can be merged when the middle region
#   depends on the first region
@instantiate(executors=(nvFuserExecutor,), dtypes=(thunder.float32,))
def test_nvfuser_toposort_dependent0(executor, device: str, dtype: dtypes.dtype):
    torch_dtype = ltorch.to_torch_dtype(dtype)
    a = make_tensor((2, 2), device=device, dtype=torch_dtype)
    b = make_tensor((2, 2), device=device, dtype=torch_dtype)

    def foo(a, b):
        c = a + b
        d = a @ b
        e = a - c
        f = b @ a
        g = a * b

        return c, d, e, f, g

    cfoo = thunder.compile_with_info(foo)

    result, traces = cfoo(a, b)

    fusions = examine.get_fusions(traces[-1])

    assert len(fusions) == 1


# Tests that three separated nvFuser regions can be merged when the middle
#   and final regions depend on the first one
@instantiate(executors=(nvFuserExecutor,), dtypes=(thunder.float32,))
def test_nvfuser_toposort_dependent1(executor, device: str, dtype: dtypes.dtype):
    torch_dtype = ltorch.to_torch_dtype(dtype)
    a = make_tensor((2, 2), device=device, dtype=torch_dtype)
    b = make_tensor((2, 2), device=device, dtype=torch_dtype)

    def foo(a, b):
        c = a + b
        d = a @ b
        e = a - c
        f = b @ a
        g = c * b

        return c, d, e, f, g

    cfoo = thunder.compile_with_info(foo)

    result, traces = cfoo(a, b)

    fusions = examine.get_fusions(traces[-1])

    assert len(fusions) == 1


# Tests that three separated nvFuser regions can be merged when each region
#   depends on the other
@instantiate(executors=(nvFuserExecutor,), dtypes=(thunder.float32,))
def test_nvfuser_toposort_dependent2(executor, device: str, dtype: dtypes.dtype):
    torch_dtype = ltorch.to_torch_dtype(dtype)
    a = make_tensor((2, 2), device=device, dtype=torch_dtype)
    b = make_tensor((2, 2), device=device, dtype=torch_dtype)

    def foo(a, b):
        c = a + b
        d = a @ b
        e = a - c
        f = b @ a
        g = c * e

        return c, d, e, f, g

    cfoo = thunder.compile_with_info(foo)

    result, traces = cfoo(a, b)

    fusions = examine.get_fusions(traces[-1])

    assert len(fusions) == 1


# Tests that three separated nvFuser regions can be merged when the first region
#   is entirely consumed by later regions
@instantiate(executors=(nvFuserExecutor,), dtypes=(thunder.float32,))
def test_nvfuser_toposort_dependent3(executor, device: str, dtype: dtypes.dtype):
    torch_dtype = ltorch.to_torch_dtype(dtype)
    a = make_tensor((2, 2), device=device, dtype=torch_dtype)
    b = make_tensor((2, 2), device=device, dtype=torch_dtype)

    def foo(a, b):
        c = a + b
        d = a @ b
        e = a - c
        f = b @ a
        g = c * e

        return d, f, g

    cfoo = thunder.compile_with_info(foo)

    result, traces = cfoo(a, b)

    fusions = examine.get_fusions(traces[-1])

    assert len(fusions) == 1


# Tests that three separated nvFuser regions can be merged even if a PyTorch
#   region has to be reordered BEFORE them
@instantiate(executors=(nvFuserExecutor,), dtypes=(thunder.float32,))
def test_nvfuser_toposort_dependent4(executor, device: str, dtype: dtypes.dtype):
    torch_dtype = ltorch.to_torch_dtype(dtype)
    a = make_tensor((2, 2), device=device, dtype=torch_dtype)
    b = make_tensor((2, 2), device=device, dtype=torch_dtype)

    def foo(a, b):
        c = a + b
        d = a @ b
        e = a - c
        f = b @ a
        g = d * e

        return d, f, g

    cfoo = thunder.compile_with_info(foo)

    result, traces = cfoo(a, b)

    fusions = examine.get_fusions(traces[-1])

    assert len(fusions) == 1


# Tests that three separated nvFuser regions can only be partially merged
#   if there's a PyTorch data dependency between them
@instantiate(executors=(nvFuserExecutor,), dtypes=(thunder.float32,))
def test_nvfuser_toposort_dependent5(executor, device: str, dtype: dtypes.dtype):
    torch_dtype = ltorch.to_torch_dtype(dtype)
    a = make_tensor((2, 2), device=device, dtype=torch_dtype)
    b = make_tensor((2, 2), device=device, dtype=torch_dtype)

    def foo(a, b):
        c = a + b
        d = c @ b
        e = a - c
        f = b @ a
        g = d * e

        return d, f, g

    cfoo = thunder.compile_with_info(foo)

    result, traces = cfoo(a, b)

    fusions = examine.get_fusions(traces[-1])

    assert len(fusions) == 2


#
# Tests related to trace manipulation and transformation
#
# TODO Maybe move to test_transforms.py?


@instantiate(dtypes=NOTHING)
def test_detached_trace(executor, device: str, _):
    # This test ensures that the detached_trace context manager works as expected.
    #   It should be possible to enter a detached trace, and then exit it, and
    #   the trace should be restored to its original state.
    from thunder.core.trace import set_tracectx, get_tracectx, TraceCtx, reset_tracectx, detached_trace

    try:
        new_trace = TraceCtx(None)
        trace_token = set_tracectx(new_trace)
        outer_trace = get_tracectx()
        assert outer_trace is not None
        assert outer_trace is trace_token.var.get()
        with detached_trace():
            assert get_tracectx() is not None
            assert get_tracectx() is not outer_trace
    finally:
        reset_tracectx(trace_token)


@instantiate(dtypes=(thunder.float32,))
def test_symbol_all_constant_args(executor, device: str, dtype: dtypes.dtype):
    def foo():
        return clang.maybe_convert_to_dtype(1, dtype)

    trace = thunder.trace(foo)

    assert len(trace.bound_symbols) == 2
    symbol = trace.bound_symbols[0]
    assert symbol.sym.name == "convert_element_type"
    assert symbol.are_all_args_constant

    def bar(a, b):
        return clang.add(a, b)

    trace = thunder.trace(bar, 1, 2)
    # Trace consists of two trivial unpack and addition
    assert len(trace.bound_symbols) == 4
    symbol = trace.bound_symbols[-2]
    assert symbol.sym.name == "add"
    assert not symbol.are_all_args_constant


# Check for https://github.com/Lightning-AI/lightning-thunder/issues/471
@instantiate(dtypes=(thunder.float32,))
def test_argument_of_none(executor, device, dtype):
    from thunder.executors.utils import Region

    def foo(x, y, z):
        return x + y

    tdtype = ltorch.to_torch_dtype(dtype)
    a, b = [make_tensor((1,), device=device, dtype=tdtype) for _ in range(2)]
    c = None
    trace = thunder.trace(foo, a, b, c)

    producers = thunder.core.utils.producers(trace)
    consumers = thunder.core.utils.consumers(trace)
    region_bsyms = trace.bound_symbols[:3]
    region = Region(trace, producers, consumers, region_bsyms, executor=executor, counter=0)
    assert len(region.inputs) == 0 and sorted(str(v) for v in region.outputs) == ["x", "y"]


# This test ensures that calls to torch functions are recorded in the trace
@instantiate(executors=(TorchExecutor,), dtypes=NOTHING)
def test_torch_call_recording(executor, device: str, _):
    def func(a):
        return ltorch.dropout(a)

    a = make_tensor((2, 3), device=device, dtype=torch.float32)

    torch_trace = thunder.trace(func, a)
    assert len(torch_trace.bound_symbols) == 3
    assert torch_trace.bound_symbols[-2].sym.name == "dropout"
    assert torch_trace.bound_symbols[-2].sym.id == "torch.nn.functional.dropout"

    # Ensure that the trace can be fused and executed
    # TODO: Restore this
    # ex = _get_executor(executor)
    # fusion = ex.fuse(torch_trace)
    # actual = fusion(a)
    # assert actual.shape == (2, 3)


# Asserts that all the elements of a collection are equal to each other.
def all_eq(l):
    for e1 in l:
        for e2 in l:
            assert e1 == e2


# Asserts that all the elements of a collection are not equal to each other,
# and that elements are equal to themselves.
def all_neq(l):
    el = enumerate(l)
    for i, e1 in el:
        for j, e2 in el:
            assert e1 == e2 if i == j else e1 != e2


@instantiate(dtypes=(thunder.float32,))
def test_boundsymbol_hash_eq_examples(executor, device, dtype: dtypes.dtype):
    torch_dtype = ltorch.to_torch_dtype(dtype)

    a = make_tensor((2, 2), device=device, dtype=torch_dtype)
    b = make_tensor((2, 2), device=device, dtype=torch_dtype)

    # Returns the bound symbols for a function and args.
    def compile_bsyms(fn, args):
        fn = executor.make_callable_with_info(fn)
        _, traces = fn(*args)
        return traces[0].bound_symbols

    # Extracts the bound symbols for the function with
    # the given symbol names.
    def extract_bsyms(fn, args, ops):
        return [b for b in compile_bsyms(fn, args) if b.sym.name in ops]

    # We want .rhs() for a + b and torch.add() to hash and compare
    # the same for writing the CSE pass.
    def add_rhs(a, b):
        c = a + b
        d = a + b
        e = ltorch.add(a, b)
        return c, d, e

    bsyms = extract_bsyms(add_rhs, (a, b), ("add",))
    all_eq([hash(b.rhs()) for b in bsyms])
    all_eq([b.rhs() for b in bsyms])

    # TODO: The current way BoundSymbols are compared treats args and kwargs
    #       differently, so the same semantic call can be considered 'not equal'
    #       if the arguments are passed differently. This is probably not the
    #       behavior we want, and in the future we may change it, but for now
    #       this test asserts the current behavior.
    def add_rhs_kwargs(a, b):
        c = a + b
        d = ltorch.add(a=a, b=b)
        return c, d

    # Assert the current behavior.
    # When the test case is supported, switch this to all_eq.
    bsyms = extract_bsyms(add_rhs_kwargs, (a, b), ("add",))
    all_neq([hash(b.rhs()) for b in bsyms])
    all_neq([b.rhs() for b in bsyms])

    # Also make sure the symbols are the same.
    all_eq([b.sym for b in bsyms])
    all_eq([hash(b.sym) for b in bsyms])

    # TODO: We also currently cannot assert that the right hand side of
    #       identical operators with kwargs are equal.
    def same_kwargs(device, dtype):
        a = ltorch.full((2, 2), 5, device=device, dtype=dtype)
        b = ltorch.full((2, 2), 5, device=device, dtype=dtype)
        return a + b

    # Assert the current behavior.
    # When the test case is supported, switch the all_neq below to all_eq.
    bsyms = extract_bsyms(same_kwargs, (device, dtype), ("full",))
    all_eq([hash(b.rhs()) for b in bsyms])
    all_neq([b.rhs() for b in bsyms])

    # Again, the symbols should be the same.
    all_eq([b.sym for b in bsyms])
    all_eq([hash(b.sym) for b in bsyms])

    # We can, however, know when the number of kwargs are different,
    # or the args are different.
    def diff_kwargs(device, dtype):
        a = ltorch.full((1, 2), 2, device=device, dtype=dtype)
        b = ltorch.full((2, 3), 5, device=device, dtype=dtype)
        c = ltorch.full((2, 3), 5, device=device)
        return a, b, c

    bsyms = extract_bsyms(diff_kwargs, (device, dtype), ("full",))
    all_eq([hash(b.rhs()) for b in bsyms])
    all_neq([b.rhs() for b in bsyms])

    # Assert that boundsymbols for different ops hash/compare differently.
    def different_ops(a, b):
        c = a + b
        d = a - b
        return c, d

    c, d = extract_bsyms(different_ops, (a, b), ("add", "sub"))
    assert hash(c.sym) != hash(d.sym)
    assert hash(c) != hash(d)
    assert hash(c.rhs()) != hash(d.rhs())
    assert c.sym != d.sym
    assert c != d
    assert c.rhs() != d.rhs()


# @instantiate(dtypes=NOTHING)
# @requiresCUDA
# def test_torch_call_lowering_for_nvfuser(executor, device, _):
#     pytest.xfail(reason="lower_for_nvfuser is removed and replaced with 'flattening'")
#     # This test ensures that calls to torch functions are lowered to the
#     # nvFuser supported primitives
#     from thunder import _get_executor
#     from thunder.executors.nvfuser import lower_for_nvfuser

#     def func(a):
#         cos = tlang.cos(a)
#         return ttorch.softmax(cos, 1) + a

#     a = make_tensor((2, 3), device=device, dtype=torch.float32)

#     trace = thunder.make_trace(func, executor=executor)(a)
#     assert len(trace.symbols) == 3
#     assert trace.symbols[0].name == "cos"
#     assert trace.symbols[1].name == "torch.nn.functional.softmax"
#     assert trace.symbols[2].name == "add"

#     nvfuser_trace = thunder.make_trace(lower_for_nvfuser(func), executor=executor)(a)
#     assert len(nvfuser_trace.symbols) == 11
#     assert not any(s.name == "torch.nn.functional.softmax" for s in nvfuser_trace.symbols)

#     # Ensure that the trace can be fused and executed
#     ex = _get_executor(executor)
#     fusion = ex.fuse(nvfuser_trace)
#     actual = fusion(a)
#     expected = thunder.make_traced(func, executor=executor)(a)
#     assert_close(actual, expected)


@instantiate(dtypes=NOTHING)
def test_nested_trace(executor, device, _):
    # This test ensures that trace() can be called from within a traced
    # function without leaking the trace context.
    # from thunder import _get_executor

    def foo(a, b):
        return clang.add(a, b)

    def bar(a, b):
        foo_trace = thunder.trace(foo, a, b, inline_trace=False)
        assert len(foo_trace.bound_symbols) == 4
        assert foo_trace.bound_symbols[-2].sym.name == "add"
        return clang.mul(a, b)

    a = make_tensor((2, 2), device=device, dtype=torch.float32)
    b = make_tensor((2, 2), device=device, dtype=torch.float32)

    bar_trace = thunder.trace(bar, a, b)
    assert len(bar_trace.bound_symbols) == 4
    assert bar_trace.bound_symbols[-2].sym.name == "mul"

    # TODO: Restore this once there's an equivalent
    # ex = _get_executor(executor)
    # fusion = ex.fuse(bar_trace)
    # actual = fusion(a, b)
    # expected = a * b
    # assert_close(actual, expected)


@instantiate(dtypes=NOTHING)
def test_nested_trace_no_name_collision(executor, device, _):
    def foo(a, b):
        return clang.add(a, b)

    def bar(__a, __b):
        a, b = __a, __b
        foo_trace = thunder.trace(foo, a, b, inline_trace=False)
        # The name of the output of the add symbol should not be the same as
        # the name of the first argument to the bar function.
        assert foo_trace.bound_symbols[-2].sym.name == "add"
        assert foo_trace.bound_symbols[-2].output.name != foo_trace.args[0].name
        return foo(a, b)

    a = make_tensor((2, 2), device=device, dtype=torch.float32)
    b = make_tensor((2, 2), device=device, dtype=torch.float32)

    thunder.trace(bar, a, b)


@instantiate(dtypes=NOTHING)
def test_trace_args_no_name_collision(executor, device, _):
    from thunder.core.trace import detached_trace
    from thunder.core.proxies import TensorProxy

    with detached_trace():
        a = TensorProxy(name="__a", shape=(2, 2), device=thunder.core.devices.cpu, dtype=thunder.core.dtypes.float32)

    def func(*args):
        return args[0] + args[1]

    trace = thunder.trace(func, a, a)
    # trace.args must have non-duplicate names
    # because Python disallows duplicate names in function definitions
    assert trace.args[0].name != trace.args[1].name


@instantiate(dtypes=NOTHING)
def test_eval_trace(executor, device, _):
    # This test ensures that eval_trace() can be called from within a trace
    #   and that all the symbols in the trace are properly evaluated.

    from thunder.core.transforms import eval_trace
    from thunder.core.trace import TraceCtx, reset_tracectx, set_tracectx, maybe_start_trace
    from thunder.core.proxies import TensorProxy

    def foo(a, b, *, c=5):
        return clang.mul(clang.add(a, b), c)

    a = make_tensor((2, 2), device=device, dtype=torch.float32)
    b = make_tensor((2, 2), device=device, dtype=torch.float32)
    c = 4.0

    # Test eval_trace() with eager proxy execution
    foo_trace = thunder.trace(foo, a, b, c=c)
    try:
        trace = TraceCtx(None)
        trace_token = set_tracectx(trace)
        new_args = [arg for arg in foo_trace.args]
        new_kwargs = {k: v for k, v in foo_trace.kwargs.items()}
        # TODO: trace object doesn't respect the original tuple/non-tuple spec
        # for output, it's always a tuple
        actual = eval_trace(foo_trace, *new_args, **new_kwargs)[0]
        assert isinstance(actual, TensorProxy)
        assert actual.shape == foo_trace.output[0].shape
        assert actual.dtype == foo_trace.output[0].dtype
        assert actual.device == foo_trace.output[0].device
    finally:
        reset_tracectx(trace_token)

    # Test eval_trace() with retracing + fusion + execution
    def eval_trace_as_function(trace):
        def func(*args, **kwargs):
            return eval_trace(trace, *args, **kwargs)

        return func

    foo_traced = executor.make_callable(eval_trace_as_function(foo_trace))
    actual = foo_traced(a, b, c=c)
    expected = (a + b) * c
    assert_close(actual, expected)

    # Test eval_trace() with retracing
    foo_trace2 = thunder.trace(eval_trace_as_function(foo_trace), a, b, c=c)
    # How to test that two traces are equal?
    # Two operators and others are do-nothing annotations
    assert len(foo_trace2.bound_symbols) == 7
    assert foo_trace2.bound_symbols[-3].sym.name == "add"
    assert foo_trace2.bound_symbols[-2].sym.name == "mul"


@instantiate(
    dtypes=NOTHING,
    executors=[
        TorchExecutor,
        # TODO: nvFuser executor doesn't support duplicate outputs
        # TODO: nvFuser executor doesn't support clashing input and output names
    ],
)
def test_eval_trace_duplicate_output(executor, device, _):
    # This test ensures that eval_trace() can evaluate a trace with duplicate
    # outputs.
    from thunder.core.transforms import eval_trace, identity

    def foo1(a):
        return a, a

    a = torch.ones((2, 2), device=device, dtype=torch.float32)

    foo_trace = thunder.trace(foo1, a)
    assert len(foo_trace.bound_symbols) == 2
    assert foo_trace.bound_symbols[0].sym.name == "unpack_trivial"
    assert len(foo_trace.output) == 2
    assert foo_trace.output[0].name == foo_trace.output[1].name

    def func(a):
        return eval_trace(foo_trace, a)

    actual = executor.make_callable(func)(a)
    assert_close(actual, (a, a))

    # Identity is needed to ensure that the duplicated outputs of a symbol
    # don't cause problems.
    def foo2(a):
        a = 1.0 * a
        return a, a

    for foo in [foo1, foo2]:
        foo_trace = thunder.trace(identity(foo), a)
        assert len(foo_trace.bound_symbols) == 4
        assert len(foo_trace.output) == 2
        assert foo_trace.output[0].name == foo_trace.output[1].name

    # TODO: enable this once executors can work with identity call
    #     actual = executor.make_callable(partial(eval_trace, foo_trace))(a)
    #     assert_close(actual, (a, a))


@instantiate(
    dtypes=NOTHING,
    executors=[
        TorchExecutor,
    ],
)
def test_transforms_identity(executor, device, _):
    # This test ensures that identity() can be called from within a traced
    # function without leaking the trace context.
    # Also tests that identity() can be nested.
    # Also tests that identity() can be used with "torch" executor.
    from thunder.core.transforms import identity, Transforms

    # from thunder import _get_executor

    def func(a, b, *, c=5):
        return clang.mul(clang.mul(clang.add(a, b), 1), c)

    nested_id_func = identity(identity(identity(func)))

    a = make_tensor((2, 2), device=device, dtype=torch.float32)
    b = make_tensor((2, 2), device=device, dtype=torch.float32)
    c = 4.0

    nested_id_trace = thunder.trace(nested_id_func, a, b, c=c)
    # one annotating symbol per input and one actual symbol
    assert len(nested_id_trace.bound_symbols) == 6
    assert nested_id_trace.bound_symbols[-2].sym.id == Transforms.IdentityOp

    trace = nested_id_trace.bound_symbols[-2].kwargs.get("trace", None)
    for _ in range(2):
        assert len(trace.bound_symbols) == 6
        assert trace.bound_symbols[-2].sym.id == Transforms.IdentityOp
        trace = trace.bound_symbols[-2].kwargs.get("trace", None)

    assert len(trace.bound_symbols) == 8
    assert trace.bound_symbols[-5].sym.name == "add"
    assert trace.bound_symbols[-4].sym.name == "convert_element_type"
    assert trace.bound_symbols[-3].sym.name == "mul"
    assert trace.bound_symbols[-2].sym.name == "mul"

    # TODO: Restore this once there's an equivalent
    # ex = _get_executor(executor)
    # fusion = ex.fuse(nested_id_trace)
    # actual = fusion(a, b, c=c)
    # expected = executor.make_callable(func)(a, b, c=c)
    # torch.testing.assert_close(actual, expected)


@instantiate(
    dtypes=NOTHING,
    executors=[
        TorchExecutor,
    ],
)
def test_transforms_inline(executor, device, _):
    # This test ensures that inline() can be called from within a traced
    # function removing (inlining) all identity() transforms.
    # Also tests that inline() can be nested.
    # Also tests that inline() can be used with "torch" executor.
    from thunder.core.transforms import identity, inline, Transforms

    def func(a, b):
        return clang.mul(clang.add(a, b), 1)

    nested_id_func = identity(identity(identity(func)))

    a = make_tensor((2, 2), device=device, dtype=torch.float32)
    b = make_tensor((2, 2), device=device, dtype=torch.float32)

    inlined_nested_id_trace = thunder.trace(inline(nested_id_func), a, b)
    assert len(inlined_nested_id_trace.bound_symbols) == 6
    assert not any(symbol.sym.id == Transforms.IdentityOp for symbol in inlined_nested_id_trace.bound_symbols)
    assert inlined_nested_id_trace.bound_symbols[-4].sym.name == "add"
    assert inlined_nested_id_trace.bound_symbols[-3].sym.name == "convert_element_type"
    assert inlined_nested_id_trace.bound_symbols[-2].sym.name == "mul"

    transforms = (inline, identity, inline, inline, identity, identity, inline)
    for transform in transforms:
        transformed_func = transform(func)

    # Since the outer-most transform is inline, the trace should not contain
    # any identity transforms.
    transformed_trace = thunder.trace(transformed_func, a, b)
    assert len(transformed_trace.bound_symbols) == 6
    assert not any(symbol.sym.id == Transforms.IdentityOp for symbol in transformed_trace.bound_symbols)


@instantiate(
    dtypes=NOTHING,
    executors=(
        TorchExecutor,
        # TODO: nvFuser executor does not support full(shape=()) yet
    ),
)
def test_transforms_vmap_axis_size(executor, device, _):
    from thunder.core.transforms import inline, vmap

    actual = executor.make_callable(inline(vmap(lambda: 2, axis_size=4)))()
    expected = torch.full((4,), 2, device="cpu")
    assert_close(actual, expected)

    actual = executor.make_callable(inline(vmap(lambda x: x, axis_size=4)))(2)
    assert_close(actual, expected)


# TODO Re-enable this, broken by raising NotImplementedError from bool(tensor)
# @instantiate(
#     dtypes=NOTHING,
# )
# def test_transforms_vmap_identity(executor, device, _):
#     from thunder.core.transforms import identity, vmap

#     def func(a):
#         return clang.sin(a)

#     a = torch.randn(2, 2)

#     thunder._make_trace(vmap(identity(func)))(a)


# @instantiate(
#     dtypes=NOTHING,
# )
# def test_transforms_jvp_eager(executor, device, _):
#     from thunder.core.transforms import jvp_eager

#     def func(a, b):
#         c = tlang.sin(a)
#         return tlang.mul(tlang.add(c, b), 1)

#     a = torch.ones(2, 3, device=device, dtype=torch.float32)
#     b = torch.ones(2, 3, device=device, dtype=torch.float32) * 2

#     primals = (a, b)
#     tangents = (a, b)
#     out_p, out_t = jvp_eager(func, primals, tangents, executor=executor)
#     expected_out_p = torch.sin(a) + b
#     expected_out_t = torch.cos(a) + b
#     assert_close(out_p, expected_out_p)
#     assert_close(out_t, expected_out_t)


@instantiate(
    dtypes=NOTHING,
)
def test_transforms_vjp_1_2(executor, device, _):
    from thunder.core.transforms import inline, vjp

    # 1 input, 2 outputs
    def func_1_2(x):
        a = clang.sin(x)
        b = clang.add(0.2, a)
        c = clang.asin(b)
        return b, c

    a = make_tensor((2, 3), device=device, dtype=torch.float32)

    g1 = make_tensor((2, 3), device=device, dtype=torch.float32)
    g2 = make_tensor((2, 3), device=device, dtype=torch.float32)

    vjp_eager = executor.make_callable(inline(vjp(func_1_2)))

    primals = (a,)
    cotangents = (g1, g2)
    out_p, grads = vjp_eager(primals, cotangents)
    expected_out_p = executor.make_callable(func_1_2)(a)
    assert_close(out_p, expected_out_p, equal_nan=True)

    # Now check the gradients
    # TODO: We will have this automatically tested with OpInfo tests
    aa = a.clone().requires_grad_(True)

    def pt_func_1_2(x):
        a = torch.sin(x)
        b = torch.add(0.2, a)
        c = torch.asin(b)
        return b, c

    out = pt_func_1_2(aa)
    expected_grads = torch.autograd.grad(out, aa, grad_outputs=(g1, g2), retain_graph=True)
    assert_close(expected_grads, grads, equal_nan=True)


@instantiate(
    dtypes=NOTHING,
)
def test_transforms_vjp_2_2_kwarg(executor, device, _):
    # This test ensures that combination of positional and keyword arguments
    # is differentiable.
    from thunder.core.transforms import inline, vjp

    # 2 inputs, 1 kwarg, 2 outputs
    def func_2_2(x, y, *, z):
        def func(x):
            a = clang.sin(x)
            b = clang.add(0.2, a)
            c = clang.asin(b)
            return c

        a, b = func(x), func(y)
        c = clang.add(a, b)
        d = clang.add(c, func(z))
        return c, d

    x = make_tensor((2, 3), device=device, dtype=torch.float64)
    y = make_tensor((2, 3), device=device, dtype=torch.float64)
    z = make_tensor((2, 3), device=device, dtype=torch.float64)

    g1 = make_tensor((2, 3), device=device, dtype=torch.float64)
    g2 = make_tensor((2, 3), device=device, dtype=torch.float64)

    vjp_eager = executor.make_callable(inline(vjp(func_2_2)))

    primals = (x, y)
    primal_kwargs = {"z": z}
    cotangents = (g1, g2)
    out_p, grads = vjp_eager(primals, cotangents, **primal_kwargs)
    expected_out_p = executor.make_callable(func_2_2)(*primals, **primal_kwargs)
    assert_close(out_p, expected_out_p, equal_nan=True)

    # Now check the gradients
    # TODO: We will have this automatically tested with OpInfo tests
    xx = x.clone().requires_grad_(True)
    yy = y.clone().requires_grad_(True)
    zz = z.clone().requires_grad_(True)

    def pt_func_2_2(x, y, *, z):
        def func(x):
            a = torch.sin(x)
            b = torch.add(0.2, a)
            c = torch.asin(b)
            return c

        a, b = func(x), func(y)
        c = torch.add(a, b)
        d = torch.add(c, func(z))
        return c, d

    out = pt_func_2_2(xx, yy, z=zz)
    expected_grads = torch.autograd.grad(out, [xx, yy, zz], grad_outputs=(g1, g2), retain_graph=True)
    # vjp returns a tuple of (primals, cotangents) where cotangents is a tuple of
    # derivatives with respect to the positional arguments and a dict of derivatives
    # with respect to the keyword arguments.
    *gprimals, gkwargs = grads
    assert_close(expected_grads[:2], gprimals, equal_nan=True)
    assert_close(expected_grads[2], gkwargs["z"], equal_nan=True)


@instantiate(
    dtypes=NOTHING,
)
def test_transforms_vjp_2_1(executor, device, _):
    from thunder.core.transforms import inline, vjp

    def pt_func_2_1(x, y):
        a = torch.sin(x + y)
        b = torch.add(0.2, a)
        c = torch.asin(b)
        return c

    def func_2_1(x, y):
        a = clang.sin(x + y)
        b = clang.add(0.2, a)
        c = clang.asin(b)
        return c

    vjp_eager = executor.make_callable(inline(vjp(func_2_1)))
    a = make_tensor((2, 3), device=device, dtype=torch.float32)
    b = make_tensor((2, 3), device=device, dtype=torch.float32)
    g1 = make_tensor((2, 3), device=device, dtype=torch.float32)
    primals = (a, b)
    cotangents = (g1,)
    out_p, grads = vjp_eager(primals, cotangents)
    expected_out_p = executor.make_callable(func_2_1)(*primals)
    assert_close(out_p, expected_out_p, equal_nan=True)

    aa = a.clone().requires_grad_(True)
    bb = b.clone().requires_grad_(True)
    out = pt_func_2_1(aa, bb)
    expected_grads = torch.autograd.grad(out, [aa, bb], grad_outputs=(g1,), retain_graph=True)
    assert_close(expected_grads, grads, equal_nan=True)


# TODO Enable me, disabled when extra error checking was added to reduction prims
# @instantiate(
#     dtypes=NOTHING,
#     executors=(
#         nvFuserExecutor,
#         # TODO: Enable Torch executor once the issue with sum is fixed
#         # See: https://github.com/Lightning-AI/lightning-thunder/issues/438
#     ),
# )
# def test_transforms_vmap_inline_value_and_grad(executor, device, _):
#     # This test checks whether it's possible to vmap a function that is
#     # traced with inline and value_and_grad.
#     # For applications see
#     # https://jax.readthedocs.io/en/latest/jax-101/04-advanced-autodiff.html#per-example-gradients
#     # https://pytorch.org/functorch/stable/notebooks/per_sample_grads.html
#     from thunder.core.transforms import inline, value_and_grad, vmap
#     from thunder.core import prims

#     def func(x):
#         a = prims.sin(x)
#         a = prims.sum(a, ())
#         return prims.sum(a, tuple(range(a.ndim)))

#     vjp_func = executor.make_callable(inline(value_and_grad(func)))
#     a = make_tensor((2, 3), device=device, dtype=torch.float32)
#     single_out, (single_grad,) = vjp_func(a)

#     aaa = torch.stack([a, a, a])
#     vmap_inline_vjp = executor.make_callable(inline(vmap(inline(value_and_grad(func)))))
#     batched_out, (batched_grad,) = vmap_inline_vjp(aaa)
#     for i in range(3):
#         assert_close(single_out, batched_out[i])
#         assert_close(single_grad, batched_grad[i])


# @instantiate(
#     dtypes=NOTHING,
# )
# def test_transforms_vmap_x(executor, device, _):
#     from thunder.core.transforms import vmap_eager

#     def func(a, b):
#         assert isinstance(a, proxies.TensorProxy)
#         assert isinstance(b, proxies.TensorProxy)
#         assert a.ndim == 1
#         assert a.shape == b.shape
#         c = tlang.sin(a)
#         return tlang.mul(tlang.add(c, b), 1)

#     a = torch.ones(2, 3, device=device, dtype=torch.float32)
#     b = torch.ones(2, 3, device=device, dtype=torch.float32) * 2

#     args = (a, b)
#     out = vmap_eager(func, args, executor=executor)
#     expected_out_p = torch.sin(a) + b
#     assert_close(out, expected_out_p)


@instantiate(
    dtypes=NOTHING,
)
def test_transforms_inline_jvp_inline_vmap(executor, device, _):
    pytest.xfail("AttributeError: 'NoneType' object has no attribute 'mul'")
    from thunder.core.transforms import vmap, jvp, inline

    if executor == nvFuserExecutor:
        # Couldn't find metadata for 1.0 of type <class 'float'>
        pytest.xfail("Something is failing with the nvFuser executor")

    def func(a, b):
        assert isinstance(a, proxies.TensorProxy)
        assert isinstance(b, proxies.TensorProxy)
        assert a.ndim == 1
        assert a.shape == b.shape
        c = clang.sin(a)
        return clang.mul(clang.add(c, b), 1)

    a = torch.ones(2, 3, device=device, dtype=torch.float32)
    b = torch.ones(2, 3, device=device, dtype=torch.float32) * 2

    args = (a, b)
    out_p, out_t = executor.make_callable(inline(jvp(inline(vmap(func)))))(args, args)
    expected_out_p = torch.sin(a) + b
    assert_close(out_p, expected_out_p)
    expected_out_t = torch.cos(a) + b
    assert_close(out_t, expected_out_t)


@instantiate(
    dtypes=NOTHING,
)
def test_transforms_inline_vmap_inline_jvp(executor, device, _):
    from thunder.core.transforms import vmap, jvp, inline

    def func(a, b):
        assert isinstance(a, proxies.TensorProxy)
        assert isinstance(b, proxies.TensorProxy)
        assert a.ndim == 1
        assert a.shape == b.shape
        c = clang.sin(a)
        return clang.mul(clang.add(c, b), 1)

    a = torch.ones(2, 3, device=device, dtype=torch.float32)
    b = torch.ones(2, 3, device=device, dtype=torch.float32) * 2

    args = (a, b)
    out_p, out_t = executor.make_callable(inline(vmap(inline(jvp(func)), out_dims=(0, 0))))(args, args)
    expected_out_p = torch.sin(a) + b
    assert_close(out_p, expected_out_p)
    expected_out_t = torch.cos(a) + b
    assert_close(out_t, expected_out_t)


@instantiate(dtypes=NOTHING)
def test_torch_autocast_exception(executor, device, _):
    def f(a):
        return 2.0 * a

    executors_list = executor.executors_list()
    compiled_f = thunder.compile(f, executors_list=executors_list)
    a = torch.ones((), device=device, dtype=torch.float32)
    devicetype = torch.device(device).type

    with pytest.raises(RuntimeError) as excinfo:
        with torch.autocast(device_type=devicetype):
            compiled_f(a)
    assert "A callable optimized" in str(excinfo.value)


def test_traceback():
    def f(a):
        return -(a > 0)  # negating a bool tensor raises

    compiled_f = thunder.compile(f)
    a = torch.ones((), dtype=torch.float32)
    with pytest.raises(RuntimeError) as excinfo:
        compiled_f(a)
    assert "on a bool tensor" in str(excinfo.value)
    assert "torch.neg" in str(excinfo.traceback[-1].statement)
    assert "LC.gen" in excinfo.traceback[-1].path


@instantiate(
    dtypes=NOTHING,
    executors=(
        nvFuserExecutor,
        # NOTE torch executor does not have bookend optimization.
        # See comment: https://github.com/Lightning-AI/lightning-thunder/issues/571#issuecomment-1610778432
    ),
)
def test_bookend_meta_optimization(executor, device, _):
    test_cases = list()

    a = torch.ones(2, 3, 5, device=device, dtype=torch.float32)

    # one transpose at the beginning
    # should be moved out of fusion
    def func_0(t):
        t0 = t.transpose(0, 1)
        t1 = t0.tanh()
        t2 = t1.sin()
        return t2

    test_cases.append((func_0, 1))

    # one transpose at the end
    # should be moved out of fusion
    def func_1(t):
        t0 = t.tanh()
        t1 = t0.sin()
        t2 = t1.transpose(0, 1)
        return t2

    test_cases.append((func_1, 1))

    # one transpose at the beginning and another at the end
    # both should be moved out of fusion
    def func_2(t):
        t0 = t.transpose(0, 1)
        t1 = t0.tanh()
        t2 = t1.sin()
        t3 = t2.transpose(0, 2)
        return t3

    test_cases.append((func_2, 2))

    # a couple independent transposes at the beginning
    # both should be moved out of fusion
    def func_3(t):
        t0 = t.transpose(0, 1)
        t1 = t0.tanh()
        t2 = t1.sin()

        t3 = t.transpose(0, 2)
        t4 = t3.sin()
        t5 = t4.tanh()
        return t2, t5

    test_cases.append((func_3, 2))

    # a couple independent transposes at the end
    # both should be moved out of fusion
    def func_4(t):
        t0 = t.tanh()
        t1 = t0.sin()
        t2 = t1.transpose(0, 1)

        t3 = t.sin()
        t4 = t3.tanh()
        t5 = t4.transpose(0, 2)
        return t2, t5

    test_cases.append((func_4, 2))

    # a couple chained transposes at the beginning
    # both should be moved out of fusion
    def func_5(t):
        t0 = t.transpose(0, 1)
        t1 = t0.transpose(0, 2)
        t2 = t1.tanh()
        t3 = t2.sin()
        return t3

    test_cases.append((func_5, 2))

    # a couple chained transposes at the end
    # both should be moved out of fusion
    def func_6(t):
        t0 = t.tanh()
        t1 = t0.sin()
        t2 = t1.transpose(0, 1)
        t3 = t2.transpose(0, 2)
        return t3

    test_cases.append((func_6, 2))

    # a couple chained transposes at the beginning and end
    # both should be moved out of fusion
    def func_7(t):
        t0 = t.transpose(0, 1)
        t1 = t0.transpose(0, 2)
        t2 = t1.tanh()
        t3 = t2.sin()
        t4 = t3.transpose(0, 1)
        t5 = t4.transpose(0, 2)
        return t5

    test_cases.append((func_7, 4))

    # complicated case, where two non-meta ops are each sandwiched by transpose
    # the two transposes on the edge should be moved out of fusion
    def func_8(t):
        t0 = t.transpose(0, 1)
        t1 = t0.tanh()
        # transpose in the middle should stay
        t2 = t1.transpose(0, 1)
        t3 = t2.sin()
        t4 = t3.transpose(0, 2)
        return t4

    test_cases.append((func_8, 2))

    # NOTE func_9 and func_10 are symmetrical, this is designed to double check our toposort based approach can break
    # ties

    # complicated case, where two branches have transpose ops towards the end
    # the two transposes on the edge should be moved out of fusion
    def func_9(t):
        t0 = t.tanh()
        t1 = t0.sin()
        t2 = t1.transpose(0, 1)
        t3 = t2.transpose(2, 1)

        t4 = t.sin()
        t5 = t4.tanh()
        t6 = t5.transpose(0, 2)
        t7 = t6.sin()
        return t3, t7

    test_cases.append((func_9, 2))

    # complicated case, where two branches have transpose ops towards the end
    # the two transposes on the edge should be moved out of fusion
    def func_10(t):
        t0 = t.tanh()
        t1 = t0.sin()
        t2 = t1.transpose(0, 1)
        t3 = t2.sin()

        t4 = t.sin()
        t5 = t4.tanh()
        t6 = t5.transpose(0, 2)
        t7 = t6.transpose(2, 1)
        return t3, t7

    test_cases.append((func_10, 2))

    # complicated case, where a chain of transposed operations is both an output and consumed as an intermediate
    # no transposes should be removed
    def func_11(t):
        t0 = t.tanh()
        t1 = t0.sin()
        t2 = t1.transpose(0, 1)
        t3 = t2.transpose(0, 2)

        t4 = t3.sin()
        return t3, t4

    test_cases.append((func_11, 0))

    # complicated case
    def func_12(t):
        t0 = t.transpose(0, 1)
        t1 = t0.transpose(0, 2)
        t2 = t1.tanh()
        t3 = t2 + 1.0
        t4 = t3.transpose(2, 1)
        t4 = t4.transpose(0, 1)

        t5 = t * 0.5
        # this is the only transpose that should stay in fusion, because it is surrounded by non-meta ops
        t6 = t5.transpose(0, 2)
        t7 = t6.tanh()

        t8 = t1.transpose(1, 2)

        t9 = t.transpose(2, 1)
        t10 = t9.tanh()

        t11 = t.transpose(1, 2)
        t12 = t11.transpose(0, 2)
        t13 = t12.transpose(0, 1)

        return t4, t6, t7, t8, t10, t13

    test_cases.append((func_12, 8))

    for func, num_permute in test_cases:
        cfoo = thunder.compile(func)

        _ = cfoo(a)
        traces = thunder.last_traces(cfoo)
        exposed_permute = list(filter(lambda x: x.sym.name == "permute", traces[-1].bound_symbols))

        assert len(exposed_permute) == num_permute


@instantiate(dtypes=NOTHING)
def test_inplace(executor, device, _):
    # Usually in this scenario we would make a big list of
    # the names of methods to test, then use getattr() to call
    # them in the trace. However, this would not also test that
    # the syntax wouldn't get broken by preprocessing.

    def test_add(s, o):
        s += o
        return s

    def test_and(s, o):
        s &= o
        return s

    def test_concat(s, o):
        s.__iconcat__(o)
        return s

    def test_floordiv(s, o):
        s //= o
        return s

    def test_lshift(s, o):
        s <<= o
        return s

    def test_matmul(s, o):
        s @= o
        return s

    def test_mod(s, o):
        s %= o
        return s

    def test_mul(s, o):
        s *= o
        return s

    def test_or(s, o):
        s |= o
        return s

    def test_pow(s, o):
        s **= o
        return s

    def test_rshift(s, o):
        s >>= o
        return s

    def test_sub(s, o):
        s -= o
        return s

    def test_truediv(s, o):
        s /= o
        return s

    def test_xor(s, o):
        s ^= o
        return s

    t1 = make_tensor((2, 3), device=device, dtype=torch.float32)
    t2 = make_tensor((1, 2), device=device, dtype=torch.float32)

    tests = (
        test_add,
        test_and,
        test_concat,
        test_floordiv,
        test_lshift,
        test_matmul,
        test_mod,
        test_mul,
        test_or,
        test_pow,
        test_rshift,
        test_sub,
        test_truediv,
        test_xor,
    )

    for t in tests:
        cfn = thunder.compile(t)
        with pytest.raises(RuntimeError, match="not supported"):
            cfn(t1, t2)
        with pytest.raises(RuntimeError, match="not supported"):
            cfn(5, 6)
        with pytest.raises(RuntimeError, match="not supported"):
            cfn(1.2, 2.4)
        with pytest.raises(RuntimeError, match="not supported"):
            cfn(1.2j, 2.4j)


@instantiate(dtypes=NOTHING)
def test_thunder_autocast_transform(executor, device, _):
    from thunder.core.transforms import autocast

    def f(a, b, c):
        return a @ (b + c)

    # The following functions needs to be updated as autocast_impls grows.
    def g(a, b, c):
        return a + b - c

    def h(a, b, c):
        return (a @ b) + c

    for func, should_autocast in ((f, True), (g, False), (h, False)):
        dtype = thunder.bfloat16 if device == "cpu" else thunder.float16
        torch_dtype = ltorch.to_torch_dtype(dtype)
        x, y, z = [torch.randn((2, 2), device=device, dtype=torch.float32) for _ in range(3)]
        compiled = thunder.compile_with_info(
            autocast(func, dtype=dtype),
            executors_list=executor.executors_list(),
            # NOTE(crcrpar): preprocessing needs to be disabled as this transform would introduce
            # nonlocals that are not supported.
            disable_preprocessing=True,
        )
        out, traces = compiled(x, y, z)
        assert out.dtype == (torch_dtype if should_autocast else torch.float32), traces[-1]

        # note(crcrpar): This test could be broken in the future as thunder autocast develops.
        devicetype = torch.device(device).type
        with torch.autocast(device_type=devicetype, dtype=torch_dtype):
            torch_output = func(x, y, z)
        assert out.dtype == torch_output.dtype


@instantiate(dtypes=NOTHING)
def test_torch_scaled_dot_product_attention_non_decomposed(executor, device, _):
    n_embd = 32
    B = 2
    qkv = make_tensor(B, n_embd, 3 * n_embd, device=device, dtype=torch.float32)

    def func(qkv):
        # Preprocessing doesn't support nonlocal variables yet, so
        # we need to define the constants here.
        n_embd = 32
        n_head = 16
        B = 2
        T = 32
        C = n_embd
        q, k, v = qkv.split(n_embd, dim=2)  # Results in 3 non-contiguous but "viewable" tensors
        k = k.view(B, T, n_head, C // n_head).transpose(1, 2)  # (B, nh, T, hs)
        q = q.view(B, T, n_head, C // n_head).transpose(1, 2)  # (B, nh, T, hs)
        v = v.view(B, T, n_head, C // n_head).transpose(1, 2)  # (B, nh, T, hs)
        y = torch.nn.functional.scaled_dot_product_attention(q, k, v, attn_mask=None, dropout_p=0, is_causal=True)
        return y

    compiled = thunder.compile(func, executors_list=executor.executors_list())
    out = compiled(qkv)
    history = thunder.last_traces(compiled)
    torch.testing.assert_close(out, func(qkv))
    assert "torch.nn.functional.scaled_dot_product_attention" in tuple(
        bsym.sym.id for bsym in history[-1].bound_symbols
    )


@instantiate(dtypes=NOTHING)
def test_cse(executor, device, _):
    from thunder.core.pytree import tree_flatten

    def func(x, y, device):
        a = x + y
        b = y - x
        c = x + y  # Expected to be removed in favor of `a`.
        d = y - x  # Expected to be removed in favor of `b`.
        z = a + b  # Expected to be intact.
        w = c + d  # Expected to be converted to `w = a + b` and then removed in favor of `z`.
        m = w + 1  # Expected to be updated to `m = z + 1`.
        a = clang.uniform(w.shape, device=device, dtype=thunder.float16)
        b = clang.uniform(w.shape, device=device, dtype=thunder.float16)
        c = clang.uniform(z.shape, device=device, dtype=thunder.float16)
        d = clang.uniform(z.shape, device=device, dtype=thunder.float16)
        return z, w, m, (a, b, c, d)

    x, y = [make_tensor((2, 2), device=device, dtype=torch.float32) for _ in range(2)]
    compiled_func = thunder.compile(
        func,
        disable_preprocessing=True,
        executors_list=executor.executors_list(),
    )
    compiled_func(x, y, device)
    traces = thunder.last_traces(compiled_func)
    flatten_dce_trace = [
        t for t in traces if t._provenance is not None and t._provenance.pss.startswith("Dead Code Elimination")
    ][1]
    flatten_cse_trace = [
        t
        for t in traces
        if t._provenance is not None and t._provenance.pss.startswith("Common Subexpression Elimination")
    ][0]
    # CSE is supposed to remove `c`, `d`, and `w`.
    assert len(flatten_cse_trace.bound_symbols) == len(flatten_dce_trace.bound_symbols) - 3
    assert len([bsym for bsym in flatten_cse_trace.bound_symbols if bsym.sym.id == prims.PrimIDs.UNIFORM]) == 4

    assert [t.name for t in tree_flatten(flatten_cse_trace.output)[0]] == ["t4", "t4", "t6", "t7", "t8", "t9", "t10"]


# @instantiate(
#     dtypes=NOTHING,
# )
# def test_transforms_vmap_jvp(executor, device, _):
#     from thunder.core.transforms import vmap, jvp

#     def func(a, b):
#         assert isinstance(a, proxies.TensorProxy)
#         assert isinstance(b, proxies.TensorProxy)
#         assert a.ndim == 1
#         assert a.shape == b.shape
#         c = tlang.sin(a)
#         return tlang.mul(tlang.add(c, b), 1)

#     a = torch.ones(2, 3, device=device, dtype=torch.float32)
#     b = torch.ones(2, 3, device=device, dtype=torch.float32) * 2

#     args = (a, b)
#     out_p, out_t = thunder.make_traced(vmap(jvp(func), out_dims=(0, 0)), executor=executor)(args, args)
#     expected_out_p = torch.sin(a) + b
#     assert_close(out_p, expected_out_p)
#     expected_out_t = torch.cos(a) + b
#     assert_close(out_t, expected_out_t)


# @instantiate(
#     dtypes=NOTHING,
# )
# def test_transforms_jvp_vmap(executor, device, _):
#     from thunder.core.transforms import vmap, jvp

#     def func(a, b):
#         assert isinstance(a, proxies.TensorProxy)
#         assert isinstance(b, proxies.TensorProxy)
#         assert a.ndim == 1
#         assert a.shape == b.shape
#         c = tlang.sin(a)
#         return tlang.mul(tlang.add(c, b), 1)

#     a = torch.ones(2, 3, device=device, dtype=torch.float32)
#     b = torch.ones(2, 3, device=device, dtype=torch.float32) * 2

#     args = (a, b)
#     out_p, out_t = thunder.make_traced(jvp(vmap(func, out_dims=(0, 0))), executor=executor)(args, args)
#     expected_out_p = torch.sin(a) + b
#     assert_close(out_p, expected_out_p)
#     expected_out_t = torch.cos(a) + b
#     assert_close(out_t, expected_out_t)


# @instantiate(
#     dtypes=NOTHING,
# )
# def test_transforms_jvp(executor, device, _):
#     from thunder.core.transforms import jvp, inline, identity

#     def func(a, b):
#         c = tlang.sin(a)
#         return tlang.mul(tlang.add(c, b), 1)

#     a = torch.ones(2, 3, device=device, dtype=torch.float32)
#     b = torch.ones(2, 3, device=device, dtype=torch.float32) * 2

#     primals = (a, b)
#     tangents = (a, b)
#     out_p, out_t = thunder.make_traced(inline(identity(jvp(identity(func)))), executor=executor)(primals, tangents)
#     expected_out_p = torch.sin(a) + b
#     expected_out_t = torch.cos(a) + b
#     assert_close(out_p, expected_out_p)
#     assert_close(out_t, expected_out_t)


# @instantiate(
#     dtypes=NOTHING,
# )
# def test_transforms_jvp_no_inline(executor, device, _):
#     from thunder.core.transforms import jvp, inline, identity

#     def func(a, b):
#         c = tlang.sin(a)
#         return tlang.mul(tlang.add(c, b), 1)

#     a = torch.ones(2, 3, device=device, dtype=torch.float32)
#     b = torch.ones(2, 3, device=device, dtype=torch.float32) * 2

#     primals = (a, b)
#     tangents = (a, b)
#     out_p, out_t = thunder.make_traced(jvp(func), executor=executor)(primals, tangents)
#     expected_out_p = torch.sin(a) + b
#     expected_out_t = torch.cos(a) + b
#     assert_close(out_p, expected_out_p)
#     assert_close(out_t, expected_out_t)


# @instantiate(
#     dtypes=NOTHING,
# )
# def test_transforms_vmap_sum(executor, device, _):
#     from thunder.core.transforms import vmap

#     def func(a):
#         assert isinstance(a, proxies.TensorProxy)
#         assert a.ndim == 1
#         return ttorch.sum(a)

#     a = torch.ones(2, 3, device=device, dtype=torch.float32)

#     out = thunder.make_traced(vmap(func, out_dims=0), executor="torch")(a)
#     expected_out = torch.sum(a, dim=1)
#     assert_close(out, expected_out)


# @instantiate(
#     dtypes=NOTHING,
# )
# def test_transforms_jvp_python_number(executor, device, _):
#     from thunder.core.transforms import jvp, inline

#     scalars = (
#         2,
#         2.0,
#         True,
#     )
#     for scalar in scalars:

#         def func(a):
#             return tlang.mul(a, scalar)

#         a = make_tensor((2, 3), device=device, dtype=torch.float32)

#         primals = (a,)
#         tangents = (a,)
#         out_p, out_t = thunder.make_traced(inline(jvp(func)), executor=executor)(primals, tangents)

#         expected_out_p = a * scalar
#         expected_out_t = a * scalar
#         assert_close(out_p, expected_out_p)
#         assert_close(out_t, expected_out_t)


# @instantiate(
#     dtypes=NOTHING,
#     executors=[
#         TorchEx(),
#     ],
# )
# def test_get_executor(executor, device, _):
#     from thunder import _get_executor
#     from thunder.executors.torch import torchCtx

#     with pytest.raises(ValueError, match="No executor specified!"):
#         _get_executor(None)

#     ex = _get_executor(executor)
#     if executor.name == "TorchEx":
#         assert isinstance(ex, torchCtx)


# TODO Move to test_tensor_creation.py
# @instantiate(
#     dtypes=(thunder.float32, thunder.float16),
# )
# def test_uniform(executor, device, dtype):
#     if isinstance(executor, nvFuser) and LooseVersion(executor.version()) < "0.0.3":
#         pytest.skip("'uniform' not implemented before nvfuser 0.0.3")

#     thunder_uniform = executor.make_callable(tlang.uniform)
#     uniform = partial(thunder_uniform, dtype=dtype, device=device)

#     # lo, hi, shape
#     cases = (
#         (-12.0, 128, (8, 12, 7)),
#         (-0.3, 0.5, (2, 3, 4, 1)),
#         (0.0, 128.0, (2, 4)),
#         (-12.0, 0.0, (8, 3)),
#         (-1e-3, 1e-3, (8, 3)),
#         (0.0, 7.0, (0, 3)),
#         (0.0, 1.0, ()),
#     )

#     for lo, hi, shape in cases:
#         result = uniform(shape, lo, hi)
#         assert result.shape == shape
#         # note: numpy.random.uniform take value from [lo, hi)
#         #       But that doesn't seem to be the case for all backends. I'm relaxing this
#         if result.numel() != 0:
#             assert result.min() >= lo
#             assert result.max() <= hi

#     def foo():
#         return tlang.uniform([2, 3, 4], 0.5, 1.0, dtype=dtype, device=device)

#     thunder_static_uniform = executor.make_callable(foo)
#     result = thunder_static_uniform()
#     result.shape == (2, 3, 4)
#     result.min() >= 0.5
#     result.max() <= 1.0


# @instantiate(
#     dtypes=NOTHING,
# )
# def test_torch_gen_remove_last_used_variables(executor, device, _):
#     # This test is to make sure that the last used variables are removed
#     # from the generated code. This is important for freeing up memory.
#     from thunder.executors.torch import _fuse_region

#     def foo(a):
#         b = a + 1.0
#         c = b + 1.0
#         d = c + 1.0
#         e = d + 1.0
#         return e

#     a = make_tensor((2, 2), device=device, dtype=torch.float32)
#     trace = thunder.make_trace(foo, executor=executor)(a)
#     code_str, _ = _fuse_region((), [trace.outputs], trace.symbols)

#     # Check that there are for del commands
#     assert code_str.count("del") == 4

#     def foo(a):
#         b = a + 1.0
#         c = b + 1.0
#         d = c + 1.0
#         e = d + 1.0
#         return e, d

#     a = make_tensor((2, 2), device=device, dtype=torch.float32)
#     trace = thunder.make_trace(foo, executor=executor)(a)
#     code_str, _ = _fuse_region(_, [trace.outputs], trace.symbols, global_outputs=trace.outputs)
#     # Same as above, but now the last del should be removed since the variable
#     # is used in the output
#     assert code_str.count("del") == 3
