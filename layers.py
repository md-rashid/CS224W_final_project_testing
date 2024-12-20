from typing import Tuple
from datetime import datetime
import torch
from torch import nn
from torch_geometric.utils import degree

def decrease_to_max_value(x, max_value):
    x[x > max_value] = max_value
    return x

class CentralityEncoding(nn.Module):
    def __init__(self, max_in_degree: int, max_out_degree: int, node_dim: int):
        """
        :param max_in_degree: max in degree of nodes
        :param max_out_degree: max in degree of nodes
        :param node_dim: hidden dimensions of node features
        """
        super().__init__()
        self.max_in_degree = max_in_degree
        self.max_out_degree = max_out_degree
        self.node_dim = node_dim
        self.z_in = nn.Parameter(torch.randn((max_in_degree, node_dim)))
        self.z_out = nn.Parameter(torch.randn((max_out_degree, node_dim)))

    def forward(self, x: torch.Tensor, edge_index: torch.LongTensor) -> torch.Tensor:
        """
        :param x: node feature matrix
        :param edge_index: edge_index of graph (adjacency list)
        :return: torch.Tensor, node embeddings after Centrality encoding
        """
        num_nodes = x.shape[0]

        in_degree = decrease_to_max_value(degree(index=edge_index[1], num_nodes=num_nodes).long(),
                                          self.max_in_degree - 1)
        out_degree = decrease_to_max_value(degree(index=edge_index[0], num_nodes=num_nodes).long(),
                                           self.max_out_degree - 1)

        x += self.z_in[in_degree] + self.z_out[out_degree]

        return x
# this spatial encoding with gaussians is Dynaformer-specific, and differs from Graphormer    
class SpatialEncoding(nn.Module):  
    def __init__(self, num_heads: int, embedding_size: int):
        """
        :param num_heads: number of encoding heads in the GBF function
        :param embedding_size: dimension of node embedding vector
        """
        super().__init__()
        self.num_heads = num_heads
        self.embedding_size = embedding_size
        
        self.means = nn.Parameter(torch.randn(self.num_heads))
        self.stds = nn.Parameter(torch.randn(self.num_heads))
        self.weights_dist = nn.Parameter(torch.randn(2 * self.embedding_size + 1))

    def forward(self, x: torch.Tensor, coords: torch.Tensor) -> torch.Tensor:
        """
        :param x: node feature matrix (feature vectors in rows)
        :param coords: spatial coordinates of atoms (N x 3)
        :return: torch.Tensor, spatial Encoding matrix (N x N)

        # NOTE: currently averaging each GBF head (mean pooling), but is there better approach?
                (I couln't figure out what the dynaformer paper did)
        """
        norms = (torch.linalg.vector_norm(coords, ord=2, dim=1) ** 2).reshape(-1,1)
        distances = torch.sqrt(norms - 2 * coords @ coords.T + norms.T)
        
        x1 = x.unsqueeze(1)
        x0 = x.unsqueeze(0)
        N, D = x.shape
        concats = torch.cat((x1.expand(N, N, D), distances.unsqueeze(-1), x0.expand(N, N, D)), dim=-1)
        concats = concats.reshape(N ** 2, 2 * D + 1) @ self.weights_dist
        spatial_matrix = concats.reshape(N, N)
        spatial_matrix = torch.exp((spatial_matrix - self.means.reshape(-1, 1, 1)) ** 2 / (2 * self.stds.reshape(-1,1,1) ** 2))
        spatial_matrix = torch.mean(spatial_matrix, dim=0)  # mean pooling

        return spatial_matrix

