from inspect import Parameter
from typing import List, Optional, Set
from torch_geometric.typing import Adj, Size

import torch
from torch import Tensor
from torch_sparse import SparseTensor
from torch_scatter import gather_csr, scatter, segment_csr

from torch_geometric.nn.conv.utils.helpers import expand_left
from mp.smp_inspector import SimplicialInspector


class ChainMessagePassing(torch.nn.Module):
    r"""Base class for creating message passing layers of the form

    .. math::
        \mathbf{x}_i^{\prime} = \gamma_{\mathbf{\Theta}} \left( \mathbf{x}_i,
        \square_{j \in \mathcal{N}(i)} \, \phi_{\mathbf{\Theta}}
        \left(\mathbf{x}_i, \mathbf{x}_j,\mathbf{e}_{j,i}\right) \right),

    where :math:`\square` denotes a differentiable, permutation invariant
    function, *e.g.*, sum, mean or max, and :math:`\gamma_{\mathbf{\Theta}}`
    and :math:`\phi_{\mathbf{\Theta}}` denote differentiable functions such as
    MLPs.
    See `here <https://pytorch-geometric.readthedocs.io/en/latest/notes/
    create_gnn.html>`__ for the accompanying tutorial.

    Args:
        aggr (string, optional): The aggregation scheme to use
            (:obj:`"add"`, :obj:`"mean"`, :obj:`"max"` or :obj:`None`).
            (default: :obj:`"add"`)
        flow (string, optional): The flow direction of message passing
            (:obj:`"source_to_target"` or :obj:`"target_to_source"`).
            (default: :obj:`"source_to_target"`)
        node_dim (int, optional): The axis along which to propagate.
            (default: :obj:`-2`)
    """

    special_args: Set[str] = {
        'edge_index', 'adj_t', 'edge_index_i', 'edge_index_j', 'size',
        'size_i', 'size_j', 'ptr', 'index', 'dim_size'
    }

    def __init__(self,
                 aggr_up: Optional[str] = "add",
                 aggr_down: Optional[str] = "add",
                 flow: str = "source_to_target", node_dim: int = -2):

        super(ChainMessagePassing, self).__init__()

        self.aggr_up = aggr_up
        self.aggr_down = aggr_down
        assert self.aggr_up in ['add', 'mean', 'max', None]
        assert self.aggr_down in ['add', 'mean', 'max', None]

        self.flow = flow
        assert self.flow in ['source_to_target', 'target_to_source']

        # This is the dimension in which nodes live in the feature matrix x.
        # i.e. if x has shape [N, in_channels], then node_dim = 0 or -2
        self.node_dim = node_dim

        self.inspector = SimplicialInspector(self)
        # This stores the parameters of these functions. If pop first is true
        # the first parameter is not stored (I presume this is for self.)
        # I presume this doesn't pop first to avoid including the self parameter multiple times.
        self.inspector.inspect(self.message_up)
        self.inspector.inspect(self.message_down)
        self.inspector.inspect(self.aggregate_up, pop_first=True)
        self.inspector.inspect(self.aggregate_down, pop_first=True)
        self.inspector.inspect(self.message_and_aggregate_up, pop_first=True)
        self.inspector.inspect(self.message_and_aggregate_down, pop_first=True)
        self.inspector.inspect(self.update, pop_first_two=True)

        # Return the parameter name for these functions minus those specified in special_args
        self.__user_args__ = self.inspector.keys(
            ['message_up', 'message_down', 'aggregate_up',
             'aggregate_down']).difference(self.special_args)
        self.__fused_user_args__ = self.inspector.keys(
            ['message_and_aggregate_up',
             'message_and_aggregate_down']).difference(self.special_args)
        self.__update_user_args__ = self.inspector.keys(
            ['update']).difference(self.special_args)

        # Support for "fused" message passing.
        self.fuse_up = self.inspector.implements('message_and_aggregate_up')
        self.fuse_down = self.inspector.implements('message_and_aggregate_down')

    def __check_input_together__(self, index_up, index_down, size_up, size_down):
        # Check that at most one of these is missing (i.e. we must have at least upper
        # or lower adjacency at each level of the complex)
        assert not (index_up is None and index_down is None)
        # If we have both up and down adjacency, then check the sizes agree.
        if (index_up is not None and index_down is not None
                and size_up is not None and size_down is not None):
            assert size_up[0] == size_down[0]
            assert size_up[1] == size_down[1]

    def __check_input_separately__(self, index, size, direction):
        """This gets an up or down index and the size of the assignment matrix"""
        assert direction == 'up' or direction == 'down'
        the_size: List[Optional[int]] = [None, None]

        if isinstance(index, Tensor):
            assert index.dtype == torch.long
            assert index.dim() == 2
            assert index.size(0) == 2
            if size is not None:
                the_size[0] = size[0]
                the_size[1] = size[1]
            return the_size

        elif isinstance(index, SparseTensor):
            if self.flow == 'target_to_source':
                raise ValueError(
                    ('Flow direction "target_to_source" is invalid for '
                     'message propagation via `torch_sparse.SparseTensor`. If '
                     'you really want to make use of a reverse message '
                     'passing flow, pass in the transposed sparse tensor to '
                     'the message passing module, e.g., `adj_t.t()`.'))
            the_size[0] = index.sparse_size(1)
            the_size[1] = index.sparse_size(0)
            return the_size

        raise ValueError(
            ('`MessagePassing.propagate` only supports `torch.LongTensor` of '
             'shape `[2, num_messages]` or `torch_sparse.SparseTensor` for '
             'argument `edge_index`.'))

    def __set_size__(self, size: List[Optional[int]], dim: int, src: Tensor):
        the_size = size[dim]
        if the_size is None:
            size[dim] = src.size(self.node_dim)
        elif the_size != src.size(self.node_dim):
            raise ValueError(
                (f'Encountered tensor with size {src.size(self.node_dim)} in '
                 f'dimension {self.node_dim}, but expected size {the_size}.'))

    def __lift__(self, src, index, dim):
        if isinstance(index, Tensor):
            index = index[dim]
            return src.index_select(self.node_dim, index)
        elif isinstance(index, SparseTensor):
            if dim == 1:
                rowptr = index.storage.rowptr()
                rowptr = expand_left(rowptr, dim=self.node_dim, dims=src.dim())
                return gather_csr(src, rowptr)
            elif dim == 0:
                col = index.storage.col()
                return src.index_select(self.node_dim, col)
        raise ValueError

    def __collect__(self, args, up_index, down_index, up_size, down_size, kwargs):
        i, j = (1, 0) if self.flow == 'source_to_target' else (0, 1)

        out = {}
        for arg in args:
            # Here the x_i and x_j parameters are automatically extracted
            # from an argument having the prefix x.
            if arg[-2:] not in ['_i', '_j']:
                out[arg] = kwargs.get(arg, Parameter.empty)
            else:
                dim = 0 if arg[-2:] == '_j' else 1
                # Extract any part up to _j or _i. So for x_j extract x
                index, data = None, None
                if arg.startswith('up_'):
                    data = kwargs.get(arg[3:-2], Parameter.empty)
                    index = up_index
                elif arg.startswith('down_'):
                    data = kwargs.get(arg[5:-2], Parameter.empty)
                    index = down_index

                # This was used before for the case when data is supplied directly
                # as (x_i, x_j) as opposed to a matrix X [N, in_channels]
                # (the 2nd case is handled by the next if)
                if isinstance(data, (tuple, list)):
                    raise ValueError('This format is not supported for simplicial message passing')

                # This is the usual case when we get a feature matrix of shape [N, in_channels]
                if isinstance(data, Tensor):
                    # Same size checks as above.
                    self.__set_size__(up_size, dim, data)
                    self.__set_size__(down_size, dim, data)
                    # Select the features of the nodes indexed by i or j from the data matrix
                    data = self.__lift__(data, index, j if arg[-2:] == '_j' else i)

                out[arg] = data

        # Automatically builds some default parameters that can be used in the message passing
        # functions as needed. This was modified to be discriminative of upper and lower adjacency.
        if isinstance(up_index, Tensor):
            assert isinstance(down_index, Tensor)
            out['adj_t'] = None
            out['ptr'] = None
            # Upper adjacency
            out['up_index'] = up_index
            out['up_index_i'] = up_index[i]
            out['up_index_j'] = up_index[j]
            # Down adjacency
            out['down_index'] = down_index
            out['down_index_i'] = down_index[i]
            out['down_index_j'] = down_index[j]
        elif isinstance(up_index, SparseTensor):
            assert isinstance(down_index, SparseTensor)
            out['edge_index'] = None
            # Upper adjacency
            out['up_adj_t'] = up_index
            out['up_index_i'] = up_index.storage.row()
            out['up_index_j'] = up_index.storage.col()
            out['up_ptr'] = up_index.storage.rowptr()
            out['up_weight'] = up_index.storage.value()
            out['up_attr'] = up_index.storage.value()
            out['up_type'] = up_index.storage.value()
            # Lower adjacency
            out['down_adj_t'] = down_index
            out['down_index_i'] = down_index.storage.row()
            out['down_index_j'] = down_index.storage.col()
            out['down_ptr'] = down_index.storage.rowptr()
            out['down_weight'] = down_index.storage.value()
            out['down_attr'] = down_index.storage.value()
            out['down_type'] = down_index.storage.value()

        # Up
        out['up_index'] = out['up_index_i']
        out['up_size'] = up_size
        out['up_size_i'] = up_size[1] or up_size[0]
        out['up_size_j'] = up_size[0] or up_size[1]
        out['up_dim_size'] = out['up_size_i']
        # Down
        out['down_index'] = out['down_index_i']
        out['down_size'] = down_size
        out['down_size_i'] = down_size[1] or down_size[0]
        out['down_size_j'] = down_size[0] or down_size[1]
        out['down_dim_size'] = out['down_size_i']

        return out

    def __message_and_aggregate__(self, index: Adj, direction: str, size: Size = None,
                                  **kwargs):
        size = self.__check_input_separately__(index, size, direction)

        # Fused message and aggregation
        fuse = self.fuse_up if direction == 'up' else self.fuse_down
        if isinstance(index, SparseTensor) and fuse:
            # Collect the objects to pass to the function params in __user_arg.
            coll_dict = self.__collect__(self.__fused_user_args__, index, size, kwargs)

            # message and aggregation are fused in a single function
            msg_aggr_kwargs = self.inspector.distribute(
                f'message_and_aggregate_{direction}', coll_dict)
            return self.message_and_aggregate_up(index, **msg_aggr_kwargs)

        # Otherwise, run message and aggregation in separation.
        elif isinstance(index, Tensor) or not fuse:
            # Collect the objects to pass to the function params in __user_arg.
            coll_dict = self.__collect__(self.__user_args__, index, size, kwargs)

            # Up message and aggregation
            msg_kwargs = self.inspector.distribute(f'message_{direction}', coll_dict)
            out = self.message_up(**msg_kwargs)

            aggr_kwargs = self.inspector.distribute(f'aggregate_{direction}', coll_dict)
            return self.aggregate_up(out, **aggr_kwargs)

    def propagate(self, up_index: Optional[Adj],
                  down_index: Optional[Adj],
                  up_size: Size = None,
                  down_size: Size = None,
                  **kwargs):
        r"""The initial call to start propagating messages.

        """
        self.__check_input_together__(up_index, down_index, up_size, down_size)

        up_out = self.__message_and_aggregate__(up_index, 'up', up_size, **kwargs)
        down_out = self.__message_and_aggregate__(down_index, 'down', down_size, **kwargs)

        coll_dict = self.__collect__(self.__update_user_args__, up_index, down_index, up_size, down_size, kwargs)
        update_kwargs = self.inspector.distribute('update', coll_dict)
        return self.update(up_out, down_out, **update_kwargs)

    def message_up(self, up_x_j: Tensor) -> Tensor:
        r"""Constructs messages from node :math:`j` to node :math:`i`
        in analogy to :math:`\phi_{\mathbf{\Theta}}` for each edge in
        :obj:`edge_index`.
        This function can take any argument as input which was initially
        passed to :meth:`propagate`.
        Furthermore, tensors passed to :meth:`propagate` can be mapped to the
        respective nodes :math:`i` and :math:`j` by appending :obj:`_i` or
        :obj:`_j` to the variable name, *.e.g.* :obj:`x_i` and :obj:`x_j`.
        """
        return up_x_j

    def message_down(self, down_x_j: Tensor) -> Tensor:
        r"""Constructs messages from node :math:`j` to node :math:`i`
        in analogy to :math:`\phi_{\mathbf{\Theta}}` for each edge in
        :obj:`edge_index`.
        This function can take any argument as input which was initially
        passed to :meth:`propagate`.
        Furthermore, tensors passed to :meth:`propagate` can be mapped to the
        respective nodes :math:`i` and :math:`j` by appending :obj:`_i` or
        :obj:`_j` to the variable name, *.e.g.* :obj:`x_i` and :obj:`x_j`.
        """
        return down_x_j

    def aggregate_up(self, inputs: Tensor, up_index: Tensor,
                     up_ptr: Optional[Tensor] = None,
                     up_dim_size: Optional[int] = None) -> Tensor:
        r"""Aggregates messages from neighbors as
        :math:`\square_{j \in \mathcal{N}(i)}`.

        Takes in the output of message computation as first argument and any
        argument which was initially passed to :meth:`propagate`.

        By default, this function will delegate its call to scatter functions
        that support "add", "mean" and "max" operations as specified in
        :meth:`__init__` by the :obj:`aggr` argument.
        """
        if up_ptr is not None:
            up_ptr = expand_left(up_ptr, dim=self.node_dim, dims=inputs.dim())
            return segment_csr(inputs, up_ptr, reduce=self.aggr_up)
        else:
            return scatter(inputs, up_index, dim=self.node_dim, dim_size=up_dim_size,
                           reduce=self.aggr_up)

    def aggregate_down(self, inputs: Tensor, down_index: Tensor,
                       down_ptr: Optional[Tensor] = None,
                       down_dim_size: Optional[int] = None) -> Tensor:
        r"""Aggregates messages from neighbors as
        :math:`\square_{j \in \mathcal{N}(i)}`.

        Takes in the output of message computation as first argument and any
        argument which was initially passed to :meth:`propagate`.

        By default, this function will delegate its call to scatter functions
        that support "add", "mean" and "max" operations as specified in
        :meth:`__init__` by the :obj:`aggr` argument.
        """
        if down_ptr is not None:
            down_ptr = expand_left(down_ptr, dim=self.node_dim, dims=inputs.dim())
            return segment_csr(inputs, down_ptr, reduce=self.aggr_down)
        else:
            return scatter(inputs, down_index, dim=self.node_dim, dim_size=down_dim_size,
                           reduce=self.aggr_down)

    def message_and_aggregate_up(self, up_adj_t: SparseTensor) -> Tensor:
        r"""Fuses computations of :func:`message` and :func:`aggregate` into a
        single function.
        If applicable, this saves both time and memory since messages do not
        explicitly need to be materialized.
        This function will only gets called in case it is implemented and
        propagation takes place based on a :obj:`torch_sparse.SparseTensor`.
        """
        raise NotImplementedError

    def message_and_aggregate_down(self, down_adj_t: SparseTensor) -> Tensor:
        r"""Fuses computations of :func:`message` and :func:`aggregate` into a
        single function.
        If applicable, this saves both time and memory since messages do not
        explicitly need to be materialized.
        This function will only gets called in case it is implemented and
        propagation takes place based on a :obj:`torch_sparse.SparseTensor`.
        """
        raise NotImplementedError

    def update(self, up_inputs: Tensor, down_inputs: Tensor) -> Tensor:
        r"""Updates node embeddings in analogy to
        :math:`\gamma_{\mathbf{\Theta}}` for each node
        :math:`i \in \mathcal{V}`.
        Takes in the output of aggregation as first argument and any argument
        which was initially passed to :meth:`propagate`.
        """
        return up_inputs + down_inputs


class SimplicialMessagePassing(torch.nn.Module):
    def __init__(self, vertex_mp: ChainMessagePassing, edge_mp: ChainMessagePassing,
                 triangle_mp: ChainMessagePassing):

        super(SimplicialMessagePassing, self).__init__()

        self.vertex_mp = vertex_mp
        self.edge_mp = edge_mp
        self.triangle_mp = triangle_mp

    def propagate(self, vertex_params, edge_params, triangle_param):
        pass