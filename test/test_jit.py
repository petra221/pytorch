from __future__ import division
import torch
import torch.jit
import torch.nn as nn
import torch.nn.functional as F
from contextlib import contextmanager
from itertools import product, chain
import torch.jit.frontend
from torch.autograd import Variable, Function
from torch.autograd.function import traceable
from torch.testing import assert_allclose
from torch.onnx import OperatorExportTypes
from torch._six import inf, PY2
from common import TestCase, run_tests, IS_WINDOWS, TEST_WITH_UBSAN, skipIfRocm, suppress_warnings
from textwrap import dedent
import os
import io
import sys
import unittest
import inspect
import textwrap
import numpy as np
import tempfile
import shutil
import warnings
from test_autograd import method_tests, create_input, unpack_variables, \
    exclude_tensor_method, non_differentiable, EXCLUDE_GRADCHECK, EXCLUDE_FUNCTIONAL
from copy import deepcopy
import random

from torch.jit.frontend import NotSupportedError
from torch.jit import BatchTensor

# For testing truediv in python 2
from test_module.future_div import div_int_future, div_float_future
from test_module.no_future_div import div_int_nofuture, div_float_nofuture

try:
    import torchvision
    HAS_TORCHVISION = True
except ImportError:
    HAS_TORCHVISION = False


skipIfNoTorchVision = unittest.skipIf(not HAS_TORCHVISION, "no torchvision")

RUN_CUDA = torch.cuda.is_available()
RUN_CUDA_HALF = RUN_CUDA
if torch.cuda.is_available():
    CUDA_VERSION = torch._C._cuda_getCompiledVersion()
    for d in range(torch.cuda.device_count()):
        major = torch.cuda.get_device_capability(d)[0]
        if (CUDA_VERSION < 8000 and major >= 6) or (CUDA_VERSION < 9000 and major >= 7):
            RUN_CUDA = False
        if (CUDA_VERSION < 9000 or major < 6):
            RUN_CUDA_HALF = False

RUN_CUDA_MULTI_GPU = RUN_CUDA and torch.cuda.device_count() > 1

PY35 = sys.version_info >= (3, 5)
WINDOWS = sys.platform == 'win32'


def LSTMCellF(input, hx, cx, *params):
    return LSTMCell(input, (hx, cx), *params)


def LSTMCell(input, hidden, w_ih, w_hh, b_ih=None, b_hh=None):
    hx, cx = hidden
    gates = F.linear(input, w_ih, b_ih) + F.linear(hx, w_hh, b_hh)

    ingate, forgetgate, cellgate, outgate = gates.chunk(4, 1)
    ingate = torch.sigmoid(ingate)
    forgetgate = torch.sigmoid(forgetgate)
    cellgate = torch.tanh(cellgate)
    outgate = torch.sigmoid(outgate)

    cy = (forgetgate * cx) + (ingate * cellgate)
    hy = outgate * torch.tanh(cy)
    return hy, cy


def LSTMCellC(*args, **kwargs):
    hy, cy = LSTMCellF(*args, **kwargs)
    return torch.cat((hy, cy))


def LSTMCellS(x, hx, cx, w_ih, w_hh, b_ih, b_hh):
    gates = x.mm(w_ih.t()) + hx.mm(w_hh.t()) + b_ih + b_hh
    ingate, forgetgate, cellgate, outgate = gates.chunk(4, 1)
    ingate = torch.sigmoid(ingate)
    forgetgate = torch.sigmoid(forgetgate)
    cellgate = torch.tanh(cellgate)
    outgate = torch.sigmoid(outgate)
    cy = (forgetgate * cx) + (ingate * cellgate)
    hy = outgate * torch.tanh(cy)
    return hy, cy


# Code reference: https://github.com/pytorch/translate/blob/master/pytorch_translate/rnn_cell.py#L27:44
def MiLSTMCell(x, hx, cx, w_ih, w_hh, alpha, beta_i, beta_h, bias):
    Wx = x.mm(w_ih.t())
    Uz = hx.mm(w_hh.t())
    # Section 2.1 in https://arxiv.org/pdf/1606.06630.pdf
    gates = alpha * Wx * Uz + beta_i * Wx + beta_h * Uz + bias
    # Same as LSTMCell after this point
    ingate, forgetgate, cellgate, outgate = gates.chunk(4, 1)
    ingate = ingate.sigmoid()
    forgetgate = forgetgate.sigmoid()
    cellgate = cellgate.tanh()
    outgate = outgate.sigmoid()
    cy = (forgetgate * cx) + (ingate * cellgate)
    hy = outgate * cy.tanh()
    return hy, cy


def canonical(graph):
    return str(torch._C._jit_pass_canonicalize(graph))


def get_lstm_inputs(device, training=False):
    input = torch.randn(3, 10, dtype=torch.float, device=device, requires_grad=training)
    hx = torch.randn(3, 20, dtype=torch.float, device=device, requires_grad=training)
    cx = torch.randn(3, 20, dtype=torch.float, device=device, requires_grad=training)
    module = nn.LSTMCell(10, 20).to(device, torch.float)  # Just to allocate weights with correct sizes
    if training:
        params = tuple(module.parameters())
    else:
        params = tuple(p.requires_grad_(False) for p in module.parameters())
    return (input, hx, cx) + params


def get_milstm_inputs(device, training=False):
    minibatch = 3
    input_size = 10
    hidden_size = 20
    x = torch.randn(minibatch, input_size, device=device, dtype=torch.float)
    hx = torch.randn(minibatch, hidden_size, device=device, dtype=torch.float)
    cx = torch.randn(minibatch, hidden_size, device=device, dtype=torch.float)

    ih = torch.randn(4 * hidden_size, input_size, device=device, dtype=torch.float, requires_grad=training)
    hh = torch.randn(4 * hidden_size, hidden_size, device=device, dtype=torch.float, requires_grad=training)
    alpha = torch.randn(4 * hidden_size, dtype=torch.float, device=device, requires_grad=training)
    ibeta = torch.randn(4 * hidden_size, dtype=torch.float, device=device, requires_grad=training)
    hbeta = torch.randn(4 * hidden_size, dtype=torch.float, device=device, requires_grad=training)
    bias = torch.randn(4 * hidden_size, dtype=torch.float, device=device, requires_grad=training)
    return x, hx, cx, ih, hh, alpha, ibeta, hbeta, bias