class EdgeEncoding(nn.Module):
    def __init__(self, edge_dim: int, max_path_distance: int):
        """
        :param edge_dim: edge feature matrix number of dimension
        """
        super().__init__()
        self.edge_dim = edge_dim
        self.max_path_distance = max_path_distance
        self.edge_weights = nn.Parameter(torch.randn(self.max_path_distance, self.edge_dim))

    def forward(self, x: torch.Tensor, edge_attr: torch.Tensor, edge_paths) -> torch.Tensor:
        """
        :param x: node feature matrix
        :param edge_attr: edge feature matrix
        :param edge_paths: pairwise node paths in edge indexes
        :return: torch.Tensor, Edge Encoding matrix
        """
        current_datetime = datetime.now()
        print(current_datetime.strftime("%Y-%m-%d %H:%M:%S"))
        
        # Preallocate output tensor
        device = next(self.parameters()).device
        cij = torch.zeros((x.shape[0], x.shape[0]),device=device)
        
#        weights_inds = torch.arange(0, self.max_path_distance)
#        # TODO: make this more efficient
#        for src in edge_paths:
#            for dst in edge_paths[src]:
#                path_ij = edge_paths[src][dst][:self.max_path_distance]
#                cij[src][dst] = (self.edge_weights[weights_inds[:len(path_ij)]] * edge_attr[path_ij]).sum(dim=1).mean()

        # Vectorize path processing
        for src, destinations in edge_paths.items():
            for dst, path in destinations.items():
                # Limit path length
                path = path[:self.max_path_distance]
            
                # Use tensor operations instead of explicit loops
                path_weights = self.edge_weights[:len(path)]
                path_edge_features = edge_attr[path]
            
                # Compute path encoding, commenting out mean() for slight performance gain
                path_encoding = (path_weights * path_edge_features).sum(dim=1) #.mean()
                cij[src][dst] = path_encoding

        current_datetime = datetime.now()
        print(current_datetime.strftime("%Y-%m-%d %H:%M:%S"))
        return torch.nan_to_num(cij)

class GraphormerAttentionHead(nn.Module):
    def __init__(self, dim_in: int, dim_q: int, dim_k: int, edge_dim: int, max_path_distance: int):
        """
        :param dim_in: node feature matrix input number of dimension
        :param dim_q: query node feature matrix input number dimension
        :param dim_k: key node feature matrix input number of dimension
        :param edge_dim: edge feature matrix number of dimension
        """
        super().__init__()
        self.edge_encoding = EdgeEncoding(edge_dim, max_path_distance)

        self.q = nn.Linear(dim_in, dim_q)
        self.k = nn.Linear(dim_in, dim_k)
        self.v = nn.Linear(dim_in, dim_k)

    def forward(self,
                x: torch.Tensor,
                edge_attr: torch.Tensor,
                b: torch.Tensor,
                edge_paths,
                ptr=None) -> torch.Tensor:
        """
        :param query: node feature matrix
        :param key: node feature matrix
        :param value: node feature matrix
        :param edge_attr: edge feature matrix
        :param b: spatial Encoding matrix
        :param edge_paths: pairwise node paths in edge indexes
        :param ptr: batch pointer that shows graph indexes in batch of graphs
        :return: torch.Tensor, node embeddings after attention operation
        """
        batch_mask_neg_inf = torch.full(size=(x.shape[0], x.shape[0]), fill_value=-1e6).to(
            next(self.parameters()).device)
        batch_mask_zeros = torch.zeros(size=(x.shape[0], x.shape[0])).to(next(self.parameters()).device)

        # OPTIMIZE: get rid of slices: rewrite to torch
        if type(ptr) == type(None):
            batch_mask_neg_inf = torch.ones(size=(x.shape[0], x.shape[0])).to(next(self.parameters()).device)
            batch_mask_zeros += 1
        else:
            for i in range(len(ptr) - 1):
                batch_mask_neg_inf[ptr[i]:ptr[i + 1], ptr[i]:ptr[i + 1]] = 1
                batch_mask_zeros[ptr[i]:ptr[i + 1], ptr[i]:ptr[i + 1]] = 1

        query = self.q(x)
        key = self.k(x)
        value = self.v(x)

        c = self.edge_encoding(x, edge_attr, edge_paths)
        a = self.compute_a(key, query, ptr)
        a = (a + b + c) * batch_mask_neg_inf
        softmax = torch.softmax(a, dim=-1) * batch_mask_zeros
        x = softmax.mm(value)
        return x

    def compute_a(self, key, query, ptr=None):
        if type(ptr) == type(None):
            a = query.mm(key.transpose(0, 1)) / query.size(-1) ** 0.5
        else:
            a = torch.zeros((query.shape[0], query.shape[0]), device=key.device)
            for i in range(len(ptr) - 1):
                a[ptr[i]:ptr[i + 1], ptr[i]:ptr[i + 1]] = query[ptr[i]:ptr[i + 1]].mm(
                    key[ptr[i]:ptr[i + 1]].transpose(0, 1)) / query.size(-1) ** 0.5

        return a


