import numpy

from chainer import _ideep
from chainer import cuda
from chainer import function_node
import chainer.functions
from chainer.utils import type_check


class LinearFunction(function_node.FunctionNode):

    _ideep_hint = None

    def check_type_forward(self, in_types):
        n_in = in_types.size()
        type_check.expect(2 <= n_in, n_in <= 3)
        x_type, w_type = in_types[:2]

        type_check.expect(
            x_type.dtype.kind == 'f',
            w_type.dtype.kind == 'f',
            x_type.ndim == 2,
            w_type.ndim == 2,
            x_type.shape[1] == w_type.shape[1],
        )
        if type_check.eval(n_in) == 3:
            b_type = in_types[2]
            type_check.expect(
                b_type.dtype == x_type.dtype,
                b_type.ndim == 1,
                b_type.shape[0] == w_type.shape[0],
            )

    def forward(self, inputs):
        if (all([v.dtype == numpy.float32 for v in inputs])
                and cuda.get_array_module(*inputs) is numpy
                and _ideep.should_use_ideep('>=auto')):

            # iDeep implementation
            return self._forward_ideep(inputs)

        # Generic implementation
        if len(inputs) == 3:
            x, W, b = inputs
        else:
            (x, W), b = inputs, None

        # NumPy raises an error when the array is not contiguous.
        # See: https://github.com/chainer/chainer/issues/2744
        # TODO(niboshi): Remove this code when NumPy is fixed.
        if (isinstance(x, numpy.ndarray) and
                not (x.flags.c_contiguous or x.flags.f_contiguous) and
                1 in x.shape):
            x = numpy.ascontiguousarray(x)

        y = x.dot(W.T).astype(x.dtype, copy=False)
        if b is not None:
            y += b
        self.retain_inputs((0, 1))  # b is not retained
        return y,

    def _forward_ideep(self, inputs):
        cc = _ideep.ideep.xnn.LinearForward(inputs)
        self._ideep_hint = cc.hint
        self._ideep_W = cc.W

        y, = cc.execute_on()
        y.reset_buf_order()

        if len(inputs) == 3:
            self.retain_inputs((0, 1, 2))
        else:
            self.retain_inputs((0, 1))
        return y,

    def backward(self, indexes, grad_outputs):
        ret = []
        gy, = grad_outputs

        if self._ideep_hint is not None:

            # iDeep implementation
            inputs = self.get_retained_inputs()
            input_data = [v.data for v in inputs]
            x, W = inputs[:2]

            if 0 in indexes:  # grad_x
                gx = LinearGradDIdeep(
                    self._ideep_hint, self._ideep_W, input_data).apply((W, gy))
                ret.append(gx[0])
            if 1 in indexes or 2 in indexes:
                gW_b = LinearGradWIdeep(
                    self._ideep_hint, input_data).apply((x, gy))
                if 1 in indexes:  # grad_W
                    ret.append(gW_b[0])
                if 2 in indexes:  # grad_b
                    ret.append(gW_b[1])
        else:
            # Generic implementation
            inputs = self.get_retained_inputs()
            x, W = inputs
            if 0 in indexes:  # grad_x
                gx = linear(gy, W.T)
                ret.append(chainer.functions.cast(gx, x.dtype))
            if 1 in indexes:  # grad_W
                gW = linear(gy.T, x.T)
                ret.append(chainer.functions.cast(gW, W.dtype))
            if 2 in indexes:  # grad_b
                gb = chainer.functions.sum(gy, axis=0)
                ret.append(gb)

        return ret


class LinearGradDIdeep(function_node.FunctionNode):

    def __init__(self, hint, ccW, input_data):
        super(LinearGradDIdeep, self).__init__()

        self._input_data = input_data
        self.W = ccW
        self.hint = hint

    def forward_cpu(self, inputs):
        W, gy = inputs

        cc = _ideep.ideep.xnn.LinearBackwardData(
            self._input_data, (gy,), self.hint, self.W)

        gx = cc.execute_on()
        gx[0].reset_buf_order()

        self.retain_inputs((0, 1))
        return gx

    def backward(self, indexes, grad_outputs):
        inputs = self.get_retained_inputs()
        W, gy = inputs
        ggx, = grad_outputs

        ret = []
        if 0 in indexes:  # grad_W
            gg = linear(gy.T, ggx.T)
            ret.append(gg)
        if 1 in indexes:  # grad_gy
            gg = linear(ggx, W)
            ret.append(gg)

        return ret


class LinearGradWIdeep(function_node.FunctionNode):

    def __init__(self, hint, input_data):
        super(LinearGradWIdeep, self).__init__()

        self._input_data = input_data
        self.hint = hint

    def forward_cpu(self, inputs):
        x, gy = inputs
        cc = _ideep.ideep.xnn.LinearBackwardWeighs(
            self._input_data, (gy,), self.hint)

        gW_b = cc.execute_on()
        gW_b[0].reset_buf_order()
        self.retain_inputs((0, 1))

        return gW_b

    def backward(self, indexes, grad_outputs):
        inputs = self.get_retained_inputs()
        x, gy = inputs
        if len(grad_outputs) == 2:
            ggW, ggb = grad_outputs
        else:
            ggW, = grad_outputs
            ggb = None

        ret = []
        if 0 in indexes:  # grad_x
            gg = linear(gy, ggW.T)
            ret.append(gg)
        if 1 in indexes:  # grad_gy
            gg = linear(x, ggW)
            if ggb is not None:
                gg += chainer.functions.broadcast_to(ggb, gg.shape)
            ret.append(gg)

        return ret


def linear(x, W, b=None):
    """Linear function, or affine transformation.

    It accepts two or three arguments: an input minibatch ``x``, a weight
    matrix ``W``, and optionally a bias vector ``b``. It computes

    .. math:: Y = xW^\\top + b.

    Args:
        x (:class:`~chainer.Variable` or :class:`numpy.ndarray` or \
        :class:`cupy.ndarray`): Input variable, which is a :math:`(s_B, s_1, \
            s_2, ..., s_n)`-shaped float array. Its first dimension
            :math:`(s_B)` is assumed to be the *minibatch dimension*. The
            other dimensions are treated as concatenated one dimension whose
            size must be :math:`(s_1 * ... * s_n = N)`.
        W (:class:`~chainer.Variable` or :class:`numpy.ndarray` or \
        :class:`cupy.ndarray`): Weight variable of shape :math:`(M, N)`,
            where :math:`(N = s_1 * ... * s_n)`.
        b (:class:`~chainer.Variable` or :class:`numpy.ndarray` or \
        :class:`cupy.ndarray`): Bias variable (optional) of shape
            :math:`(M,)`.

    Returns:
        ~chainer.Variable: Output variable. A float array with shape
        of :math:`(s_B, M)`.

    .. seealso:: :class:`~chainer.links.Linear`

    .. admonition:: Example

        >>> x = np.random.uniform(0, 1, (3, 4)).astype('f')
        >>> W = np.random.uniform(0, 1, (5, 4)).astype('f')
        >>> b = np.random.uniform(0, 1, (5,)).astype('f')
        >>> y = F.linear(x, W, b)
        >>> y.shape
        (3, 5)

    """
    if x.ndim > 2:
        x = x.reshape(len(x), -1)

    if b is None:
        args = x, W
    else:
        args = x, W, b

    y, = LinearFunction().apply(args)
    return y