def get_fn(file_name, script_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(file_name, script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fn = module.fn
    return fn


def get_execution_plan(graph_executor_state):
    execution_plans = list(graph_executor_state.execution_plans.values())
    num_plans = len(execution_plans)
    if num_plans != 1:
        raise RuntimeError('This test assumes this GraphExecutor should '
                           'only have one execution plan, got: {}'.format(num_plans))
    return execution_plans[0]


def get_grad_executor(plan_state):
    if len(list(plan_state.graph.nodes())) != 1:
        raise RuntimeError("Can't get a grad_executor for a non-differentiable graph")
    grad_executors = list(plan_state.code.grad_executors())
    return grad_executors[0]


def backward_graph(script_module):
    if not isinstance(script_module, torch.jit.ScriptModule):
        raise RuntimeError('Expected ScriptModule')
    ge_state = script_module.get_debug_state()
    fwd_plan = get_execution_plan(ge_state)
    grad_executor = get_grad_executor(fwd_plan)
    bwd_plan = get_execution_plan(grad_executor.get_debug_state())
    # Running JIT passes requires that we own the graph (with a shared_ptr).
    # The debug state struct does not own its graph so we make a copy of it.
    return bwd_plan.graph.copy()


# make it easy to quicky define/trace a function for these tests
def _trace(*args, **kwargs):
    def wrapper(func):
        return torch.jit.trace(func, args, **kwargs)
    return wrapper


# Python equivalents for the empty list construction builtins. We need
# these otherwise the tests won't execute in regular Python mode.
def _construct_empty_int_list():
    return []


def _construct_empty_float_list():
    return []


def _construct_empty_tensor_list():
    return []


def enable_cpu_fuser(fn):
    def wrapper(*args, **kwargs):
        torch._C._jit_override_can_fuse_on_cpu(True)
        try:
            fn(*args, **kwargs)
        except Exception:
            torch._C._jit_override_can_fuse_on_cpu(False)
            raise
    return wrapper


class JitTestCase(TestCase):
    _do_cuda_memory_leak_check = True
    _restored_warnings = False

    def setUp(self):
        # unittest overrides all warning filters and forces all of them to show up
        # after we install our own to silence those coming from inside PyTorch.
        # This will ensure that our filter still takes precedence.
        if not JitTestCase._restored_warnings:
            torch.jit.TracerWarning.ignore_lib_warnings()
            JitTestCase._restored_warnings = True

    def getExportImportCopy(self, m):
        # Ideally we would like to not have to manually delete the file, but NamedTemporaryFile
        # opens the file, and it cannot be opened multiple times in Windows. To support Windows,
        # close the file after creation and try to remove it manually
        f = tempfile.NamedTemporaryFile(delete=False)
        try:
            f.close()
            m.save(f.name)
            imported = torch.jit.load(f.name)
        finally:
            os.unlink(f.name)
        return imported

    def assertGraphContains(self, graph, kind):
        self.assertTrue(any(n.kind() == kind for n in graph.nodes()))

    def assertExpectedONNXGraph(self, trace, *args, **kwargs):
        torch.onnx._optimize_trace(trace, operator_export_type=OperatorExportTypes.ONNX)
        self.assertExpectedGraph(trace, *args, **kwargs)

    def assertExpectedGraph(self, trace, *args, **kwargs):
        if isinstance(trace, torch._C.Graph):
            graph = trace
        else:
            graph = trace.graph()

        torch._C._jit_pass_lint(graph)
        torch._C._jit_pass_dce(graph)
        torch._C._jit_pass_lint(graph)
        graph = torch._C._jit_pass_canonicalize(graph)
        torch._C._jit_pass_lint(graph)
        self.assertExpected(str(graph), *args, **kwargs)

    def run_pass(self, name, trace):
        if isinstance(trace, torch._C.Graph):
            graph = trace
            set_graph = False
        else:
            set_graph = True
            graph = trace.graph()

        torch._C._jit_pass_lint(graph)
        result = getattr(torch._C, '_jit_pass_' + name)(graph)
        if result is not None:
            graph = result
        torch._C._jit_pass_lint(graph)

        if set_graph:
            trace.set_graph(graph)
        return graph

    def checkTrace(self, func, reference_tensors, input_tensors=None,
                   optimize=True, drop=None, allow_unused=False, verbose=False,
                   inputs_require_grads=True, check_tolerance=1e-5, export_import=True):
        # TODO: check gradients for parameters, not just inputs
        def allSum(vs):
            # drop allows us to remove some values from ever being used
            # to test unused outputs
            if drop is not None:
                vs = vs[:-drop]
            # we don't want all the grad for all the outputs to be the same
            # so we multiply each by a constant
            return sum([(i + 1) * v.sum() for i, v in enumerate(vs) if v is not None])
        if input_tensors is None:
            input_tensors = reference_tensors

        nograd_inputs = reference_tensors
        if inputs_require_grads:
            recording_inputs = [t.clone().requires_grad_() for t in reference_tensors]
        else:
            recording_inputs = reference_tensors

        if isinstance(func, torch._C.Graph):
            ge = torch._C.GraphExecutor(func, optimize)
        else:
            ge = torch.jit.trace(func, input_tensors, optimize=optimize, check_tolerance=check_tolerance)

        if export_import:
            ge = self.getExportImportCopy(ge)

        if verbose:
            print(ge.graph)

        # test no gradients case
        outputs = func(*nograd_inputs)
        outputs_ge = ge(*nograd_inputs)
        self.assertEqual(outputs, outputs_ge)

        # test single grad case
        outputs = func(*recording_inputs)
        if inputs_require_grads:
            grads = torch.autograd.grad(allSum(outputs), recording_inputs,
                                        allow_unused=allow_unused)

        outputs_ge = ge(*recording_inputs)
        if inputs_require_grads:
            grads_ge = torch.autograd.grad(allSum(outputs_ge), recording_inputs,
                                           allow_unused=allow_unused)
        self.assertEqual(outputs, outputs_ge)
        if inputs_require_grads:
            self.assertEqual(grads, grads_ge)

        # test the grad grad case

        outputs = func(*recording_inputs)
        l1 = allSum(outputs)
        if inputs_require_grads:
            grads = torch.autograd.grad(l1, recording_inputs, create_graph=True,
                                        allow_unused=allow_unused)
        if inputs_require_grads:
            l2 = (allSum(grads) * l1)
            grads2 = torch.autograd.grad(l2, recording_inputs, allow_unused=allow_unused)

        if inputs_require_grads:
            recording_inputs = [Variable(t, requires_grad=True)
                                for t in reference_tensors]

        outputs_ge = ge(*recording_inputs)
        l1_ge = allSum(outputs_ge)
        if inputs_require_grads:
            grads_ge = torch.autograd.grad(
                l1_ge, recording_inputs, create_graph=True, allow_unused=allow_unused)

        if inputs_require_grads:
            l2_ge = (allSum(grads_ge) * l1_ge)
            grads2_ge = torch.autograd.grad(l2_ge, recording_inputs, allow_unused=allow_unused)

        self.assertEqual(outputs, outputs_ge)
        if inputs_require_grads:
            self.assertEqual(grads, grads_ge)
            for g2, g2_ge in zip(grads2, grads2_ge):
                if g2 is None and g2_ge is None:
                    continue
                self.assertTrue(torch.allclose(g2, g2_ge, atol=7e-4, rtol=1e-4))

        return ge

    def assertAllFused(self, graph):
        self.assertTrue(all(node.kind() in {'prim::Constant', 'prim::FusionGroup'} for node in graph.nodes()))
        self.assertTrue([node.kind() for node in graph.nodes()].count('prim::FusionGroup') == 1)

    def assertExportImport(self, trace, inputs):
        graph = trace if isinstance(trace, torch._C.Graph) else trace.graph()
        m = torch.jit.ScriptModule()
        m._create_method_from_graph("forward", graph)
        m_import = self.getExportImportCopy(m)

        self.assertEqual(m.forward(*inputs), m_import.forward(*inputs))


class TestJit(JitTestCase):

    def test_simple(self):
        x = torch.tensor([0.4], requires_grad=True)
        y = torch.tensor([0.7], requires_grad=True)

        def f(x, y):
            return torch.sigmoid(torch.tanh(x * (x + y)))

        trace, z = torch.jit.get_trace_graph(f, (x, y))
        self.assertExpectedGraph(trace)
        self.assertExportImport(trace, (x, y))

    def test_typeas_trace_check(self):
        a = torch.tensor([0.4], requires_grad=True)
        b = torch.tensor([0.7], requires_grad=True)

        def f(x, y):
            return x.type_as(y)

        trace = torch.jit.trace(f, (a, b))

    def test_peephole(self):
        a = torch.tensor([0.4])
        b = torch.tensor([0.7])
        c = torch.tensor([0], dtype=torch.int32)

        def f(x, y):
            return x.type_as(y)

        tf = torch.jit.trace(f, (a, b))
        self.run_pass('peephole', tf.graph)
        self.assertExpectedGraph(tf.graph)
        tf2 = torch.jit.trace(f, (a, c))
        s = str(tf2.graph)
        self.run_pass('peephole', tf2.graph)
        self.assertEqual(s, str(s))

    def test_peephole_dynamic(self):
        def f(x, y):
            return x.type_as(y)

        fn = torch.jit.script(f)
        s = str(fn.graph)
        torch._C._jit_pass_peephole(fn.graph)
        self.assertEqual(s, str(fn.graph))

    @unittest.skipIf(not RUN_CUDA, "cpp tests require CUDA")
    def test_peephole_cuda(self):
        a = torch.tensor([0.4], device='cpu')
        b = torch.tensor([0.7], device='cuda')
        c = torch.tensor([0.7], device='cuda')

        def f(x, y):
            return x.type_as(y)

        trace = torch.jit.trace(f, (a, c))
        s = str(trace.graph)
        self.run_pass('peephole', trace.graph)
        self.assertEqual(s, str(trace.graph))
        trace = torch.jit.trace(f, (b, c))
        self.run_pass('peephole', trace.graph)
        self.assertExpectedGraph(trace.graph, subname="same_device")

    def test_index(self):
        x = torch.tensor([0.4], requires_grad=True)
        y = torch.tensor([0], dtype=torch.int64)

        def fn(x, y):
            return x[y]

        fn_traced = torch.jit.trace(fn, (x, y,))

        self.assertEqual(fn(x, y), fn_traced(x, y))

    def test_disabled(self):
        torch.jit._enabled = False
        try:
            def f(x, y):
                return x + y

            self.assertIs(torch.jit.trace(f, (torch.randn(2, 2), torch.randn(2, 2))), f)
            self.assertIs(torch.jit.script(f), f)

            class MyModule(torch.jit.ScriptModule):
                @torch.jit.script_method
                def method(self, x):
                    return x

            # XXX: Unfortunately ScriptModule won't simply become Module now,
            # because that requires disabling the JIT at startup time, which
            # we can't do in here.
            # We need to or those two conditions to make it work with all versions of Python
            self.assertTrue(inspect.ismethod(MyModule.method) or inspect.isfunction(MyModule.method))
        finally:
            torch.jit._enabled = True

    def test_train_eval(self):
        class Sub(nn.Module):
            def forward(self, input):
                if self.training:
                    return input
                else:
                    return -input

        class MyModule(torch.jit.ScriptModule):
            def __init__(self):
                super(MyModule, self).__init__()
                self.sub = Sub()

            @torch.jit.script_method
            def forward(self, input):
                return self.sub(input) + 1

        m = MyModule()
        input = torch.rand(3, 4)
        self.assertEqual(input + 1, m(input))
        m.eval()
        self.assertEqual(-input + 1, m(input))

    def test_train_eval_const(self):
        class MyModule(torch.jit.ScriptModule):
            __constants__ = ['training']

            def __init__(self):
                super(MyModule, self).__init__()
                # TODO: it is illegal to try to call
                # eval/train because training has already
                # been set. Consider allowing
                # constants to be mutable until the end of __init__

            @torch.jit.script_method
            def forward(self, input):
                if self.training:
                    x = 2 * input
                else:
                    x = -input
                return x + 1

        m = MyModule()
        input = torch.rand(3, 4)
        self.assertEqual(2 * input + 1, m(input))

    # Backwards tracing was broken for indexing by a constant,
    # because it's internally implemented using as_strided,
    # and we attempted to trace its derivative (which is not
    # currently supported.)  It currently works because
    # slice() is now not marked as traceable.
    def test_index_constant(self):
        x = torch.tensor([0.4], requires_grad=True)

        def fn(x):
            return x[0]

        def run(f):
            y = f(x)
            grad = torch.autograd.grad(y, x)[0].clone()
            return y, grad

        traced_fn = torch.jit.trace(fn, torch.ones(1))
        self.assertEqual(run(fn), run(traced_fn))

    def test_scopes(self):
        x = torch.tensor([0.4], requires_grad=True)
        y = torch.tensor([0.7], requires_grad=True)

        def f(x, y):
            out = x + y
            with torch.jit.scope('Foo'):
                out = x * out
                with torch.jit.scope('Bar'):
                    out = torch.tanh(out)
                out = torch.sigmoid(out)
            return out

        trace, z = torch.jit.get_trace_graph(f, (x, y))
        self.assertExpectedGraph(trace)
        self.assertExportImport(trace, (x, y))

    def test_scopes_intermediate_node(self):

        class Net(nn.Module):
            def forward(self, x):
                return F.log_softmax(x, dim=0)

        net = Net()
        t = torch.ones(2, requires_grad=True)
        trace, _ = torch.jit.get_trace_graph(net, (t,))
        self.assertExportImport(trace, (t,))
        self.assertExpectedONNXGraph(trace)

    def test_scopes_identity_node(self):

        class Net(nn.Module):

            def __init__(self):
                super(Net, self).__init__()
                self.features = nn.Sequential(
                    nn.Conv2d(3, 64, kernel_size=11, stride=4, padding=2),
                    nn.ReLU(inplace=True),
                    nn.MaxPool2d(kernel_size=3, stride=2),
                )

            def forward(self, x):
                x = self.features(x)
                return x

        model = Net()

        t = torch.ones(1, 3, 227, 227, requires_grad=True)

        with torch.onnx.set_training(model, False):
            trace, _ = torch.jit.get_trace_graph(model, (t,))

        self.assertExportImport(trace, (t,) + tuple(model.parameters()))
        self.assertExpectedONNXGraph(trace)

    @unittest.skipIf(not IS_WINDOWS, "Testing Fuse skipped on windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    def test_windows_fuse(self):
        def scaleshift(x, scale, shift):
            return x * scale + shift

        graph = torch.jit.script(scaleshift).graph

        inputs = [
            torch.randn(4, 4, dtype=torch.float, device='cuda'),
            torch.randn(4, dtype=torch.float, device='cuda'),
            torch.randn(4, dtype=torch.float, device='cuda'),
        ]

        ge = self.checkTrace(scaleshift, inputs)
        fuse_graph = ge.graph_for(*inputs)

        def run_graph(graph, inputs):
            m = torch.jit.ScriptModule()
            m._create_method_from_graph("forward", graph)
            return m(*inputs)

        self.assertEqual(run_graph(graph, inputs), run_graph(fuse_graph, inputs))

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @skipIfRocm
    def test_broadcast_fusion_cuda(self):
        def scaleshift(x, scale, shift):
            return x * scale + shift

        inputs = [
            torch.randn(4, 4, dtype=torch.float, device='cuda'),
            torch.randn(4, dtype=torch.float, device='cuda'),
            torch.randn(4, dtype=torch.float, device='cuda'),
        ]
        ge = self.checkTrace(scaleshift, inputs)
        self.assertExpectedGraph(ge.graph_for(*inputs))

    # TODO: Fuser doesn't work at all when inputs require grad. Fix that
    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @skipIfRocm
    def test_lstm_fusion_cuda(self):
        inputs = get_lstm_inputs('cuda')
        ge = self.checkTrace(LSTMCellF, inputs)
        self.assertExpectedGraph(ge.graph_for(*inputs))

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skip("Test is flaky, see https://github.com/pytorch/pytorch/issues/8746")
    @enable_cpu_fuser
    def test_lstm_fusion_cpu(self):
        inputs = get_lstm_inputs('cpu')
        try:
            ge = self.checkTrace(LSTMCellF, inputs)
            self.assertExpectedGraph(ge.graph_for(*inputs))
        except RuntimeError as e:
            if 'Failed to compile' in e.args[0]:
                warnings.warn('CPU fuser test has failed! This is not a hard failure, '
                              'because the kernels sometimes trigger bugs in compilers '
                              '(most notably GCC 7.2).')
                raise unittest.SkipTest('Failed to compile')
            else:
                raise

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @skipIfRocm
    def test_lstm_fusion_concat_cuda(self):
        inputs = get_lstm_inputs('cuda')
        ge = self.checkTrace(LSTMCellC, inputs)
        self.assertExpectedGraph(ge.graph_for(*inputs))

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @skipIfRocm
    def test_concat_fusion_cuda(self):
        hx = torch.randn(3, 20, dtype=torch.float, device='cuda')
        cx = torch.randn(3, 20, dtype=torch.float, device='cuda')

        def foo(hx, cx):
            return torch.cat((hx + cx, hx * cx))

        ge = self.checkTrace(foo, (hx, cx))
        self.assertExpectedGraph(ge.graph_for(hx, cx))

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @skipIfRocm
    def test_concat_fusion_invariant_cuda(self):
        # Invariant: the output of prim::FusedConcat may
        # not be an input to any node inside the FusionGroup.
        def fn(x, y, z):
            x1 = x + y
            y1 = x - y
            w = torch.cat([x1, y1])
            return w + z

        x = torch.randn(2, 2, dtype=torch.float, device='cuda')
        y = torch.randn(2, 2, dtype=torch.float, device='cuda')
        z = torch.randn(4, 2, dtype=torch.float, device='cuda')
        ge = self.checkTrace(fn, (x, y, z))
        self.assertExpectedGraph(ge.graph_for(x, y, z))

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @skipIfRocm
    def test_fusion_distribute_cuda(self):
        def f(x, y):
            z1, z2 = (x + y).chunk(2, dim=1)
            return z1 * z2

        x = torch.randn(4, 4, dtype=torch.float, device='cuda')
        y = torch.randn(4, 4, dtype=torch.float, device='cuda')

        ge = self.checkTrace(f, (x, y))
        self.assertExpectedGraph(ge.graph_for(x, y))

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @skipIfRocm
    def test_fusion_rand_cuda(self):
        class M(torch.jit.ScriptModule):
            __constants__ = ['d']

            def __init__(self):
                self.d = torch.device('cuda')

            @torch.jit.script_method
            def create(self, x):
                return x * x + x + torch.rand_like(x)

        x = torch.zeros([3, 4, 5], dtype=torch.float, device='cuda')
        m = M()
        out1 = m.create(x)
        out2 = m.create(x)
        self.assertNotEqual(out1, out2)
        self.assertTrue(torch.all(out1 >= 0))
        self.assertTrue(torch.all(out1 < 1))
        self.assertTrue(torch.all(out2 >= 0))
        self.assertTrue(torch.all(out2 < 1))

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @skipIfRocm
    def test_fusion_arg_configurations_cuda(self):
        # A smoke test to make sure we won't use the same kernel for contiguous
        # and non-contiguous arguments.
        # TODO: add optionally enabled debug counters to the fuser to verify
        #       that we really can tell the difference between configurations
        def f(x, y):
            z1, z2 = (x + y).chunk(2, dim=1)
            return z1 * z2

        x = torch.randn(4, 4, dtype=torch.float, device='cuda')
        y = torch.randn(4, 4, dtype=torch.float, device='cuda')
        traced_f = torch.jit.trace(f, (x, y,))
        self.assertEqual(traced_f(x.t().contiguous(), y), traced_f(x.t(), y))

    @staticmethod
    def fn_test_comparison_gt_lt(x, y):
        mask = (x > 0).type_as(x)
        z = x * mask + y
        mask = (x < 0).type_as(x)
        z = z * mask + y
        return z

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @skipIfRocm
    def test_comparison_gt_lt_cuda(self):
        x = torch.randn(4, 4, dtype=torch.float, device='cuda')
        y = torch.randn(4, 4, dtype=torch.float, device='cuda')

        ge = self.checkTrace(self.fn_test_comparison_gt_lt, (x, y))
        self.assertAllFused(ge.graph_for(x, y))

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @skipIfRocm
    def test_comparison_ge_le_cuda(self):
        def f(x, y):
            mask = (x >= 0).type_as(x)
            z = x * mask + y
            mask = (x <= 0).type_as(x)
            z = z * mask + y
            return z

        x = torch.randn(4, 4, dtype=torch.float, device='cuda')
        y = torch.randn(4, 4, dtype=torch.float, device='cuda')

        ge = self.checkTrace(f, (x, y))
        self.assertAllFused(ge.graph_for(x, y))

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @skipIfRocm
    def test_comparison_eq_ne(self):
        def f(x, y):
            mask = (x == 0).type_as(x)
            z = x * mask + y
            mask = (x != 0).type_as(x)
            z = z * mask + y
            return z

        x = torch.randn(4, 4, dtype=torch.float, device='cuda')
        y = torch.randn(4, 4, dtype=torch.float, device='cuda')

        ge = self.checkTrace(f, (x, y))
        self.assertAllFused(ge.graph_for(x, y))

    @staticmethod
    def fn_test_relu(x, y):
        return F.relu(x + .5 * y)

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @skipIfRocm
    def test_relu_cuda(self):
        x = torch.randn(4, 4, dtype=torch.float, device='cuda')
        y = torch.randn(4, 4, dtype=torch.float, device='cuda')

        ge = self.checkTrace(self.fn_test_relu, (x, y))

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    def test_small_constant_cuda(self):
        def fn_test_small_constant(x, y):
            return (1e-8 * x + 5e-9 * y) * 1e8
        x = torch.randn(4, 4, dtype=torch.float, device='cuda')
        y = torch.randn(4, 4, dtype=torch.float, device='cuda')

        ge = self.checkTrace(fn_test_small_constant, (x, y))

    @staticmethod
    def fn_test_exp(x, y):
        return (x + .5 * y).exp()

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @skipIfRocm
    def test_exp_cuda(self):
        x = torch.randn(4, 4, dtype=torch.float, device='cuda')
        y = torch.randn(4, 4, dtype=torch.float, device='cuda')

        ge = self.checkTrace(self.fn_test_exp, (x, y))

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @unittest.skipIf(not RUN_CUDA_HALF, "no half support")
    def test_cuda_half(self):
        x = torch.randn(4, 4, dtype=torch.half, device='cuda')
        y = torch.randn(4, 4, dtype=torch.half, device='cuda')

        funcs = [
            self.fn_test_comparison_gt_lt,
            self.fn_test_relu,
            self.fn_test_exp
        ]

        # Note: Non fused inputs must be float to prevent loss of precision
        inputs = (x.float(), y.float())
        fusion_inputs = (x, y)
        for fn in funcs:
            local_inputs = [t.clone().requires_grad_() for t in inputs]
            local_fusion_inputs = [t.clone().requires_grad_() for t in fusion_inputs]

            # Verifies outputs
            fusion = torch.jit.trace(fn, local_fusion_inputs, check_trace=False, optimize=True)
            outputs = fn(*local_inputs)
            fusion_outputs = fusion(*local_fusion_inputs)
            outputs_half = [t.half() for t in outputs]
            self.assertEqual(outputs_half, fusion_outputs)

            # Verifies gradients
            for output, fusion_output in zip(outputs_half, fusion_outputs):
                grads = torch.autograd.grad(
                    output.float().sum(), local_inputs, allow_unused=True, retain_graph=True)
                fusion_grads = torch.autograd.grad(
                    fusion_output.sum(), local_fusion_inputs, allow_unused=True, retain_graph=True)
                grads_half = [t.half() for t in grads]
                self.assertEqual(grads_half, fusion_grads)

    # TODO: adapt this test to check that GraphExecutor treats them differently
    @unittest.skip("Need to be adjusted to Graph Executor")
    def test_arg_configurations(self):
        """Different arg configurations should trigger different traces"""
        x = Variable(torch.FloatTensor(4, 4).uniform_())
        x_double = Variable(x.data.double())
        x_grad = Variable(x.data.clone(), requires_grad=True)
        y = Variable(torch.randn(4))

        configurations = [
            (x,),
            (x_double,),
            (x_grad,),
            (y,),
            ([x, x],),
            ([x, y],),
        ]
        if torch.cuda.is_available():
            x_cuda = Variable(x.data.cuda())
            configurations += [
                (x_cuda,),
                ([x, x_cuda],),
                ([x_cuda, x],),
                ([[x_cuda, x]],),
            ]
            if torch.cuda.device_count() > 1:
                x_cuda_1 = Variable(x.data.cuda(1))
                configurations += [
                    (x_cuda_1,),
                    ([x_cuda, x_cuda_1],),
                ]

        @torch.jit.compile(nderivs=0)
        def fn(*args):
            in_vars, _ = torch._C._jit_flatten(args)
            return in_vars[0] + 1

        for i, config in enumerate(configurations):
            self.assertFalse(fn.has_trace_for(*config))
            fn(*config)
            self.assertTrue(fn.has_trace_for(*config))
            for unk_config in configurations[i + 1:]:
                self.assertFalse(fn.has_trace_for(*unk_config))
        self.assertEqual(fn.hits, 0)

    def test_cse(self):
        x = torch.tensor([0.4, 0.3], requires_grad=True)
        y = torch.tensor([0.7, 0.5], requires_grad=True)

        def fn(x, y):
            w = (x + y) * (x + y) * (x + y)
            t = torch.tanh(w) + torch.tanh(w)
            z = (x + y) * (x + y) * (x + y) + t
            return z

        trace, _ = torch.jit.get_trace_graph(fn, (x, y))
        self.run_pass('cse', trace)
        self.assertExpectedGraph(trace)
        self.assertExportImport(trace, (x, y))

    def test_recursive_cse(self):
        x = torch.tensor([0.1])
        y = torch.tensor([0.2])

        def fn(x, y):
            z = x
            if bool(x + y > x):
                z = x + y
            return z

        graph = torch.jit.script(fn).graph
        self.run_pass('cse', graph)
        self.assertExpectedGraph(graph)

    def test_scalar(self):
        # NB: must not require grad; if it requires grad, it's always a Tensor
        x = torch.tensor(2.)
        y = torch.tensor(3.)

        def fn(x, y):
            return x - y
        trace, _ = torch.jit.get_trace_graph(fn, (x, y))

    def test_shape_analysis_broadcast(self):
        def broadcast(a, b):
            return a + b

        x = torch.randn(3, 1, 5, requires_grad=True)
        y = torch.randn(4, 1, 8, 5, requires_grad=True)

        graph = torch.jit.script(broadcast).graph
        torch._C._jit_pass_complete_shape_analysis(graph, (x, y), False)
        self.assertExpectedGraph(graph)

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA_MULTI_GPU, "needs non-zero device")
    @skipIfRocm
    def test_fuse_last_device_cuda(self):
        device = 'cuda:' + str(1)
        x = torch.tensor([0.4], dtype=torch.float, device=device)
        y = torch.tensor([0.7], dtype=torch.float, device=device)

        def doit(x, y):
            return torch.sigmoid(torch.tanh(x * (x + y) + x))

        ge = self.checkTrace(doit, (x, y))
        self.assertExpectedGraph(ge.graph_for(x, y))

    # TODO: update verify to work with GraphExecutors
    @unittest.skip("verify needs to be updated to work with GraphExecutors")
    def test_verify(self):
        x = torch.tensor([0.4], requires_grad=True)
        y = torch.tensor([0.7], requires_grad=True)

        @torch.jit.compile
        def f(x, y):
            z = torch.sigmoid(x * (x + y))
            w = torch.abs(x * x * x + y) + Variable(torch.ones(1))
            return z, w

        torch.jit.verify(f, (x, y), loss_fn=lambda z, w: z * w, devices=[])

    @suppress_warnings
    def test_constant(self):
        x = torch.randn(2, 2, requires_grad=True)

        def f(x):
            return x.matmul(torch.diag(torch.tensor([2., 2.])))

        self.checkTrace(f, (x,), (torch.ones(2, 2, requires_grad=True),))

    def test_legacy_fail(self):
        class MyLegacyFn(Function):
            def forward(self, x):
                return x

            def backward(self, grad_output):
                return grad_output

        x = torch.tensor([0.], requires_grad=True)
        with self.assertRaisesRegex(RuntimeError, "MyLegacyFn"):
            torch.jit.get_trace_graph(lambda x: MyLegacyFn()(x), (x,))

    def test_inplace_transplant(self):
        x = torch.tensor([0.], requires_grad=True)

        def fn(x):
            y = x.clone()
            y.add_(2)
            y.add_(3)
            return y

        trace, _ = torch.jit.get_trace_graph(fn, (x,))
        self.assertExpectedGraph(trace)
        self.assertExportImport(trace, (x,))

    def test_inplace_flags(self):
        class InplaceFn(Function):
            @staticmethod
            def forward(ctx, x):
                ctx.mark_dirty(x)
                return x.add_(1)

            @staticmethod
            def backward(ctx, go):
                return go

        class RegularFn(Function):
            @staticmethod
            def forward(ctx, x):
                return x.add(1)

            @staticmethod
            def backward(ctx, go):
                return go

        x = torch.tensor([0.], requires_grad=True)

        def fn(x):
            y = RegularFn.apply(x)
            y = InplaceFn.apply(y)
            y = InplaceFn.apply(y)
            y = RegularFn.apply(y)
            return y

        trace, _ = torch.jit.get_trace_graph(fn, (x,))
        self.run_pass('dce', trace)
        ops = [n for n in trace.graph().nodes()]
        for op in ops:
            self.assertTrue(op.hasAttribute('inplace'))
        inplace_flags = [False, True, True, False]
        for op, is_inplace in zip(ops, inplace_flags):
            self.assertEqual(op.i('inplace'), is_inplace)

    def test_inplace_check(self):
        class MyInplaceFn(Function):
            @staticmethod
            def forward(self, x):
                x.add_(1)
                self.mark_dirty(x)
                return x

            @staticmethod
            def backward(self, grad):
                return grad

        def fn(x):
            return MyInplaceFn.apply(x)

        x = torch.randn(5, 5)
        ge = torch._C.GraphExecutor(fn, (x,))
        with self.assertRaisesRegex(RuntimeError, 'inplace MyInplaceFn'):
            ge(x)

    def do_trace_size(self, requires_grad):
        def fn(x):
            return x.view(x.shape[1] * 2, x.size(0), 2)

        x = torch.randn(5, 2, 4, requires_grad=requires_grad)
        y = torch.randn(4, 8, 4, requires_grad=requires_grad)

        # Check that it behaves as expected
        traced_fn = torch.jit.trace(fn, x)
        self.assertEqual(traced_fn(y), fn(y))
        self.assertEqual(traced_fn(x), fn(x))

        # Check that the trace looks ok
        trace, _ = torch.jit.get_trace_graph(fn, (x,))
        self.assertExpectedGraph(trace)

    def test_trace_size(self):
        self.do_trace_size(False)

    # test the different graph_executor path that happens when
    # gradients are required and sizes are involved
    def test_trace_size_with_grad(self):
        self.do_trace_size(True)

    def test_trace_casts(self):
        casts = [
            lambda x: x.byte(),
            lambda x: x.float(),
            lambda x: x.cpu(),
            lambda x: x.to(device='cpu'),
            lambda x: x.to(dtype=torch.int64),
            lambda x: x.to(device='cpu', dtype=torch.float),
            lambda x: x.to(x)
        ]

        def assertContainsCast(trace):
            self.assertEqual(sum(n.kind() == 'aten::to' for n in trace.graph.nodes()), 1)

        for cast in casts:
            trace = torch.jit.trace(cast, torch.randn(2, 2))
            assertContainsCast(trace)
            x = torch.randn(2, 2)
            self.assertEqual(trace(x), cast(x))

        def to_tensor(x, y):
            return x.to(y)

        to_tensor_trace = torch.jit.trace(to_tensor, (torch.randn(2, 2), torch.randn(1, 8)))
        assertContainsCast(to_tensor_trace)
        x, y = torch.randn(2, 2), torch.randn(1, 10)
        self.assertEqual(to_tensor_trace(x, y), to_tensor(x, y))

    def test_trace_warn(self):
        def fn(x):
            int(x)  # Warning 1.
            y = x * 1
            if y:   # Warning 2.
                pass
            q = [x, x * 4]
            z = q[y]  # Warning 3.
            float(z)  # Warning 4.
            z.tolist()  # Warning 5.
            z.numpy()  # Warning 6.
            for elem in torch.ones(4, 4):  # Warning 7.
                pass
            return z + 4

        with warnings.catch_warnings(record=True) as warns:
            traced_fn = torch.jit.trace(fn, torch.tensor([1]))
        warns = [str(w.message) for w in warns]
        self.assertEqual(len(warns), 7)
        self.assertIn('a Python integer', warns[0])
        self.assertIn('a Python boolean', warns[1])
        self.assertIn('a Python index', warns[2])
        self.assertIn('a Python float', warns[3])
        self.assertIn('a Python list', warns[4])
        self.assertIn('a NumPy array', warns[5])
        self.assertIn('Iterating over', warns[6])

    def test_trace_tuple(self):
        def fn(x, y):
            return x, (x * y[1], x * y[0])

        x, y = torch.randn(2, 2), (torch.ones(2, 2), torch.randn(2, 2))
        traced_fn = torch.jit.trace(fn, (x, y))
        self.assertEqual(traced_fn(x, y), fn(x, y))
        self.assertExpectedGraph(traced_fn.graph)
        self.assertExportImport(traced_fn.graph, (x, y))

    def test_trace_random(self):
        def f(mean, std):
            return torch.normal(mean, std)

        traced = torch.jit.trace(f, (torch.zeros(2, 3), torch.ones(2, 3)), check_trace=False)
        mean, std = torch.zeros(5, 5), torch.ones(5, 5)
        with torch.random.fork_rng(devices=[]):
            output = f(mean, std)
        traced_output = traced(mean, std)
        self.assertEqual(output, traced_output)

    def test_trace_tensor_factory(self):
        def run(**kwargs):
            inputs_require_grads = kwargs.pop('inputs_require_grads', True)

            def fn(x):
                return x + torch.ones(2, 3, **kwargs)
            input = torch.ones(2, 3, **kwargs)
            self.checkTrace(fn, (input,), inputs_require_grads=inputs_require_grads)
            # check we recorded 'ones' and did not just record a constant
            tfn = torch.jit.trace(fn, input)
            self.assertTrue("ones" in str(tfn.graph))
        run()
        run(dtype=torch.int, inputs_require_grads=False)
        if RUN_CUDA:
            run(device="cuda:0")
        if RUN_CUDA_MULTI_GPU:
            run(device="cuda:1")

    # TODO: implement
    @unittest.expectedFailure
    def test_output_unflatten(self):
        """Check that outputs of traced functions retain the original structure and nesting"""
        def fn(x):
            return (x * 2, (x ** 2, x + 4, (x + 2,), ), x * 4)

        self.checkTrace(fn, (torch.randn(2, 2),))

    # TODO: implement
    @unittest.expectedFailure
    def test_input_flatten(self):
        """Check that inputs to traced functions are flattened"""

        def fn(x, t):
            y, z = t
            return x * y * z

        inputs = (torch.randn(1), (torch.randn(1), torch.randn(1)))
        self.checkTrace(fn, inputs)

    # TODO: adapt to a GraphExecutor test
    @unittest.skip("Need to instrument GraphExecutors a bit more")
    def test_flags(self):
        x, y = torch.randn(2, 2)
        y = Variable(torch.randn(2, 2))

        @torch.jit.compile
        def fn(x, y):
            return (x * x + y * y + x * y).sum()

        grads = {}
        for rx, ry in product((True, False), repeat=2):
            x.requires_grad = rx
            y.requires_grad = ry

            self.assertFalse(fn.has_trace_for(x, y))
            out = fn(x, y)

            self.assertFalse(fn.has_trace_for(x, y))
            for v, name, compute in [(x, 'x', rx), (y, 'y', ry)]:
                if not compute:
                    continue
                grad_v, = torch.autograd.grad(out, v, retain_graph=True)
                expected_grad = grads.setdefault(name, grad_v)
                self.assertEqual(grad_v, expected_grad)
            self.assertEqual(fn.has_trace_for(x, y), rx or ry)

    def test_python_ir(self):
        x = torch.tensor([0.4], requires_grad=True)
        y = torch.tensor([0.7], requires_grad=True)

        def doit(x, y):
            return torch.sigmoid(torch.tanh(x * (x + y)))

        trace, _ = torch.jit.get_trace_graph(doit, (x, y))
        self.run_pass('dce', trace)
        self.run_pass('canonicalize', trace)
        g = trace.graph()
        g2 = torch._C.Graph()
        g_to_g2 = {}
        for node in g.inputs():
            g_to_g2[node] = g2.addInput()
        for node in g.nodes():
            n_ = g2.createClone(node, lambda x: g_to_g2[x])
            g2.appendNode(n_)
            for o, no in zip(node.outputs(), n_.outputs()):
                g_to_g2[o] = no

        for node in g.outputs():
            g2.registerOutput(g_to_g2[node])

        t_node = g2.create("prim::TensorTest").t_("a", torch.ones([2, 2]))
        self.assertEqual(t_node.attributeNames(), ["a"])
        g2.appendNode(t_node)
        self.assertTrue(torch.equal(torch.ones(2, 2), t_node.t("a")))
        self.assertExpected(str(g2))

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "cpp tests require CUDA")
    @skipIfRocm
    def test_cpp_cuda(self):
        # rather than rebuild assertExpected in cpp,
        # just glob all the cpp outputs into one file for now
        self.assertExpected(torch._C._jit_run_cpp_tests())

    def test_batchnorm(self):
        x = torch.ones(2, 2, 2, 2)
        trace, _ = torch.jit.get_trace_graph(nn.BatchNorm2d(2), x)
        self.assertExpectedGraph(trace)

    def test_dropout(self):
        x = torch.ones(2, 2)
        trace, _ = torch.jit.get_trace_graph(nn.Dropout(0.6), x)
        self.assertExpectedGraph(trace)

    def test_conv(self):
        x = torch.ones(20, 16, 50, 40)
        trace, _ = torch.jit.get_trace_graph(nn.Conv2d(16, 13, 3, bias=False), x)
        self.assertExpectedGraph(trace)

    def test_repeated_input(self):
        def fn(a, b):
            return a + b

        ge = self.checkTrace(fn, [torch.randn(2, 2)] * 2)
        self.assertExpectedGraph(ge.graph)

    def test_repeated_output(self):
        def fn(a, b):
            z = a + b
            return z, z

        ge = self.checkTrace(fn, [torch.randn(2, 2) for _ in range(2)])
        self.assertExpectedGraph(ge.graph)

    @skipIfNoTorchVision
    def test_alexnet(self):
        x = torch.ones(1, 3, 224, 224)
        trace, _ = torch.jit.get_trace_graph(torchvision.models.AlexNet(), x)
        self.run_pass('cse', trace)
        self.assertExpectedGraph(trace)

    # Inplace copies don't work with tracer yet.
    # This is actually somewhat important to support correctly
    # as all backwards functions of views are implemented
    # as a zero filled tensor with a gradient fill on the
    # viewed portion.
    @unittest.expectedFailure
    def test_inplace_copy(self):
        x = torch.randn(4, 4, requires_grad=True)

        def f(x):
            out = Variable(torch.zeros(x.size()))
            out.copy_(x)
            return out

        trace, z = torch.jit.get_trace_graph(f, (x, ))
        self.run_pass('dce', trace)
        self.assertExpectedGraph(trace)
        self.assertExportImport(trace, (x,))

    def test_shared_param(self):

        class MyModule(torch.nn.Module):
            def __init__(self):
                super(MyModule, self).__init__()
                self.b = self.a = nn.Parameter(torch.randn(2, 2))

            def forward(self, x):
                return x * self.a + self.b

        m = MyModule()
        trace, _ = torch.jit.get_trace_graph(m, (torch.randn(2, 2),))
        self.assertEqual(len(list(trace.graph().inputs())), 2)
        self.assertExpectedGraph(trace)

    def test_nested_inplace(self):
        x = torch.randn(2, 2)
        trace, _ = torch.jit.get_trace_graph(lambda x: F.threshold(x, 0, 0, inplace=True), (x,))
        self.assertExpectedGraph(trace)
        self.assertExportImport(trace, (x,))

    def run_ge_tests(self, optimize, use_cuda):
        def rand(*args):
            t = torch.rand(*args).float()
            if use_cuda:
                t = t.cuda()
            return t
        self.checkTrace(lambda a, b: a * b + b,
                        [rand(1), rand(1)], [rand(2, 3), rand(2, 3)],
                        optimize=optimize)
        # trivial identity
        self.checkTrace(lambda a, b: (
            b, a), [rand(1), rand(1)], optimize=optimize)

        def foo(a):
            t = a * a
            return t * t, 4 * t
        self.checkTrace(foo, [rand(1)], optimize=optimize)
        # unused input
        self.checkTrace(
            lambda a, b: a * a, [rand(1), rand(1)], optimize=optimize,
            allow_unused=True)
        # test outputs that do not get used in grad
        self.checkTrace(foo, [rand(1)], drop=1, optimize=optimize)
        # test autograd fallback
        self.checkTrace(lambda a, b: a * b /
                        (a - 2 * b) + b, [rand(1), rand(1)],
                        optimize=optimize)

    def test_ge_unoptimized(self):
        self.run_ge_tests(False, False)

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @enable_cpu_fuser
    def test_ge_optimized(self):
        self.run_ge_tests(True, False)

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "requires CUDA")
    @skipIfRocm
    def test_ge_cuda(self):
        self.run_ge_tests(True, True)

    # more manual test of graph executor that can be used as a scratchpad
    def test_ge(self):
        def foo(a, b):
            return a * b / (a - b) + b
        V = Variable
        a, b = V(torch.rand(1)), V(torch.rand(1))
        ge = torch._C.GraphExecutor(foo, (a, b))
        a, b = V(torch.rand(1), requires_grad=True), V(
            torch.rand(1), requires_grad=True)
        r, = ge(a, b)
        da, db = torch.autograd.grad(r + 3, [a, b], create_graph=True)

        l2 = (da * db + db * db)
        g2result = torch.autograd.grad(l2, [da, db])

        r = foo(a, b)
        da2, db2 = torch.autograd.grad(r + 3, [a, b], create_graph=True)
        self.assertEqual(da, da2)
        self.assertEqual(db, db2)
        l3 = (da2 * db2 + db2 * db2)
        g2result2 = torch.autograd.grad(l3, [da2, db2])
        self.assertEqual(g2result, g2result2)

    def test_trace_annotation(self):
        @_trace(torch.rand(1))
        def foo(a):
            return a + a + a

        x = torch.randn(5, 5)
        self.assertEqual(foo(x), x + x + x)

    def test_einsum(self):
        def outer(x, y):
            return torch.einsum('i,j->ij', (x, y))

        traced = torch.jit.trace(outer, (torch.randn(4), torch.randn(5)))
        script = torch.jit.script(outer)
        fns = [traced, script]
        x, y = torch.randn(10), torch.randn(2)
        for fn in [traced, script]:
            self.assertGraphContains(fn.graph, kind='aten::einsum')
            self.assertEqual(fn(x, y), outer(x, y))

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "calls .cuda()")
    @skipIfRocm
    def test_traced_module_cuda(self):
        class Model(nn.Module):
            def __init__(self, num_features, num_layers):
                super(Model, self).__init__()
                self.num_layers = num_layers
                layers = [[nn.Linear(num_features, num_features), nn.Sigmoid()]
                          for _ in range(num_layers)]
                self.submodule = nn.Sequential(*chain(*layers))

            def forward(self, x):
                for i in range(self.num_layers):
                    x = self.submodule[i](x) + x
                return x

        model = Model(5, 3)
        x = torch.randn(2, 5)
        traced_model = torch.jit.trace(model, x)

        # We're missing some attributes these modules had initially. Make sure we can
        # still get the __repr__()
        model.__repr__()

        # XXX: indexing sequentials is broken
        linear_submodule = next(iter(traced_model.submodule._modules.values()))

        # All attributes that aren't parameters should raise
        with self.assertRaises(AttributeError):
            linear_submodule.in_features
        linear_submodule.weight
        with self.assertRaises(RuntimeError):
            traced_model.asdf = 4
        linear_submodule.weight = nn.Parameter(torch.randn(linear_submodule.weight.shape))
        with self.assertRaises(RuntimeError):
            del linear_submodule.weight

        # Submodules can't be called
        with self.assertRaises(RuntimeError):
            linear_submodule(x)

        # Type casts
        linear_submodule.cuda()
        traced_model.float().cuda()
        cuda_out = traced_model(x.float().cuda())
        traced_model.cpu()
        cpu_out = traced_model(x.float())
        self.assertEqual(cpu_out, cuda_out)
        traced_model.double()

        # state_dict + load_state_dict
        state = {k: v.clone() for k, v in traced_model.state_dict().items()}
        new_state = {k: v.clone().fill_(1) for k, v in state.items()}
        out = traced_model(x)
        traced_model.load_state_dict(new_state)
        out_ones = traced_model(x)
        traced_model.load_state_dict(state)
        out_state = traced_model(x)
        self.assertEqual(out, out_state)
        self.assertNotEqual(out, out_ones)

    def test_python_function(self):
        class MyFn(Function):
            @staticmethod
            def forward(ctx, x):
                return x + 1

            @staticmethod
            def backward(ctx, grad_output):
                return grad_output

        @_trace(torch.zeros(2))
        def fn(x):
            return MyFn.apply(x + 2) + 3

        x = torch.tensor([1., 2., 3.])
        y = torch.randn(2, 2, requires_grad=True)
        fn(x)
        fn(y)

    def test_python_function_tup(self):
        class MyFn(Function):
            @staticmethod
            def forward(ctx, x):
                return x + 1, x - 1

            @staticmethod
            def backward(ctx, grad_output):
                return grad_output, grad_output

        @_trace(torch.zeros(2))
        def fn(x):
            a, b = MyFn.apply(x + 2)
            return a + b + 3
        x = torch.tensor([1., 2., 3.])
        y = torch.randn(2, 2, requires_grad=True)
        fn(x)
        fn(y)

    def test_decompose_addmm(self):
        @torch.jit.script
        def addmm(mat, mat1, mat2, alpha, beta):
            a = mat.addmm(mat1, mat2)
            b = mat.addmm(mat1, mat2, alpha=1.0, beta=1.0)
            c = mat.addmm(mat1, mat2, alpha=4.20, beta=2.0)
            d = mat.addmm(mat1, mat2, alpha=int(alpha), beta=int(beta))

            return a + b + c + d

        mat = torch.randn(2, 2)
        mat1 = torch.randn(2, 4)
        mat2 = torch.randn(4, 2)
        alpha = torch.FloatTensor([123.0])
        beta = torch.FloatTensor([321.0])

        out_ref = addmm(mat, mat1, mat2, alpha, beta)
        self.run_pass('canonicalize_ops', addmm.graph)
        out_test = addmm(mat, mat1, mat2, alpha, beta)
        self.assertEqual(out_ref, out_test)
        self.assertExpected(canonical(addmm.graph))

    def test_addmm_fusion_scalar_type(self):
        @torch.jit.script
        def addmm(a, b, c):
            return a + b.mm(c)

        a, b, c = [torch.tensor(e) for e in (1, [[2.]], [[3.]])]
        addmm(a, b, c)
        graph = addmm.graph_for(a, b, c)
        # graph fusion skipped in windows, which runs cse
        self.run_pass('cse', graph)
        self.assertExpectedGraph(graph)

    def test_index_put(self):
        ten = torch.zeros(3, 3)
        mask = torch.Tensor([[True, True, True],
                             [True, False, False],
                             [True, True, False]]).byte()

        def test_fn(ten, mask):
            ten[mask] = torch.ones(6)
            return ten

        traced_test_fn = torch.jit.trace(test_fn, (ten, mask))

        ten = torch.rand(3, 3)
        self.assertEqual(test_fn(ten, mask), traced_test_fn(ten, mask))

    def test_sparse_tensors_error(self):
        def get_sparse():
            return torch.sparse.FloatTensor(2, 3)

        @torch.jit.script
        def sparse(input):
            output = get_sparse()
            return output, input

        with self.assertRaisesRegex(RuntimeError, "sparse tensors not supported"):
            sparse(get_sparse())

        # has a different entry point than calling sparse directly
        with self.assertRaisesRegex(RuntimeError, "sparse tensors not supported"):
            torch._C._jit_pass_shape_analysis(
                sparse.graph, (get_sparse(),), False)

        with self.assertRaisesRegex(RuntimeError, "sparse tensors not supported"):
            sparse(torch.tensor([1]))

    def test_constant_prop_simple(self):
        @torch.jit.script
        def constant_prop(input_tensor):
            a = 2 * 3
            b = a + 2
            return b + input_tensor

        x = torch.tensor(2)
        out_ref = constant_prop(x)
        self.run_pass('constant_propagation', constant_prop.graph)
        out_test = constant_prop(torch.tensor(2))
        self.assertEqual(out_ref, out_test)
        self.assertExpected(canonical(constant_prop.graph))

    def test_constant_prop_nested(self):
        @torch.jit.script
        def constant_prop(a):
            b = 2 + 1
            if bool(a < 2):
                c = b + 2
            else:
                c = b - 2
            return c
        out_ref = constant_prop(torch.tensor(2))
        self.run_pass('constant_propagation', constant_prop.graph)
        out_test = constant_prop(torch.tensor(2))
        self.assertEqual(out_ref, out_test)
        self.assertExpected(canonical(constant_prop.graph))

    def test_constant_prop_print(self):
        @torch.jit.script
        def constant_prop(input_tensor):
            a = 2 * 3
            print(a)
            b = a + 2
            return b + input_tensor

        self.run_pass('constant_propagation', constant_prop.graph)
        self.assertExpected(canonical(constant_prop.graph))

    def test_constant_prop_rand(self):
        @torch.jit.script
        def constant_prop():
            a = torch.randn([3])
            b = a + 2
            return b

        self.run_pass('constant_propagation', constant_prop.graph)
        self.assertExpected(canonical(constant_prop.graph))

    def test_constant_prop_if_constant(self):
        @torch.jit.script
        def constant_prop(a, b):
            c0 = 1
            c1 = 1
            c2 = 1
            if bool(a):  # -> c0, c1
                if bool(b):  # -> c0
                    if True:  # -> c0
                        c0 = c0 + 1
                        if False:
                            c1 = c1 + 1
                            c2 = c2 + 1
            else:  # -> c0, c1
                c1 = c1 + 1

            if True:  # inlined
                c0 = c0 + 1  # dynamic
                c2 = c2 + 4  # set to 5
            return a + c0 + c1 + c2

        self.run_pass('constant_propagation', constant_prop.graph)
        self.assertExpected(canonical(constant_prop.graph))

    def test_constant_prop_loop_constant(self):
        @torch.jit.script
        def constant_prop():
            b = 0
            while True:
                b = 1
            while False:
                b = 2
            return b

        self.run_pass('constant_propagation', constant_prop.graph)
        self.assertExpected(canonical(constant_prop.graph))

    def test_trace_detach(self):
        def foo(x, w):
            return torch.matmul(x, w).detach()

        traced = torch.jit.trace(foo, (torch.rand(3, 4), torch.rand(4, 5)))

        self.assertExpectedGraph(traced.graph)
        x, w = torch.rand(3, 4), torch.rand(4, 5, requires_grad=True)
        traced_result = traced(x, w)
        self.assertEqual(foo(x, w), traced_result)
        self.assertFalse(traced_result.requires_grad)
        self.assertIsNone(traced_result.grad_fn)

    def test_trace_detach_inplace(self):
        def foo(x, w):
            y = torch.matmul(x, w)
            y.detach_()
            return y

        traced = torch.jit.trace(foo, (torch.rand(3, 4), torch.rand(4, 5)))

        self.assertExpectedGraph(traced.graph)
        x, w = torch.rand(3, 4), torch.rand(4, 5)
        traced_result = traced(x, w)
        self.assertEqual(foo(x, w), traced_result)
        self.assertFalse(traced_result.requires_grad)
        self.assertIsNone(traced_result.grad_fn)

    def test_trace_detach_onnx_erase(self):
        class Mod(torch.nn.Module):
            def forward(self, x, w):
                return torch.matmul(x, w).detach()

        f = io.BytesIO()
        self.assertExpected(torch.onnx.export_to_pretty_string(
            Mod(), (torch.rand(3, 4), torch.rand(4, 5)), f))

    def test_trace_slice_full_dim(self):
        def foo(x):
            return x[0:5, 0] + 1.0

        traced = torch.jit.trace(foo, (torch.rand(5, 4),))
        test_x = torch.rand(6, 3)
        self.assertEqual(foo(test_x), traced(test_x))

    def test_export_expand_aten_fallback(self):
        class ExpandTest(torch.jit.ScriptModule):
            @torch.jit.script_method
            def forward(self, x):
                y = x
                for i in range(5):
                    y = x.expand([3, 4, i])
                return y

        mod = ExpandTest()
        example_outs = mod(torch.rand(3, 4, 1))
        f = io.BytesIO()
        with self.assertRaisesRegex(RuntimeError, 'Could not export a broadcasted operation'):
            torch.onnx.export_to_pretty_string(mod, (torch.rand(3, 4, 1),), f, verbose=False,
                                               example_outputs=example_outs)

        self.assertExpected(
            torch.onnx.export_to_pretty_string(mod, (torch.rand(3, 4, 1),), f, verbose=False,
                                               example_outputs=example_outs,
                                               operator_export_type=OperatorExportTypes.ONNX_ATEN_FALLBACK))

    def test_export_dropout(self):
        test = torch.nn.Dropout()
        test.eval()

        traced = torch.jit.trace(test, (torch.rand(3, 4),), check_trace=False)
        imported = self.getExportImportCopy(traced)
        x = torch.randn(3, 4)
        self.assertEqual(traced(x), imported(x))

    @unittest.skipIf(not RUN_CUDA, "requires CUDA")
    def test_cuda_export_restore(self):
        class Sub(torch.jit.ScriptModule):
            def __init__(self):
                super(Sub, self).__init__()
                self.weight = nn.Parameter(torch.randn(3, 4))

            @torch.jit.script_method
            def forward(self, thing):
                return self.weight + thing

        class M(torch.jit.ScriptModule):
            def __init__(self):
                super(M, self).__init__()
                self.mod = Sub()

            @torch.jit.script_method
            def forward(self, v):
                return self.mod(v)
        m = M()
        m.cuda()
        m2 = self.getExportImportCopy(m)
        m2.cuda()
        input = torch.rand(3, 4).cuda()
        self.assertEqual(m(input), m2(input))

    def test_export_batchnorm(self):
        for mode in ['eval', 'train']:
            for clazz in [
                    torch.nn.BatchNorm1d(100),
                    torch.nn.BatchNorm1d(100, affine=False),
                    torch.nn.BatchNorm2d(100),
                    torch.nn.BatchNorm2d(100, affine=False)]:
                getattr(clazz, mode)()

                input = torch.randn(20, 100) if isinstance(clazz, torch.nn.BatchNorm1d) else \
                    torch.randn(20, 100, 35, 45)

                traced = torch.jit.trace(clazz, (input,))
                imported = self.getExportImportCopy(traced)
                x = torch.randn(20, 100) if isinstance(clazz, torch.nn.BatchNorm1d) else \
                    torch.randn(20, 100, 35, 45)
                self.assertEqual(traced(x), imported(x))

    def test_export_rnn(self):
        for clazz in [nn.RNN(10, 20, 2), nn.GRU(10, 20, 2)]:
            class RNNTest(torch.nn.Module):
                def __init__(self):
                    super(RNNTest, self).__init__()
                    self.rnn = clazz

                def forward(self, x, lengths, h0):
                    packed = torch.nn.utils.rnn.pack_padded_sequence(x, lengths)
                    out, h = self.rnn(packed, h0)
                    padded_outs, _ = torch.nn.utils.rnn.pad_packed_sequence(out)
                    return padded_outs

            test = RNNTest()

            traced = torch.jit.trace(test, (torch.randn(5, 3, 10), torch.LongTensor([3, 2, 1]), torch.randn(2, 3, 20)))
            imported = self.getExportImportCopy(traced)
            x, lengths, h0 = torch.randn(5, 3, 10), torch.LongTensor([3, 3, 2]), torch.randn(2, 3, 20)
            self.assertEqual(traced(x, lengths, h0), imported(x, lengths, h0))

    def test_export_lstm(self):
        class LSTMTest(torch.nn.Module):
            def __init__(self):
                super(LSTMTest, self).__init__()
                self.rnn = nn.LSTM(10, 20, 2)

            def forward(self, x, lengths, hiddens):
                h0, c0 = hiddens
                packed = torch.nn.utils.rnn.pack_padded_sequence(x, lengths)
                out, (h, c) = self.rnn(packed, (h0, c0))
                padded_outs, _ = torch.nn.utils.rnn.pad_packed_sequence(out)
                return padded_outs

        test = LSTMTest()

        traced = torch.jit.trace(test, (torch.randn(5, 3, 10),
                                        torch.LongTensor([3, 2, 1]),
                                        (torch.randn(2, 3, 20), torch.randn(2, 3, 20))))
        imported = self.getExportImportCopy(traced)
        x, lengths, h0, c0 = \
            torch.randn(5, 3, 10), torch.LongTensor([3, 3, 2]), torch.randn(2, 3, 20), torch.randn(2, 3, 20)
        self.assertEqual(traced(x, lengths, (h0, c0)), imported(x, lengths, (h0, c0)))

    def test_trace_variable_instantiation(self):
        def random_foo(x):
            return Variable(Variable(x) + 1.0)

        random_foo_traced = torch.jit.trace(random_foo, (torch.rand(3, 4),))

        x = torch.rand(5, 6)
        self.assertEqual(random_foo(x), random_foo_traced(x))

    def test_trace_slice_expr_complete_type(self):
        def random_foo(x):
            return x + 1.0

        random_foo_traced = torch.jit.trace(random_foo, (torch.rand(3, 4),))

        @torch.jit.script
        def random_bar(x):
            return random_foo_traced(x)[0:1]

        x = torch.rand(3, 4)
        self.assertEqual(random_bar(x), (x + 1)[0:1])


