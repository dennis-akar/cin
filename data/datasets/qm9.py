import torch
import os.path as osp

from data.utils import convert_graph_dataset_with_rings
from data.datasets import InMemoryComplexDataset
from torch_geometric.datasets import QM9
import torch_geometric.transforms as transforms
from torch_geometric.utils import remove_self_loops, to_dense_adj, dense_to_sparse


class QM9Dataset(InMemoryComplexDataset):
    """This is QM9 from the ---- paper. This is a graph regression task."""

    def __init__(self, root, max_ring_size, use_edge_features=False, transform=None,
                 pre_transform=None, pre_filter=None, subset=True, n_jobs=2):
        self.name = 'QM9'
        self._max_ring_size = max_ring_size
        self._use_edge_features = use_edge_features
        self._subset = subset
        self._n_jobs = n_jobs
        super(QM9Dataset, self).__init__(root, transform, pre_transform, pre_filter,
                                          max_dim=2, cellular=True, num_classes=1)

        self.data, self.slices, idx = self.load_dataset()
        self.train_ids = idx[0]
        self.val_ids = idx[1]
        self.test_ids = idx[2]

        self.num_node_type = 28
        self.num_edge_type = 4

    @property
    def raw_file_names(self):
        return ['train.pt', 'val.pt', 'test.pt']

    @property
    def processed_file_names(self):
        name = self.name
        return [f'{name}_complex.pt', f'{name}_idx.pt']

    def download(self):
        # Instantiating this will download and process the graph dataset.
        QM9(self.raw_dir)#, subset=self._subset)

    def load_dataset(self):
        """Load the dataset from here and process it if it doesn't exist"""
        print("Loading dataset from disk...")
        data, slices = torch.load(self.processed_paths[0])
        idx = torch.load(self.processed_paths[1])
        return data, slices, idx

    def process(self):
        # At this stage, the graph dataset is already downloaded and processed
        print(f"Processing cell complex dataset for {self.name}")

        print("Modify QM9 such that one-hot-encoding becomes scalar and delete other features for now")
        # transform = transforms.Compose([CompleteGraph(), CollapseDeleteQM9Features()])
        transform = CollapseDeleteQM9Features()
        # transform = None
        
        if self._subset:
            train_data = QM9(self.raw_dir, transform=transform)[:1000]
            val_data = QM9(self.raw_dir, transform=transform)[1000:2000]
            test_data = QM9(self.raw_dir, transform=transform)[2000:3000]
        else:
            train_data = QM9(self.raw_dir, transform=transform)[:100000]
            val_data = QM9(self.raw_dir, transform=transform)[100000:118000]
            test_data = QM9(self.raw_dir, transform=transform)[118000:]

        data_list = []
        idx = []
        start = 0
        print("Converting the train dataset to a cell complex...")
        train_data = train_data[16:17]
        train_complexes, _, _ = convert_graph_dataset_with_rings(
            train_data,
            max_ring_size=self._max_ring_size,
            include_down_adj=self.include_down_adj,
            init_edges=self._use_edge_features,
            init_rings=False,
            n_jobs=self._n_jobs)
        data_list += train_complexes
        idx.append(list(range(start, len(data_list))))
        start = len(data_list)
        print("Converting the validation dataset to a cell complex...")
        val_complexes, _, _ = convert_graph_dataset_with_rings(
            val_data,
            max_ring_size=self._max_ring_size,
            include_down_adj=self.include_down_adj,
            init_edges=self._use_edge_features,
            init_rings=False,
            n_jobs=self._n_jobs)
        data_list += val_complexes
        idx.append(list(range(start, len(data_list))))
        start = len(data_list)
        print("Converting the test dataset to a cell complex...")
        test_complexes, _, _ = convert_graph_dataset_with_rings(
            test_data,
            max_ring_size=self._max_ring_size,
            include_down_adj=self.include_down_adj,
            init_edges=self._use_edge_features,
            init_rings=False,
            n_jobs=self._n_jobs)
        data_list += test_complexes
        # breakpoint()
        idx.append(list(range(start, len(data_list))))

        path = self.processed_paths[0]
        print(f'Saving processed dataset in {path}....')
        torch.save(self.collate(data_list, 2), path)
        # breakpoint()
        path = self.processed_paths[1]
        print(f'Saving idx in {path}....')
        torch.save(idx, path)

    @property
    def processed_dir(self):
        """Overwrite to change name based on edges"""
        directory = super(QM9Dataset, self).processed_dir
        suffix0 = "_full" if self._subset is False else ""
        suffix1 = f"_{self._max_ring_size}rings" if self._cellular else ""
        suffix2 = "-E" if self._use_edge_features else ""
        return directory + suffix0 + suffix1 + suffix2