# FIX: PyG attention instead of regular attention, due to specificity of GNNs
class GraphormerMultiHeadAttention(nn.Module):
    def __init__(self, num_heads: int, dim_in: int, dim_q: int, dim_k: int, edge_dim: int, max_path_distance: int):
        """
        :param num_heads: number of attention heads
        :param dim_in: node feature matrix input number of dimension
        :param dim_q: query node feature matrix input number dimension
        :param dim_k: key node feature matrix input number of dimension
        :param edge_dim: edge feature matrix number of dimension
        """
        super().__init__()
        self.heads = nn.ModuleList(
            [GraphormerAttentionHead(dim_in, dim_q, dim_k, edge_dim, max_path_distance) for _ in range(num_heads)]
        )
        self.linear = nn.Linear(num_heads * dim_k, dim_in)

    def forward(self,
                x: torch.Tensor,
                edge_attr: torch.Tensor,
                b: torch.Tensor,
                edge_paths,
                ptr) -> torch.Tensor:
        """
        :param x: node feature matrix
        :param edge_attr: edge feature matrix
        :param b: spatial Encoding matrix
        :param edge_paths: pairwise node paths in edge indexes
        :param ptr: batch pointer that shows graph indexes in batch of graphs
        :return: torch.Tensor, node embeddings after all attention heads
        """
        return self.linear(
            torch.cat([
                attention_head(x, edge_attr, b, edge_paths, ptr) for attention_head in self.heads
            ], dim=-1)
        )


class GraphormerEncoderLayer(nn.Module):
    def __init__(self, node_dim, edge_dim, n_heads, ff_dim, max_path_distance):
        """
        :param node_dim: node feature matrix input number of dimension
        :param edge_dim: edge feature matrix input number of dimension
        :param n_heads: number of attention heads
        """
        super().__init__()

        self.node_dim = node_dim
        self.edge_dim = edge_dim
        self.n_heads = n_heads
        self.ff_dim = ff_dim

        self.attention = GraphormerMultiHeadAttention(
            dim_in=node_dim,
            dim_k=node_dim,
            dim_q=node_dim,
            num_heads=n_heads,
            edge_dim=edge_dim,
            max_path_distance=max_path_distance,
        )
        self.ln_1 = nn.LayerNorm(self.node_dim)
        self.ln_2 = nn.LayerNorm(self.node_dim)
        self.ff = nn.Sequential(
                    nn.Linear(self.node_dim, self.ff_dim),
                    nn.GELU(),
                    nn.Linear(self.ff_dim, self.node_dim)
        )


    def forward(self,
                x: torch.Tensor,
                edge_attr: torch.Tensor,
                b: torch,
                edge_paths,
                ptr) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        h′(l) = MHA(LN(h(l−1))) + h(l−1)
        h(l) = FFN(LN(h′(l))) + h′(l)

        :param x: node feature matrix
        :param edge_attr: edge feature matrix
        :param b: spatial Encoding matrix
        :param edge_paths: pairwise node paths in edge indexes
        :param ptr: batch pointer that shows graph indexes in batch of graphs
        :return: torch.Tensor, node embeddings after Graphormer layer operations
        """
        x_prime = self.attention(self.ln_1(x), edge_attr, b, edge_paths, ptr) + x
        x_new = self.ff(self.ln_2(x_prime)) + x_prime

        return x_new