class TestBatched(TestCase):
    # generate random examples and create an batchtensor with them
    def rand_batch(self, *dims):
        dims = [dim for dim in dims if dim != ()]
        xs = [torch.rand(1, *(random.randint(1, size) if b else size for b, size in dims[1:]),
                         requires_grad=True) for i in range(dims[0])]
        xb = BatchTensor(xs, torch.tensor([b for b, d in dims[1:]]).byte())
        return xs, xb

    def test_create_batchtensor(self):
        # create from tensorlist
        xs, batch = self.rand_batch(4, (True, 3), (False, 2), (True, 5))
        self.assertEqual(xs, batch.examples())
        # create from data, mask, dims
        batch2 = BatchTensor(batch.get_data(), batch.get_mask(), batch.get_dims())
        self.assertEqual(xs, batch2.examples())
        # expand a tensor to a batchtensor given batch_size
        xs = torch.rand(3, 4, 5)
        batch3 = BatchTensor(xs, 2)
        xs = xs.unsqueeze(0)
        self.assertEqual([xs, xs], batch3.examples())

    def test_batch_elementwise_unary(self):
        @torch.jit.batch(batch_size=4)
        def tanh(a):
            return torch.tanh(a)

        xs, batch = self.rand_batch(4, (True, 3), (False, 2))
        res_batch = tanh(batch)
        res = [torch.tanh(xs[j]) for j in range(4)]
        self.assertEqual(res, res_batch.examples())

    def test_batch_elementwise_binary(self):
        @torch.jit.batch(batch_size=4)
        def add(a, b):
            return a + b

        xs, batch = self.rand_batch(4, (True, 3), (False, 2))
        xs2, batch2 = xs, batch
        res_batch = add(batch, batch2)
        res = [torch.add(xs[j], xs2[j]) for j in range(4)]
        self.assertEqual(res, res_batch.examples())

        # test broadcast
        xs, batch = self.rand_batch(4, (False, 3), (False, 2))
        b = torch.rand(3, 2)
        res_batch = add(batch, b)
        res = [torch.add(xs[j], b) for j in range(4)]
        self.assertEqual(res, res_batch.examples())

    def test_batch_mm(self):
        @torch.jit.batch(batch_size=4)
        def mm(a, b):
            return torch.mm(a, b)

        xs, batch = self.rand_batch(4, (True, 3), (False, 2))
        xs2, batch2 = self.rand_batch(4, (False, 2), (True, 3))
        res_batch = mm(batch, batch2)
        res = [torch.mm(xs[j].squeeze(0), xs2[j].squeeze(0)).unsqueeze(0) for j in range(4)]
        self.assertEqual(res, res_batch.examples())

        # test broadcast
        b = torch.rand(2, 4)
        res_batch = mm(batch, b)
        res = [torch.mm(xs[j].squeeze(0), b).unsqueeze(0) for j in range(4)]
        self.assertEqual(res, res_batch.examples())

    def test_batch_matmul(self):
        @torch.jit.batch(batch_size=4)
        def matmul(a, b):
            return torch.matmul(a, b)

        def matmul_test(xs, batch, xs2, batch2):
            ys = [torch.matmul(xs[j].squeeze(0), xs2[j].squeeze(0)).unsqueeze(0) for j in range(4)]
            ybs = matmul(batch, batch2)
            self.assertEqual(ys, ybs.examples())

        # 1 dimension * 1 dimension
        xs, batch = self.rand_batch(4, (False, 2))
        xs2, batch2 = self.rand_batch(4, (False, 2))
        matmul_test(xs, batch, xs2, batch2)
        # 1 dimension * 2 dimension
        xs, batch = self.rand_batch(4, (False, 2))
        xs2, batch2 = self.rand_batch(4, (False, 2), (True, 3))
        matmul_test(xs, batch, xs2, batch2)
        # 2 dimension * 1 dimensions
        xs, batch = self.rand_batch(4, (True, 3), (False, 2))
        xs2, batch2 = self.rand_batch(4, (False, 2))
        matmul_test(xs, batch, xs2, batch2)
        # 2 dimension * 2 dimension
        xs, batch = self.rand_batch(4, (True, 3), (False, 2))
        xs2, batch2 = self.rand_batch(4, (False, 2), (True, 3))
        matmul_test(xs, batch, xs2, batch2)

    def test_batch_select(self):
        @torch.jit.batch(batch_size=4)
        def select(x):
            return torch.select(x, 1, 0)

        xs, batch = self.rand_batch(4, (True, 3), (True, 2))
        res_batch = select(batch)
        res = [torch.select(xs[j], 1, 0) for j in range(4)]
        self.assertEqual(res, res_batch.examples())

        xs, batch = self.rand_batch(4, (False, 3), (True, 2))
        res_batch = select(batch)
        res = [torch.select(xs[j], 1, 0) for j in range(4)]
        self.assertEqual(res, res_batch.examples())

    def test_batch_index_select(self):
        @torch.jit.batch(batch_size=4)
        def index_select(x, ind):
            return x.index_select(1, ind)

        xs, batch = self.rand_batch(4, (False, 5), (True, 2))
        ind = [torch.randint(0, 4, (1,), dtype=torch.long) for i in range(4)]
        ind_batch = BatchTensor(ind, torch.tensor([]).byte())
        res_batch = index_select(batch, ind_batch)
        res = [torch.index_select(xs[j], 1, ind[j]) for j in range(4)]
        self.assertEqual(res, res_batch.examples())

    def test_batch_where(self):
        @torch.jit.batch(batch_size=4)
        def where(c, a, b):
            return torch.where(c, a, b)

        xs, batch = self.rand_batch(4, (False, 3), (False, 2))
        xs2, batch2 = self.rand_batch(4, (False, 3), (False, 2))

        dims = [4, (False, 3), (False, 2)]
        xs_cond = [torch.rand(1, 3, 2).byte() for i in range(dims[0])]
        batch_cond = BatchTensor(xs_cond, torch.tensor([b for b, d in dims[1:]]))

        res_batch = where(batch_cond, batch, batch2)
        res = [torch.where(xs_cond[j], xs[j], xs2[j]) for j in range(4)]
        self.assertEqual(res, res_batch.examples())

    def test_batch_argmax(self):
        @torch.jit.batch(batch_size=4)
        def argmax(a):
            return torch.argmax(a, 1)

        xs, batch = self.rand_batch(4, (True, 5), (True, 6))
        res_batch = argmax(batch)
        res = [torch.argmax(xs[j], 1) for j in range(4)]
        self.assertEqual(res, res_batch.examples())

        @torch.jit.batch(batch_size=4)
        def argmax(a):
            return torch.argmax(a, 1, False)

        res_batch = argmax(batch)
        res = [torch.argmax(xs[j], 1, False) for j in range(4)]
        self.assertEqual(res, res_batch.examples())

    def test_batch_topk(self):
        @torch.jit.batch(batch_size=4)
        def topk(a):
            return torch.topk(a, 3, 1)

        xs, batch = self.rand_batch(4, (False, 5), (True, 6))

        # along static dim
        res_batch = topk(batch)
        res = [torch.topk(xs[j], 3, 1)[0] for j in range(4)]
        res_idx = [torch.topk(xs[j], 3, 1)[1] for j in range(4)]
        self.assertEqual(res, res_batch[0].examples())
        self.assertEqual(res_idx, res_batch[1].examples())

        @torch.jit.batch(batch_size=4)
        def topk(a):
            return torch.topk(a, 1, 2)

        # along dynamic dim
        res_batch = topk(batch)
        res = [torch.topk(xs[j], 1, 2)[0] for j in range(4)]
        res_idx = [torch.topk(xs[j], 1, 2)[1] for j in range(4)]
        self.assertEqual(res, res_batch[0].examples())
        self.assertEqual(res_idx, res_batch[1].examples())

    def test_batch_softmax(self):
        @torch.jit.batch(batch_size=4)
        def softmax(a):
            return torch.softmax(a, 1)

        xs, batch = self.rand_batch(4, (False, 5), (True, 6))

        # along static dim
        res_batch = softmax(batch)
        res = [torch.softmax(xs[j], 1) for j in range(4)]
        self.assertEqual(res, res_batch.examples())

        @torch.jit.batch(batch_size=4)
        def softmax(a):
            return torch.softmax(a, 2)

        # along dynamic dim
        res_batch = softmax(batch)
        res = [torch.softmax(xs[j], 2) for j in range(4)]
        self.assertEqual(res, res_batch.examples())

    def test_batch_view(self):
        @torch.jit.batch(batch_size=4)
        def view(a):
            return a.view([4, -1, 3])

        xs, batch = self.rand_batch(4, (True, 5), (False, 3))
        res_batch = view(batch)
        res = [xs[j].view([1, -1, 3]) for j in range(4)]
        self.assertEqual(res, res_batch.examples())

    def test_batch_cat(self):
        @torch.jit.batch(batch_size=4)
        def cat2(a, b):
            return torch.cat([a, b], 2)

        xs, batch = self.rand_batch(4, (True, 5), (False, 3))
        xs2, batch2 = xs, batch
        res_batch = cat2(batch, batch2)
        res = [torch.cat([xs[j], xs2[j]], 2) for j in range(4)]
        self.assertEqual(res, res_batch.examples())

    def test_batch_sum(self):
        @torch.jit.batch(batch_size=4)
        def batch_sum(a):
            return a.sum()

        xs, batch = self.rand_batch(4, (True, 5), (False, 3))
        res_batch = batch_sum(batch)
        res = [xs[j].sum().unsqueeze(0) for j in range(4)]
        self.assertEqual(res, res_batch.examples())

    def test_if_else(self):
        def single_if(a, b):
            if bool(a > b):
                a = a + b
            else:
                a = a - b
            return a

        batch_if = torch.jit.batch(batch_size=4)(single_if)

        a, batch_a = self.rand_batch(4, ())
        b, batch_b = self.rand_batch(4, ())
        res_batch = batch_if(batch_a, batch_b)
        res = [single_if(a[j], b[j]) for j in range(4)]
        self.assertEqual(res, res_batch.examples())

        script_if = torch.jit.script(single_if)
        graph = torch.to_batch_graph(script_if.graph)
        self.assertExpected(str(graph))

    def test_if_else_with_scalar(self):
        def single_if(a, b):
            if bool(a > 0.1):
                a = a + b
            else:
                a = a - b
            return a

        batch_if = torch.jit.batch(batch_size=4)(single_if)

        a, batch_a = self.rand_batch(4, ())
        b, batch_b = self.rand_batch(4, ())
        res_batch = batch_if(batch_a, batch_b)
        res = [single_if(a[j], b[j]) for j in range(4)]
        self.assertEqual(res, res_batch.examples())

        script_if = torch.jit.script(single_if)
        graph = torch.to_batch_graph(script_if.graph)
        self.assertExpected(str(graph))

    def test_if_noelse(self):
        def single_if(a, b):
            if bool(a > b):
                a = a + b
            return a

        batch_if = torch.jit.batch(batch_size=4)(single_if)

        a, batch_a = self.rand_batch(4, ())
        b, batch_b = self.rand_batch(4, ())
        res_batch = batch_if(batch_a, batch_b)
        res = [single_if(a[j], b[j]) for j in range(4)]
        self.assertEqual(res, res_batch.examples())

        script_if = torch.jit.script(single_if)
        graph = torch.to_batch_graph(script_if.graph)
        self.assertExpected(str(graph))

    def test_if_noelse_with_scalar(self):
        def single_if(a, b):
            if bool(a > 0.1):
                a = a + b
            return a

        batch_if = torch.jit.batch(batch_size=4)(single_if)

        a, batch_a = self.rand_batch(4, ())
        b, batch_b = self.rand_batch(4, ())
        res_batch = batch_if(batch_a, batch_b)
        res = [single_if(a[j], b[j]) for j in range(4)]
        self.assertEqual(res, res_batch.examples())

        script_if = torch.jit.script(single_if)
        graph = torch.to_batch_graph(script_if.graph)
        self.assertExpected(str(graph))

    def test_while(self):
        def single_while(a, b):
            while bool(a > b):
                a = a - b
            return a

        batch_while = torch.jit.batch(batch_size=4)(single_while)

        a, batch_a = self.rand_batch(4, ())
        b = [torch.abs(torch.rand(1)) for i in range(4)]
        batch_b = BatchTensor(b, torch.tensor([]).byte())
        res_batch = batch_while(batch_a, batch_b)
        res = [single_while(a[j], b[j]) for j in range(4)]
        self.assertEqual(res, res_batch.examples())

        script_while = torch.jit.script(single_while)
        graph = torch.to_batch_graph(script_while.graph)
        self.assertExpected(str(graph))

    def test_for(self):
        def single_for(x, y):
            for _ in range(10):
                x = x + y
            return x

        batch_for = torch.jit.batch(batch_size=4)(single_for)

        a, batch_a = self.rand_batch(4, ())
        b, batch_b = self.rand_batch(4, ())
        res_batch = batch_for(batch_a, batch_b)
        res = [single_for(a[j], b[j]) for j in range(4)]
        self.assertEqual(res, res_batch.examples())

        script_for = torch.jit.script(single_for)
        graph = torch.to_batch_graph(script_for.graph)
        self.assertExpected(str(graph))

    def test_lstm(self):
        def LSTM(x_all, h, c, w_xi, w_xf, w_xo, w_xc, w_hi, w_hf, w_ho, w_hc, b_i, b_f, b_o, b_c):
            for i in range(x_all.size(1)):
                x = x_all.select(1, i)
                i_t = torch.matmul(x, w_xi) + torch.matmul(h, w_hi) + b_i
                f_t = torch.matmul(x, w_xf) + torch.matmul(h, w_hf) + b_f
                o_t = torch.matmul(x, w_xo) + torch.matmul(h, w_ho) + b_o
                # activations
                i_t = torch.sigmoid(i_t)
                f_t = torch.sigmoid(f_t)
                o_t = torch.sigmoid(o_t)
                # cell computations
                c_t = torch.matmul(x, w_xc) + torch.matmul(h, w_hc) + b_c
                c_t = torch.tanh(c_t)
                c_t = torch.mul(c_t, f_t) + torch.mul(i_t, c_t)
                h_t = torch.mul(o_t, torch.tanh(c_t))
                h = h_t
                c = c_t
            return h

        LSTM_batch = torch.jit.batch(batch_size=4)(LSTM)

        batch_size, input_size, hidden_size = 4, 3, 2
        xs, batch = self.rand_batch(batch_size, (True, 4), (False, input_size))
        hx, h_batch = self.rand_batch(batch_size, (False, hidden_size))
        cx, c_batch = self.rand_batch(batch_size, (False, hidden_size))

        # input to hidden weights
        w_xi = torch.rand(input_size, hidden_size)
        w_xf = torch.rand(input_size, hidden_size)
        w_xo = torch.rand(input_size, hidden_size)
        w_xc = torch.rand(input_size, hidden_size)
        # hidden to hidden weights
        w_hi = torch.rand(hidden_size, hidden_size)
        w_hf = torch.rand(hidden_size, hidden_size)
        w_ho = torch.rand(hidden_size, hidden_size)
        w_hc = torch.rand(hidden_size, hidden_size)
        # bias terms
        b_i = torch.rand(hidden_size)
        b_f = torch.rand(hidden_size)
        b_o = torch.rand(hidden_size)
        b_c = torch.rand(hidden_size)

        ys = [LSTM(xs[j], hx[j], cx[j], w_xi, w_xf, w_xo, w_xc,
                   w_hi, w_hf, w_ho, w_hc, b_i, b_f, b_o, b_c) for j in range(batch_size)]
        ybs = LSTM_batch(batch, h_batch, c_batch, w_xi, w_xf, w_xo, w_xc,
                         w_hi, w_hf, w_ho, w_hc, b_i, b_f, b_o, b_c)
        self.assertEqual(ys, ybs.examples())

    def test_greedy_search(self):
        def greedy(x, h, c, embed, w_xi, w_xf, w_xo, w_xc, w_hi, w_hf, w_ho, w_hc,
                   b_i, b_f, b_o, b_c, w_hs, b_s, iter_num):
            iter_count = torch.zeros_like(iter_num)
            while bool(iter_count < iter_num):
                iter_count += 1
                # LSTM Cell
                i_t = torch.matmul(x, w_xi) + torch.matmul(h, w_hi) + b_i
                f_t = torch.matmul(x, w_xf) + torch.matmul(h, w_hf) + b_f
                o_t = torch.matmul(x, w_xo) + torch.matmul(h, w_ho) + b_o
                # activations
                i_t = torch.sigmoid(i_t)
                f_t = torch.sigmoid(f_t)
                o_t = torch.sigmoid(o_t)
                # cell computations
                c_t = torch.matmul(x, w_xc) + torch.matmul(h, w_hc) + b_c
                c_t = torch.tanh(c_t)
                c_t = torch.mul(c_t, f_t) + torch.mul(i_t, c_t)
                h_t = torch.mul(o_t, torch.tanh(c_t))
                h = h_t
                c = c_t
                # calculate feature with max probability
                s_t = torch.matmul(h_t, w_hs) + b_s
                p_t = torch.softmax(s_t, 1)
                i_t = torch.argmax(p_t, 1)
                x = embed.index_select(1, i_t).squeeze(1)
            return h

        greedy_batch = torch.jit.batch(batch_size=4)(greedy)

        batch_size, input_size, hidden_size, vocab_size = 4, 6, 8, 7
        xs, batch = self.rand_batch(batch_size, (False, input_size))
        hx, h_batch = self.rand_batch(batch_size, (False, hidden_size))
        cx, c_batch = self.rand_batch(batch_size, (False, hidden_size))
        embed, embed_batch = self.rand_batch(batch_size, (False, vocab_size), (False, input_size))
        iter_num = [torch.randint(2, 5, (1,)) for i in range(batch_size)]
        iter_num_batch = BatchTensor(iter_num, torch.tensor([]).byte())

        # input to hidden weights
        w_xi = torch.rand(input_size, hidden_size)
        w_xf = torch.rand(input_size, hidden_size)
        w_xo = torch.rand(input_size, hidden_size)
        w_xc = torch.rand(input_size, hidden_size)
        # hidden to hidden weights
        w_hi = torch.rand(hidden_size, hidden_size)
        w_hf = torch.rand(hidden_size, hidden_size)
        w_ho = torch.rand(hidden_size, hidden_size)
        w_hc = torch.rand(hidden_size, hidden_size)
        # bias terms
        b_i = torch.rand(hidden_size)
        b_f = torch.rand(hidden_size)
        b_o = torch.rand(hidden_size)
        b_c = torch.rand(hidden_size)
        # hidden to vocab weights, bias
        w_hs = torch.rand(hidden_size, vocab_size)
        b_s = torch.rand(vocab_size)

        ys = [greedy(xs[j], hx[j], cx[j], embed[j], w_xi, w_xf, w_xo, w_xc,
                     w_hi, w_hf, w_ho, w_hc, b_i, b_f, b_o, b_c, w_hs, b_s, iter_num[j]) for j in range(batch_size)]
        ybs = greedy_batch(batch, h_batch, c_batch, embed_batch, w_xi, w_xf, w_xo, w_xc,
                           w_hi, w_hf, w_ho, w_hc, b_i, b_f, b_o, b_c, w_hs, b_s, iter_num_batch)
        self.assertEqual(ys, ybs.examples())

    def test_beam_search(self):
        def beam(x, h, c, embed, w_xi, w_xf, w_xo, w_xc, w_hi, w_hf, w_ho, w_hc,
                 b_i, b_f, b_o, b_c, w_hs, b_s, iter_num, idx):
            k = 5
            vocab_size = embed.size(1)
            iter_count = torch.zeros_like(iter_num)
            max_len = idx.size(2)
            while bool(iter_count < iter_num):
                iter_count += 1
                # LSTM Cell
                i_t = torch.matmul(x, w_xi) + torch.matmul(h, w_hi) + b_i
                f_t = torch.matmul(x, w_xf) + torch.matmul(h, w_hf) + b_f
                o_t = torch.matmul(x, w_xo) + torch.matmul(h, w_ho) + b_o
                # activations
                i_t = torch.sigmoid(i_t)
                f_t = torch.sigmoid(f_t)
                o_t = torch.sigmoid(o_t)
                # cell computations
                c_t = torch.matmul(x, w_xc) + torch.matmul(h, w_hc) + b_c
                c_t = torch.tanh(c_t)
                c_t = torch.mul(c_t, f_t) + torch.mul(i_t, c_t)
                h_t = torch.mul(o_t, torch.tanh(c_t))
                h = h_t
                c = c_t
                # calculate features with max probability
                s_t = torch.matmul(h_t, w_hs) + b_s
                s_t = s_t.view([1, s_t.size(1) * s_t.size(2)])
                p_t = torch.softmax(s_t, 1)
                prob_t, idx_t = torch.topk(p_t, k, 1)
                if(int(idx_t.dim()) > 1):
                    idx_t_tmp = idx_t.squeeze(0)
                else:
                    idx_t_tmp = idx_t
                new_y = torch.fmod(idx_t_tmp, vocab_size)
                pre_y = idx_t_tmp / vocab_size
                x = embed.index_select(1, new_y)
                h = h_t.index_select(1, pre_y)
                c = c_t.index_select(1, pre_y)
                iter = int(iter_count[0])
                idx = torch.cat([idx.narrow(2, 0, iter).index_select(1, pre_y),
                                torch.fmod(idx_t, vocab_size).unsqueeze(-1),
                                idx.narrow(2, iter, max_len - iter)], 2)
                idx = idx.narrow(2, 0, max_len)
            return idx

        beam_batch = torch.jit.batch(batch_size=4)(beam)

        k = 5
        batch_size, input_size, hidden_size, vocab_size = 4, 6, 8, 7
        max_len = 5
        xs, batch = self.rand_batch(batch_size, (False, 1), (False, input_size))
        hx, h_batch = self.rand_batch(batch_size, (False, 1), (False, hidden_size))
        cx, c_batch = self.rand_batch(batch_size, (False, 1), (False, hidden_size))
        embed, embed_batch = self.rand_batch(batch_size, (False, vocab_size), (False, input_size))
        iter_num = [torch.randint(2, max_len + 1, (1,)) for i in range(batch_size)]
        iter_num_batch = BatchTensor(iter_num, torch.tensor([]).byte())

        # input to hidden weights
        w_xi = torch.rand(input_size, hidden_size)
        w_xf = torch.rand(input_size, hidden_size)
        w_xo = torch.rand(input_size, hidden_size)
        w_xc = torch.rand(input_size, hidden_size)
        # hidden to hidden weights
        w_hi = torch.rand(hidden_size, hidden_size)
        w_hf = torch.rand(hidden_size, hidden_size)
        w_ho = torch.rand(hidden_size, hidden_size)
        w_hc = torch.rand(hidden_size, hidden_size)
        # bias terms
        b_i = torch.rand(1, hidden_size)
        b_f = torch.rand(1, hidden_size)
        b_o = torch.rand(1, hidden_size)
        b_c = torch.rand(1, hidden_size)
        # hidden to vocab weights, bias
        w_hs = torch.rand(hidden_size, vocab_size)
        b_s = torch.rand(1, vocab_size)

        idx_batch = torch.jit.BatchTensor(torch.zeros([batch_size, k, max_len], dtype=torch.long),
                                          torch.zeros([batch_size, 1, max_len]).byte(),
                                          torch.tensor([0, 1]).byte())
        idx = [torch.zeros([1, k, max_len], dtype=torch.long) for _ in range(batch_size)]

        ys = [beam(xs[j], hx[j], cx[j], embed[j], w_xi, w_xf, w_xo, w_xc, w_hi, w_hf, w_ho, w_hc,
                   b_i, b_f, b_o, b_c, w_hs, b_s, iter_num[j], idx[j]).narrow(2, 0, int(iter_num[j]))
              for j in range(batch_size)]
        ybs = beam_batch(batch, h_batch, c_batch, embed_batch, w_xi, w_xf, w_xo, w_xc,
                         w_hi, w_hf, w_ho, w_hc, b_i, b_f, b_o, b_c, w_hs, b_s, iter_num_batch, idx_batch)
        self.assertEqual(ys, ybs.examples())