def load_qm9_graph_dataset(root, subset=True):
    raw_dir = osp.join(root, 'QM9', 'raw')

    train_data = QM9(raw_dir, subset=subset, split='train')
    val_data = QM9(raw_dir, subset=subset, split='val')
    test_data = QM9(raw_dir, subset=subset, split='test')
    data = train_data + val_data + test_data

    if subset:
        assert len(train_data) == 1000
        assert len(val_data) == 1000
        assert len(test_data) == 1000
    else:
        assert len(train_data) == 220011
        assert len(val_data) == 24445
        assert len(test_data) == 5000

    idx = []
    start = 0
    idx.append(list(range(start, len(train_data))))
    start = len(train_data)
    idx.append(list(range(start, start + len(val_data))))
    start = len(train_data) + len(val_data)
    idx.append(list(range(start, start + len(test_data))))

    return data, idx[0], idx[1], idx[2]


class CollapseDeleteQM9Features(object):
    """
    This transform mofifies the labels vector per data sample to only keep 
    the label for a specific target (there are 19 targets in QM9).

    Note: for this practical, we have hardcoded the target to be target #0,
    i.e. the electric dipole moment of a drug-like molecule.
    (https://en.wikipedia.org/wiki/Electric_dipole_moment)
    """
    def __call__(self, data):
        target = 0 # we hardcoded choice of target  
        data.y = data.y[:, target]

        # Get the first 5 features (atom type, one-hot H, C, N, O, F) of every atom
        # Get the indices at which it is non-zero (row, col), then just get col.
        # This is to convert from one-hot to scalar, keeping the dimensions ([node_num, 1] shape)
        # We, for now, delete the rest of the features.
        # data.x = torch.nonzero(data.x[:, :5])[:, 1:2]
        data.x = torch.cat((torch.nonzero(data.x[:, :5])[:, 1:2], data.x[:, 5:]), 1)
        # data.x = torch.cat((data.x, data.pos), 1)

        # NON-COMMMENT: Edges are also one-hot, we convert to scalar, this time not keeping the dimensions ([edge_num] shape)
        # COMMENT: We actually keep the edge attribute as is.
        # data.edge_attr = torch.nonzero(data.edge_attr[:, :])[:, 1]

        return data

class CompleteGraph(object):
    """
    This transform adds all pairwise edges into the edge index per data sample, 
    then removes self loops, i.e. it builds a fully connected or complete graph
    """
    def __call__(self, data):
        device = data.edge_index.device

        row = torch.arange(data.num_nodes, dtype=torch.long, device=device)
        col = torch.arange(data.num_nodes, dtype=torch.long, device=device)

        row = row.view(-1, 1).repeat(1, data.num_nodes).view(-1)
        col = col.repeat(data.num_nodes)
        edge_index = torch.stack([row, col], dim=0)

        edge_attr = None
        # Connect every node with every node including itself: new_edge_count = data.num_nodes * data.num_nodes
        # Remove self-loops: new_edge_count = new_edge_count - data.num_nodes
        # data.edge_index: [2, data.num_edges] -> [2, new_edge_count]
        # data.edge_attr: [data.num_edges, num_edge_feats] - > [new_edge_count, num_edge_feats]
        if data.edge_attr is not None:
            # Why do we do this?
            idx = data.edge_index[0] * data.num_nodes + data.edge_index[1]
            size = list(data.edge_attr.size())
            size[0] = data.num_nodes * data.num_nodes
            edge_attr = data.edge_attr.new_zeros(size)
            # Assign the existing data.edge_attr to the edges we know to exist.
            edge_attr[idx] = data.edge_attr

        edge_index, edge_attr = remove_self_loops(edge_index, edge_attr)
        data.edge_attr = edge_attr
        data.edge_index = edge_index

        return data