class TestScript(JitTestCase):
    @contextmanager
    def capture_stdout(self):
        # No idea how to capture stdout from C++ on Windows
        if WINDOWS:
            yield ['']
            return
        import os
        import fcntl
        import errno
        sys.stdout.flush()
        stdout_fd = os.dup(1)
        r, w = os.pipe()
        try:
            # Override stdout with r - dup is guaranteed to return the lowest free fd
            os.close(1)
            os.dup(w)

            captured_stdout = ['']
            yield captured_stdout
            sys.stdout.flush()  # Make sure that Python hasn't buffered anything

            # Do the ugly dance to read all the data that was written into the pipe
            fcntl.fcntl(r, fcntl.F_SETFL, os.O_NONBLOCK)
            total_stdout = ''
            while True:
                try:
                    total_stdout += os.read(r, 1000).decode('ascii')
                except OSError as e:
                    if e.errno != errno.EAGAIN:
                        raise
                    break
            captured_stdout[0] = total_stdout
        finally:
            # Revert the change, and clean up all fds
            os.close(1)
            os.dup(stdout_fd)
            os.close(stdout_fd)
            os.close(r)
            os.close(w)

    def checkScriptRaisesRegex(self, script, inputs, exception, regex,
                               optimize=True, outputs=None, capture_output=False):
        """
        Checks that a given function will throw the correct exception,
        when executed with normal python, the string frontend, and the AST frontend
        """
        # normal python
        with self.assertRaisesRegex(exception, regex):
            script(*inputs)
        # string frontend
        with self.assertRaisesRegex(exception, regex):
            source = textwrap.dedent(inspect.getsource(script))
            cu = torch.jit.CompilationUnit(source, optimize)
            ge = getattr(cu, script.__name__)
            ge(*inputs)
        # python AST frontend
        with self.assertRaisesRegex(exception, regex):
            ge = torch.jit.script(script, optimize)
            ge(*inputs)

    def checkScript(self, script, inputs, optimize=True, outputs=None, name='func', capture_output=False, frames_up=1):
        if isinstance(script, str):
            cu = torch.jit.CompilationUnit(script, optimize, _frames_up=frames_up)
            ge = getattr(cu, name)
        else:
            if capture_output:
                with self.capture_stdout() as captured:
                    outputs = script(*inputs)
            else:
                outputs = script(*inputs)
            # Check the string frontend first
            source = textwrap.dedent(inspect.getsource(script))
            self.checkScript(source, inputs, optimize, outputs, script.__name__, capture_output, frames_up=2)
            # Continue checking the Python frontend
            ge = torch.jit.script(script, optimize, _frames_up=1)

        if capture_output:
            with self.capture_stdout() as captured:
                outputs_ge = ge(*inputs)
            if not WINDOWS:
                self.assertExpected(captured[0], subname='stdout')
        else:
            outputs_ge = ge(*inputs)
        self.assertEqual(outputs, outputs_ge)

        return ge

    def test_robust_op_resolution(self):
        neg = torch.add  # misleading name to make sure we resolve by function

        def stuff(x):
            return neg(x, x)

        a = (torch.rand(3),)
        self.checkScript(stuff, a)

    def test_tuple_io(self):
        def stuff(x):
            # type: (Tuple[Tensor, Tensor]) -> Tuple[Tensor, Tensor]
            a, b = x
            return b, a

        a = (torch.rand(3), torch.rand(3))
        self.checkScript(stuff, (a,))

    def test_tuple_create_return(self):
        def stuff2(x):
            # type: (int) -> Tuple[Tensor, Tensor]
            a = (torch.ones(x), torch.zeros(x))
            return a
        self.checkScript(stuff2, (3,))

    def test_list_io(self):
        def stuff3(x):
            # type: (List[int]) -> Tuple[Tensor, List[int]]
            return torch.ones(x), x
        self.checkScript(stuff3, ([3, 2],))

    def test_nested_list_error(self):
        with self.assertRaisesRegex(RuntimeError, "Lists can only contain"):
            @torch.jit.script
            def foo(x):
                # type: (Tuple[List[List[int]]]) -> int
                return 4

    def test_nested_list_construct_error(self):
        with self.assertRaisesRegex(RuntimeError, "Lists can only contain"):
            @torch.jit.script
            def foo(x):
                return [[4]]

    def test_script_cu(self):
        cu = torch.jit.CompilationUnit('''
            def foo(a):
                b = a
                return b
        ''')
        a = Variable(torch.rand(1))
        self.assertEqual(a, cu.foo(a))

    # because the compilation unit ingests python strings
    # to use an escape sequence escape the backslash (\\n = \n)
    def test_string_cu(self):
        cu = torch.jit.CompilationUnit('''
            def foo(a):
                print(a, """a\\n\tb\\n""", 2, "a\
a")
                return a
        ''')
        self.assertExpected(str(cu.foo.graph))

    def test_string_new_line(self):
        with self.assertRaisesRegex(RuntimeError, "expected a valid token*"):
            torch.jit.CompilationUnit('''
            def test_while(a):
                print("
                    a")
                return a
            ''')

    def test_string_single_escape(self):
        with self.assertRaisesRegex(RuntimeError, "expected a valid token*"):
            torch.jit.CompilationUnit('''
            def test_while(a):
                print("\\")
                return a
            ''')

    def test_script_annotation(self):
        @torch.jit.script
        def foo(a):
            return a + a + a
        s = Variable(torch.rand(2))
        self.assertEqual(s + s + s, foo(s))

    def test_inf(self):
        @torch.jit.script
        def foo(a):
            return a < float('inf')
        s = torch.rand(1)
        self.assertTrue(foo(s))

    def test_add(self):
        def func(a, b):
            c = a + b
            c += a
            return c

        a = torch.rand(1, requires_grad=True)
        b = torch.rand(1, requires_grad=True)
        self.checkScript(func, (a, b), optimize=True)

    def test_mul(self):
        def func(a, b):
            return a * b

        a = torch.rand(1, requires_grad=True)
        b = torch.rand(1, requires_grad=True)
        self.checkScript(func, (a, b), optimize=True)

    @unittest.skipIf(not PY35, "Python 3.5 needed")
    def test_matmul_py3(self):
        code = dedent("""
        def fn(a, b):
            return a @ b
        """)

        with tempfile.TemporaryDirectory() as tmp_dir:
            script_path = os.path.join(tmp_dir, 'script.py')
            with open(script_path, 'w') as f:
                f.write(code)
            fn = get_fn('test_matmul_py3', script_path)

            a = torch.rand(4, 3, requires_grad=True)
            b = torch.rand(3, 2, requires_grad=True)
            self.checkScript(fn, (a, b), optimize=True)

    def test_pow(self):
        def func(a, b):
            return a ** b

        def func2(a, b, c, d):
            return c + a ** b ** d

        a = torch.rand(1, requires_grad=True)
        b = torch.rand(1, requires_grad=True)
        c = torch.rand(1, requires_grad=True)
        d = torch.rand(1, requires_grad=True)
        self.checkScript(func, (a, b), optimize=True)
        self.checkScript(func2, (a, b, c, d), optimize=True)

    def test_triple(self):
        def func(x):
            return 3. * x

        x = torch.rand(1, dtype=torch.float, requires_grad=True)
        self.checkScript(func, [x], optimize=True)

    def test_slice(self):
        def func(x):
            return x[:5]

        x = torch.rand(10, dtype=torch.float, requires_grad=True)
        self.checkScript(func, [x], optimize=True)

        def func2(x):
            return x[5:]

        self.checkScript(func2, [x], optimize=True)

    def test_gather(self):
        def func(x):
            return x[0]

        x = torch.rand(10, dtype=torch.float, requires_grad=True)
        self.checkScript(func, [x], optimize=True)

    def test_random(self):
        @torch.jit.script
        def f(mean, std):
            return torch.normal(mean, std)

        mean, std = torch.zeros(5, 5), torch.ones(5, 5)
        with torch.random.fork_rng(devices=[]):
            output = torch.normal(mean, std)
        with torch.random.fork_rng(devices=[]):
            script_output = f(mean, std)
        self.assertEqual(output, script_output)

    def _check_code(self, code_str, fn_name, inputs):
        scope = {}
        exec(code_str, globals(), scope)
        cu = torch.jit.CompilationUnit(code_str)
        self.assertEqual(cu.func(*inputs), scope[fn_name](*inputs))

    @unittest.skipIf(not RUN_CUDA, 'no CUDA')
    def test_scriptmodule_releases_tensors_cuda(self):
        @torch.jit.script
        def fn(x, y):
            return x.sigmoid() * y.tanh()

        def test(backward=False):
            x = torch.randn(3, 3, dtype=torch.double, device='cuda', requires_grad=True)
            y = torch.randn(3, 3, dtype=torch.double, device='cuda', requires_grad=True)
            out = fn(x, y)
            if backward:
                out.sum().backward()

        with self.assertLeaksNoCudaTensors():
            test()
            test()
            test()

        with self.assertLeaksNoCudaTensors():
            test(backward=True)
            test(backward=True)
            test(backward=True)

    def test_index(self):
        def consec(size, start=0):
            numel = torch.tensor(size).prod().item()
            return torch.arange(numel).view(size)

        def check_indexing(indexing, tensor):
            template = dedent("""
            def func(x):
                return x{}
            """)

            self._check_code(template.format(indexing), "func", [tensor])

        def check_dynamic_indexing(indexing, tensor, value1, value2):
            value1 = torch.tensor(value1)
            value2 = torch.tensor(value2)

            template = dedent("""
            def func(x, value1, value2):
                i = int(value1)
                j = int(value2)
                return x{}
            """)

            self._check_code(template.format(indexing), "func", [tensor, value1, value2])

        # basic slices
        check_indexing('[0]', consec((3, 3)))
        check_indexing('[1]', consec((3, 3), 10))
        check_indexing('[2]', consec((3, 3), 19))
        check_indexing('[2]', consec((3,)))
        check_indexing('[-1]', consec((3, 3), 19))
        check_indexing('[0:2]', consec((3, 3, 3)))
        check_indexing('[1:-1]', consec((3, 3, 3)))
        check_indexing('[-3:-1]', consec((6, 3)))
        check_indexing('[1:]', consec((3, 3)))
        check_indexing('[:1]', consec((3, 3)))
        check_indexing('[:]', consec((3, 2)))

        # multi-dim: indexes
        check_indexing('[0, 1]', consec((3, 3)))
        check_indexing('[0, 1]', consec((3, 3, 2)))
        check_indexing('[1, 0, 2]', consec((3, 3, 3)))
        check_indexing('[2, -1]', consec((3, 3)))

        # multi-dim: mixed slicing and indexing
        check_indexing('[0, 1:2]', consec((3, 3)))
        check_indexing('[0, :1]', consec((3, 3, 2)))
        check_indexing('[1, 2:]', consec((3, 3, 3)))
        check_indexing('[-1, 1:, 0]', consec((3, 3, 3, 3)))
        check_indexing('[1:, -1, 0]', consec((3, 3, 3, 3)))
        check_indexing('[-1, 2:, 1:2]', consec((3, 3, 3, 3)))
        check_indexing('[-1, 1:, 0]', consec((3, 3, 3, 3)))
        check_indexing('[-1, :, 0, 2]', consec((3, 3, 3, 3)))

        # zero-sized slices
        check_indexing('[0:0]', consec((2, 2)))
        check_indexing('[0:0, 1]', consec((3, 3)))

        # trivial expression usage
        check_indexing('[1+1]', consec((3, 3)))
        check_indexing('[1:(0 + 2)]', consec((3, 3, 3)))

        # dynamic expression usage
        check_dynamic_indexing("[i + j]", consec((3, 3)), 0, 1)
        check_dynamic_indexing("[i:j, i]", consec((3, 3, 2)), 0, 2)

    def test_method_on_number(self):
        def func():
            c = 1
            return c.add(1)
        with self.assertRaisesRegex(RuntimeError, 'Cannot call methods on numbers'):
                    torch.jit.script(func)

    # testing implicit conversion of tensors to scalars to match function arguments
    def test_scalar_to_num_conversions(self):
        @torch.jit.script
        def multiple_defs(x):
            c = 1
            x = x + c
            return x

        self.assertTrue("ImplicitTensorToNum" not in str(multiple_defs.graph))

        @torch.jit.script
        def tensor_to_int_script(x, tensor):
            return x.unsqueeze(tensor)

        def tensor_to_int(x, tensor):
            return x.unsqueeze(tensor)

        @torch.jit.script
        def tensor_to_float_script(x, tensor):
            return x.addcmul(tensor, tensor, value=tensor)

        def tensor_to_float(x, tensor):
            return x.addcmul(tensor, tensor, value=tensor)

        x = torch.zeros(10)
        # float tensor, float tensor with grad, int tensor (can't set grad on int tensor)
        tensors = [torch.tensor(1.1),
                   torch.tensor(1.1, requires_grad=True),
                   torch.tensor(0),
                   torch.tensor([2])]

        script_funs = [tensor_to_int_script, tensor_to_float_script]
        funs = [tensor_to_int, tensor_to_float]

        # return the result, or whether exception was thrown
        def test_func(func, x, tensor):
            try:
                result = func(x, tensor)
            except RuntimeError as e:
                result = True
            except TypeError as e:
                result = True
            return result

        # assert result or exception equal for each (function, inputs)
        for tensor in tensors:
            for i in range(len(script_funs)):
                self.assertEqual(test_func(script_funs[i], x, tensor), test_func(funs[i], x, tensor))

    def test_advancedindex(self):
        def consec(size, start=0):
            numel = torch.tensor(size).prod().item()
            return torch.arange(numel).view(size)

        def check_indexing(indexing, tensor, **kwargs):
            indices_dict = kwargs

            template = dedent("""
            def func(x{formals}):
                return x{expr}
            """)

            formals = []
            values = []
            for formal, value in indices_dict.items():
                formals.append(formal)
                values.append(value)

            formals = ''.join(map(', {}'.format, formals))
            inputs = [tensor] + values

            self._check_code(template.format(formals=formals, expr=indexing),
                             "func", inputs)

        # Indexing with tensor (basic)
        check_indexing('[i]', consec((3, 3)), i=torch.tensor([0]))
        check_indexing('[i]', consec((3, 3)), i=torch.tensor(1))
        check_indexing('[i]', consec((3, 3)), i=torch.tensor([-2]))
        check_indexing('[i]', consec((3, 3), 2), i=torch.tensor([0, 0]))
        check_indexing('[i]', consec((3, 3, 2, 2)), i=torch.tensor([0, -2, 1]))

        # NB: indexing with tensors and indexing with sequences can be implemented
        # in a very similar way (sequences are converted to tensors), so only one
        # case needs to be tested extensively.
        # XXX: When we can index with sequences, replace these cases with
        # sequence indexing expressions; those are much easier to read.

        # Misc sequence advanced indexing
        inp = consec((4, 8, 5))
        to_check = [
            # [[0, 2], [1, 3]]
            ['[i, j]', dict(i=[0, 2], j=[1, 3])],
            # [[0, 2], [1, 3], [1, 1]]
            ['[i, j, k]', dict(i=[0, 2], j=[1, 3], k=[1, 1])],
            # [[0, 2], 1, [1, 1]]
            ['[i, j, k]', dict(i=[0, 2], j=1, k=[1, 1])],
            # [:, :, [0, 3, 4]]
            ['[:, :, i]', dict(i=[0, 3, 4])],
            # [:, [2, 4, 5, 7], 2:4]
            ['[:, i, 2:4]', dict(i=[0, 2, 3])],
            # [[2, 3], :, :]
            ['[i, :, :]', dict(i=[2, 3])],
            # [:, [0, 2, 3], [1, 3, 4]]
            ['[:, i, j]', dict(i=[0, 2, 3], j=[1, 3, 4])],
            # [:, [0], [1, 2, 4]]
            ['[:, i, j]', dict(i=[0], j=[1, 2, 4])],
            # [:, [0, 1, 3], [4]]
            ['[:, i, j]', dict(i=[0, 1, 3], j=[4])],
            # [:, [[0, 1], [1, 0]], [[2, 3]]]
            ['[:, i, j]', dict(i=[[0, 1], [1, 0]], j=[[2, 3]])],
            # [:, [[0, 1], [2, 3]], [[0]]]
            ['[:, i, j]', dict(i=[[0, 1], [2, 3]], j=[[0]])],
            # [:, [[5, 6]], [[0, 3], [4, 4]]]
            ['[:, i, j]', dict(i=[[5, 6]], j=[[0, 3], [4, 4]])],
            # [[0, 2, 3], [1, 3, 4], :]
            ['[i, j, :]', dict(i=[0, 2, 3], j=[1, 3, 4])],
            # [0, [1, 2, 4], :]
            ['[i, j, :]', dict(i=0, j=[1, 2, 4])],
            # [[0, 1, 3], 4, :]
            ['[i, j, :]', dict(i=[0, 1, 3], j=4)],
            # [[[0, 1], [1, 0]], [[2, 1], [3, 5]], :]
            ['[i, j, :]', dict(i=[[0, 1], [1, 0]], j=[[2, 1], [3, 5]])],
            # [[[0, 1], [1, 0]], [[2, 3]], :]
            ['[i, j, :]', dict(i=[[0, 1], [1, 0]], j=[[2, 3]])],
            # [[[0, 1], [2, 3]], [[0]], :]
            ['[i, j, :]', dict(i=[[0, 1], [2, 3]], j=[[0]])],
            # [[[2, 1]], [[0, 3], [4, 4]], :]
            ['[i, j, :]', dict(i=[[2, 1]], j=[[0, 3], [4, 4]])],
            # [[[2]], [[0, 3], [4, 1]], 0:2]
            ['[i, j, 0:2]', dict(i=[[2]], j=[[0, 3], [4, 1]])],
        ]

        for expr, argdict in to_check:
            tensordict = {k: torch.tensor(v) for (k, v) in argdict.items()}
            check_indexing(expr, inp, **tensordict)

    def test_keyword(self):
        @torch.jit.script
        def func(x):
            return torch.sum(x, dim=0)

        x = torch.rand(10, dtype=torch.float, requires_grad=True)
        y = func(x)
        y2 = torch.sum(x, dim=0)
        self.assertEqual(y, y2)

    def test_literal(self):
        def func1(a, b):
            c = a, b
            d, e = c
            return d + e

        def func2(a, b):
            c = a, (a, b)
            d, e = c
            f, g = e
            return d + f + g

        def func3(a, b):
            # type: (float, float) -> float
            c = 0., (0., 0.)
            x = True
            while x:
                x = False
                c = a, (a, b)
            d, e = c
            f, g = e
            return d + f + g

        a = torch.rand(1, requires_grad=True)
        b = torch.rand(1, requires_grad=True)
        self.checkScript(func1, (a, b), optimize=True)
        self.checkScript(func2, (a, b), optimize=True)
        self.checkScript(func3, (a.item(), b.item()), optimize=True)

    def test_expand(self):
        @torch.jit.script
        def func(x, y):
            return x + y

        x = torch.rand(2, 3, dtype=torch.float, requires_grad=True)
        y = torch.rand(3, dtype=torch.float, requires_grad=True)
        out = func(x, y)
        self.assertEqual(func(x, y), x + y)

        grad = torch.randn(2, 3, dtype=torch.float)
        out.backward(grad)
        self.assertEqual(x.grad, grad)
        self.assertEqual(y.grad, grad.sum(dim=0))

    def test_sum(self):
        @torch.jit.script
        def func(x):
            return x.sum(dim=[4])

        @torch.jit.script
        def func2(x):
            return x.sum(dim=4)

        self.assertExpected(canonical(func.graph), subname='1')
        # test that shape analysis is written correctly for sum with IntList[1] dim argument
        torch._C._jit_pass_shape_analysis(
            func2.graph, (torch.zeros(1, 1, 1, 1, 4),), False)
        self.assertExpected(canonical(func2.graph), subname='2')

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "No CUDA")
    @skipIfRocm
    def test_chunk_fusion_cuda(self):
        def fn(x):
            a, b, c = x.chunk(3, 1)
            return a * b + c

        inputs = [torch.randn(10, 6, dtype=torch.float, device='cuda')]

        self.checkScript(fn, inputs)

        fn_script = torch.jit.script(fn)
        _ = fn_script(*inputs)
        self.assertExpectedGraph(fn_script.graph_for(*inputs))

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "No CUDA")
    @skipIfRocm
    def test_chunk_multiple_fusion_cuda(self):
        # The arguments are intentionally used out of order as a test to see
        # if the fusion compiler adds extra args in the correct order
        def fn(s, x, y, z):
            z1, z2 = z.chunk(2, 2)
            x1, x2, x3 = x.chunk(3, 1)
            y1, y2 = y.chunk(2, 0)
            return s + x1 + x2 + x3 + y1 + y2 + z1 + z2

        inputs = [
            torch.randn(5, 2, 3, dtype=torch.float, device='cuda'),
            torch.randn(5, 6, 3, dtype=torch.float, device='cuda'),
            torch.randn(10, 2, 3, dtype=torch.float, device='cuda'),
            torch.randn(5, 2, 6, dtype=torch.float, device='cuda'),
        ]

        self.checkScript(fn, inputs)

        fn_script = torch.jit.script(fn)
        _ = fn_script(*inputs)
        self.assertExpectedGraph(fn_script.graph_for(*inputs))

    @staticmethod
    def _test_chunk_fusion_correctness(self, device='cpu'):
        def chunk_4_0(x):
            x0, x1, x2, x3 = x.chunk(4, 0)
            return x0 + x1 + x2 + x3

        def chunk_4_1(x):
            x0, x1, x2, x3 = x.chunk(4, 1)
            return x0 + x1 + x2 + x3

        def chunk_4_last(x):
            x0, x1, x2, x3 = x.chunk(4, 2)
            return x0 + x1 + x2 + x3

        fns = [chunk_4_0, chunk_4_1, chunk_4_last]
        tensors = [
            # splitSize = 1
            torch.randn(4, 4, 4, dtype=torch.float, device=device),

            # contiguous case
            torch.randn(12, 8, 16, dtype=torch.float, device=device),

            # non-contiguous case
            torch.randn(12, 8, 16, dtype=torch.float, device=device).transpose(1, 2),
        ]

        for tensor in tensors:
            for fn in fns:
                self.checkScript(fn, [tensor])

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @skipIfRocm
    @enable_cpu_fuser
    def test_chunk_fusion_correctness(self):
        return self._test_chunk_fusion_correctness(self, 'cpu')

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "No CUDA")
    @skipIfRocm
    def test_chunk_fusion_correctness_cuda(self):
        return self._test_chunk_fusion_correctness(self, 'cuda')

    def test_cat(self):
        @torch.jit.script
        def func(x):
            return torch.cat((x, x), dim=0)

        x = torch.rand(10, dtype=torch.float, requires_grad=True)
        self.assertEqual(func(x), torch.cat((x, x), dim=0))

        @torch.jit.script
        def func2(x, y):
            return torch.cat((x, x), y)

        x = torch.rand([2, 2])
        y = torch.tensor(1)
        self.assertEqual(func2(x, y), torch.cat((x, x), y))

    def test_cat_lifts(self):
        @torch.jit.script
        def foo(x):
            return torch.cat([x, x], dim=1)

        @torch.jit.script
        def foo2(x):
            return torch.cat(_construct_empty_tensor_list(), dim=1)

        @torch.jit.script
        def foo3(x):
            return torch.cat([x], dim=1)

        self.assertExpected(
            canonical(foo.graph) +
            canonical(foo2.graph) +
            canonical(foo3.graph))

    def test_list_literal(self):
        def reassign():
            x = [1]
            if True:
                x = [2, 3]
            return
        self.checkScript(reassign, (), optimize=False)

        def reassign_arity_change():
            x = [1]
            if True:
                x = [1, 2, 3]
            return
        self.checkScript(reassign_arity_change, (), optimize=False)

        def reassign_from_empty_literal():
            x = []
            if True:
                x = [1, 2, 3]
            return
        with self.assertRaisesRegex(RuntimeError, "previously has type Tensor\[\]"):
            self.checkScript(reassign_from_empty_literal, (), optimize=False)

        def reassign_from_empty_builtin():
            x = _construct_empty_int_list()
            if True:
                x = [1, 2, 3]
            y = _construct_empty_float_list()
            if True:
                y = [1.0, 2.0, 3.0]
            z = _construct_empty_tensor_list()
            if True:
                z = [torch.randn([1])]
            return
        self.checkScript(reassign_from_empty_builtin, (), optimize=False)

        def reassign_bad_type():
            x = [1]
            if True:
                x = [1.0]
            return
        with self.assertRaisesRegex(RuntimeError, "previously has type"):
            self.checkScript(reassign_bad_type, (), optimize=False)

        def reassign_nested():
            x = _construct_empty_int_list()
            if True:
                x = [1, 2, 3]
                if True:
                    x = [1.0]
            return
        with self.assertRaisesRegex(RuntimeError, "previously has type"):
            self.checkScript(reassign_nested, (), optimize=False)

    def test_list_gather(self):
        def index():
            a = [1, 2, 3]
            return a[1]

        self.checkScript(index, ())

        def negative_index():
            a = [1, 2, 3]
            return a[-1]

        self.checkScript(negative_index, ())

        def bad_index():
            a = [1, 2, 3]
            return a[4]

        self.checkScriptRaisesRegex(bad_index, (), IndexError,
                                    "list index out of range")

        def bad_negative_index():
            a = [1, 2, 3]
            return a[-5]

        self.checkScriptRaisesRegex(bad_negative_index, (), IndexError,
                                    "list index out of range")

    def test_list_len(self):
        def func():
            a = [1, 2, 3]
            return len(a) == 3

        self.checkScript(func, ())

        def func2():
            a = _construct_empty_tensor_list()
            return len(a) == 0

        self.checkScript(func2, ())

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @skipIfRocm
    def test_tensor_scalar_fusion_cuda(self):
        def should_fuse(x):
            z = 3.
            y = x + z
            return x * y

        # XXX: right now we only support fusing scalars if
        # they're constant (#9940)
        def should_not_fuse(x, z):
            y = x + int(z)
            return x * y

        inputs = [torch.randn(2, 2, dtype=torch.float, device='cuda')]
        ge = self.checkScript(should_fuse, inputs)
        self.assertExpectedGraph(ge.graph_for(*inputs), subname='1')

        inputs = [
            torch.randn(2, 2, dtype=torch.float, device='cuda'),
            torch.tensor(3., dtype=torch.float, device='cuda'),
        ]
        ge = self.checkScript(should_not_fuse, inputs)
        self.assertExpectedGraph(ge.graph_for(*inputs), subname='2')

    def test_list_ops(self):
        def test_equality():
            a = [1, 2, 3]
            b = [1, 2, 3]
            return a == b

        self.checkScript(test_equality, (), optimize=True)

        def test_non_equality():
            a = [1, 2, 3]
            b = [3]
            return a == b

        self.checkScript(test_non_equality, (), optimize=True)

        def test_list_add():
            a = [1, 2, 3]
            b = [2]
            c = a + b
            return c == [1, 2, 3, 2]

        self.checkScript(test_list_add, (), optimize=True)

        def test_list_add_empty():
            a = [1, 2, 3]
            b = _construct_empty_int_list()
            c = a + b
            return c == [1, 2, 3]

        self.checkScript(test_list_add_empty, (), optimize=True)

        def test_tensor_list_equality():
            t1 = torch.ones([1, 1])
            t2 = torch.ones([1, 1])
            x = [t1, t2]
            y = [t2, t1]
            return x == y

        self.checkScript(test_tensor_list_equality, (), optimize=True)

        def test_invalid_list_equality():
            t1 = torch.ones([2, 2])
            t2 = torch.ones([2, 2])
            x = [t1, t2]
            y = [t2, t1]
            # will throw since the tensors have more than one element
            return x == y

        self.checkScriptRaisesRegex(
            test_invalid_list_equality,
            (),
            RuntimeError,
            "bool value of Tensor")

    def test_list_slice(self):
        def test_regular_slice():
            a = [0, 1, 2, 3, 4]
            return a[2:3] == [2]
        self.checkScript(test_regular_slice, ())

        def test_open_ended_slice():
            a = [0, 1, 2, 3, 4]
            return a[2:] == [2, 3, 4]
        self.checkScript(test_open_ended_slice, ())

        def test_open_ended_slice2():
            a = [0, 1, 2, 3, 4]
            return a[:2] == [0, 1]
        self.checkScript(test_open_ended_slice2, ())

        def test_negative_slice():
            a = [0, 1, 2, 3, 4]
            return a[:-1] == [0, 1, 2, 3]
        self.checkScript(test_negative_slice, ())

        def test_negative_slice2():
            a = [0, 1, 2, 3, 4]
            return a[-3:-1] == [2, 3]
        self.checkScript(test_negative_slice2, ())

        def test_backward_slice():
            a = [0, 1, 2, 3, 4]
            return a[3:2] == _construct_empty_int_list()
        self.checkScript(test_backward_slice, ())

        def test_over_slice():
            a = [0, 1, 2, 3, 4]
            return a[3:10] == [3, 4]
        self.checkScript(test_backward_slice, ())

    def test_func_call(self):
        script = '''
        def add(a, b):
            return a + b

        def mul(a, x):
            return a * x

        def func(alpha, beta, x, y):
            return add(mul(alpha, x), mul(beta, y))
        '''
        alpha = torch.rand(1, dtype=torch.float, requires_grad=True)
        beta = torch.rand(1, dtype=torch.float, requires_grad=True)
        x = torch.rand(3, dtype=torch.float, requires_grad=True)
        y = torch.rand(3, dtype=torch.float, requires_grad=True)
        outputs = alpha * x + beta * y
        # NOTE: cannot optimize yet because broadcasts are not inserted before the fuser runs
        self.checkScript(script, [alpha, beta, x, y], optimize=False, outputs=outputs)

    def test_view_shape_prop(self):
        cu = torch.jit.CompilationUnit('''
        def test_view_shape_prop(a):
            return view(a, size=[-1])
        ''')
        inputs = [torch.zeros(10, 10)]
        outputs = torch.zeros(100)

        real_outs = cu.test_view_shape_prop(*inputs)
        self.assertEqual(real_outs, outputs)

    def test_integral_shape_inference(self):
        cu = torch.jit.CompilationUnit('''
        def test_integral_shape_inference(a):
            return a / a
        ''')
        inputs = [torch.ones(10, 10).type(torch.LongTensor)]
        outputs = torch.ones(10, 10)

        self.assertEqual(cu.test_integral_shape_inference(*inputs), outputs)

    def test_fuser_multiple_blocks(self):
        cu = torch.jit.CompilationUnit('''
        def test_fuser_multiple_blocks(this, that, theother, meme):
            i = 0
            while i < 20:
                this = cat([this, meme], dim=0)
                that = cat([that, meme], dim=0)
                theother = cat([theother, meme], dim=0)
                i = i + 1
            return this, that, theother
        ''')

        inputs = [torch.ones(0, 10, 10)] * 3
        inputs += [torch.ones(1, 10, 10)]
        outputs = [torch.ones(20, 10, 10)] * 3

        self.assertEqual(cu.test_fuser_multiple_blocks(*inputs), outputs)

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skip("this test is flaky, see #11360")
    def test_scalar_fusion(self):
        def fn(x, y):
            return x + y.type_as(x)

        x = torch.tensor(0.1, dtype=torch.float, device='cpu')
        y = torch.tensor(1, dtype=torch.float, device='cpu')
        ge = self.checkScript(fn, (x, y))
        self.assertExpectedGraph(ge.graph_for(x, y))

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @skipIfRocm
    def test_lstm_fusion_cuda(self):
        inputs = get_lstm_inputs('cuda', training=True)
        module = self.checkScript(LSTMCellS, inputs)
        self.assertExpectedGraph(module.graph_for(*inputs), subname='forward')

        hy, cy = module(*inputs)
        (hy + cy).sum().backward()
        self.assertExpectedGraph(backward_graph(module), subname='backward')

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @skipIfRocm
    def test_milstm_fusion_cuda(self):
        inputs = get_milstm_inputs('cuda', training=True)
        module = self.checkScript(MiLSTMCell, inputs)
        self.assertExpectedGraph(module.graph_for(*inputs), subname='forward')

        hy, cy = module(*inputs)
        (hy + cy).sum().backward()
        self.assertExpectedGraph(backward_graph(module), subname='backward')

    def test_dropout_script(self):

        eg = torch.zeros(1, 2, 3, requires_grad=True)

        @_trace(eg)
        def foo(x):
            x = torch.neg(x)
            return F.dropout(x)

        class MyDrop(nn.Module):
            def forward(self, x):
                return foo(x)

        f = io.BytesIO()
        torch.onnx.export(MyDrop(), (eg,), f, verbose=False)

    @unittest.skip("RuntimeError: VariableType::ID() not implemented")
    def test_cast(self):
        script = '''
        def to_int(x):
            return int(x)
        '''
        x = Variable(torch.FloatTensor([1.1, 2.3]), requires_grad=True)
        out = Variable(torch.IntTensor([1, 2]), requires_grad=True)
        self.checkScript(script, [x], optimize=True, outputs=[out], func='to_int')

    def test_python_frontend(self):
        def fn(x, y, z):
            q = None
            q = x + y - z.sigmoid()
            print(q)
            w = -z
            if not x and not y and z:
                m = x if not z else y
            while x < y > z:
                q = x
            return x

        ast = torch.jit.frontend.get_jit_ast(fn, is_method=False)
        self.assertExpected(str(ast))

    def _make_scalar_vars(self, arr, dtype):
        return [torch.tensor(val, dtype=dtype) for val in arr]

    def test_string_print(self):
        def func(a):
            print(a, "a" 'b' '''c''' """d""", 2, 1.5)
            return a

        inputs = self._make_scalar_vars([1], torch.int64)
        self.checkScript(func, inputs, capture_output=True)

    def test_while(self):
        def func(a, b, max):
            while bool(a < max):
                a = a + 1
                b = b + 1
            c = a + b
            return c

        inputs = self._make_scalar_vars([1, 1, 10], torch.int64)
        self.checkScript(func, inputs, optimize=True)

    def test_fibb(self):
        def func(lim):
            first = 1
            second = 1
            i = 1
            somenum = 5
            dontmutateme = 3
            third = 0
            while bool(i < lim):
                third = first + second
                first = second
                second = third
                j = 0
                while j < 10:
                    somenum = somenum * 2
                    j = j + 1
                i = i + j
                i = i + dontmutateme

            st = second + third
            fs = first + second
            return third, st, fs

        inputs = self._make_scalar_vars([10], torch.int64)
        self.checkScript(func, inputs, optimize=True)

    def test_if(self):
        def func(a, b):
            # type: (int, int) -> int
            d = 3
            if bool(a > 10):
                a = 3 + d
            else:
                b = 3 + d
                d = 4
            c = a + b
            return c

        inputs = self._make_scalar_vars([1, -1], torch.int64)
        self.checkScript(func, inputs, optimize=True)

    def test_if_for_in_range(self):
        def func(a, b):
            # type: (int, int) -> int
            d = 3
            for _ in range(20):
                if bool(a > 10):
                    a = 3 + d
                else:
                    b = 3 + d
                    d = 4
                c = a + b
            return d
        inputs = self._make_scalar_vars([1, -1], torch.int64)
        self.checkScript(func, inputs, optimize=True)

    def test_if_noelse(self):
        def func(a, b):
            if bool(a > 10):
                a = 3 + b
            c = a + b
            return c

        inputs = self._make_scalar_vars([-1, 1], torch.int64)
        self.checkScript(func, inputs, optimize=True)

    def test_explicit_bool_cast(self):
        with self.assertRaisesRegex(RuntimeError, "expected an integer"):
            @torch.jit.script
            def test_bool_cast(a):
                if a:
                    return a + 2
                return a + 1

    def test_while_nonexistent_value(self):
        with self.assertRaisesRegex(RuntimeError, "undefined value x"):
            torch.jit.CompilationUnit('''
            def test_while(a, b):
                while bool(a < 10):
                    a = a + x
                    b = b + 1
                return a + b
            ''')

    def test_while_nonexistent_cond_value(self):
        with self.assertRaisesRegex(RuntimeError, "undefined value x"):
            torch.jit.CompilationUnit('''
            def test_while(a, b):
                while a < x:
                    a = a + 1
                    b = b + 1
                return a + b
            ''')

    def test_while_write_outer_then_read(self):
        def func(a, b):
            while bool(a < 10):
                a = a + 1
                b = a + 1
            return a + b

        inputs = self._make_scalar_vars([42, 1337], torch.int64)
        self.checkScript(func, inputs, optimize=True)

    def test_while_nest_if(self):
        def func(a, b):
            # type: (int, int) -> int
            c = 0
            while a < 10:
                a = a + 1
                b = b + 1
                if a > b:
                    c = -a
                else:
                    c = -b
            return c + 1

        inputs = self._make_scalar_vars([-1234, 4321], torch.int64)
        self.checkScript(func, inputs, optimize=True)

    def test_math_schema(self):
        # This should use the add(Tensor, Tensor) schema.
        # Also tests to see if alpha={1} is lifted correctly.
        def fn(x, y):
            return x + y

        graph = torch.jit.script(fn).graph
        self.assertExpectedGraph(graph)

    def test_math_tensor_number(self):
        # Test that 7 is casted to tensor, then casted to the
        # correct type, and finally added to x.
        def fn(x):
            return x + 7

        graph = torch.jit.script(fn).graph
        self.assertExpectedGraph(graph)

    def test_math_numbers(self):
        # Test that the numbers are casted to tensor,
        # added, and then casted back.
        def fn1(x):
            return 7 + 8

        def fn2(x):
            return 1.1 + 3.1

        graph1 = torch.jit.script(fn1).graph
        self.assertExpectedGraph(graph1, subname="int")
        graph2 = torch.jit.script(fn2).graph
        self.assertExpectedGraph(graph2, subname="float")

    def test_if_nest_while(self):
        def func(a, b):
            # type: (int, int) -> int
            c = 0
            if a > b:
                while a > b:
                    b = b + 1
                    c = -b
            return c

        inputs = self._make_scalar_vars([4321, 1234], torch.int64)
        self.checkScript(func, inputs, optimize=True)

    def test_script_for_in_range(self):
        def fn():
            c = 0
            for i in range(100):
                c += i
            return c
        self.checkScript(fn, (), outputs=4950, optimize=True)

    def test_script_for_in_range_dynamic(self):
        def fn():
            c = 0
            for i in range(100):
                acc = 0
                for j in range(i):
                    acc += j
                c += acc
            return c
        self.checkScript(fn, (), optimize=False)

    def test_script_for_in_range_ast(self):
        @torch.jit.script
        def test_script_for_in_range_ast():
            c = 0
            for i in range(100):
                acc = 0
                for j in range(i):
                    acc += j
                c += acc
            return c

        self.assertEqual(test_script_for_in_range_ast(), 161700)

    def test_script_for_in_range_if_ast(self):
        @torch.jit.script
        def test_script_for_in_range_if_ast(x):
            output = x
            for i in range(20):
                if i == 0:
                    output = x.unsqueeze(0)
                else:
                    output = torch.cat((output, x.unsqueeze(0)), dim=0)
            return output
        inputs = self._make_scalar_vars([0], torch.int64)

        self.assertEqual(test_script_for_in_range_if_ast(*inputs).shape[0], 20)

    def test_script_None(self):
        def func(x):
            output = None
            output = x
            return output

        self.checkScript(func, [torch.arange(0, 2)], optimize=True)

    def test_script_clamp_none(self):
        # TODO: could not enable default/optional argument for None in JIT
        # result from Aten native python_default_init for clamp, it is used
        # in Aten but not in JIT, need to fix type/default arg system in ATen
        def test_script_clamp_max_none(x):
            return torch.clamp(x, min=None, max=2)

        def test_script_clamp_min_none(x):
            return torch.clamp(x, min=2, max=None)

        input = [torch.arange(0, 3)]
        self.checkScript(test_script_clamp_max_none, input, optimize=True)
        self.checkScript(test_script_clamp_min_none, input, optimize=True)

    def test_script_bool_constant(self):
        script = '''
        def test_script_bool_constant():
            a = True
            return a
        '''
        outputs = [1]
        self.checkScript(script, [], outputs[0], True, 'test_script_bool_constant')

    def test_ternary(self):
        def func(a, b):
            c = 3
            c = a + b if bool(a > 3) else b
            return c

        inputs_true = self._make_scalar_vars([5, 2], torch.int64)
        inputs_false = self._make_scalar_vars([1, 0], torch.int64)
        self.checkScript(func, inputs_true, optimize=True)
        self.checkScript(func, inputs_false, optimize=True)

    def test_print(self):
        def func(x, y):
            q = (x + y).sigmoid()
            print(q, 1, 2, [1, 2], [1.0, 2.0])
            w = -q
            return w * w

        x = torch.arange(4., requires_grad=True)
        y = torch.arange(0., 8, 2, requires_grad=True)
        self.checkScript(func, [x, y], optimize=True, capture_output=True)

    def test_logical_short_circuit(self):
        @torch.jit.script
        def testNoThrows(t):
            c1 = 1
            if (False and bool(t[1])) or (True or bool(t[1])):
                c1 = 0
            return c1

        @torch.jit.script
        def throwsOr(t):
            c0 = False or bool(t[1])
            print(c0)

        @torch.jit.script
        def throwsAnd(t):
            c0 = True and bool(t[1])
            print(c0)

        t = torch.randn(0)
        self.assertEqual(0, testNoThrows(torch.randn(0)))
        self.assertExpectedGraph(testNoThrows.graph)
        with self.assertRaisesRegex(RuntimeError, "index 1 out of range for tensor of size"):
            throwsOr(t)
        with self.assertRaisesRegex(RuntimeError, "index 1 out of range for tensor of size"):
            throwsAnd(t)

    def test_type_cast(self):
        def test_int_to_float():
            b = float(2)
            return b + 1.0

        def test_float_to_int():
            b = int(2.0)
            return b + 1

        graph1 = torch.jit.script(test_int_to_float).graph
        self.assertExpectedGraph(graph1, subname="int_to_float")
        graph2 = torch.jit.script(test_float_to_int).graph
        self.assertExpectedGraph(graph2, subname="float_to_int")

    def test_multiple_assignment(self):
        def outer_func(x):
            return x * 2, x + 2

        @torch.jit.script
        def func(x):
            y, z = outer_func(x)
            return y + z

        x = torch.arange(4)
        self.assertEqual(func(x), x * 2 + x + 2)

    def test_literals(self):
        def func(a):
            return a.view(size=[1, 2, 3])

        a = torch.randn(6)
        self.checkScript(func, [a], optimize=True)

    def test_return(self):
        def no_return(a):
            a + 1

        def void_return(a):
            return

        def one_return(a):
            return a + 1.

        def multiple_returns(a):
            return a * 1., a * 2., a * 3.

        a = torch.randn(1, dtype=torch.float)
        self.checkScript(no_return, [a], optimize=True)
        self.checkScript(void_return, [a], optimize=True)
        self.checkScript(one_return, [a], optimize=True)
        self.checkScript(multiple_returns, [a], optimize=True)

    def test_error(self):
        @torch.jit.script
        def foo(a):
            return a.t()
        s = Variable(torch.rand(10))
        # XXX: this should stay quiet in stay propagation and only fail in the interpreter
        with self.assertRaisesRegex(RuntimeError, "failed in interpreter"):
            foo(s)

        @torch.jit.script
        def bar(c, b):
            return c + b

        with self.assertRaisesRegex(RuntimeError, "failed in interpreter"):
            bar(Variable(torch.rand(10), requires_grad=True), Variable(torch.rand(9), requires_grad=True))

    def test_binop_unsupported_error(self):
        with self.assertRaisesRegex(NotSupportedError, "unsupported binary operator:"):
            @torch.jit.script
            def binop(x, y):
                # Replace this with another unsupported op when/if it gets supported
                return x << y

    def test_number_math(self):
        template = dedent('''
        # int, int -> int
        def func1():
            return 7 {op} 2

        def func2():
            return 3 {op} 2

        def func3():
            return -7 {op} 3

        def func4():
            return 7 {op} -3

        # float, float -> float
        def func5():
            return 3.14 {op} 0.125

        def func6():
            return 3.14 {op} 3.14

        def func7():
            return -0.5 {op} 2.0

        def func8():
            return 3.5 {op} -2.0

        ''')
        ops = ['+', '-', '*', '%', '<', '<=', '>', '>=', '==', '!=']

        for op in ops:
            code = template.format(op=op)
            scope = {}
            exec(code, globals(), scope)
            cu = torch.jit.CompilationUnit(code)

            self.assertEqual(cu.func1(), scope['func1']())
            self.assertEqual(cu.func2(), scope['func2']())
            self.assertEqual(cu.func3(), scope['func3']())
            self.assertEqual(cu.func4(), scope['func4']())
            self.assertEqual(cu.func5(), scope['func5']())
            self.assertEqual(cu.func6(), scope['func6']())
            self.assertEqual(cu.func7(), scope['func7']())
            self.assertEqual(cu.func8(), scope['func8']())

    def test_number_div(self):
        self.checkScript(div_int_future, (), optimize=True)
        self.checkScript(div_float_future, (), optimize=True)

        if PY2:
            with self.assertRaisesRegex(RuntimeError, 'from __future__ import division'):
                torch.jit.script(div_int_nofuture)
            with self.assertRaisesRegex(RuntimeError, 'from __future__ import division'):
                torch.jit.script(div_float_nofuture)
        else:
            self.checkScript(div_int_nofuture, (), optimize=True)
            self.checkScript(div_float_nofuture, (), optimize=True)

    def test_number_augassign(self):
        def func():
            z = 1
            z += 2
            return z

        self.checkScript(func, (), optimize=True)

    def test_number_neg(self):
        # int -> int
        def func1():
            return -8

        # float -> float
        def func2():
            return -3.14

        self.checkScript(func1, (), optimize=True)
        self.checkScript(func2, (), optimize=True)

    def _test_tensor_number_math(self, device='cpu'):
        template = dedent('''
        def func(t):
            return {lhs} {op} {rhs}
        ''')

        def test(op, const, swap_args):
            args = ('t', const)
            if swap_args:
                args = (const, 't')

            code = template.format(lhs=args[0], rhs=args[1], op=op)
            scope = {}
            exec(code, globals(), scope)
            cu = torch.jit.CompilationUnit(code)
            self.assertEqual(cu.func(tensor), scope['func'](tensor))

        var_int = [2, -2]
        var_float = [1.4321, -1.2]

        ops = ['+', '-', '*', '%', '<', '<=', '>', '>=', '==', '!=']
        # TODO: turn this on for py3 (and add PY3 division semantics)
        ops_py2_only = ['/']
        if PY2:
            ops.extend(ops_py2_only)

        float_tensor = torch.randn(5, 5, device=device)
        double_tensor = torch.randn(5, 5, dtype=torch.double, device=device)
        long_tensor = torch.randint(-5, 5, (5, 5), dtype=torch.long, device=device)
        long_tensor[long_tensor == 0] = 2

        tensors = [float_tensor, double_tensor, long_tensor]
        consts = var_int + var_float

        for op, tensor, const, swap_args in product(ops, tensors, consts, [True, False]):
            # FIXME: things like 2 / long_tensor are not implemented correctly
            # Look in torch/tensor.py to see how pytorch implements it.
            if op == '/' and tensor.data_ptr() == long_tensor.data_ptr():
                continue

            # % operator does not take: const % tensor
            if op == '%' and swap_args is True:
                continue

            test(op, const, swap_args)

    def test_tensor_number_math(self):
        self._test_tensor_number_math()

    @unittest.skipIf(not RUN_CUDA, "No CUDA")
    @skipIfRocm
    def test_tensor_number_math_cuda(self):
        self._test_tensor_number_math(device='cuda')

    def test_python_call(self):
        def pyfunc(a):
            return a * 3.0

        cu = torch.jit.CompilationUnit('''
        def other_func(a):
            return a + a

        def test_call_python(a):
            b = pyfunc(a)
            b = other_func(b)
            i = 0
            step = 1
            while i < 10:
                b = pyfunc(b)
                if bool(b > 3.0):
                    b = pyfunc(b)
                i = 11
            return b
        ''')
        inputs = self._make_scalar_vars([1], torch.float)
        outputs = self._make_scalar_vars([54], torch.float)

        self.assertEqual(cu.test_call_python(*inputs), outputs[0])

    def test_python_call_failure(self):
        with self.assertRaisesRegex(RuntimeError, "undefined value pyfunc2"):
            def pyfunc(a):
                return a * 3.0

            cu = torch.jit.CompilationUnit('''
            def other_func(a):
                return a + a

            def test_call_python(a):
                b = pyfunc(a)
                b = other_func(b)
                i = 0
                step = 1
                while i < 10:
                    b = pyfunc2(b)
                    if b > 3.0:
                        b = pyfunc(b)
                    i = 11
                return b
            ''')
            inputs = self._make_scalar_vars([1], torch.float)
            outputs = self._make_scalar_vars([54], torch.float)

            self.assertEqual(cu.test_call_python(*inputs), outputs)

    def test_python_call_annotation(self):
        def pyfunc(a):
            return a * 3.0

        @torch.jit.script
        def foo(a):
            return pyfunc(a) + pyfunc(a)

        inputs = self._make_scalar_vars([1], torch.float)
        outputs = self._make_scalar_vars([6], torch.float)
        self.assertEqual(foo(*inputs), outputs[0])

    def test_python_call_annoytation_failure(self):
        with self.assertRaisesRegex(RuntimeError, "undefined value pyfunc2"):
            def pyfunc(a):
                return a * 3.0

            @torch.jit.script
            def foo(a):
                return pyfunc2(a) + pyfunc(a)

            inputs = self._make_scalar_vars([1], torch.float)
            outputs = self._make_scalar_vars([6], torch.float)

            self.assertEqual(foo(*inputs), outputs[0])

    def test_desugar_module(self):
        import torch.nn.functional as F

        def fn(x, slope):
            a = torch.abs(x)
            b = torch.nn.functional.prelu(x, slope)
            c = F.prelu(x, slope)
            return a, b, c

        x = torch.arange(-3., 4)
        slope = torch.tensor([0.5])
        self.checkScript(fn, [x, slope], optimize=True)

    def test_script_docstring(self):
        @torch.jit.script
        def with_docstring(x):
            """test str"""
            y = x
            """y is the same as x"""
            return y
        self.assertEqual(with_docstring.__doc__, 'test str')

    def test_script_method_docstring(self):
        class A(torch.jit.ScriptModule):
            @torch.jit.script_method
            def with_docstring(self, x):
                """test str"""
                y = x
                """y is the same as x"""
                return y
        a = A()
        self.assertEqual(a.with_docstring.__doc__, 'test str')

    def test_script_module(self):
        class M1(torch.jit.ScriptModule):
            def __init__(self):
                super(M1, self).__init__(False)
                self.weight = nn.Parameter(torch.randn(2))

            @torch.jit.script_method
            def forward(self, thing):
                return self.weight + thing

        class PModule(nn.Module):
            def __init__(self):
                super(PModule, self).__init__()
                self.a = nn.Parameter(torch.randn(2, 3))

            def forward(self, a):
                return self.a.mm(a)

        class M2(torch.jit.ScriptModule):
            def __init__(self):
                super(M2, self).__init__(False)
                # test submodule
                self.sub = M1()
                self.sub2 = PModule()
                # test parameters
                self.weight = nn.Parameter(torch.randn(2, 3))
                self.bias = nn.Parameter(torch.randn(2))
                # test defining a method from a string
                self.define("""
                    def hi(self, a):
                        return self.weight.mm(a)
                """)
            # test script methods

            @torch.jit.script_method
            def doit(self, input):
                # test use of parameter
                return self.weight.mm(input)

            @torch.jit.script_method
            def doit2(self, input):
                return self.weight.mm(input)

            @torch.jit.script_method
            def forward(self, input):
                a = self.doit(input)
                b = self.doit2(input)
                c = self.hi(input)
                d = self.sub2(input)
                return a + b + self.bias + self.sub(a) + c + d
        m2 = M2()
        input = torch.randn(3, 2)
        a = m2.weight.mm(input)
        b = m2.weight.mm(input)
        c = m2.weight.mm(input)
        d = m2.sub2.a.mm(input)
        ref = a + b + m2.bias + m2.sub.weight + a + c + d
        self.assertEqual(ref, m2.forward(input))
        m2.weight = nn.Parameter(torch.zeros_like(m2.weight))
        m2.bias = nn.Parameter(torch.zeros_like(m2.bias))
        m2.sub.weight = nn.Parameter(torch.zeros_like(m2.sub.weight))
        m2.sub2.a.data.zero_()
        self.assertEqual(torch.zeros(2, 2), m2.forward(torch.randn(3, 2)))

    def test_script_module_call_noscript(self):
        class M(torch.jit.ScriptModule):
            def __init__(self):
                super(M, self).__init__(False)
                self.value = 1

            def foo(self):
                return torch.ones(2, 2) + self.value

            @torch.jit.script_method
            def forward(self, input):
                return input + self.foo()

        m = M()
        input = torch.randn(2, 2)
        o = m(input)
        self.assertEqual(o, input + torch.ones(2, 2) + 1)
        # check that we can change python attributes
        # and that those changes are picked up in script methods
        m.value = 2
        o = m(input)
        self.assertEqual(o, input + torch.ones(2, 2) + 2)

    def test_script_module_nochange_submodule(self):
        class M(torch.jit.ScriptModule):
            def __init__(self):
                super(M, self).__init__(False)
                self.sub = nn.Linear(5, 5)

            @torch.jit.script_method
            def forward(self, input):
                return self.sub(input)

        m = M()
        input = torch.randn(1, 5, 5)
        o = m(input)
        self.assertEqual(o, m.sub(input))
        with self.assertRaisesRegex(RuntimeError, "cannot re-assign"):
            m.sub = nn.Linear(5, 5)

    def test_script_inline_trace_multiple_args(self):
        class M(torch.jit.ScriptModule):
            def __init__(self):
                super(M, self).__init__(False)

            def forward(self, input, input2):
                return input + input2

        class M2(torch.jit.ScriptModule):
            def __init__(self):
                super(M2, self).__init__(False)
                self.m = torch.jit.trace(M(), (torch.zeros(4, 3), torch.zeros(4, 3)))

            @torch.jit.script_method
            def forward(self, inp):
                return self.m(inp, inp)

        m2 = M2()
        m2(torch.zeros(4, 3))

    def test_script_module_const(self):
        class M(torch.jit.ScriptModule):

            __constants__ = ['b', 'i', 'c']

            def __init__(self):
                super(M, self).__init__(False)
                self.b = False
                self.i = 1
                self.c = 3.5

            @torch.jit.script_method
            def forward(self):
                return self.b, self.i, self.c

        m = M()
        o0, o1, o2 = m()
        self.assertEqual(o0, 0)
        self.assertEqual(o1, 1)
        self.assertEqual(o2, 3.5)

    def test_script_module_fail_const(self):
        class M(torch.jit.ScriptModule):
            def __init__(self):
                super(M, self).__init__(False)
                self.b = False

            @torch.jit.script_method
            def forward(self):
                return self.b
        with self.assertRaisesRegex(RuntimeError, "is not usable in a script method"):
            M()

    def test_script_module_valid_consts(self):
        class Foo(torch.jit.ScriptModule):
            __constants__ = ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i']

            def __init__(self):
                super(Foo, self).__init__(False)
                self.a = 1
                self.b = 1.2
                self.c = False
                self.d = [nn.Linear(3, 4)]
                self.e = lambda x: x
                self.f = [3, 4, 5]
                self.assertTrue(type(self.f) is tuple)
                self.g = [3, (3, 4), 5]
                with self.assertRaisesRegex(TypeError, "is not a valid constant"):
                    self.h = type(1)
                with self.assertRaisesRegex(TypeError, "is not a valid constant"):
                    self.i = (3, 4, {})

    def test_script_module_for(self):
        class M(torch.jit.ScriptModule):
            __constants__ = ['b']

            def __init__(self):
                super(M, self).__init__(False)
                self.b = [1, 2, 3, 4]

            @torch.jit.script_method
            def forward(self):
                sum = 0
                for i in self.b:
                    sum += i
                return sum

        m = M()
        self.assertEqual(m(), 10)

    def test_script_module_for2(self):
        class Sub(torch.jit.ScriptModule):
            def __init__(self):
                super(Sub, self).__init__(False)
                self.weight = nn.Parameter(torch.randn(2))

            @torch.jit.script_method
            def forward(self, thing):
                return self.weight + thing

        class M(torch.jit.ScriptModule):
            __constants__ = ['mods']

            def __init__(self):
                super(M, self).__init__(False)
                self.mods = nn.ModuleList([Sub() for i in range(10)])

            @torch.jit.script_method
            def forward(self, v):
                for m in self.mods:
                    v = m(v)
                return v

        i = torch.Tensor(2)
        m = M()
        o = m(i)
        v = i
        for sub in m.mods:
            v = sub(v)
        self.assertEqual(o, v)

    def test_script_module_const_submodule_fail(self):
        class Sub(torch.jit.ScriptModule):
            def __init__(self):
                super(Sub, self).__init__(False)
                self.weight = nn.Parameter(torch.randn(2))

            @torch.jit.script_method
            def forward(self, thing):
                return self.weight + thing

        class M(torch.jit.ScriptModule):
            def __init__(self):
                super(M, self).__init__(False)
                self.mods = [Sub() for _ in range(10)]

            @torch.jit.script_method
            def forward(self):
                for _ in self.mods:
                    print(1)
                return 4

        with self.assertRaisesRegex(RuntimeError, "did you forget to add it __constants__"):
            M()

    def test_script_module_not_tuple(self):
        class M(torch.jit.ScriptModule):
            __constants__ = ['mods']

            def __init__(self):
                super(M, self).__init__(False)
                self.mods = 1

            @torch.jit.script_method
            def forward(self, v):
                for m in self.mods:
                    print(m)
                return v
        with self.assertRaisesRegex(RuntimeError, "cannot be used as a tuple"):
            M()

    def test_script_sequential_for(self):
        class Sub(torch.jit.ScriptModule):
            def __init__(self):
                super(Sub, self).__init__(False)
                self.weight = nn.Parameter(torch.randn(2))

            @torch.jit.script_method
            def forward(self, thing):
                return self.weight + thing

        class M(torch.jit.ScriptModule):
            __constants__ = ['mods']

            def __init__(self):
                super(M, self).__init__(False)
                self.mods = nn.Sequential(Sub(), Sub(), Sub())

            @torch.jit.script_method
            def forward(self, v):
                for m in self.mods:
                    v = m(v)
                return v

            @torch.jit.script_method
            def forward2(self, v):
                return self.mods(v)

        i = torch.Tensor(2)
        m = M()
        o = m(i)
        v = i
        for sub in m.mods:
            v = sub(v)
        self.assertEqual(o, v)

        o2 = m.forward2(i)
        self.assertEqual(o2, v)

    def test_script_sequential_multi_output_fail(self):
        class Sub(torch.jit.ScriptModule):
            def __init__(self):
                super(Sub, self).__init__(False)
                self.weight = nn.Parameter(torch.randn(2))

            @torch.jit.script_method
            def forward(self, thing):
                return self.weight + thing

        class ReturnMulti(torch.jit.ScriptModule):
            def __init__(self):
                super(ReturnMulti, self).__init__(False)

            @torch.jit.script_method
            def forward(self, x):
                return x, x, x

        class HaveSequential(torch.jit.ScriptModule):
            __constants__ = ['someseq']

            def __init__(self):
                super(HaveSequential, self).__init__(False)
                self.someseq = nn.Sequential(
                    Sub(),
                    ReturnMulti(),
                    Sub()
                )

            @torch.jit.script_method
            def forward(self, x):
                return self.someseq(x)

        with self.assertRaisesRegex(RuntimeError, "(Tensor, Tensor, Tensor)"):
            hs = HaveSequential()
            i = torch.Tensor(2)
            hs(i)

    def test_constant_as_attr(self):
        class M(torch.jit.ScriptModule):
            __constants__ = ['dim']

            def __init__(self):
                super(M, self).__init__(False)
                self.dim = 1

            @torch.jit.script_method
            def forward(self, v):
                return torch.cat([v, v, v], dim=self.dim)
        v = torch.zeros(1, 1)
        self.assertEqual(torch.cat([v, v, v], dim=1), M()(v))

    class StarTestSumStarred(torch.nn.Module):
        def __init__(self):
            super(TestScript.StarTestSumStarred, self).__init__()

        def forward(self, *inputs):
            output = inputs[0]
            for i in range(1, len(inputs)):
                output += inputs[i]
            return output

    class StarTestReturnThree(torch.nn.Module):
        def __init__(self):
            super(TestScript.StarTestReturnThree, self).__init__()

        def forward(self, rep):
            return rep, rep, rep

    def test_script_star_expr(self):

        class M2(torch.jit.ScriptModule):
            def __init__(self):
                super(M2, self).__init__(True)
                self.m = torch.jit.trace(TestScript.StarTestSumStarred(),
                                         (torch.ones(4, 3), torch.ones(4, 3), torch.ones(4, 3)))
                self.g = torch.jit.trace(TestScript.StarTestReturnThree(), torch.ones(4, 3))

            @torch.jit.script_method
            def forward(self, rep):
                tup = self.g(rep)
                return self.m(*tup)

        m = M2()
        self.assertEqual(m(torch.zeros(4, 3)), 3 * torch.zeros(4, 3))

    def test_script_star_expr_string(self):
        class M2(torch.jit.ScriptModule):
            def __init__(self):
                super(M2, self).__init__(True)
                self.m = torch.jit.trace(TestScript.StarTestSumStarred(),
                                         (torch.ones(4, 3), torch.ones(4, 3), torch.ones(4, 3)))
                self.g = torch.jit.trace(TestScript.StarTestReturnThree(), torch.ones(4, 3))

                self.define('''
            def forward(self, rep):
                tup = self.g(rep)
                return self.m(*tup)
                ''')

        m = M2()
        self.assertEqual(m(torch.zeros(4, 3)), 3 * torch.zeros(4, 3))

    class StarTestSumAndReturnThree(torch.nn.Module):
        def __init__(self):
            super(TestScript.StarTestSumAndReturnThree, self).__init__()

        def forward(self, *inputs):
            output = inputs[0]
            for i in range(1, len(inputs)):
                output += inputs[i]
            return output, output, output

    def test_script_star_assign(self):
        class M2(torch.jit.ScriptModule):
            def __init__(self):
                super(M2, self).__init__(True)
                self.g = torch.jit.trace(TestScript.StarTestSumAndReturnThree(), torch.ones(4, 3))
                self.define('''
            def forward(self, rep):
                head, *tail = self.g(rep)
                return head
                ''')

        m = M2()
        self.assertEqual(m(torch.zeros(4, 3)), 3 * torch.zeros(4, 3))

    def test_script_module_star_assign2(self):
        class M2(torch.jit.ScriptModule):
            def __init__(self):
                super(M2, self).__init__(True)
                self.g = torch.jit.trace(
                    TestScript.StarTestSumAndReturnThree(),
                    (torch.ones(4, 3), torch.ones(4, 3), torch.ones(4, 3)))
                self.define('''
            def forward(self, rep):
                *head, tail = self.g(rep, rep, rep)
                return tail
                ''')

        m = M2()
        self.assertEqual(m(torch.ones(4, 3)), 3 * torch.ones(4, 3))

    def test_script_module_star_assign_fail_pythonop(self):

        with self.assertRaisesRegex(RuntimeError, "cannot be used as a tuple"):
            class M2(torch.jit.ScriptModule):
                def __init__(self):
                    super(M2, self).__init__(True)

                    def myfunc():
                        return torch.zeros(1, 2, 3), torch.zeros(1, 2, 3)

                    self.define('''
                def forward(self, rep):
                    a, *b = myfunc()
                    return a
                    ''')

            m = M2()
            m(torch.zeros(4, 3))

    def test_script_module_star_assign_fail_builtin(self):
        with self.assertRaisesRegex(RuntimeError, "cannot be used as a tuple"):
            class M2(torch.jit.ScriptModule):
                def __init__(self):
                    super(M2, self).__init__(True)

                    self.define('''
                def forward(self, rep):
                    a, *b = torch.neg(rep)
                    return a
                    ''')

            m = M2()
            m(torch.zeros(4, 3))

    def test_pack_padded_pad_packed_trace(self):
        from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence
        T, B, C = 3, 5, 7

        class PadPackedWrapper(torch.nn.Module):
            def __init__(self):
                super(PadPackedWrapper, self).__init__()

            def forward(self, x, seq_lens):
                x = pack_padded_sequence(x, seq_lens)
                x, _ = pad_packed_sequence(x)
                return x

        x = np.ones((T, B, C))
        seq_lens = np.array([3, 3, 2, 2, 1], dtype=np.int32)
        # set padding value so we can test equivalence
        for b in range(B):
            if seq_lens[b] < T:
                x[seq_lens[b]:, b, :] = 0
        seq_lens = torch.from_numpy(seq_lens)
        x = torch.autograd.Variable(torch.from_numpy(x), requires_grad=True)

        m = PadPackedWrapper()
        m_traced = torch.jit.trace(m, (x, seq_lens,))

        y = m(x, seq_lens)
        loss = torch.sum(y)
        loss.backward()
        grad = x.grad.clone()
        x.grad.zero_()

        y_traced = m_traced(x, seq_lens)
        loss_traced = torch.sum(y_traced)
        loss_traced.backward()
        grad_traced = x.grad.clone()

        self.assertEqual(y_traced, x)
        self.assertEqual(y_traced, y)
        self.assertEqual(grad, grad_traced)

        f = io.BytesIO()
        torch.onnx._export(m, (x, seq_lens), f, verbose=False)

    def test_script_outputs(self):
        with self.assertRaisesRegex(RuntimeError, "cannot be used as a tuple"):
            @torch.jit.script
            def foo(a):
                c, d = a + a
                return c + d

        @torch.jit.script
        def return3():
            return 1, 2, 3

        with self.assertRaisesRegex(RuntimeError, "too many values to unpack"):
            @torch.jit.script
            def bind2():
                a, b = return3()
                print(a)
                print(b)

    def test_script_chunk(self):
        @torch.jit.script
        def foo(a):
            b, c = torch.chunk(a, dim=0, chunks=2)
            return b
        v = torch.rand(10, 3)
        self.assertEqual(torch.chunk(v, dim=0, chunks=2)[0], foo(v))

    def test_rnn_trace_override(self):
        from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence
        num_layers = 3
        T, B, C = 11, 5, 7

        class RNNTraceWrapper(torch.nn.Module):
            def __init__(self, cell_type):
                super(RNNTraceWrapper, self).__init__()
                if cell_type == 'RNN':
                    self.rnn = torch.nn.RNN(input_size=C, hidden_size=C, num_layers=num_layers)
                elif cell_type == 'LSTM':
                    self.rnn = torch.nn.LSTM(input_size=C, hidden_size=C, num_layers=num_layers)
                elif cell_type == 'GRU':
                    self.rnn = torch.nn.GRU(input_size=C, hidden_size=C, num_layers=num_layers)

            def forward(self, x, seq_lens):
                x = pack_padded_sequence(x, seq_lens)
                x, _ = self.rnn(x)
                x, _ = pad_packed_sequence(x)
                return x

        for cell_type in ['RNN', 'LSTM', 'GRU']:
            x = torch.ones(T, B, C, requires_grad=True)
            seq_lens = torch.from_numpy(np.array([11, 3, 2, 2, 1], dtype=np.int32))

            m = RNNTraceWrapper(cell_type)
            m_traced = torch.jit.trace(m, (x, seq_lens,))

            y = m(x, seq_lens)
            loss = torch.sum(y)
            loss.backward()
            grad = x.grad.clone()
            x.grad.zero_()

            y_traced = m_traced(x, seq_lens)
            loss_traced = torch.sum(y_traced)
            loss_traced.backward()
            grad_traced = x.grad.clone()

            self.assertEqual(y_traced, y)
            self.assertEqual(grad, grad_traced)

            f = io.BytesIO()
            torch.onnx._export(m, (x, seq_lens), f, verbose=False)

    def test_python_call_non_tensor(self):
        def foo(a, b, c):
            # type: (Tensor, int, Tuple[Tensor, int]) -> Tuple[int, Tensor]
            d, e = c
            return b + e, a + d

        @torch.jit.script
        def bar():
            x = torch.ones(3, 4)
            a, b = foo(x, 3, (x, 3))
            return a, b

        self.assertEqual((6, torch.ones(3, 4) + 1), bar())

    def test_python_call_non_tensor_wrong(self):
        with self.assertRaisesRegex(RuntimeError, r"but instead got value of type tuple"):
            def foo():
                # type: () -> Tensor
                return ((3, 4),)

            @torch.jit.script
            def bar():
                return foo()

            bar()

    def test_tuples(self):
        @torch.jit.script
        def foo(i):
            a = (i + 4, i * 2)
            c = a
            # some nonsense with if-statements and loops to check
            # that tuple lowering doesn't fail
            if True:
                c = (i * 9, i + 1)
            t0, t1 = c
            while False:
                t0, t1 = c
                c = (t1, t0)
            return t0

        v = torch.rand(10, 3)
        self.assertEqual(v * 9, foo(v))

        with self.assertRaisesRegex(RuntimeError, r"variable 'a' previously has type \(Tensor, Tensor\)"):
            @torch.jit.script
            def mixtypes(x):
                a = (x, x)
                if True:
                    a = 4

    def test_if_tuple_sizes(self):
        with self.assertRaisesRegex(RuntimeError, "Type mismatch"):
            @torch.jit.script
            def diff_tuple_sizes(x):
                if False:
                    c0 = ((x, x), (x, x, x))
                else:
                    c0 = ((x, x, x), (x, x))
                return c0

    def test_if_different_type(self):
        with self.assertRaisesRegex(RuntimeError, "Type mismatch: c0 is set to type int "
                                    "in the true branch and type float in the false branch:"):
            @torch.jit.script
            def diff_type_used():
                if False:
                    c0 = 1
                else:
                    c0 = 1.0
                return c0

        with self.assertRaisesRegex(RuntimeError, "variable 'c0' previously has type float"):
            @torch.jit.script
            def diff_existing_type(x):
                c0 = 1.0
                if False:
                    c0 = 1
                    print(x)
                return x

        @torch.jit.script
        def diff_type_unused():
            if True:
                c0 = 1
                print(c0)
            else:
                c0 = 1.0
                print(c0)
            return 1

    def test_if_list(self):
        # testing that different length lists don't throw error
        @torch.jit.script
        def test_list(x):
            if True:
                c = [x, x]
            else:
                c = [x, x, x]
            return torch.cat(c)

        b = torch.zeros(2, 4)
        test_list.graph.propagate_shapes((b,), False)
        self.assertExpected(canonical(test_list.graph))

    def test_if_supertype(self):
        @torch.jit.script
        def tensor_unifying(x, y, z):

            # testing dynamic is appropriately set for y and z
            if True:
                x, y, z = x, y, z
            else:
                x, y, z = x, x, y

            return x, y, z

        a = torch.zeros(2, 2, dtype=torch.float)
        b = torch.zeros(2, 4, dtype=torch.long)
        c = torch.zeros(2, 4, dtype=torch.float)

        tensor_unifying.graph.propagate_shapes((a, b, c), False)
        self.assertExpected(canonical(tensor_unifying.graph))

    def test_type_annotations(self):
        def fn(x, y):
            # type: (Tensor, Tensor) -> Tuple[Tensor, Tensor, Tensor]
            return x, x * 2, x * 3

        with self.assertRaisesRegex(RuntimeError, r"need 4 values .* found only 3"):
            @torch.jit.script
            def script_fn(x):
                x, y, z, w = fn(x, x)

        with self.assertRaisesRegex(RuntimeError, r"too many values .* need 2 but found 3"):
            @torch.jit.script
            def script_fn2(x):
                x, y = fn(x, x)

        def fn_unpack(x):
            y, z, w = fn(x, x)
            return y

        def fn_index(x):
            q = fn(x, x)
            return x

        x = torch.ones(2, 2)
        self.checkScript(fn_unpack, (x,), optimize=True)
        self.checkScript(fn_index, (x,), optimize=True)

    def test_type_annotations_varargs(self):
        def fn_varargs(x, *args):
            return args[0] if args else x

        def fn1(x, y, z):
            return fn_varargs(x)

        def fn2(x, y, z):
            return fn_varargs(x, y)

        def fn3(x, y, z):
            return fn_varargs(x, y, z)

        x, y, z = [torch.randn(2, 2) for _ in range(3)]
        self.checkScript(fn1, (x, y, z), optimize=True)
        self.checkScript(fn2, (x, y, z), optimize=True)
        self.checkScript(fn3, (x, y, z), optimize=True)

    @unittest.skipIf(not PY35, "Python 3.5 needed")
    def test_type_annotation_py3(self):
        import importlib.util

        code = dedent("""
        import torch
        from torch import Tensor
        from typing import Tuple

        def fn(x : torch.Tensor, y : Tensor, z) -> Tuple[Tensor, Tensor, Tensor]:
            return (x, y + z, z)
        """)

        with tempfile.TemporaryDirectory() as tmp_dir:
            script_path = os.path.join(tmp_dir, 'script.py')
            with open(script_path, 'w') as f:
                f.write(code)
            fn = get_fn('test_type_annotation_py3', script_path)

            with self.assertRaisesRegex(RuntimeError, r"expected a value of type Tensor for argument"
                                                      r" '0' but found \(Tensor, Tensor\)"):
                @torch.jit.script
                def bad_fn(x):
                    x, y = fn((x, x), x, x)
                    return y

            with self.assertRaisesRegex(RuntimeError, r"too many values .* need 2 but found 3"):
                @torch.jit.script
                def bad_fn2(x):
                    x, y = fn(x, x, x)
                    return y

            with self.assertRaisesRegex(RuntimeError, r"need 4 values .* found only 3"):
                @torch.jit.script
                def bad_fn3(x):
                    x, y, z, w = fn(x, x, x)
                    return y

            def good_fn(x):
                y, z, w = fn(x, x, x)
                return y, z, w

            self.checkScript(good_fn, (torch.ones(2, 2),), optimize=True)

    def test_type_annotation_module(self):
        class BaseModule(torch.jit.ScriptModule):
            def foo(self, x):
                # type: (Tensor) -> Tensor
                return x + 1

            def bar(self, x, y):
                # type: (Tensor, Tensor) -> Tuple[Tensor, Tensor]
                return x + y, y

            def baz(self, x, y):
                return x

        class ModuleTooMany(BaseModule):
            @torch.jit.script_method
            def method(self, x):
                return self.foo(x, x)

        class ModuleTooFew(BaseModule):
            @torch.jit.script_method
            def method(self, x):
                return self.bar(x)

        class ModuleTooManyAssign(BaseModule):
            @torch.jit.script_method
            def method(self, x):
                y, z, w = self.bar(x, x)
                return x

        class ModuleDefault(BaseModule):
            @torch.jit.script_method
            def method(self, x):
                y = self.baz(x)
                return x

        with self.assertRaisesRegex(RuntimeError, "expected at most 1 arguments but found 2"):
            ModuleTooMany()
        with self.assertRaisesRegex(RuntimeError, "argument 1 not provided"):
            ModuleTooFew()
        with self.assertRaisesRegex(RuntimeError, "need 3 values .* found only 2"):
            ModuleTooManyAssign()
        with self.assertRaisesRegex(RuntimeError, "argument 1 not provided."):
            ModuleDefault()

    def test_script_define_order(self):
        class M(torch.jit.ScriptModule):
            def __init__(self):
                pass

            @torch.jit.script_method
            def call_foo(self, input):
                return self.foo(input)

            @torch.jit.script_method
            def foo(self, input):
                return input + 1
        m = M()
        self.assertEqual(2, m.call_foo(torch.ones((), dtype=torch.int64)))

    def test_script_define_order_recursive_fail(self):
        class M(torch.jit.ScriptModule):
            def __init__(self):
                pass

            @torch.jit.script_method
            def call_foo(self, input):
                return self.foo(input)

            @torch.jit.script_method
            def foo(self, input):
                self.call_foo(input)

        with self.assertRaisesRegex(RuntimeError, 'called recursively involving'):
            M()

    def test_script_kwargs_fn_call(self):
        class M(torch.jit.ScriptModule):
            def __init__(self):
                pass

            @torch.jit.script_method
            def call_foo(self, input):
                return self.foo(input=input, bar=1)

            @torch.jit.script_method
            def foo(self, bar, input):
                # type: (int, Tensor) -> Tensor
                return input + bar
        m = M()
        self.assertEqual(2, m.call_foo(torch.ones((), dtype=torch.int64)))

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    def test_trace_of_script(self):
        @torch.jit.script
        def foo(a, c):
            b = 0.0
            if bool(a == 0.0):
                b = 1.0
            return b + c

        a = torch.ones(1, dtype=torch.float)

        @_trace(torch.zeros(1, dtype=torch.float))
        def use(b):
            return foo(b - 1.0, a) + 1.0

        # test we propagated shapes through the function
        self.assertTrue("Dynamic" not in str(use.graph))

        self.assertEqual(3, use(torch.ones(1, dtype=torch.float)))
        self.assertEqual(2, use(torch.zeros(1, dtype=torch.float)))

    def test_if_define(self):
        @torch.jit.script
        def foo(a):
            if bool(a == 0):
                b = 1
            else:
                b = 0
            return b + 1

        @torch.jit.script
        def foo2(a):
            b = 0
            if bool(a == 0):
                b = 1
            return b + 1

        @torch.jit.script
        def foo3(a):
            b = 1
            if bool(a == 0):
                c = 4
            else:
                b = 0
            return b + 1

        a = torch.ones(1, dtype=torch.long)
        b = torch.zeros(1, dtype=torch.long)
        self.assertEqual(1, foo(a))
        self.assertEqual(2, foo(b))
        self.assertEqual(1, foo2(a))
        self.assertEqual(2, foo2(b))
        self.assertEqual(1, foo3(a))
        self.assertEqual(2, foo3(b))

    def test_script_module_export_submodule(self):
        class M1(torch.jit.ScriptModule):
            def __init__(self):
                super(M1, self).__init__(False)
                self.weight = nn.Parameter(torch.randn(2))

            @torch.jit.script_method
            def forward(self, thing):
                return self.weight + thing

        class M2(torch.jit.ScriptModule):
            def __init__(self):
                super(M2, self).__init__(False)
                # test submodule
                self.sub = M1()
                self.weight = nn.Parameter(torch.randn(2, 3))
                self.bias = nn.Parameter(torch.randn(2))
                self.define("""
                    def hi(self, a):
                        return self.weight.mm(a)
                """)

            @torch.jit.script_method
            def doit(self, input):
                return self.weight.mm(input)

            @torch.jit.script_method
            def doit2(self, input):
                return self.weight.mm(input)

            @torch.jit.script_method
            def doit3(self, input):
                return input + torch.ones([1], dtype=torch.double)

            @torch.jit.script_method
            def forward(self, input):
                a = self.doit(input)
                b = self.doit2(input)
                c = self.hi(input)
                return a + b + self.bias + c

        m_orig = M2()
        m_import = self.getExportImportCopy(m_orig)

        input = torch.randn(3, 2)
        self.assertEqual(m_orig.doit(input), m_import.doit(input))
        self.assertEqual(m_orig.hi(input), m_import.hi(input))
        self.assertEqual(m_orig.doit3(input), m_import.doit3(input))
        self.assertEqual(m_orig.forward(input), m_import.forward(input))

    @skipIfNoTorchVision
    def test_script_module_export_resnet18(self):
        x = torch.ones(1, 3, 224, 224)
        m_orig = torch.jit.trace(torchvision.models.resnet18(), torch.ones(1, 3, 224, 224))
        m_import = self.getExportImportCopy(m_orig)

        input = torch.randn(1, 3, 224, 224, requires_grad=True)
        output_orig = m_orig(input)
        output_orig.sum().backward()
        grad_orig = input.grad.clone()
        input.grad.zero_()

        output_import = m_import(input)
        output_import.sum().backward()
        grad_import = input.grad.clone()

        self.assertEqual(output_orig, output_import)
        self.assertEqual(grad_orig, grad_import)

    def test_script_module_export_tensor_type(self):
        class M(torch.jit.ScriptModule):

            def __init__(self, type):
                super(M, self).__init__(False)
                self.param = torch.nn.Parameter(torch.zeros((5, 5), dtype=type).random_())

            @torch.jit.script_method
            def foo(self):
                return self.param

        for type in [torch.float, torch.double]:
            m_orig = M(type)
            m_import = self.getExportImportCopy(m_orig)
            self.assertEqual(m_orig.foo(), m_import.foo())
            self.assertTrue(m_orig.foo().dtype == m_import.foo().dtype)

    @unittest.skipIf(not RUN_CUDA, "testing cuda tensors require CUDA")
    def test_script_module_export_tensor_cuda(self):
        class M(torch.jit.ScriptModule):

            def __init__(self):
                super(M, self).__init__(False)
                self.param = torch.nn.Parameter(torch.zeros((5, 5), device='cuda').random_())

            @torch.jit.script_method
            def foo(self):
                return self.param

        m_orig = M()
        m_import = self.getExportImportCopy(m_orig)
        self.assertTrue(m_import.foo().device == torch.device('cpu'))
        self.assertEqual(m_orig.foo(), m_import.foo())
        self.assertTrue(m_orig.foo().dtype == m_import.foo().dtype)

    def test_script_module_export_shared_storage(self):
        class M(torch.jit.ScriptModule):

            def __init__(self):
                super(M, self).__init__(False)
                self.param1 = torch.nn.Parameter(torch.rand(5, 5))
                self.param2 = torch.nn.Parameter(self.param1[3])
                self.param3 = torch.nn.Parameter(torch.rand(5, 5))
                self.param4 = torch.nn.Parameter(torch.rand(11, 5)[1:6])

            @torch.jit.script_method
            def foo(self):
                return self.param1 + self.param2 + self.param3 + self.param4

        m_orig = M()
        m_import = self.getExportImportCopy(m_orig)

        self.assertEqual(m_orig.foo(), m_import.foo())
        self.assertTrue(m_import.param1.storage().data_ptr() == m_import.param2.storage().data_ptr())
        self.assertTrue(m_import.param1.storage().data_ptr() != m_import.param3.storage().data_ptr())

    def test_onnx_export_script_module(self):
        class ModuleToExport(torch.jit.ScriptModule):
            def __init__(self):
                super(ModuleToExport, self).__init__()

            @torch.jit.script_method
            def forward(self, x):
                y = x - x
                return x + x

        mte = ModuleToExport()
        outputs = mte(torch.zeros(1, 2, 3))
        self.assertExpected(torch.onnx.export_to_pretty_string(
            mte, (torch.zeros(1, 2, 3),), None, verbose=False,
            example_outputs=outputs))

    def test_onnx_export_script_python_fail(self):
        class ModuleToInline(torch.jit.ScriptModule):
            def __init__(self):
                super(ModuleToInline, self).__init__()

            def forward(self, x):
                return torch.neg(x)

        class ModuleToExport(torch.jit.ScriptModule):
            def __init__(self):
                super(ModuleToExport, self).__init__()
                self.mod = ModuleToInline()

            @torch.jit.script_method
            def forward(self, x):
                y = self.mod(x)
                return y + y

        mte = ModuleToExport()
        outputs = mte(torch.zeros(1, 2, 3))
        f = io.BytesIO()
        with self.assertRaisesRegex(RuntimeError, "Couldn't export Python operator"):
            torch.onnx._export(mte, (torch.zeros(1, 2, 3),), f, verbose=False,
                               example_outputs=outputs)

    def test_onnx_export_script_inline_trace(self):
        class ModuleToInline(torch.jit.ScriptModule):
            def __init__(self):
                super(ModuleToInline, self).__init__()

            def forward(self, x):
                return torch.neg(x)

        class ModuleToExport(torch.jit.ScriptModule):
            def __init__(self):
                super(ModuleToExport, self).__init__()
                self.mod = torch.jit.trace(ModuleToInline(), torch.zeros(1, 2, 3))

            @torch.jit.script_method
            def forward(self, x):
                y = self.mod(x)
                return y + y

        mte = ModuleToExport()
        outputs = mte(torch.zeros(1, 2, 3))
        self.assertExpected(torch.onnx.export_to_pretty_string(
            mte, (torch.zeros(1, 2, 3),), None, verbose=False,
            example_outputs=outputs))

    def test_onnx_export_script_inline_script(self):
        class ModuleToInline(torch.jit.ScriptModule):
            def __init__(self):
                super(ModuleToInline, self).__init__()

            @torch.jit.script_method
            def forward(self, x):
                return torch.neg(x)

        class ModuleToExport(torch.jit.ScriptModule):
            def __init__(self):
                super(ModuleToExport, self).__init__()
                self.mod = ModuleToInline()

            @torch.jit.script_method
            def forward(self, x):
                y = self.mod(x)
                return y + y

        mte = ModuleToExport()
        outputs = mte(torch.zeros(1, 2, 3))
        self.assertExpected(torch.onnx.export_to_pretty_string(
            mte, (torch.zeros(1, 2, 3),), None, verbose=False,
            example_outputs=outputs))

    def test_onnx_export_script_module_loop(self):
        class ModuleToExport(torch.jit.ScriptModule):
            def __init__(self):
                super(ModuleToExport, self).__init__()

            @torch.jit.script_method
            def forward(self, x):
                for _ in range(100):
                    x = x + x
                return x

        mte = ModuleToExport()
        outputs = mte(torch.zeros(1, 2, 3))
        self.assertExpected(torch.onnx.export_to_pretty_string(
            mte, (torch.zeros(1, 2, 3),), None, verbose=False,
            example_outputs=outputs))

    def test_onnx_export_script_truediv(self):
        class ModuleToExport(torch.jit.ScriptModule):
            def __init__(self):
                super(ModuleToExport, self).__init__()

            @torch.jit.script_method
            def forward(self, x):
                z = x.size(0) / 2
                return x + z

        mte = ModuleToExport()
        outputs = mte(torch.zeros(1, 2, 3))
        self.assertExpected(torch.onnx.export_to_pretty_string(
            mte, (torch.zeros(1, 2, 3),), None, verbose=False,
            example_outputs=outputs))

    def test_onnx_raw_export_script_truediv(self):
        class ModuleToExport(torch.jit.ScriptModule):
            def __init__(self):
                super(ModuleToExport, self).__init__()

            @torch.jit.script_method
            def forward(self, x):
                z = x.size(0) / 2
                return x + z

        mte = ModuleToExport()
        outputs = mte(torch.zeros(1, 2, 3))
        self.assertExpected(torch.onnx.export_to_pretty_string(
            mte, (torch.zeros(1, 2, 3),), None, verbose=False,
            example_outputs=outputs, export_raw_ir=True))

    def test_onnx_export_script_module_if(self):
        class ModuleToExport(torch.jit.ScriptModule):
            def __init__(self):
                super(ModuleToExport, self).__init__()

            @torch.jit.script_method
            def forward(self, x):
                if bool(torch.sum(x) > 0):
                    x = torch.neg(x)
                return x

        mte = ModuleToExport()
        outputs = mte(torch.zeros(1, 2, 3, dtype=torch.long))
        self.assertExpected(torch.onnx.export_to_pretty_string(
            mte, (torch.zeros(1, 2, 3),), None, verbose=False,
            example_outputs=outputs))

    def test_onnx_export_script_inline_params(self):
        class ModuleToInline(torch.jit.ScriptModule):
            def __init__(self):
                super(ModuleToInline, self).__init__()
                self.m = torch.nn.Parameter(torch.ones(3, 3))
                self.unused = torch.nn.Parameter(torch.ones(1, 2, 3))

            @torch.jit.script_method
            def forward(self, x):
                return torch.mm(x, self.m)

        class ModuleToExport(torch.jit.ScriptModule):
            def __init__(self):
                super(ModuleToExport, self).__init__()
                self.mod = ModuleToInline()
                self.param = torch.nn.Parameter(torch.ones(3, 4))

            @torch.jit.script_method
            def forward(self, x):
                y = self.mod(x)
                return torch.mm(y, self.param)

        mte = ModuleToExport()
        result = mte(torch.zeros(2, 3))
        reference = torch.mm(torch.mm(torch.zeros(2, 3), torch.ones(3, 3)), torch.ones(3, 4))
        self.assertEqual(result, reference)
        self.assertExpected(torch.onnx.export_to_pretty_string(
            mte, (torch.ones(2, 3),), None, verbose=False,
            example_outputs=result, propagate=True))

    def test_trace_with_size(self):
        @_trace(torch.zeros(1, 1))
        def foo(x):
            return x + 1

        @torch.jit.script
        def bar(x):
            y = int(foo(x))
            if True:
                y = 7
            return y + 1

        self.assertEqual(8, bar(torch.ones(1, 1)))

    def test_tracing_slicing(self):
        @_trace(torch.zeros(10))
        def foo_trace(x):
            return x[-5:-3]

        @torch.jit.script
        def foo_script(x):
            return x[-5:-3]

        def foo(x):
            return x[-5:-3]

        a = torch.arange(0, 8)
        b = torch.arange(0, 20)
        self.assertEqual(foo_trace(a), foo_script(a))
        self.assertEqual(foo_trace(a), foo(a))
        self.assertNotEqual(foo_trace(a), foo_trace(b))

    def test_tracing_indexing(self):
        @_trace(torch.zeros(10))
        def foo_trace(x):
            return x[-2]

        @torch.jit.script
        def foo_script(x):
            return x[-2]

        def foo(x):
            return x[-2]

        a = torch.arange(0, 8)
        b = torch.arange(0, 20)
        self.assertEqual(foo_script(a), foo_trace(a))
        self.assertEqual(foo_trace(a), foo(a))
        self.assertNotEqual(foo_trace(a), foo_trace(b))

    def test_index_select_shape_prop(self):

        @torch.jit.script
        def foo(x, y):
            return torch.index_select(x, index=y, dim=1)

        a = torch.zeros(2, 2)
        b = torch.zeros(4, dtype=torch.long)
        torch._C._jit_pass_complete_shape_analysis(foo.graph, (a, b), False)
        self.assertExpected(canonical(foo.graph))

    def test_onnx_export_speculate(self):

        class Foo(torch.jit.ScriptModule):
            def __init__(self, m):
                super(Foo, self).__init__()
                self.m = m

            @torch.jit.script_method
            def forward(self, x):
                x += x
                # because we are testing if we emit `if` statement correctly
                # we cannot use `True` as the condition. Constant prop
                # would remove the `if` statements.
                c = sum(x) > 4
                if bool(c):
                    if bool(c):
                        y = self.m(x)
                    else:
                        y = self.m(x)
                else:
                    y = self.m(x)
                return y

        linear = torch.jit.trace(nn.Linear(10, 20).float(), torch.zeros(1, 10, dtype=torch.float))

        @torch.jit.script
        def transpose(x):
            return x.t()

        f1 = Foo(transpose)
        outputs_f1 = f1(torch.ones(1, 10, dtype=torch.float))
        f2 = Foo(linear)
        outputs_f2 = f2(torch.ones(1, 10, dtype=torch.float))

        onnx_ish = torch.onnx.export_to_pretty_string(
            f1,
            (torch.ones(1, 10, dtype=torch.float), ),
            None, verbose=False, example_outputs=outputs_f1)
        self.assertExpected(onnx_ish, subname='f1')
        onnx_ish = torch.onnx.export_to_pretty_string(
            f2,
            (torch.ones(1, 10, dtype=torch.float), ),
            None, verbose=False, example_outputs=outputs_f2)
        self.assertExpected(onnx_ish, subname='f2')

    def test_onnx_export_shape_reshape(self):
        class Foo(torch.nn.Module):
            def forward(self, x):
                import torch.onnx.operators
                x = x.repeat(5, 1, 1)
                shape = torch.onnx.operators.shape_as_tensor(x)
                reshaped = torch.onnx.operators.reshape_from_tensor_shape(x, shape)
                return reshaped

        foo = torch.jit.trace(Foo(), torch.zeros(1, 2, 3))
        outputs = foo(torch.zeros(1, 2, 3))
        f = io.BytesIO()
        s = torch.onnx.export_to_pretty_string(foo, (torch.zeros(1, 2, 3)), f,
                                               example_outputs=outputs)
        self.assertExpected(s)

    def test_shape_analysis_loop(self):
        def foo(a, b, x):
            c = a
            # on the first iteration of the loop it appears that
            # c should have a expand to the size of b
            # but on the second+ iterations, there is no broadcast and the
            # sizes are different.
            # previously this would cause the compiler to (1) enter an infinite
            # loop trying to compute the shape, and (2) insert invalid
            # broadcasts.
            # this test ensure we don't regress on these issues
            for _ in range(2):
                a = c + b
                c = x
                b = x
            return a

        self.checkScript(foo, (torch.zeros(1), torch.zeros(4), torch.zeros(5)), optimize=False)

    def test_intlist_args(self):
        def func_1(x):
            return torch.nn.functional.adaptive_avg_pool1d(x, 1)

        def func_2(x):
            return torch.nn.functional.adaptive_avg_pool1d(x, output_size=1)

        def func_3(x):
            return torch.nn.functional.adaptive_avg_pool1d(x, output_size=[1])

        x = torch.randn(8, 8, 8)
        self.checkScript(func_1, [x], optimize=True)
        self.checkScript(func_2, [x], optimize=True)
        self.checkScript(func_3, [x], optimize=True)

    def test_wrong_implicit_expand(self):

        @_trace(torch.zeros(3), torch.zeros(1))
        def foo(a, b):
            return a + b

        a = torch.rand(4)
        b = torch.rand(4)
        self.assertEqual(a + b, foo(a, b))

    def test_builtin_args_fails(self):

        with self.assertRaisesRegex(RuntimeError, 'expected at most'):
            @torch.jit.script
            def f0(a):
                torch.sum(a, a, a, a)

        with self.assertRaisesRegex(RuntimeError, 'argument self not provided'):
            @torch.jit.script
            def f1(a):
                torch.sum(foo=4)

        with self.assertRaisesRegex(RuntimeError, 'specified twice'):
            @torch.jit.script
            def f2(a):
                torch.sum(a, self=a)

        with self.assertRaisesRegex(RuntimeError, 'not provided'):
            @torch.jit.script
            def f3(a):
                torch.sum(dim=4)

        with self.assertRaisesRegex(RuntimeError, 'for argument \'tensors\' but found Tensor'):
            @torch.jit.script
            def f4(a):
                torch.cat(a)

        with self.assertRaisesRegex(RuntimeError, 'argument \'tensors\' but found int\[\]'):
            @torch.jit.script
            def f5(a):
                torch.cat([3])

        with self.assertRaisesRegex(RuntimeError, 'Lists must contain only a single type'):
            @torch.jit.script
            def f6(a):
                a.expand(size=[3, [4]])

        with self.assertRaisesRegex(RuntimeError, 'xpected a value of type Tensor for argument \'self\''):
            @torch.jit.script
            def f7(a):
                torch.sum([4])

    def test_builtin_args(self):

        def t0(a):
            # default arg dim
            return torch.cat([a, a])

        self.checkScript(t0, (torch.zeros(1, 1),))

        def t1(a):
            # keywords out of order
            return torch.cat(dim=1, tensors=[a, a])

        self.checkScript(t1, (torch.zeros(1, 1, 2),))

        def t2(a):
            # mix const/non-const attributes
            if True:
                b = 1
            else:
                b = 0
            return torch.sum(a, dim=b, keepdim=False)

        self.checkScript(t2, (torch.zeros(1, 1, 2),))

    def test_parser_type_annotations(self):
        cu = torch.jit.CompilationUnit('''
            def foo(x : Tensor, y : Tuple[Tuple[Tensor, Tensor], Tensor]) -> Tuple[Tensor, Tensor]:
                return x, x
        ''')

        self.assertExpected(cu.__getattr__('foo').pretty_print_schema())

    def test_parser_type_annotations_comment(self):
        cu = torch.jit.CompilationUnit('''
            def foo(x, y):
                # type: (Tensor, Tuple[Tuple[Tensor, Tensor], Tensor]) -> Tuple[Tensor, Tensor]
                return x, x
        ''')

        self.assertExpected(cu.__getattr__('foo').pretty_print_schema())

    def test_parser_type_annotations_unknown_type(self):
        with self.assertRaisesRegex(RuntimeError, r'Unknown type name Foo'):
            cu = torch.jit.CompilationUnit('''
                def foo(x : Tensor, y : Tuple[Tuple[Foo, Tensor], Tensor]) -> Tuple[Tensor, Tensor]:
                    return x, x
            ''')

    def test_parser_type_annotations_subscript_non_ident(self):
        with self.assertRaisesRegex(RuntimeError, r'Subscripted type must be a type identifier'):
            cu = torch.jit.CompilationUnit('''
                def foo(x : Tensor, y : Tuple[Tensor, Tensor][Tensor]) -> Tuple[Tensor, Tensor]:
                    return x, x
            ''')

    def test_parser_type_annotations_subscript_tensor(self):
        with self.assertRaisesRegex(RuntimeError, r'Unknown type constructor Tensor'):
            cu = torch.jit.CompilationUnit('''
                def foo(x : Tensor, y : Tensor[Tensor, Tensor]) -> Tuple[Tensor, Tensor]:
                    return x, x
            ''')

    def test_parser_type_annotations_incompatible_expression(self):
        with self.assertRaisesRegex(RuntimeError, r'Expression of type \+ cannot be used in a type expression'):
            cu = torch.jit.CompilationUnit('''
                def foo(x : Tensor, y : Tuple[3 + 4, Tensor]) -> Tuple[Tensor, Tensor]:
                    return x, x
            ''')

    def test_gather_dynamic_index(self):
        def t(x):
            gather1 = x[0]
            idx = 0 + 1
            gather2 = x[idx]
            return gather1 + gather2

        self.checkScript(t, (torch.zeros(3, 2, 3),))

    def test_slice_dynamic_index(self):
        def t(x):
            slice1 = x[0:1]
            zero = 0
            one = zero + 1
            slice2 = x[zero:one]
            return slice1 + slice2

        self.checkScript(t, (torch.zeros(3, 2, 3),))

    def test_addmm_grad(self):
        """ This test checks several things:
            1. An expand node was inserted before the addmm operating on the
               bias term.
            2. The fused form of addmm appears in the ultimate graph that's
               executed.
            3. A sum op was emitted for accumulating gradients along the 0th
               (expanded) dimension of the bias term.
            4. The correct symbolic representation for the backward pass of the
               mm operator was emitted (x.t() -> mm)

            TODO: we should actually check these conditions once we have a way
            to dump the GraphExecutor state. Namely the processed forward graph
            and the backward graph.
        """
        @torch.jit.script
        def addmm_grad_test(b, x, w):
            return torch.addmm(b, x, w)

        # Initialize param and input values
        w_init = torch.rand(2, 5)
        b_init = torch.rand(5)
        x = torch.rand(3, 2)

        # Clone trainable params
        b = b_init.clone()
        b.requires_grad_()
        w = w_init.clone()
        w.requires_grad_()

        # Test symbolic differentiation
        y = addmm_grad_test(b, x, w)
        y.sum().backward()

        # clone params for autograd reference
        b_ref = b_init.clone()
        b_ref.requires_grad_()
        w_ref = w_init.clone()
        w_ref.requires_grad_()
        y_ref = torch.addmm(b_ref, x, w_ref)
        y_ref.sum().backward()

        self.assertEqual(w.grad, w_ref.grad)
        self.assertEqual(b.grad, b_ref.grad)

    def test_zeros(self):
        class M(torch.jit.ScriptModule):
            __constants__ = ['d']

            def __init__(self):
                self.d = torch.device('cpu')

            @torch.jit.script_method
            def create(self):
                return torch.zeros([1, 1, 2], dtype=torch.float, device=self.d, layout=torch.strided)

        r = M().create()
        self.assertEqual(r.dtype, torch.float)
        self.assertEqual(torch.zeros([1, 1, 2], dtype=torch.float), r)

    def test_vararg_zeros(self):
        def foo():
            return torch.zeros(3, 4, 5, dtype=torch.int)

        self.checkScript(foo, ())

    @unittest.skipIf(IS_WINDOWS, "NYI: fuser support for Windows")
    def test_rand(self):

        def test_rand():
            a = torch.rand([3, 4])
            return a + 1.0 - a

        self.checkScript(test_rand, ())

    def test_erase_number_types(self):
        def func(a):
            b = 7 + 1 + 3
            c = a + b
            c += b
            return c

        graph = torch.jit.script(func).graph
        self.run_pass('erase_number_types', graph)
        self.assertExpectedGraph(graph)

    def test_loop_unrolling(self):
        def fn(x):
            y = 0
            for i in range(int(x)):
                y += i
            return y

        graph = torch.jit.script(fn).graph
        self.run_pass('loop_unrolling', graph)
        self.assertExpectedGraph(graph)
        self.checkScript(fn, (torch.tensor(10),))

    def test_loop_unrolling_const(self):
        def fn():
            y = 0
            for i in range(10):
                y += 1
            return y

        def fn2():
            y = 0
            for i in range(10):
                y += i
            return y

        def check(fn, name):
            graph = torch.jit.script(fn).graph
            self.run_pass('loop_unrolling', graph)
            self.assertExpectedGraph(graph, subname=name)
            self.checkScript(fn, ())

        check(fn, 'add_const')
        check(fn2, 'add_iter')

    def test_loop_unrolling_nested(self):
        def fn(x):
            y = 0
            for i in range(10):
                for j in range(int(x)):
                    y += j
            return y

        graph = torch.jit.script(fn).graph
        self.run_pass('loop_unrolling', graph)
        self.assertExpectedGraph(graph)
        self.checkScript(fn, (torch.tensor(10),))

    def test_loop_unroll_unused_counter(self):
        def fn(x):
            y = 0
            for i in range(int(x)):
                y += 1
            return y

        graph = torch.jit.script(fn).graph
        self.run_pass('loop_unrolling', graph)
        self.assertExpectedGraph(graph)

    def test_loop_unroll_negative(self):
        def fn(x):
            y = 0
            for i in range(int(x)):
                y += 1
            return y

        self.checkScript(fn, (torch.tensor(-20),))
        self.checkScript(fn, (torch.tensor(-2),))
        self.checkScript(fn, (torch.tensor(-1),))
        self.checkScript(fn, (torch.tensor(0),))
        self.checkScript(fn, (torch.tensor(1),))
        self.checkScript(fn, (torch.tensor(2),))

    def test_where(self):
        def fn(x, y):
            return torch.where(x > 0.0, x, y)

        self.checkScript(fn, (torch.randn(3, 2, dtype=torch.float), torch.ones(3, 2, dtype=torch.float)))

    def test_reassign_module_lhs(self):
        with self.assertRaisesRegex(RuntimeError, 'Cannot re-assign \'self\' because it has type value and self is'
                                    ' not a first-class value.  Only reassignments to first-class values are allowed'):
            class ReassignSelfLHS(torch.jit.ScriptModule):
                @torch.jit.script_method
                def forward(self, x):
                    for i in range(20):
                        self = x
                    return self

            ReassignSelfLHS()

    def test_reassign_module_rhs(self):
        with self.assertRaisesRegex(RuntimeError, 'Cannot re-assign \'x\' to a value of type module because x is not a'
                                    ' first-class value.  Only reassignments to first-class values are allowed'):
            class ReassignSelfRHS(torch.jit.ScriptModule):
                @torch.jit.script_method
                def forward(self, x):
                    for i in range(20):
                        x = self
                    return self

            ReassignSelfRHS()

    def test_unknown_builtin(self):
        with self.assertRaisesRegex(RuntimeError, 'unknown builtin op'):
            @torch.jit.script
            def unknown_builtin(x):
                return x.splork(3)

    def test_return_tuple(self):
        def return_tuple(x):
            a = (x, x)
            return a, x
        self.checkScript(return_tuple, (torch.rand(4),))

    def test_method_no_self(self):
        with self.assertRaisesRegex(RuntimeError, 'methods must have a self argument'):
            class MethodNoSelf(torch.jit.ScriptModule):
                @torch.jit.script_method
                def forward():
                    return torch.zeros(3, 4)

            MethodNoSelf()

    def test_return_stmt_not_at_end(self):
        with self.assertRaisesRegex(RuntimeError, 'return statements can appear only at the end of the function body'):
            @torch.jit.script
            def return_stmt_wrong(x):
                if bool(x > 3):
                    return 3
                else:
                    return x

    def test_for_range_no_arg(self):
        with self.assertRaisesRegex(RuntimeError, 'range\(\) expects 1 argument but got 0'):
            @torch.jit.script
            def range_no_arg(x):
                for i in range():
                    x += 1
                return x

    def test_list_iterables(self):
        with self.assertRaisesRegex(RuntimeError, 'List of iterables is not supported currently'):
            cu = torch.jit.CompilationUnit('''
            def list_iterables(x):
                for i, j in [2, 3, 4], [5, 6, 7]:
                    x += i
                    x += j
                return x
            ''')

    def test_for_tuple_unpack(self):
        with self.assertRaisesRegex(RuntimeError, 'Iteration variable unpacking is not supported'):
            cu = torch.jit.CompilationUnit('''
            def for_tuple_unpack(x, y):
                for i, j in [[3, 4], [5, 6], [7, 8]]:
                    x += i
                    y += j
                return x, y
            ''')

    def test_single_starred_lhs(self):
        with self.assertRaisesRegex(RuntimeError, 'A Starred expression may only appear on the lhs within the presence'
                                                  ' of another non-starred expression'):
            cu = torch.jit.CompilationUnit('''
            def single_starred_lhs(x):
                a = (x, x, x)
                *b = a
                return b
            ''')

    def test_multi_reduction(self):
        with self.assertRaisesRegex(RuntimeError, 'reductions are only allowed when there is a single variable on'
                                                  ' the left-hand side'):
            cu = torch.jit.CompilationUnit('''
            def multi_reduction(x):
                a, b += x
                return a, b
            ''')

    def test_invalid_call_arguments(self):
        with self.assertRaisesRegex(RuntimeError, 'arguments for call are not valid'):
            @torch.jit.script
            def invalid_call_arguments(x):
                return torch.unsqueeze(3, 4, 5, 6, 7, 8)

    def test_invalid_lhs_assignment(self):
        with self.assertRaisesRegex(RuntimeError, 'lhs of assignment must be a variable or starred expression'):
            cu = torch.jit.CompilationUnit('''
            def invalid_lhs_assignment(x):
                x + 1 = x
                return x
            ''')

    def test_multi_starred_expr_lhs(self):
        with self.assertRaisesRegex(RuntimeError, 'Only one starred expression is allowed on the lhs'):
            cu = torch.jit.CompilationUnit('''
            def multi_starred_expr_lhs():
                a, *b, *c = [1, 2, 3, 4, 5, 6]
                return a
            ''')

    def test_pack_tuple_into_non_var(self):
        with self.assertRaisesRegex(RuntimeError, 'Cannot pack a tuple into a non-variable'):
            cu = torch.jit.CompilationUnit('''
            def pack_tuple_into_non_var(x):
                a, *1 = (3, 4, 5)
                return x
            ''')

    def test_print_kwargs(self):
        with self.assertRaisesRegex(RuntimeError, 'print doesn\'t accept any keyword arguments'):
            cu = torch.jit.CompilationUnit('''
            def print_kwargs(x):
                print(x, flush=True)
                return x
            ''')

    def test_builtin_use_as_value(self):
        with self.assertRaisesRegex(RuntimeError, 'builtin cannot be used as a value'):
            @torch.jit.script
            def builtin_use_as_value(x):
                return x.unsqueeze

    def test_wrong_use_as_tuple(self):
        with self.assertRaisesRegex(RuntimeError, 'cannot be used as a tuple'):
            def test_fn():
                return 3

            @torch.jit.script
            def wrong_use_as_tuple(self):
                a, b = test_fn
                return a

    def test_wrong_attr_lookup(self):
        with self.assertRaisesRegex(RuntimeError, 'attribute lookup is not defined on builtin'):
            @torch.jit.script
            def wrong_attr_lookup(self, x):
                a = x.unsqueeze.myattr
                return a

    def test_wrong_use_as_callable(self):
        with self.assertRaisesRegex(RuntimeError, 'cannot call a value'):
            @torch.jit.script
            def wrong_use_as_callable(x):
                return x(3, 4, 5)

    def test_python_val_doesnt_have_attr(self):
        with self.assertRaisesRegex(RuntimeError, 'object has no attribute abcd'):

            @torch.jit.script
            def python_val_doesnt_have_attr():
                # this has to be a module otherwise attr lookup would not be
                # allowed in the first place
                return shutil.abcd

    def test_wrong_module_attr_lookup(self):
        with self.assertRaisesRegex(RuntimeError, 'python value of type \'type\' cannot be used as a value:'):
            import io

            @torch.jit.script
            def wrong_module_attr_lookup():
                return io.BytesIO

    def test_wrong_method_call_inputs(self):
        with self.assertRaisesRegex(RuntimeError, 'argument y not provided'):
            class SomeModule(torch.jit.ScriptModule):

                @torch.jit.script_method
                def foo(self, x, y):
                    return x

                @torch.jit.script_method
                def forward(self, x, y):
                    return self.foo(x)
            SomeModule()

    def test_single_starred_expr_for_loop(self):
        with self.assertRaisesRegex(RuntimeError, 'Starred unpacking is currently not supported for for loops'):
            cu = torch.jit.CompilationUnit('''
            def test():
                x = 0
                for *a in [1, 2, 3]:
                    x = x + 1
                return x
            ''')

    def test_duplicate(self):
        with self.assertRaisesRegex(RuntimeError, 'Method \'test\' already defined'):
            cu = torch.jit.CompilationUnit('''
            def test():
                return 1

            def test():
                return 2
            ''')

    def test_call_ge(self):
        with self.assertRaisesRegex(RuntimeError, 'expected at most 1 arguments but found 3'):
            @_trace(torch.zeros(1, 2, 3))
            def foo(x):
                return x

            @torch.jit.script
            def test_fn():
                return foo(torch.full([1], 1), torch.full([1], 2), torch.full([1], 3))

    def test_wrong_return_type(self):
        with self.assertRaisesRegex(RuntimeError, 'but instead got value of type tuple'):
            def somefunc():
                # type: () -> Tuple[Tuple[Tensor, Tensor]]
                return torch.zeros(3, 4), torch.zeros(4, 5)

            @torch.jit.script
            def wrong_return_type():
                return somefunc()
            wrong_return_type()

    # Tests for calling between different front-end modes
    def test_call_python_fn_from_tracing_fn(self):
        def python_fn(x):
            return torch.neg(x)

        @_trace(torch.rand(3, 4))
        def traced_fn(x):
            return python_fn(x) + 1

        # The neg op in the python function should be properly inlined to the
        # graph
        self.assertExpected(canonical(traced_fn.graph))

    def test_call_python_mod_from_tracing_fn(self):
        class PythonMod(torch.nn.Module):
            def __init__(self):
                super(PythonMod, self).__init__()
                self.param = torch.nn.Parameter(torch.rand(4, 3))

            def forward(self, x):
                return torch.mm(x, self.param)

        pm = PythonMod()

        @_trace(torch.rand(3, 4))
        def traced_fn(x):
            return pm(x) + 1.0

        # Note: the parameter self.param from the Python module is inlined
        # into the graph
        self.assertExpected(canonical(traced_fn.graph))

    def test_call_traced_fn_from_tracing_fn(self):
        @_trace(torch.rand(3, 4))
        def traced_fn1(x):
            return torch.neg(x)

        @_trace(torch.rand(3, 4))
        def traced_fn(x):
            return traced_fn1(x) + 1

        self.assertExpected(canonical(traced_fn.graph))

    def test_call_traced_mod_from_tracing_fn(self):
        class TracedModule(torch.nn.Module):
            def __init__(self):
                super(TracedModule, self).__init__()
                self.param = torch.nn.Parameter(torch.rand(4, 3))

            def forward(self, x):
                return torch.mm(x, self.param)

        tm = torch.jit.trace(TracedModule(), torch.rand(3, 4))

        @_trace(torch.rand(3, 4))
        def traced_fn(x):
            return tm(x) + 1.0

        # Note: the parameter self.param from the Python module is inlined
        # into the graph
        self.assertExpected(canonical(traced_fn.graph))

    def test_call_script_fn_from_tracing_fn(self):
        @torch.jit.script
        def script_fn(x):
            return torch.neg(x)

        @_trace(torch.rand(3, 4))
        def traced_fn(x):
            return script_fn(x) + 1

        self.assertExpected(canonical(traced_fn.graph))

    def test_call_script_mod_from_tracing_fn(self):
        class ScriptMod(torch.jit.ScriptModule):
            def __init__(self):
                super(ScriptMod, self).__init__()
                self.param = torch.nn.Parameter(torch.rand(4, 3))

            @torch.jit.script_method
            def forward(self, x):
                return torch.mm(x, self.param)

        sm = ScriptMod()

        @_trace(torch.rand(3, 4))
        def traced_fn(x):
            return sm(x) + 1.0

        self.assertExpected(canonical(traced_fn.graph))

    def test_call_python_fn_from_traced_module(self):
        def python_fn(x):
            return torch.neg(x)

        class TracedModule(torch.nn.Module):
            def __init__(self):
                super(TracedModule, self).__init__()
                self.param = torch.nn.Parameter(torch.rand(4, 3))

            def forward(self, x):
                return torch.mm(python_fn(x), self.param)

        tm = torch.jit.trace(TracedModule(), torch.rand(3, 4))

        # Note: parameter self.param from the traced module should appear as
        # an input to the graph and the neg op from the Python function should
        # be properly inlined
        self.assertExpected(canonical(tm.graph))

    def test_call_python_mod_from_traced_module(self):
        class PythonModule(torch.nn.Module):
            def __init__(self):
                super(PythonModule, self).__init__()
                self.param = torch.nn.Parameter(torch.rand(5, 7))

            def forward(self, x):
                return torch.mm(x, self.param)

        class TracedModule(torch.nn.Module):
            def __init__(self):
                super(TracedModule, self).__init__()
                self.param = torch.nn.Parameter(torch.rand(4, 5))
                self.mod = PythonModule()

            def forward(self, x):
                return self.mod(torch.mm(x, self.param)) + 1.0

        tm = torch.jit.trace(TracedModule(), torch.rand(3, 4))

        # Note: the parameters from both modules should appear in the flattened
        # inputs of the graph. All ops from both modules should be inlined.
        self.assertExpected(canonical(tm.graph))

    def test_call_traced_fn_from_traced_module(self):
        @_trace(torch.rand(3, 4))
        def traced_fn(x):
            return torch.neg(x)

        class TracedModule(torch.nn.Module):
            def __init__(self):
                super(TracedModule, self).__init__()
                self.param = torch.nn.Parameter(torch.rand(4, 5))

            def forward(self, x):
                return traced_fn(torch.mm(x, self.param))

        tm = torch.jit.trace(TracedModule(), torch.rand(3, 4))
        # Note: neg op from the traced function should be properly inlined
        self.assertExpected(canonical(tm.graph))

    def test_call_traced_module_from_traced_module(self):
        class TracedModule1(torch.nn.Module):
            def __init__(self):
                super(TracedModule1, self).__init__()
                self.param = torch.nn.Parameter(torch.rand(5, 7))

            def forward(self, x):
                return torch.mm(x, self.param)

        class TracedModule(torch.nn.Module):
            def __init__(self):
                super(TracedModule, self).__init__()
                self.param = torch.nn.Parameter(torch.rand(4, 5))
                self.mod = torch.jit.trace(TracedModule1(), torch.rand(3, 5))

            def forward(self, x):
                return self.mod(torch.mm(x, self.param)) + 1.0

        tm = torch.jit.trace(TracedModule(), torch.rand(3, 4))

        # Note: the parameters from both modules should appear in the flattened
        # inputs of the graph. All ops from both modules should be inlined.
        self.assertExpected(canonical(tm.graph))

    def test_call_script_fn_from_traced_module(self):
        @torch.jit.script
        def traced_fn(x):
            return torch.neg(x)

        class TracedModule(torch.nn.Module):
            def __init__(self):
                super(TracedModule, self).__init__()
                self.param = torch.nn.Parameter(torch.rand(4, 5))

            def forward(self, x):
                return traced_fn(torch.mm(x, self.param))

        tm = torch.jit.trace(TracedModule(), torch.rand(3, 4))
        # Note: neg op from the script function should be properly inlined
        self.assertExpected(canonical(tm.graph))

    def test_call_script_module_from_traced_module(self):
        class ScriptMod(torch.jit.ScriptModule):
            def __init__(self):
                super(ScriptMod, self).__init__()
                self.param = torch.nn.Parameter(torch.rand(5, 7))

            @torch.jit.script_method
            def forward(self, x):
                return torch.mm(x, self.param)

        class TracedModule(torch.nn.Module):
            def __init__(self):
                super(TracedModule, self).__init__()
                self.param = torch.nn.Parameter(torch.rand(4, 5))
                self.mod = ScriptMod()

            def forward(self, x):
                return self.mod(torch.mm(x, self.param)) + 1.0

        tm = torch.jit.trace(TracedModule(), torch.rand(3, 4))

        # Note: the parameters from both modules should appear in the flattened
        # inputs of the graph. All ops from both modules should be inlined.
        self.assertExpected(canonical(tm.graph))

    def test_call_python_fn_from_script_fn(self):
        def python_fn(x):
            return torch.neg(x)

        @torch.jit.script
        def script_fn(x):
            return python_fn(x) + 1

        # Note: the call to python_fn appears as `^python_fn()` and is called
        # as a PythonOp in the interpreter
        self.assertExpected(canonical(script_fn.graph))

    def test_call_python_mod_from_script_fn(self):
        class PythonModule(torch.nn.Module):
            def __init__(self):
                super(PythonModule, self).__init__()
                self.param = torch.nn.Parameter(torch.rand(5, 7))

            def forward(self, x):
                return torch.mm(x, self.param)

        pm = PythonModule()

        @torch.jit.script
        def script_fn(x):
            return pm(x) + 1

        # Note: call to pm(x) appears as ^<python_value>() in the trace.
        # Parameters are NOT inlined.
        self.assertExpected(str(script_fn.graph))

    def test_call_traced_fn_from_script_fn(self):
        @_trace(torch.rand(3, 4))
        def traced_fn(x):
            return torch.neg(x)

        @torch.jit.script
        def script_fn(x):
            return traced_fn(x) + 1

        # Note: the neg op from traced_fn should be properly inlined into the
        # script function's graph
        self.assertExpected(str(script_fn.graph))

    def test_call_traced_mod_from_script_fn(self):
        class TracedModule(torch.nn.Module):
            def __init__(self):
                super(TracedModule, self).__init__()

            def forward(self, x):
                return torch.mm(x, torch.zeros(4, 3))

        tm = torch.jit.trace(TracedModule(), torch.rand(3, 4))

        @torch.jit.script
        def script_fn(x):
            return tm(x) + 1

        self.assertExpected(str(script_fn.graph))

    def test_call_script_fn_from_script_fn(self):
        @torch.jit.script
        def script_fn1(x):
            return torch.neg(x)

        @torch.jit.script
        def script_fn(x):
            return script_fn1(x) + 1

        # Note: the neg op from script_fn1 should be properly inlined into the
        # graph of script_fn
        self.assertExpected(str(script_fn.graph))

    def test_call_script_mod_from_script_fn(self):
        class ScriptMod(torch.jit.ScriptModule):
            def __init__(self):
                super(ScriptMod, self).__init__()

            @torch.jit.script_method
            def forward(self, x):
                return torch.mm(x, torch.zeros([4, 3]))

        sm = ScriptMod()

        @torch.jit.script
        def script_fn(x):
            return sm(x) + 1

        self.assertExpected(str(script_fn.graph))

    def test_call_python_fn_from_script_module(self):
        def python_fn(x):
            return torch.neg(x)

        class ScriptMod(torch.jit.ScriptModule):
            def __init__(self):
                super(ScriptMod, self).__init__()
                self.param = torch.nn.Parameter(torch.rand(4, 3))

            @torch.jit.script_method
            def forward(self, x):
                return python_fn(torch.mm(x, self.param))

        sm = ScriptMod()
        self.assertExpected(str(sm.__getattr__('forward').graph))

    def test_call_python_mod_from_script_module(self):
        class PythonMod(torch.nn.Module):
            def __init__(self):
                super(PythonMod, self).__init__()
                self.param = torch.nn.Parameter(torch.rand(3, 5))

            def forward(self, x):
                return torch.mm(x, self.param)

        class ScriptMod(torch.jit.ScriptModule):
            def __init__(self):
                super(ScriptMod, self).__init__()
                self.param = torch.nn.Parameter(torch.rand(4, 3))
                self.pm = PythonMod()

            @torch.jit.script_method
            def forward(self, x):
                return self.pm(torch.mm(x, self.param))

        sm = ScriptMod()
        # Note: the call into PythonMod appears as ^<python_value>(). Parameters
        # are NOT inlined
        self.assertExpected(str(sm.graph))

    def test_call_tracing_fn_from_script_module(self):
        @_trace(torch.rand(3, 3))
        def traced_fn(x):
            return torch.neg(x)

        class ScriptMod(torch.jit.ScriptModule):
            def __init__(self):
                super(ScriptMod, self).__init__()
                self.param = torch.nn.Parameter(torch.rand(4, 3))

            @torch.jit.script_method
            def forward(self, x):
                return traced_fn(torch.mm(x, self.param))

        sm = ScriptMod()
        self.assertExpected(str(sm.__getattr__('forward').graph))

    def test_call_tracing_mod_from_script_module(self):
        class TracedMod(torch.nn.Module):
            def __init__(self):
                super(TracedMod, self).__init__()
                self.param = torch.nn.Parameter(torch.rand(3, 5))

            def forward(self, x):
                return torch.mm(x, self.param)

        class ScriptMod(torch.jit.ScriptModule):
            def __init__(self):
                super(ScriptMod, self).__init__()
                self.param = torch.nn.Parameter(torch.rand(4, 3))
                self.tm = torch.jit.trace(TracedMod(), torch.rand(3, 3))

            @torch.jit.script_method
            def forward(self, x):
                return self.tm(torch.mm(x, self.param))

        sm = ScriptMod()
        # Note: the parameters from both modules should appear in the flattened
        # input list to the graph. The mm op from TracedMod should be properly
        # inlined
        self.assertExpected(str(sm.graph))

    def test_call_script_fn_from_script_module(self):
        @torch.jit.script
        def script_fn(x):
            return torch.neg(x)

        class ScriptMod(torch.jit.ScriptModule):
            def __init__(self):
                super(ScriptMod, self).__init__()
                self.param = torch.nn.Parameter(torch.rand(4, 3))

            @torch.jit.script_method
            def forward(self, x):
                return script_fn(torch.mm(x, self.param))

        sm = ScriptMod()
        self.assertExpected(str(sm.__getattr__('forward').graph))

    def test_call_script_mod_from_script_module(self):
        class ScriptMod1(torch.jit.ScriptModule):
            def __init__(self):
                super(ScriptMod1, self).__init__()
                self.param = torch.nn.Parameter(torch.rand(3, 5))

            @torch.jit.script_method
            def forward(self, x):
                return torch.mm(x, self.param)

        class ScriptMod(torch.jit.ScriptModule):
            def __init__(self):
                super(ScriptMod, self).__init__()
                self.param = torch.nn.Parameter(torch.rand(4, 3))
                self.tm = ScriptMod1()

            @torch.jit.script_method
            def forward(self, x):
                return self.tm(torch.mm(x, self.param))

        sm = ScriptMod()
        # Note: the parameters from both modules should appear in the flattened
        # input list to the graph. The mm op from ScriptMod1 should be properly
        # inlined
        self.assertExpected(str(sm.graph))

    def test_module_with_params_called_fails(self):
        with self.assertRaisesRegex(RuntimeError, "Attempted to inline a Module with parameters. Stateful "
                                                  "modules to be inlined must be submodules of the callee."):
            class ScriptMod(torch.jit.ScriptModule):
                def __init__(self):
                    super(ScriptMod, self).__init__()
                    self.param = torch.nn.Parameter(torch.rand(3, 3))

                @torch.jit.script_method
                def forward(self, x):
                    return torch.mm(x, self.param)

            sm = ScriptMod()

            @torch.jit.script
            def some_func(x):
                return sm(x)

    def test_index_put_trace_with_view(self):
        @_trace(torch.rand(100), torch.tensor([1, 2, 3, 4]), torch.rand(1, 1, 1, 4))
        def test_index_put(target, indices, rhs):
            target[indices] = rhs
            return target

        self.assertExpectedGraph(test_index_put.graph)

    def test_index_put_trace_without_view(self):
        @_trace(torch.rand(100), torch.tensor([1, 2, 3, 4]), torch.rand(4))
        def test_index_put(target, indices, rhs):
            target[indices] = rhs
            return target

        self.assertExpectedGraph(test_index_put.graph)

    def test_annotated_script_fn(self):
        @torch.jit.script
        def foo(x, y, z):
            # type: (Tensor, Tuple[Tensor, Tensor, Tensor], Tuple[Tensor, Tuple[Tensor, Tensor]]) -> Tensor
            return x

        self.assertExpected(foo.__getattr__('forward').pretty_print_schema())

    def test_annotated_script_method(self):
        class SM(torch.jit.ScriptModule):
            @torch.jit.script_method
            def forward(self, x, y):
                # type: (Tuple[Tensor, Tensor], Tensor) -> Tuple[Tensor, Tensor, Tensor]
                return y, y, y

        sm = SM()

        self.assertExpected(sm.__getattr__('forward').pretty_print_schema())

    def test_annotated_script_fn_return_mismatch(self):
        with self.assertRaisesRegex(RuntimeError, r"Return value at position 0 was annotated as "
                                                  r"having type \(Tensor, Tensor\) but is "
                                                  r"actually of type Tensor"):
            @torch.jit.script
            def return_tup(x):
                # type: (Tensor) -> Tuple[Tuple[Tensor, Tensor], Tensor]
                return x, x

    def test_annotated_script_fn_arg_mismatch(self):
        with self.assertRaisesRegex(RuntimeError, r"arguments for call are not valid"):
            @torch.jit.script
            def tuple_arg(x):
                # type: (Tuple[Tensor, Tensor]) -> Tensor
                return x + 1

    def test_script_non_tensor_args_outputs(self):
        @torch.jit.script
        def fn(x, y):
            # type: (Tensor, float) -> float
            return float((x + y).sum())

        x = torch.ones(2, 2)
        z = fn(x, 1)
        self.assertIsInstance(z, float)
        self.assertEqual(z, 8.)

    @unittest.skip('https://github.com/pytorch/pytorch/issues/9595')
    def test_inline_and_run_annotated_script_fn(self):
        @torch.jit.script
        def to_inline(x, y):
            # type: (Tuple[Tensor, Tensor], Tensor) -> Tensor
            return y

        @torch.jit.script
        def some_func(x):
            return to_inline((x, x), x)

        x = torch.rand(3, 4)
        self.assertEqual(some_func(x), x)

    def test_file_format_serialization(self):
        import tempfile
        filename = tempfile.mktemp()
        writer = torch._C.PyTorchFileWriter(filename)
        import os
        import random
        buffers = [os.urandom(size) for size in [random.randint(1, 100) for i in range(20)]]
        offsets = []
        for buf in buffers:
            offsets.append(writer.write_record(buf, len(buf)))
        import pickle
        serialized_offsets = pickle.dumps(offsets)
        writer.write_record(serialized_offsets, len(serialized_offsets))
        writer.write_end_of_file()

        reader = torch._C.PyTorchFileReader(filename)
        serialized_offsets_read = reader.get_last_record()
        parsed_serialized_offsets = pickle.loads(serialized_offsets)

        for i, offset in enumerate(parsed_serialized_offsets):
            data = reader.get_record_with_key(offset)
            assert(data == buffers[i])

    # ***** Type annotation tests ****
    # Test combinations of:
    # {String frontend, Python AST Frontend}
    # {Python 3-style type annotations, MyPy-style type comments}
    # {Script method, Script function}

    #  String frontend , Python 3-style type annotations , Script function
    def test_annot_string_py3_fn(self):
        # TODO: return non-tensor type
        cu = torch.jit.CompilationUnit('''
            def foo(x : Tensor, y : Tuple[Tensor, Tensor]) -> Tuple[Tensor, Tensor]:
                return x, x
        ''')
        self.assertExpected(cu.__getattr__('foo').pretty_print_schema())

    #  String frontend , Python 3-style type annotations , Script method
    def test_annot_string_py3_method(self):
        class TestModule(torch.jit.ScriptModule):
            def __init__(self):
                super(TestModule, self).__init__()

                self.define('''
            def foo(self, x : Tensor, y : Tuple[Tensor, Tensor]) -> Tuple[Tensor, Tensor]:
                return x, x
                ''')

        tm = TestModule()
        self.assertExpected(tm.__getattr__('foo').pretty_print_schema())

    #  String frontend , MyPy-style type comments , Script function
    def test_annot_string_mypy_fn(self):
        cu = torch.jit.CompilationUnit('''
            def foo(x, y):
                # type: (Tensor, Tuple[Tensor, Tensor]) -> Tuple[Tensor, Tensor]
                return x, x
        ''')
        self.assertExpected(cu.__getattr__('foo').pretty_print_schema())

    #  String frontend , MyPy-style type comments , Script method
    def test_annot_string_mypy_method(self):
        class TestModule(torch.jit.ScriptModule):
            def __init__(self):
                super(TestModule, self).__init__()

                self.define('''
            def foo(self, x, y):
                # type: (Tensor, Tuple[Tensor, Tensor]) -> Tuple[Tensor, Tensor]
                return x, x
                ''')

        tm = TestModule()
        self.assertExpected(tm.__getattr__('foo').pretty_print_schema())

    # Helper function to eval Python3 code without causing a syntax error for
    # this file under py2
    def _get_py3_code(self, code, fn_name):
        with tempfile.TemporaryDirectory() as tmp_dir:
            script_path = os.path.join(tmp_dir, 'script.py')
            with open(script_path, 'w') as f:
                f.write(code)
            import importlib.util
            spec = importlib.util.spec_from_file_location(fn_name, script_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            fn = getattr(module, fn_name)
            return fn

    #  Python AST Frontend , Python 3-style type annotations , Script function
    @unittest.skipIf(not PY35, "Python 3.5 needed")
    def test_annot_ast_py3_fn(self):
        code = dedent('''
            from typing import Tuple
            from torch import Tensor
            import torch
            @torch.jit.script
            def foo(x : Tensor, y : Tuple[Tensor, Tensor]) -> Tuple[Tensor, Tensor]:
                return x, x
        ''')
        fn = self._get_py3_code(code, 'foo')
        self.assertExpected(fn.__getattr__('forward').pretty_print_schema())

    #  Python AST Frontend , Python 3-style type annotations , Script method
    @unittest.skipIf(not PY35, "Python 3.5 needed")
    def test_annot_ast_py3_method(self):
        code = dedent('''
            from typing import Tuple
            from torch import Tensor
            import torch
            class FooModule(torch.jit.ScriptModule):
                @torch.jit.script_method
                def foo(self, x : Tensor, y : Tuple[Tensor, Tensor]) -> Tuple[Tensor, Tensor]:
                    return x, x
            instance = FooModule()
        ''')
        fn = self._get_py3_code(code, 'instance')
        self.assertExpected(fn.__getattr__('foo').pretty_print_schema())

    #  Python AST Frontend , MyPy-style type comments , Script function
    @unittest.skipIf(not PY35, "Python 3.5 needed")
    def test_annot_ast_mypy_fn(self):
        code = dedent('''
            from typing import Tuple
            from torch import Tensor
            import torch
            @torch.jit.script
            def foo(x, y):
                # type: (Tensor, Tuple[Tensor, Tensor]) -> Tuple[Tensor, Tensor]
                return x, x
        ''')
        fn = self._get_py3_code(code, 'foo')
        self.assertExpected(fn.__getattr__('forward').pretty_print_schema())

    #  Python AST Frontend , MyPy-style type comments , Script method
    @unittest.skipIf(not PY35, "Python 3.5 needed")
    def test_annot_ast_mypy_method(self):
        code = dedent('''
            from typing import Tuple
            from torch import Tensor
            import torch
            class FooModule(torch.jit.ScriptModule):
                @torch.jit.script_method
                def foo(self, x, y):
                    # type: (Tensor, Tuple[Tensor, Tensor]) -> Tuple[Tensor, Tensor]
                    return x, x
            instance = FooModule()
        ''')
        fn = self._get_py3_code(code, 'instance')
        self.assertExpected(fn.__getattr__('foo').pretty_print_schema())

    def test_torch_dot_tensor_annotation_py3_string(self):
        # TODO: return non-tensor type
        cu = torch.jit.CompilationUnit('''
            def foo(x : torch.Tensor, y : Tuple[torch.Tensor, Tensor]) -> Tuple[Tensor, Tensor]:
                return x, x
        ''')
        self.assertExpected(cu.__getattr__('foo').pretty_print_schema())

    def test_torch_dot_tensor_annotation_mypy_string(self):
        cu = torch.jit.CompilationUnit('''
            def foo(x, y):
                # type: (torch.Tensor, Tuple[torch.Tensor, Tensor]) -> Tuple[Tensor, Tensor]
                return x, x
        ''')
        self.assertExpected(cu.__getattr__('foo').pretty_print_schema())

    @unittest.skipIf(not PY35, "Python 3.5 needed")
    def test_torch_dot_tensor_annotation_py3_ast(self):
        code = dedent('''
            from typing import Tuple
            from torch import Tensor
            import torch
            class FooModule(torch.jit.ScriptModule):
                @torch.jit.script_method
                def foo(self, x : torch.Tensor, y : Tuple[torch.Tensor, Tensor]) -> Tuple[Tensor, Tensor]:
                    return x, x
            instance = FooModule()
        ''')
        fn = self._get_py3_code(code, 'instance')
        self.assertExpected(fn.__getattr__('foo').pretty_print_schema())

    def test_method_casts_script(self):
        cast_types = [
            'byte', 'char', 'double', 'float', 'int', 'long', 'short'
        ]

        for cast_type in cast_types:
            cu = torch.jit.CompilationUnit('''
            def cast_to(x):
                return x.{cast_type}()
            '''.format(cast_type=cast_type))

            x = torch.rand(3, 4, 5) * 128
            cu_result = cu.cast_to(x)
            reference = getattr(x, cast_type)()
            self.assertEqual(cu_result, reference)

    def test_listconstruct_erasure(self):
        class FooMod(torch.nn.Module):
            def forward(self, x):
                mask = x < 0.0
                return x[mask]

        import io
        f = io.BytesIO()
        self.assertExpected(torch.onnx.export_to_pretty_string(
            FooMod(), (torch.rand(3, 4),), f,
            operator_export_type=OperatorExportTypes.ONNX_ATEN_FALLBACK))

    def test_trace_checker_arange_as_constant(self):
        with self.assertRaisesRegex(torch.jit.TracingCheckError, r'Graphs differed across invocations!'):
            @_trace(torch.rand(3, 4), check_inputs=[(torch.rand(4, 5),)])
            def foo(x):
                y = torch.arange(0, x.shape[0]).double()
                return x + y.unsqueeze(1)

    @suppress_warnings
    def test_trace_checker_dot_data(self):
        with self.assertRaisesRegex(torch.jit.TracingCheckError, r'Tensor-valued Constant nodes differed in value '
                                                                 r'across invocations'):
            @_trace(torch.rand(3, 4), check_inputs=[(torch.rand(3, 4),)])
            def foo(x):
                y = x.data
                return x + y

    @suppress_warnings
    def test_trace_checker_control_flow(self):
        def foo(x):
            for _ in range(x.size(0)):
                x = torch.neg(x)
            return x

        with self.assertRaisesRegex(torch.jit.TracingCheckError, r'Graphs differed across invocations!'):
            torch.jit.trace(foo, torch.randn(3, 4), check_inputs=[torch.randn(4, 4)])

    @suppress_warnings
    def test_trace_checker_memoization(self):
        with self.assertRaisesRegex(torch.jit.TracingCheckError, r'Graphs differed across invocations!'):
            def foo(x):
                if not hasattr(foo, 'cache'):
                    foo.cache = torch.neg(x)
                return x + foo.cache

            traced = torch.jit.trace(foo, torch.rand(3, 4), check_inputs=[(torch.rand(3, 4),)])

    def checkTracerWarning(self, *args, **kwargs):
        with warnings.catch_warnings(record=True) as warns:
            torch.jit.trace(*args, **kwargs)
        self.assertGreater(len(warns), 0)
        for warn in warns:
            self.assertIn("cause the trace to be incorrect", str(warn.message))

    def test_trace_checker_slice_lhs(self):
        def foo(x):
            for i in range(3):
                x[i, :] = torch.zeros(4)
            return x

        self.assertWarnsRegex(lambda: torch.jit.trace(foo, torch.rand(3, 4), check_inputs=[torch.rand(3, 4)]),
                              'Output nr 1. of the traced function does not match the '
                              'corresponding output of the Python function')

    def test_trace_checker_inplace_on_view(self):
        def foo(x):
            x.view(-1).add_(-x.view(-1))
            return x

        self.assertWarnsRegex(lambda: torch.jit.trace(foo, torch.rand(3, 4), check_inputs=[torch.rand(5, 6)]),
                              'Output nr 1. of the traced function does not match the '
                              'corresponding output of the Python function')

    def test_lhs_index_fails(self):
        def foo(x):
            x[0, 1] = 4
            return x
        self.checkTracerWarning(foo, torch.rand(3, 4))

    def test_lhs_index_trivial(self):
        def foo(y, x):
            y[...] = x
            return y
        self.checkTrace(foo, (torch.rand(3, 4), torch.rand(4)), inputs_require_grads=False)

    def test_inplace_warn(self):
        def foo(x):
            x.view(-1).add_(-x.view(-1))
            return x
        self.checkTracerWarning(foo, torch.rand(3, 4))

    @suppress_warnings
    def test_trace_checker_dropout_train(self):
        def foo(x):
            return torch.dropout(x, p=0.5, train=True)

        self.assertWarnsRegex(lambda: torch.jit.trace(foo, torch.rand(3, 4), check_inputs=[torch.rand(5, 6)]),
                              'Output nr 1. of the traced function does not match the '
                              'corresponding output of the Python function')
        self.assertWarnsRegex(lambda: torch.jit.trace(foo, torch.rand(3, 4), check_inputs=[torch.rand(5, 6)]),
                              'Trace had nondeterministic nodes')

    def test_trace_checker_dropout_notrain(self):
        input = torch.rand(3, 4)

        @_trace(input)
        def foo(x):
            return torch.dropout(x, p=0.5, train=False)

        self.assertEqual(foo(input), input)

    def test_export_dynamic_slice(self):
        class DynamicSliceExportMod(torch.jit.ScriptModule):
            @torch.jit.script_method
            def forward(self, x):
                retval = x[0]
                for i in range(x.size(1)):
                    retval += torch.sum(x[0:i], dim=0)
                return retval

        mod = DynamicSliceExportMod()

        input = torch.rand(3, 4, 5)
        example_outs = mod(input)

        f = io.BytesIO()
        exported = torch.onnx.export_to_pretty_string(
            DynamicSliceExportMod(), (input,), f, example_outputs=example_outs)
        self.assertExpected(exported)

    def test_string_frontend_elif(self):
        code = '''
            def elif_test(niter : int):
                rv = 0
                for i in range(niter):
                    if i % 3 == 0 and i % 5 == 0:
                        rv += 35
                    elif i % 3 == 0:
                        rv += 3
                    elif i % 5 == 0:
                        rv += 5
                    else:
                        rv += i
                return rv
        '''

        self.checkScript(code, (101,), name='elif_test', outputs=3028)


class MnistNet(nn.Module):
    def __init__(self):
        super(MnistNet, self).__init__()
        self.conv1 = nn.Conv2d(1, 10, kernel_size=5)
        self.conv2 = nn.Conv2d(10, 20, kernel_size=5)
        self.conv2_drop = nn.Dropout2d()
        self.fc1 = nn.Linear(320, 50)
        self.fc2 = nn.Linear(50, 10)

    def forward(self, x):
        x = F.relu(F.max_pool2d(self.conv1(x), 2))
        x = F.relu(F.max_pool2d(self.conv2_drop(self.conv2(x)), 2))
        x = x.view(-1, 320)
        x = F.relu(self.fc1(x))
        x = F.dropout(x, training=self.training)
        x = self.fc2(x)
        return F.log_softmax(x, dim=1)


class TestEndToEndHybridFrontendModels(JitTestCase):
    @staticmethod
    def _test_dcgan_models(self, device, check_export_import=True):
        class DCGANGenerator(nn.Module):
            def __init__(self, nz, ngf, nc):
                super(DCGANGenerator, self).__init__()
                self.main = nn.Sequential(
                    # input is Z, going into a convolution
                    nn.ConvTranspose2d(nz, ngf * 8, 4, 1, 0, bias=False),
                    nn.BatchNorm2d(ngf * 8),
                    nn.ReLU(True),
                    # state size. (ngf*8) x 4 x 4
                    nn.ConvTranspose2d(ngf * 8, ngf * 4, 4, 2, 1, bias=False),
                    nn.BatchNorm2d(ngf * 4),
                    nn.ReLU(True),
                    # state size. (ngf*4) x 8 x 8
                    nn.ConvTranspose2d(ngf * 4, ngf * 2, 4, 2, 1, bias=False),
                    nn.BatchNorm2d(ngf * 2),
                    nn.ReLU(True),
                    # state size. (ngf*2) x 16 x 16
                    nn.ConvTranspose2d(ngf * 2, ngf, 4, 2, 1, bias=False),
                    nn.BatchNorm2d(ngf),
                    nn.ReLU(True),
                    # state size. (ngf) x 32 x 32
                    nn.ConvTranspose2d(ngf, nc, 4, 2, 1, bias=False),
                    nn.Tanh()
                    # state size. (nc) x 64 x 64
                )

            def forward(self, input):
                return self.main(input)

        class DCGANDiscriminator(nn.Module):
            def __init__(self, nc, ndf):
                super(DCGANDiscriminator, self).__init__()
                self.main = nn.Sequential(
                    # input is (nc) x 64 x 64
                    nn.Conv2d(nc, ndf, 4, 2, 1, bias=False),
                    nn.LeakyReLU(0.2, inplace=True),
                    # state size. (ndf) x 32 x 32
                    nn.Conv2d(ndf, ndf * 2, 4, 2, 1, bias=False),
                    nn.BatchNorm2d(ndf * 2),
                    nn.LeakyReLU(0.2, inplace=True),
                    # state size. (ndf*2) x 16 x 16
                    nn.Conv2d(ndf * 2, ndf * 4, 4, 2, 1, bias=False),
                    nn.BatchNorm2d(ndf * 4),
                    nn.LeakyReLU(0.2, inplace=True),
                    # state size. (ndf*4) x 8 x 8
                    nn.Conv2d(ndf * 4, ndf * 8, 4, 2, 1, bias=False),
                    nn.BatchNorm2d(ndf * 8),
                    nn.LeakyReLU(0.2, inplace=True),
                    # state size. (ndf*8) x 4 x 4
                    nn.Conv2d(ndf * 8, 1, 4, 1, 0, bias=False),
                    nn.Sigmoid()
                )

            def forward(self, input):
                return self.main(input).view(-1, 1).squeeze(1)

        bs, nz, ngf, nc, ndf = 5, 6, 9, 3, 10
        self.checkTrace(DCGANGenerator(nz, ngf, nc).to(device),
                        (torch.rand(bs, nz, 1, 1, device=device),),
                        export_import=check_export_import)
        example_input = DCGANGenerator(nz, ngf, nc).to(device)(torch.rand(bs, nz, 1, 1, device=device))
        self.checkTrace(DCGANDiscriminator(nc, ndf).to(device), (example_input,),
                        export_import=check_export_import)

    def test_dcgan_models(self):
        self._test_dcgan_models(self, device='cpu')

    @unittest.skipIf(not RUN_CUDA, "no CUDA")
    def test_dcgan_models_cuda(self):
        # XXX: export_import on CUDA modules doesn't work (#11480)
        self._test_dcgan_models(self, device='cuda', check_export_import=False)

    @staticmethod
    def _test_neural_style(self, device, check_export_import=True):
        class TransformerNet(torch.nn.Module):
            def __init__(self):
                super(TransformerNet, self).__init__()
                # Initial convolution layers
                self.conv1 = ConvLayer(3, 32, kernel_size=9, stride=1)
                self.in1 = torch.nn.InstanceNorm2d(32, affine=True)
                self.conv2 = ConvLayer(32, 64, kernel_size=3, stride=2)
                self.in2 = torch.nn.InstanceNorm2d(64, affine=True)
                self.conv3 = ConvLayer(64, 128, kernel_size=3, stride=2)
                self.in3 = torch.nn.InstanceNorm2d(128, affine=True)
                # Residual layers
                self.res1 = ResidualBlock(128)
                self.res2 = ResidualBlock(128)
                self.res3 = ResidualBlock(128)
                self.res4 = ResidualBlock(128)
                self.res5 = ResidualBlock(128)
                # Upsampling Layers
                self.deconv1 = UpsampleConvLayer(128, 64, kernel_size=3, stride=1, upsample=2)
                self.in4 = torch.nn.InstanceNorm2d(64, affine=True)
                self.deconv2 = UpsampleConvLayer(64, 32, kernel_size=3, stride=1, upsample=2)
                self.in5 = torch.nn.InstanceNorm2d(32, affine=True)
                self.deconv3 = ConvLayer(32, 3, kernel_size=9, stride=1)
                # Non-linearities
                self.relu = torch.nn.ReLU()

            def forward(self, X):
                y = self.relu(self.in1(self.conv1(X)))
                y = self.relu(self.in2(self.conv2(y)))
                y = self.relu(self.in3(self.conv3(y)))
                y = self.res1(y)
                y = self.res2(y)
                y = self.res3(y)
                y = self.res4(y)
                y = self.res5(y)
                y = self.relu(self.in4(self.deconv1(y)))
                y = self.relu(self.in5(self.deconv2(y)))
                y = self.deconv3(y)
                return y

        class ConvLayer(torch.nn.Module):
            def __init__(self, in_channels, out_channels, kernel_size, stride):
                super(ConvLayer, self).__init__()
                reflection_padding = kernel_size // 2
                self.reflection_pad = torch.nn.ReflectionPad2d(reflection_padding)
                self.conv2d = torch.nn.Conv2d(in_channels, out_channels, kernel_size, stride)

            def forward(self, x):
                out = self.reflection_pad(x)
                out = self.conv2d(out)
                return out

        class ResidualBlock(torch.nn.Module):
            """ResidualBlock
            introduced in: https://arxiv.org/abs/1512.03385
            recommended architecture: http://torch.ch/blog/2016/02/04/resnets.html
            """

            def __init__(self, channels):
                super(ResidualBlock, self).__init__()
                self.conv1 = ConvLayer(channels, channels, kernel_size=3, stride=1)
                self.in1 = torch.nn.InstanceNorm2d(channels, affine=True)
                self.conv2 = ConvLayer(channels, channels, kernel_size=3, stride=1)
                self.in2 = torch.nn.InstanceNorm2d(channels, affine=True)
                self.relu = torch.nn.ReLU()

            def forward(self, x):
                residual = x
                out = self.relu(self.in1(self.conv1(x)))
                out = self.in2(self.conv2(out))
                out = out + residual
                return out

        class UpsampleConvLayer(torch.nn.Module):
            """UpsampleConvLayer
            Upsamples the input and then does a convolution. This method gives better results
            compared to ConvTranspose2d.
            ref: http://distill.pub/2016/deconv-checkerboard/
            """

            def __init__(self, in_channels, out_channels, kernel_size, stride, upsample=None):
                super(UpsampleConvLayer, self).__init__()
                self.upsample = upsample
                if upsample:
                    self.upsample_layer = torch.nn.Upsample(mode='nearest', scale_factor=upsample)
                reflection_padding = kernel_size // 2
                self.reflection_pad = torch.nn.ReflectionPad2d(reflection_padding)
                self.conv2d = torch.nn.Conv2d(in_channels, out_channels, kernel_size, stride)

            def forward(self, x):
                x_in = x
                if self.upsample:
                    x_in = self.upsample_layer(x_in)
                out = self.reflection_pad(x_in)
                out = self.conv2d(out)
                return out

        self.checkTrace(TransformerNet(), (torch.rand(5, 3, 64, 64),), export_import=check_export_import)

    def test_neural_style(self):
        self._test_neural_style(self, device='cpu')

    @unittest.skipIf(not RUN_CUDA, "no CUDA")
    def test_neural_style_cuda(self):
        # XXX: export_import on CUDA modules doesn't work (#11480)
        self._test_neural_style(self, device='cuda', check_export_import=False)

    @staticmethod
    def _test_mnist(self, device, check_export_import=True):
        # eval() is present because dropout makes this nondeterministic
        self.checkTrace(MnistNet().to(device).eval(), (torch.rand(5, 1, 28, 28, device=device),),
                        export_import=check_export_import)

    def test_mnist(self):
        self._test_mnist(self, device='cpu')

    @unittest.skipIf(not RUN_CUDA, "no CUDA")
    def test_mnist_cuda(self):
        # XXX: export_import on CUDA modules doesn't work (#11480)
        self._test_mnist(self, device='cuda', check_export_import=False)

    @unittest.skipIf(not RUN_CUDA, "no CUDA")
    def test_mnist_training_leaks_no_memory_cuda(self):
        net = MnistNet().cuda()
        # MnistNet uses dropout, don't check its trace
        traced_net = torch.jit.trace(net, [torch.randn(5, 1, 28, 28, device='cuda')],
                                     check_trace=False)

        def train(iters):
            for _ in range(iters):
                # Get some fake data
                inp = torch.randn(5, 1, 28, 28, device='cuda')
                out = traced_net(inp)

                # Here's some fake loss
                out.sum().backward()

                # Zero out grads
                traced_net.zero_grad()

        # Set it up so the params have .grad fields so they are not reported as leaks
        train(1)

        with self.assertLeaksNoCudaTensors():
            train(5)

    @staticmethod
    def _test_reinforcement_learning(self, device, test_export_import=True):
        class Policy(nn.Module):
            def __init__(self):
                super(Policy, self).__init__()
                self.affine1 = nn.Linear(4, 128)
                self.affine2 = nn.Linear(128, 2)

            def forward(self, x):
                x = F.relu(self.affine1(x))
                action_scores = self.affine2(x)
                return F.softmax(action_scores, dim=1)

        self.checkTrace(Policy().to(device), (torch.rand(1, 4, device=device),),
                        export_import=test_export_import)

    def test_reinforcement_learning(self):
        self._test_reinforcement_learning(self, device='cpu')

    @unittest.skipIf(not RUN_CUDA, "no CUDA")
    def test_reinforcement_learning_cuda(self):
        # XXX: export_import on CUDA modules doesn't work (#11480)
        self._test_reinforcement_learning(self, device='cuda', test_export_import=False)

    @staticmethod
    def _test_snli(self, device, check_export_import=True):
        class Bottle(nn.Module):

            def forward(self, input):
                if len(input.size()) <= 2:
                    return super(Bottle, self).forward(input)
                size = input.size()[:2]
                out = super(Bottle, self).forward(input.view(size[0] * size[1], -1))
                return out.view(size[0], size[1], -1)

        class Linear(Bottle, nn.Linear):
            pass

        class Encoder(nn.Module):

            def __init__(self, config):
                super(Encoder, self).__init__()
                self.config = config
                input_size = config.d_proj if config.projection else config.d_embed
                dropout = 0 if config.n_layers == 1 else config.dp_ratio
                self.rnn = nn.LSTM(input_size=input_size, hidden_size=config.d_hidden,
                                   num_layers=config.n_layers, dropout=dropout,
                                   bidirectional=config.birnn)

            def forward(self, inputs):
                batch_size = inputs.size()[1]
                state_shape = self.config.n_cells, batch_size, self.config.d_hidden
                h0 = c0 = inputs.new_zeros(state_shape)
                outputs, (ht, ct) = self.rnn(inputs, (h0, c0))
                return ht[-1] if not self.config.birnn else ht[-2:].transpose(0, 1).contiguous().view(batch_size, -1)

        class SNLIClassifier(nn.Module):

            def __init__(self, config):
                super(SNLIClassifier, self).__init__()
                self.config = config
                self.embed = nn.Embedding(config.n_embed, config.d_embed)
                self.projection = Linear(config.d_embed, config.d_proj)
                self.encoder = Encoder(config)
                self.dropout = nn.Dropout(p=config.dp_ratio)
                self.relu = nn.ReLU()
                seq_in_size = 2 * config.d_hidden
                if self.config.birnn:
                    seq_in_size *= 2
                lin_config = [seq_in_size] * 2
                self.out = nn.Sequential(
                    Linear(*lin_config),
                    self.relu,
                    self.dropout,
                    Linear(*lin_config),
                    self.relu,
                    self.dropout,
                    Linear(*lin_config),
                    self.relu,
                    self.dropout,
                    Linear(seq_in_size, config.d_out))

            def forward(self, premise, hypothesis):
                prem_embed = self.embed(premise)
                hypo_embed = self.embed(hypothesis)
                if self.config.fix_emb:
                    prem_embed = prem_embed.detach()
                    hypo_embed = hypo_embed.detach()
                if self.config.projection:
                    prem_embed = self.relu(self.projection(prem_embed))
                    hypo_embed = self.relu(self.projection(hypo_embed))
                premise = self.encoder(prem_embed)
                hypothesis = self.encoder(hypo_embed)
                scores = self.out(torch.cat([premise, hypothesis], 1))
                return scores

        class Config:
            n_embed = 100
            d_embed = 100
            d_proj = 300
            dp_ratio = 0.0  # For deterministic testing TODO: change by fixing seed in checkTrace?
            d_hidden = 300
            birnn = True
            d_out = 300
            fix_emb = True
            projection = True
            n_layers = 2
            n_cells = 4  # 2 * n_layers because birnn = True

        premise = torch.LongTensor(48, 128).random_(0, 100).to(device)
        hypothesis = torch.LongTensor(24, 128).random_(0, 100).to(device)

        self.checkTrace(SNLIClassifier(Config()).to(device), (premise, hypothesis),
                        inputs_require_grads=False, export_import=check_export_import)

    @skipIfRocm
    def test_snli(self):
        self._test_snli(self, device='cpu')

    @skipIfRocm
    @unittest.skipIf(not RUN_CUDA, "no CUDA")
    def test_snli_cuda(self):
        # XXX: export_import on CUDA modules doesn't work (#11480)
        self._test_snli(self, device='cuda', check_export_import=False)

    @staticmethod
    def _test_super_resolution(self, device, check_export_import=True):
        import torch.nn.init as init

        class Net(nn.Module):

            def __init__(self, upscale_factor):
                super(Net, self).__init__()

                self.relu = nn.ReLU()
                self.conv1 = nn.Conv2d(1, 64, (5, 5), (1, 1), (2, 2))
                self.conv2 = nn.Conv2d(64, 64, (3, 3), (1, 1), (1, 1))
                self.conv3 = nn.Conv2d(64, 32, (3, 3), (1, 1), (1, 1))
                self.conv4 = nn.Conv2d(32, upscale_factor ** 2, (3, 3), (1, 1), (1, 1))
                self.pixel_shuffle = nn.PixelShuffle(upscale_factor)

            def forward(self, x):
                x = self.relu(self.conv1(x))
                x = self.relu(self.conv2(x))
                x = self.relu(self.conv3(x))
                x = self.pixel_shuffle(self.conv4(x))
                return x

        net = Net(upscale_factor=4).to(device)
        self.checkTrace(net, (torch.rand(5, 1, 64, 64, device=device),),
                        export_import=check_export_import)

    @skipIfRocm
    def test_super_resolution(self):
        self._test_super_resolution(self, device='cpu')

    @skipIfRocm
    @unittest.skipIf(not RUN_CUDA, 'no CUDA')
    def test_super_resolution_cuda(self):
        # XXX: export_import on CUDA modules doesn't work (#11480)
        self._test_super_resolution(self, device='cuda', check_export_import=False)

    @suppress_warnings
    def test_time_sequence_prediction(self):
        class Sequence(torch.jit.ScriptModule):
            def __init__(self):
                super(Sequence, self).__init__()
                self.lstm1 = nn.LSTMCell(1, 51)
                self.lstm2 = nn.LSTMCell(51, 51)
                self.linear = nn.Linear(51, 1)

            # TODO: could not pass tuple to a python Op and type annotations
            # is not descending to python signature, hence the wrapper
            # see https://github.com/pytorch/pytorch/issues/8778
            # and https://github.com/pytorch/pytorch/issues/8777
            def test_lstm1(self, input, hx, cx):
                # type: (Tensor, Tensor, Tensor) -> Tuple[Tensor, Tensor]
                return self.lstm1(input, (hx, cx))

            def test_lstm2(self, input, hx, cx):
                # type: (Tensor, Tensor, Tensor) -> Tuple[Tensor, Tensor]
                return self.lstm2(input, (hx, cx))

            # TODO: could not support tensor constructors in script
            # see https://github.com/pytorch/pytorch/issues/8814
            def test_tensor(self):
                return torch.tensor([], dtype=torch.double)

            @torch.jit.script_method
            def forward(self, input):
                # TODO: add future as input with default val
                # see https://github.com/pytorch/pytorch/issues/8724
                outputs = self.test_tensor()
                h_t = torch.zeros((3, 51), dtype=torch.double)
                c_t = torch.zeros((3, 51), dtype=torch.double)
                h_t2 = torch.zeros((3, 51), dtype=torch.double)
                c_t2 = torch.zeros((3, 51), dtype=torch.double)

                output = torch.zeros([3, 51])
                future = 2

                # TODO: chunk call should appear as the for loop iterable
                # We hard-code it to 4 for now.
                a, b, c, d = input.chunk(input.size(1), dim=1)
                for input_t in (a, b, c, d):
                    h_t, c_t = self.test_lstm1(input_t, h_t, c_t)
                    h_t2, c_t2 = self.test_lstm2(h_t, h_t2, c_t2)
                    output = self.linear(h_t2)
                    outputs = torch.cat((outputs, output), 1)
                for _ in range(future):  # if we should predict the future
                    h_t, c_t = self.test_lstm1(output, h_t, c_t)
                    h_t2, c_t2 = self.test_lstm2(h_t, h_t2, c_t2)
                    output = self.linear(h_t2)
                    outputs = torch.cat((outputs, output), 1)
                return outputs

        # TODO: toggle export_import once above issues are fixed
        self.checkTrace(Sequence(), (torch.rand(3, 4),),
                        export_import=False)

    @staticmethod
    def _test_vae(self, device, check_export_import=True):
        class VAE(nn.Module):
            def __init__(self):
                super(VAE, self).__init__()

                self.fc1 = nn.Linear(784, 400)
                self.fc21 = nn.Linear(400, 20)
                self.fc22 = nn.Linear(400, 20)
                self.fc3 = nn.Linear(20, 400)
                self.fc4 = nn.Linear(400, 784)

            def encode(self, x):
                h1 = F.relu(self.fc1(x))
                return self.fc21(h1), self.fc22(h1)

            def reparameterize(self, mu, logvar):
                if self.training:
                    std = torch.exp(0.5 * logvar)
                    eps = torch.randn_like(std)
                    return eps.mul(std).add_(mu)
                else:
                    return mu

            def decode(self, z):
                h3 = F.relu(self.fc3(z))
                return torch.sigmoid(self.fc4(h3))

            def forward(self, x):
                mu, logvar = self.encode(x.view(-1, 784))
                z = self.reparameterize(mu, logvar)
                return self.decode(z), mu, logvar

        # eval() is present because randn_like makes this nondeterministic
        self.checkTrace(VAE().to(device).eval(), (torch.rand(128, 1, 28, 28, device=device),),
                        export_import=check_export_import)

    def test_vae(self):
        self._test_vae(self, device='cpu')

    @unittest.skipIf(not RUN_CUDA, "no CUDA")
    def test_vae_cuda(self):
        # XXX: export_import on CUDA modules doesn't work (#11480)
        self._test_vae(self, device='cuda', check_export_import=False)


# Smoke tests for export methods
class TestPytorchExportModes(JitTestCase):
    class MyModel(nn.Module):
        def __init__(self):
            super(TestPytorchExportModes.MyModel, self).__init__()

        def forward(self, x):
            return x.transpose(0, 1)

    def test_protobuf(self):
        torch_model = TestPytorchExportModes.MyModel()
        fake_input = Variable(torch.randn(1, 1, 224, 224), requires_grad=True)
        f = io.BytesIO()
        torch.onnx._export(torch_model, (fake_input), f, verbose=False,
                           export_type=torch.onnx.ExportTypes.PROTOBUF_FILE)

    def test_zipfile(self):
        torch_model = TestPytorchExportModes.MyModel()
        fake_input = Variable(torch.randn(1, 1, 224, 224), requires_grad=True)
        f = io.BytesIO()
        torch.onnx._export(torch_model, (fake_input), f, verbose=False,
                           export_type=torch.onnx.ExportTypes.ZIP_ARCHIVE)

    def test_compressed_zipfile(self):
        torch_model = TestPytorchExportModes.MyModel()
        fake_input = Variable(torch.randn(1, 1, 224, 224), requires_grad=True)
        f = io.BytesIO()
        torch.onnx._export(torch_model, (fake_input), f, verbose=False,
                           export_type=torch.onnx.ExportTypes.COMPRESSED_ZIP_ARCHIVE)

    def test_directory(self):
        torch_model = TestPytorchExportModes.MyModel()
        fake_input = Variable(torch.randn(1, 1, 224, 224), requires_grad=True)
        d = tempfile.mkdtemp()
        torch.onnx._export(torch_model, (fake_input), d, verbose=False,
                           export_type=torch.onnx.ExportTypes.DIRECTORY)
        shutil.rmtree(d)

    @skipIfRocm
    def test_aten_fallback(self):
        class ModelWithAtenNotONNXOp(nn.Module):
            def forward(self, x, y):
                abcd = x + y
                defg = torch.qr(abcd)
                return defg

        x = torch.rand(3, 4)
        y = torch.rand(3, 4)
        f = io.BytesIO()
        exported = torch.onnx.export_to_pretty_string(
            ModelWithAtenNotONNXOp(), (x, y), f,
            operator_export_type=OperatorExportTypes.ONNX_ATEN_FALLBACK)
        self.assertExpected(exported)


# known to be failing in tracer
EXCLUDE_TRACED = {
    'test_split_dim',
    'test_split_dim_neg0',
}

EXCLUDE_TYPE_CHECK = {
    # slogdet tests use itemgetter to select its only differentiable output,
    # but this happens outside of the graph we handle, so there are fewer
    # reference outputs than graph outputs.
    'test_slogdet_1x1_neg_det',
    'test_slogdet_1x1_pos_det',
    'test_slogdet_distinct_singular_values',
    'test_slogdet_neg_det',
    'test_slogdet_pos_det',
    'test_slogdet_symmetric',
    'test_slogdet_symmetric_pd',
}

# known to be failing in script
EXCLUDE_SCRIPT = {
    # TODO: Fix var/std
    # there are two schemas for var (and std):
    # (1) var(Tensor, int, *, bool, bool, Tensor)
    # (2) var(Tensor, *, bool)
    #
    # Right now, the following is happening:
    # - Shorter schemas come before longer schemas
    # - bool, int are treated as IntType rather than DynamicType like before
    # So the schemas look like the following in operator:
    # (2) var(DynamicType, IntType)
    # (1) var(DynamicType, IntType, IntType, DynamicType)
    # Now, when one calls torch.var(tensor, dim=1), the compiler mistakingly
    # matches it with (2) instead of (1), which is a problem.
    'test_std_dim',
    'test_std_dim_1d',
    'test_std_dim_1d_neg0',
    'test_std_dim_neg0',
    'test_var_dim',
    'test_var_dim_1d',
    'test_var_dim_1d_neg0',
    'test_var_dim_neg0',
    'test_matrix_power_n=-1',  # involves inverse
    'test_matrix_power_n=-3',  # involves inverse
    # skipped nn functional tests
    # ops involves sampling which could not test
    'test_nn_dropout',
    'test_nn_alpha_dropout',
    'test_nn_dropout2d',
    'test_nn_dropout3d',
    'test_nn_feature_alpha_dropout',

    'test_nn_adaptive_max_pool1d',
    'test_nn_adaptive_max_pool2d',
    'test_nn_adaptive_max_pool3d',
    'test_nn_ctc_loss',


    # argument has custom behavior
    'test_nn_fractional_max_pool2d',
    'test_nn_max_unpool3d',
    'test_nn_embedding',
    'test_nn_embedding_bag',
    'test_nn_batch_norm',

    # aten op has additional cudnn argument
    'test_nn_group_norm',
    'test_nn_nll_loss',
    'test_nn_unfold',
    'test_nn_max_unpool2d',

    # argument type not supported
    'test_nn_affine_grid',

    # unknown builtin op
    'test_nn_tanhshrink',
    'test_nn_softsign',
    'test_nn_softmin',
    'test_nn_local_response_norm',
    'test_nn_poisson_nll_loss',
    'test_nn_cross_entropy',
    'test_nn_binary_cross_entropy_with_logits',
    'test_nn_multilabel_soft_margin_loss',
    'test_nn_pixel_shuffle',
    'test_nn_interpolate',
    'test_nn_pad',
    'test_nn_cosine_similarity',
    'test_nn_normalize',
    'test_nn_fold',
    'test_nn_linear',
    'test_nn_max_unpool1d',
    'test_nn_lp_pool1d',
    'test_nn_lp_pool2d',
    'test_nn_instance_norm',
    'test_nn_grid_sample',
    'test_nn_gumbel_softmax',
}


# make a new function where all non-tensor arguments in 'args' have been partially
# applied, and all tensor arguments remain.
# used to trace functions when some arguments are not tensors
def partial_apply_nontensors(fn, args, **kwargs):
    source = ['t' if isinstance(arg, torch.Tensor) else 's' for arg in args]

    def new_fn(*tensors_):
        tensors = iter(tensors_)
        return fn(*(args[i] if s == 's' else next(tensors) for i, s in enumerate(source)), **kwargs)

    return new_fn, [arg for arg in args if isinstance(arg, torch.Tensor)]


# create a trace function from input fn
def create_traced_fn(self, fn):
    def traced_fn(*inputs, **kwargs):
        fn_tensors, inputs_tensors = partial_apply_nontensors(fn, inputs, **kwargs)
        traced = torch.jit.trace(fn_tensors, inputs_tensors)
        self.assertExportImport(traced.graph, inputs_tensors)
        output = traced(*inputs_tensors)
        traced_fn.last_graph = traced.graph_for(*inputs_tensors)
        return output
    return traced_fn

script_template = '''
def the_method({}):
    return {}
'''


def get_constant(x):
    if x == inf or x == -inf:
        return 'float(\'inf\')' if PY2 else 'math.inf'
    return x


# create a script function from (name, func_type, output_process_fn),
# returns a function takes in (args, kwargs) and runs the compiled function and
# then applies the post process fn to the outputs
def create_script_fn(self, method_name, func_type, output_process_fn):
    def script_fn(*args, **kwargs):
        formals = []
        tensors = []
        actuals = []
        for arg in args:
            if isinstance(arg, torch.Tensor):
                name = 'i{}'.format(len(formals))
                formals.append(name)
                actuals.append(name)
                tensors.append(arg)
            else:
                actuals.append(str(get_constant(arg)))
        kwargs_str = ''
        for k, v in kwargs.items():
            kwargs_str += ', ' + k + '=' + str(v)
        if func_type == 'functional':
            call = 'torch.{}({}{})'.format(method_name, ', '.join(actuals), kwargs_str)
        elif func_type == 'method':
            call = '{}.{}({}{})'.format(actuals[0], method_name, ', '.join(actuals[1:]), kwargs_str)
        elif func_type == 'nn_functional':
            call = 'torch.nn.functional.{}({}{})'.format(method_name, ', '.join(actuals), kwargs_str)
        else:
            raise 'Unsupported function type'

        script = script_template.format(', '.join(formals), call)

        # for math.inf
        import math

        CU = torch.jit.CompilationUnit(script)
        self.assertExportImport(CU.the_method.graph, tensors)
        output = output_process_fn(CU.the_method(*tensors))
        script_fn.last_graph = CU.the_method.graph_for(*tensors)
        return output
    return script_fn


def check_output_types(self, func, ref_outputs, args, kwargs):
    graph = getattr(func, 'last_graph', None)
    if not isinstance(ref_outputs, tuple):
        ref_outputs = (ref_outputs,)
    types = [o.type() for o in graph.outputs()]
    self.assertEqual(len(types), len(ref_outputs))
    for i, (t, ref_out) in enumerate(zip(types, ref_outputs)):
        if isinstance(ref_out, list):
            assert len(ref_out) > 0
            elem = ref_out[0]
            assert isinstance(elem, torch.Tensor)
            self.assertTrue(t.isSubtypeOf(torch._C.ListType.ofTensors()))
        else:
            ref_type = torch._C.Type.inferFrom(ref_out)
            self.assertTrue(ref_type.isSubtypeOf(t))


def check_against_reference(self, func, reference_func, args, kwargs=None, allow_unused=True, check_types=True):
    kwargs = kwargs if kwargs else {}

    def allSum(vs):
        if isinstance(vs, torch.Tensor):
            vs = (vs,)
        return sum([(i + 1) * v.sum()
                    for i, v in enumerate(vs)
                    if v is not None and v.dtype.is_floating_point])

    def clone_inputs(requires_grad):
        inputs = [
            arg.detach().clone().requires_grad_(requires_grad and arg.requires_grad)
            if isinstance(arg, torch.Tensor) else arg for arg in args
        ]
        return inputs, [input for input in inputs if isinstance(input, torch.Tensor) and input.requires_grad]

    nograd_inputs, nograd_tensors = clone_inputs(False)
    recording_inputs, recording_tensors = clone_inputs(True)

    # test no gradients case
    outputs = reference_func(*nograd_inputs, **kwargs)
    outputs_test = func(*nograd_inputs, **kwargs)
    self.assertEqual(outputs, outputs_test)

    if check_types:
        check_output_types(self, func, outputs_test, nograd_inputs, kwargs)

    # test single grad case
    outputs = reference_func(*recording_inputs, **kwargs)
    grads = torch.autograd.grad(allSum(outputs), recording_tensors,
                                allow_unused=allow_unused)

    outputs_test = func(*recording_inputs, **kwargs)
    grads_test = torch.autograd.grad(allSum(outputs_test), recording_tensors,
                                     allow_unused=allow_unused)
    self.assertEqual(outputs, outputs_test)
    self.assertEqual(grads, grads_test)

    # test the grad grad case
    if self._testMethodName in nn_functional_single_grad:
        return

    outputs = reference_func(*recording_inputs, **kwargs)
    l1 = allSum(outputs)
    grads = torch.autograd.grad(l1, recording_tensors, create_graph=True,
                                allow_unused=allow_unused)
    l2 = (allSum(grads) * l1)
    grads2 = torch.autograd.grad(l2, recording_tensors, allow_unused=allow_unused)

    recording_inputs, recording_tensors = clone_inputs(True)

    outputs_test = func(*recording_inputs, **kwargs)
    l1_test = allSum(outputs_test)
    grads_test = torch.autograd.grad(
        l1_test, recording_tensors, create_graph=True, allow_unused=allow_unused)
    l2_test = (allSum(grads_test) * l1_test)
    grads2_test = torch.autograd.grad(l2_test, recording_tensors, allow_unused=allow_unused)

    self.assertEqual(outputs, outputs_test)
    self.assertEqual(grads, grads_test)
    for g2, g2_test in zip(grads2, grads2_test):
        if g2 is None and g2_ge is None:
            continue
        self.assertTrue(torch.allclose(g2, g2_test, atol=5e-4, rtol=1e-4))


class TestJitGenerated(JitTestCase):
    pass


class TestCustomOperators(JitTestCase):

    def test_dynamic_op_registry(self):
        from torch._ops import _OpNamespace
        self.assertTrue(hasattr(torch, 'ops'))

        torch.ops.__dict__.pop('aten')

        # Don't use `hasattr()` because it will call `__getattr__`.
        self.assertNotIn('aten', torch.ops.__dict__)
        torch.ops.aten
        self.assertIn('aten', torch.ops.__dict__)
        self.assertEqual(type(torch.ops.aten), _OpNamespace)

        self.assertNotIn('relu', torch.ops.aten.__dict__)
        op = torch.ops.aten.relu
        self.assertTrue(callable(op))
        self.assertIn('relu', torch.ops.aten.__dict__)
        op2 = torch.ops.aten.relu
        self.assertEqual(op, op2)

    def test_simply_calling_an_operator(self):
        input = torch.randn(100)
        output = torch.ops.aten.relu(input)
        self.assertEqual(output, input.relu())

    def test_default_arguments_are_used(self):
        output = torch.ops.aten.leaky_relu(torch.tensor([-1.0, 1.0]))
        self.assertEqual(output, torch.tensor([-0.01, 1]))

    def test_only_kwargs(self):
        output = torch.ops.aten.leaky_relu(self=torch.tensor(-1.0))
        self.assertEqual(output, torch.tensor(-0.01))

    def test_passing_too_many_args(self):
        with self.assertRaisesRegex(
            RuntimeError,
            "aten::relu\(\) expected at most 1 argument\(s\) but received 2 argument\(s\)"
        ):
            torch.ops.aten.relu(1, 2)

    def test_passing_too_few_args(self):
        with self.assertRaisesRegex(
            RuntimeError,
            "aten::relu\(\) is missing value for argument 'self'."
        ):
            torch.ops.aten.relu()

    def test_passing_one_positional_but_not_the_second(self):
        with self.assertRaisesRegex(
            RuntimeError,
            "aten::log_softmax\(\) is missing value for argument 'dim'."
        ):
            torch.ops.aten.log_softmax(torch.ones(5))

    def test_passing_an_argument_both_as_positional_and_kwarg(self):
        with self.assertRaisesRegex(
            RuntimeError,
            "Argument 'self' specified both as positional and keyword argument"
        ):
            torch.ops.aten.leaky_relu(torch.ones(5), self=torch.ones(5))

    def test_passing_unknown_kwargs(self):
        with self.assertRaisesRegex(
            RuntimeError,
            "Unknown keyword argument 'foo' for operator 'aten::leaky_relu'"
        ):
            torch.ops.aten.leaky_relu(torch.ones(5), foo=torch.ones(5))

    def test_passing_and_returning_lists(self):
        # Replace with actual test once we support lists.
        a, b = torch.rand(5), torch.rand(5)
        output = torch.ops.aten.cat([a, b])
        output_ref = torch.cat([a, b])
        self.assertEqual(output, output_ref)

    def test_script_graph_contains_custom_op(self):
        @torch.jit.script
        def func(x):
            return torch.ops.aten.relu(x)
        self.assertExpected(canonical(func.graph))

# UBSAN per-function exclusions don't seem to work with OpenMP pragmas,
# and we have to disable the failing tests here instead.
UBSAN_BLACKLISTED_TESTS = [
    "test___rdiv___constant",
    "test___rdiv___scalar_constant",
    "test_addcdiv",
    "test_addcdiv_broadcast_all",
    "test_addcdiv_broadcast_rhs",
    "test_addcdiv_scalar",
    "test_addcdiv_scalar_broadcast_lhs",
    "test_addcdiv_scalar_broadcast_rhs",
    "test_addcdiv_scalar_scale",
    "test_addcdiv_scalar_scale_broadcast_lhs",
    "test_addcdiv_scalar_scale_broadcast_rhs",
    "test_addcdiv_scale",
    "test_addcdiv_scale_broadcast_all",
    "test_addcdiv_scale_broadcast_rhs",
    "test_add_broadcast_all",
    "test_add_broadcast_lhs",
    "test_add_broadcast_rhs",
    "test_add_constant",
    "test_add_scalar",
    "test_add_scalar_broadcast_lhs",
    "test_add_scalar_broadcast_rhs",
    "test_div",
    "test_div_broadcast_all",
    "test_div_broadcast_lhs",
    "test_div_broadcast_rhs",
    "test_div_scalar",
    "test_div_scalar_broadcast_lhs",
    "test_div_scalar_broadcast_rhs",
    "test_rsqrt",
    "test_rsqrt_scalar",
    "test_add",
    "test_reciprocal",
    "test_reciprocal_scalar",
]

L = 20
M = 10
S = 5

# NB: JIT script tests for all nn functional interfaces, script mode does
# not support in_place operations yet, so no inplace operation tests added.
# removed all the deprecated functions
#
# (
#   method name,
#   input size/constructing fn,
#   args (tuple represents shape of a tensor arg),
#   test variant name (will be used at test name suffix),    // optional
#   indices for possible dim arg,                            // optional
#   fn mapping output to part that should be gradcheck'ed,   // optional
# )
nn_functional_tests = [
    # TODO: default arguments for None type not supported, add
    # manually as argument, remove when ATen default arg system ready
    ('conv1d', (S, S, S), ((S, S, S), None)),
    ('conv2d', (S, S, S, S), ((S, S, S, S), None)),
    ('conv3d', (S, S, S, S, S), ((S, S, S, S, S), None)),
    ('conv_transpose1d', (S, S, S), ((S, S, S), None)),
    ('conv_transpose2d', (S, S, S, S), ((S, S, S, S), None)),
    ('conv_transpose3d', (S, S, S, S, S), ((S, S, S, S, S), None)),
    ('conv_tbc', (S, S, S), ((S, S, S), (S,), 2)),
    ('avg_pool1d', (S, S, S), (3,)),
    ('avg_pool2d', (S, S, S, S), (3,)),
    ('avg_pool3d', (S, S, S, S, S), (3,)),
    ('fractional_max_pool2d', (S, S, S, S), (3, [2, 3], None)),
    ('max_pool1d', (S, S, S), (2, 1)),
    ('max_pool2d', (S, S, S, S), (2, 1)),
    ('max_pool3d', (S, S, S, S, S), (2, 1)),
    ('max_unpool1d', torch.tensor([[[2., 4]]]), (torch.tensor([[[1, 3]]]), 2, 2, 0)),
    ('max_unpool2d', torch.tensor([[[[2., 4]]]]), (torch.tensor([[[[1, 3]]]]), 2, 2, 0)),
    ('max_unpool3d', torch.tensor([[[[[2., 4]]]]]), (torch.tensor([[[[[1, 3]]]]]), 2, 2, 0)),
    ('lp_pool1d', (S, S, S), (2, 3, 2,)),
    ('lp_pool2d', (S, S, S, S), (2, 3, 2,)),
    ('adaptive_max_pool1d', (S, S, S), (5,)),
    ('adaptive_max_pool2d', (S, S, S, S), ([5, 7],)),
    ('adaptive_max_pool3d', (S, S, S, S, S), ([3, 2, 2],)),
    ('adaptive_avg_pool1d', (S, S, S), (5,)),
    ('adaptive_avg_pool2d', (S, S, S, S), ([5, 7],)),
    ('adaptive_avg_pool3d', (S, S, S, S, S), ([3, 2, 2],)),
    ('dropout', (S, S, S), (0.5,)),
    ('alpha_dropout', (S, S, S), (0.5,)),
    ('dropout2d', (S, S, S), (0.5,)),
    ('dropout3d', (S, S, S), (0.5,)),
    ('feature_alpha_dropout', (S, S, S), (0.5,)),
    ('threshold', (S, S, S), (0.1, 2),),
    ('relu', (S, S, S), (),),
    ('glu', (S - 1, S - 1, S - 1), (),),
    ('hardtanh', (S, S, S), (-0.5, 0.5),),
    ('elu', (S, S, S), (0.9,),),
    ('selu', (S, S, S), (),),
    ('celu', (S, S, S), (0.9,),),
    ('leaky_relu', (S, S, S), (0.02,),),
    ('rrelu', (S, S), (0.1, 0.3, False, None),),
    ('hardshrink', (S, S, S), (0.4,),),
    ('tanhshrink', (S, S, S), (),),
    ('softsign', (S, S, S), (),),
    ('softplus', (S, S, S), (),),
    ('softmin', (S, S, S), (0,),),
    ('softmax', (S, S, S), (0,),),
    ('log_softmax', (S, S, S), (0,),),
    ('linear', (S, S), ((M, S), None),),
    ('bilinear', (S, S, S), ((S, S, M), torch.zeros(M, S, M), None),),
    ('embedding', torch.tensor([[1, 2, 4, 5], [4, 3, 2, 5]]), (torch.rand(6, 3), ),),
    ('embedding_bag', torch.tensor([1, 2, 4, 2]), (torch.rand(5, 3), torch.tensor([0, 4]),),),
    ('batch_norm', (S, S), (non_differentiable(torch.randn(S)), non_differentiable(torch.ones(S)), ),),
    ('instance_norm', (S, S, S), (non_differentiable(torch.zeros(S)), non_differentiable(torch.ones(S))),),
    ('layer_norm', (S, S, S, S), ([5], None, None),),
    ('group_norm', (S, S, S), (1, torch.Tensor(5), None),),
    ('local_response_norm', (S, S, S), (2, ),),
    ('nll_loss', F.log_softmax(torch.randn(3, 5), dim=0), (torch.tensor([1, 0, 4]), None, None),),
    ('poisson_nll_loss', (S, 2), ((S, 2),),),
    ('kl_div', F.log_softmax(torch.randn(S, 10), 1), (F.softmax(torch.randn(S, 10), 1),),),
    ('cross_entropy', (3, S), (torch.randint(S, (3,), dtype=torch.int64),),),
    ('binary_cross_entropy_with_logits', (3,), (torch.empty(3).random_(2), ),),
    ('smooth_l1_loss', (3, S), (non_differentiable(torch.rand(3, S)),),),
    ('l1_loss', (3, S), (non_differentiable(torch.rand(3, S)),),),
    ('mse_loss', (3, S), (non_differentiable(torch.rand(3, S)),),),
    ('margin_ranking_loss', (3, S), ((3, S), (S,)),),
    ('hinge_embedding_loss', (3, S), (non_differentiable(torch.rand(3, S)),),),
    ('soft_margin_loss', (3, S), (non_differentiable(torch.rand(3, S)),),),
    ('multilabel_soft_margin_loss', (3, S), (non_differentiable(torch.rand(3, S)),),),
    ('cosine_embedding_loss', (S, S), ((S, S), non_differentiable(torch.rand(S,))),),
    ('pixel_shuffle', (1, 9, 4, 4), (3,),),
    ('interpolate', torch.zeros(3, 3).view(1, 1, 3, 3), (2,),),
    ('affine_grid', (S, 2, 3), (torch.Size([S, 1, 7, 7]),),),
    ('pad', (3, 3, 4, 2), ([1, 1],),),
    ('pairwise_distance', (S, S), ((S, S),),),
    ('pdist', (S, S), (),),
    ('cosine_similarity', (S, S), ((S, S),),),
    ('triplet_margin_loss', (S, S), ((S, S), (S, S)),),
    ('normalize', (S, S, S), (),),
    ('unfold', (S, S, S, S), ([2, 3]),),
    ('fold', (1, 3 * 2 * 2, 12), ([4, 5], [2, 2]),),
    ('grid_sample', (S, S, S, S), (non_differentiable(torch.rand(S, S, S, 2)),),),
    ('gumbel_softmax', (S, S), (2,),),
    ('multilabel_margin_loss', torch.tensor([[0.2, -0.2, 0.07]]), (torch.tensor([[0, 0, 1]]),),),
    ('multi_margin_loss', (S, S), (non_differentiable(torch.randint(S, (S, ), dtype=torch.int64)), \
                                   1, 1, non_differentiable(torch.randn(S))),),
    ('binary_cross_entropy', torch.randn(3, 2).sigmoid(), (non_differentiable(torch.rand(3, 2)), \
                                                           non_differentiable(torch.randn(3, 2))),),
    ('ctc_loss', torch.randn(S, S, S).log_softmax(2).detach().requires_grad_(), \
     (torch.randint(1, S + 1, (S, S), dtype=torch.long), torch.full((S,), S, dtype=torch.long), \
      torch.randint(1, S, (S,), dtype=torch.long))),
]


# Test names in this set are only checked for a single derivative
nn_functional_single_grad = frozenset('test_nn_' + name for name in [
    'pdist',
    'multilabel_margin_loss',
    'max_unpool3d',
    'multi_margin_loss',
    'binary_cross_entropy',
    'ctc_loss',
])


def add_test(
        name,
        self_size,
        args,
        variant_name='',
        dim_args_idx=(),
        skipTestIf=(),
        output_process_fn=lambda x: x,
        kwargs=None):
    basic_test_name = 'test_' + name
    if variant_name != '':
        basic_test_name += '_' + variant_name

    for dim_perm in product([-1, 1], repeat=len(dim_args_idx)):
        test_name = basic_test_name
        new_args = [arg * dim_perm[dim_args_idx.index(i)] if i in dim_args_idx else arg for i, arg in enumerate(args)]
        test_name = basic_test_name + ''.join('_neg' + str(i) for i, idx in enumerate(dim_perm) if idx < 0)
        new_args = tuple(new_args)

        # for-loop bodies don't define scopes, so we have to save the variables
        # we want to close over in some way
        def do_test(self, name=name, self_size=self_size, args=new_args, test_name=test_name,
                    output_process_fn=output_process_fn):
            def check(name):
                is_magic_method = name[:2] == '__' and name[-2:] == '__'
                is_inplace = name[-1] == "_" and not is_magic_method
                self_variable = create_input((self_size,))[0][0]
                # FixMe: run grad checks on inplace self
                if is_inplace:
                    self_variable.requires_grad = False
                # need to record this because methods can change the size (e.g. unsqueeze)
                args_variable, kwargs_variable = create_input(args, requires_grad=not is_inplace, call_kwargs=kwargs)
                self_tensor = deepcopy(self_variable.data)
                args_tensor = deepcopy(unpack_variables(args_variable))
                output_variable = getattr(self_variable, name)(*args_variable, **kwargs_variable)

                def fn(*inputs, **kwargs):
                    output = getattr(inputs[0], name)(*inputs[1:], **kwargs)
                    return output_process_fn(output)

                check_types = test_name not in EXCLUDE_TYPE_CHECK

                if not is_inplace and name not in EXCLUDE_GRADCHECK and not exclude_tensor_method(name, test_name):
                    if test_name not in EXCLUDE_TRACED:
                        check_against_reference(self, create_traced_fn(self, fn),
                                                fn, (self_variable,) + args_variable, kwargs_variable,
                                                check_types=check_types)

                    if not is_magic_method and test_name not in EXCLUDE_SCRIPT:
                        check_against_reference(self,
                                                create_script_fn(self, name, 'method', output_process_fn),
                                                fn, (self_variable,) + args_variable, kwargs_variable,
                                                check_types=check_types)

                # functional interface tests
                if hasattr(torch, name) and name not in EXCLUDE_FUNCTIONAL:
                    def fn(*inputs, **kwargs):
                        output = getattr(torch, name)(*inputs, **kwargs)
                        return output_process_fn(output)

                    f_args_variable = (self_variable,) + args_variable
                    f_args_tensor = (self_tensor,) + args_tensor

                    if not is_inplace and test_name not in EXCLUDE_TRACED:
                        check_against_reference(self, create_traced_fn(self, fn), fn,
                                                f_args_variable, kwargs_variable, check_types=check_types)

                    if not is_inplace and test_name not in EXCLUDE_SCRIPT:
                        check_against_reference(self,
                                                create_script_fn(self, name, 'functional', output_process_fn),
                                                fn, f_args_variable, kwargs_variable,
                                                check_types=check_types)

            check(name)
            inplace_name = name + '_'
            # can't broadcast inplace to left hand side
            broadcast_skip_inplace = 'broadcast_lhs' in test_name or 'broadcast_all' in test_name
            if hasattr(torch.ones(1), inplace_name) and not broadcast_skip_inplace:
                check(inplace_name)

        post_add_test(test_name, skipTestIf, do_test)


def add_nn_test(name, self_size, args, skipTestIf=(), output_process_fn=lambda x: x, kwargs=None):
    test_name = 'test_nn_' + name

    def do_test(self, name=name, args=args, test_name=test_name):
        torch.manual_seed(2)

        self_variable = create_input((self_size,))[0][0]

        # need to record this because methods can change the size (e.g. unsqueeze)
        args_variable, kwargs_variable = create_input(args, call_kwargs=kwargs)

        self_tensor = deepcopy(self_variable.data)
        args_tensor = deepcopy(unpack_variables(args_variable))

        output_variable = getattr(F, name)(self_variable, *args_variable, **kwargs_variable)

        def fn(*inputs, **kwargs):
            output = getattr(F, name)(*inputs, **kwargs)
            return output_process_fn(output)

        f_args_variable = (self_variable,) + args_variable
        f_args_tensor = (self_tensor,) + args_tensor

        if test_name not in EXCLUDE_SCRIPT:
            check_against_reference(self,
                                    create_script_fn(self, name, 'nn_functional', output_process_fn),
                                    fn, f_args_variable, kwargs_variable)

    post_add_test(test_name, skipTestIf, do_test)


def post_add_test(test_name, skipTestIf, do_test):
    assert not hasattr(TestJitGenerated, test_name), 'Two tests have the same name: ' + test_name

    for skip in skipTestIf:
        do_test = skip(do_test)

    if not (TEST_WITH_UBSAN and test_name in UBSAN_BLACKLISTED_TESTS):
        setattr(TestJitGenerated, test_name, do_test)


for test in method_tests:
    add_test(*test)

for test in nn_functional_tests:
    add_nn_test(*test)

if __name__ == '__main__':
    run_tests()
