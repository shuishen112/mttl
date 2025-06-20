import abc
import copy
import dataclasses
import os
import re
from abc import abstractmethod
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
from typing import Dict, List, Union

import numpy as np
import sklearn.decomposition
import torch
import torch.nn.functional as F
from pytorch_lightning import Trainer
from sklearn.cluster import KMeans
from sklearn.metrics.pairwise import cosine_similarity
from tqdm.auto import tqdm

from mttl.datamodule.base import get_datamodule
from mttl.logging import logger
from mttl.models.containers.lora_containers import ExpertContainer
from mttl.models.containers.selectors.phatgoose_selector import (
    PhatgooseTrainerSelectorConfig,
)
from mttl.models.expert_model import MultiExpertModel, MultiExpertModelConfig
from mttl.models.get_optimizer import get_optimizer_and_scheduler
from mttl.models.library.expert import Expert
from mttl.models.library.expert_library import ExpertLibrary
from mttl.models.lightning.callbacks import LiveCheckpointCallback
from mttl.models.lightning.loggers import get_pl_loggers
from mttl.models.modifiers.base import get_target_2_source_param_mapping
from mttl.models.modifiers.lora import LoRAConfig
from mttl.models.monitors import get_monitors
from mttl.models.train_utils import train_model
from mttl.models.utils import transfer_batch_to_device
from mttl.registrable import Registrable
from mttl.serializable import Serializable
from mttl.models.library.wudi_closed_form import AnalyticalSolver
from mttl.logging import TableLogger
import wandb


class LibraryTransform(abc.ABC, Registrable):
    """Defines a transformation of a library of experts."""

    def __init__(self, config):
        self.config = config

    @abstractmethod
    def transform(
        self, library: ExpertLibrary, persist: bool = False, recompute: bool = False
    ):
        pass


def _hash_field(val):
    # from facebookresearch / ReAgent
    if val is None:
        return ""
    elif isinstance(val, list):
        return tuple(val)
    elif isinstance(val, dict):
        return tuple(sorted(val.items()))
    else:
        return val


def param_hash(p, exclude_fields=None):
    # from facebookresearch / ReAgent
    import hashlib

    m = hashlib.md5()
    m.update(
        str(
            tuple(
                _hash_field(getattr(p, f.name))
                for f in dataclasses.fields(p)
                if not exclude_fields or f.name not in exclude_fields
            )
        ).encode()
    )
    return m.hexdigest()


@dataclass
class LibraryTransformConfig(Serializable):
    name: str = None

    @property
    def save_name(self):
        """
        Returns name of the cached data to use when persisting the library.
        If not set, it will be automatically generated.
        """
        if self.name:
            return self.name
        else:
            # form auto name based on the arguments of the config
            save_name = self.__class__.__name__.lower() + f"-{self.param_hash()}"
            return save_name

    def param_hash(self):
        return param_hash(self)


@dataclass
class SVDEmbeddingTransformConfig(LibraryTransformConfig):
    n_components: int = 64
    sparsity_threshold: float = 0.8


@LibraryTransform.register("svd_embedding", SVDEmbeddingTransformConfig)
class SVDEmbeddingTransform(LibraryTransform):
    """Creates adapter embeddings by low-rank decomposition of a sparsified version
    of the adapter experts.
    """

    def __init__(self, config, random_state=None):
        super().__init__(config)
        self.random_state = random_state

    @classmethod
    @torch.no_grad()
    def fetch(cls, library: Union[str, ExpertLibrary], config_hash: str = None):
        if isinstance(library, str):
            library = ExpertLibrary.get_expert_library(library)

        config_hash = config_hash or SVDEmbeddingTransformConfig().save_name

        # try to fetch auxiliary data
        output = library.get_auxiliary_data(data_type=config_hash)

        if len(output) == len(library):
            logger.info("Found {} precomputed SVD Embeddings".format(len(output)))
            return output

        raise ValueError(
            "SVD embeddings are missing or corrupted, please recompute them."
        )

    def transform(self, library, persist=True, recompute=False):
        if type(library) == str:
            library = ExpertLibrary.get_expert_library(library)

        try:
            output = self.fetch(library, self.config.save_name)

            if not recompute:
                logger.info("Found {} precomputed SVD Embeddings".format(len(output)))
                return output
        except ValueError:
            pass

        logger.info("Computing SVD Embeddings for %s experts", len(library))
        logger.info("Saving to: %s", self.config.save_name)

        svd = sklearn.decomposition.TruncatedSVD(
            n_components=self.config.n_components,
            algorithm="randomized",
            n_iter=5,
            n_oversamples=10,
            power_iteration_normalizer="auto",
            random_state=self.random_state,
            tol=0.0,
        )

        array, names = [], []
        for name in tqdm(list(library.keys())):
            expert = library[name]
            array += [
                torch.nn.utils.parameters_to_vector(
                    [p for p in expert.expert_weights.values()]
                )
            ]
            names += [name]
        array = torch.stack(array).cpu().numpy()

        # Use quantiles to fit the exact threshold
        thr = np.quantile(np.abs(array), self.config.sparsity_threshold, axis=1)
        array[np.abs(array) <= thr.reshape(-1, 1)] = 0.0

        logger.info("Sparsity threshold: {}".format(str([f"{x:.4f}" for x in thr])))
        assert (
            np.abs(
                (array == 0).sum(axis=1) / np.prod(array.shape[1])
                - self.config.sparsity_threshold
            ).max()
            < 1e-4
        )

        experts_embeddings = svd.fit_transform(array)
        experts_embeddings = (
            experts_embeddings / np.linalg.norm(experts_embeddings, 2, axis=1)[:, None]
        )

        if persist:
            logger.info("Uploading SVD embeddings to the library.")

            # add embeddings to the library
            with library.batched_commit():
                for i, name in enumerate(names):
                    library.add_auxiliary_data(
                        data_type=self.config.save_name,
                        expert_name=name,
                        config=self.config.__dict__,
                        data=experts_embeddings[i],
                        force=True,  # make sure we overwrite
                    )
        return dict(zip(names, experts_embeddings))


@dataclass
class WudiMergeConfig(LibraryTransformConfig):
    iter: int = 300
    lr: float = 1e-5


@LibraryTransform.register("wudi_merge_after", WudiMergeConfig)
class WudiMergeAfter(LibraryTransform):
    """
    implement the wudimerge in the paper https://arxiv.org/pdf/2503.08099v1

    we multiply the lora A and lora B and then merge the experts to the model(merge after).
    """

    def __init__(self, config: WudiMergeConfig = None):
        super().__init__(config or WudiMergeConfig())

    def _get_task_vectors(self, expert):
        """
        get the incremental weights for each layer, LoRA A outproduct LoRA B
        """
        task_vectors = {}
        for key in expert.expert_weights.keys():
            base_layer_name = key.split(".lora_")[
                0
            ]  # Get base layer name by removing .lora_a or .lora_b
            if base_layer_name not in task_vectors:
                task_vectors[base_layer_name] = None

        for layer in task_vectors.keys():
            lora_a = expert.expert_weights[f"{layer}.lora_a"]
            lora_b = expert.expert_weights[f"{layer}.lora_b"]
            task_vectors[layer] = lora_a.data @ lora_b.data

        return task_vectors

    def get_optimized_task_vector(
        self, layer_name, task_vectors, iter, lr
    ) -> torch.Tensor:
        """
        min Σᵢ (1/||τᵢ,ₗ||²F) ||(τₘ,ₗ - τᵢ,ₗ)(τᵢ,ₗ)ᵀ||²F

        return the optimized merged task vector for each layer
        """
        task_vectors = task_vectors.cuda()
        merging_vector = torch.nn.Parameter((torch.sum(task_vectors, dim=0)))
        optimizer = torch.optim.Adam([merging_vector], lr=lr, weight_decay=0)

        l2_norms = torch.square(
            torch.norm(task_vectors.reshape(task_vectors.shape[0], -1), p=2, dim=-1)
        )

        pbar = tqdm(range(iter), desc=f"Optimizing parameter {layer_name}")
        prev_loss = float("inf")
        patience = 5  # Number of steps to wait for improvement
        no_improve_count = 0
        min_delta = 1e-4  # Minimum change in loss to be considered improvement

        for step in pbar:
            disturbing_vectors = merging_vector.unsqueeze(0) - task_vectors
            inner_product = torch.matmul(
                disturbing_vectors, task_vectors.transpose(1, 2)
            )
            loss = torch.sum(torch.square(inner_product))

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            # Check if loss improvement is significant
            if abs(prev_loss - loss.item()) < min_delta:
                no_improve_count += 1
            else:
                no_improve_count = 0

            # Early stopping if no significant improvement for patience steps
            if no_improve_count >= patience:
                logger.info(f"Early stopping at step {step} due to minimal loss change")
                break

            prev_loss = loss.item()
            pbar.set_postfix({"loss": f"{loss.item():.4f}"})
        return merging_vector

    def transform(self, library, persist=True, recompute=False) -> dict:
        """
        return the task merged vectors in each layer
        """
        rank_table = TableLogger()
        if type(library) == str:
            library = ExpertLibrary.get_expert_library(library)
        expert_names = list(library.keys())
        experts = [library[name] for name in expert_names]
        logger.info("Merging {} experts using WuDi merge after".format(len(experts)))
        one_expert = experts[0]
        # get the layer names from the model
        layer_names = [
            name.split(".lora_")[0] for name in one_expert.expert_weights.keys()
        ]
        layer_names = sorted(list(set(layer_names)))

        # get the task vectors for each expert
        task_vectors_experts = {}
        for expert in experts:
            task_vectors = self._get_task_vectors(expert)
            task_vectors_experts[expert.name] = task_vectors
        task_merged_vectors = {}
        # wudi merge the task vectors
        for layer in layer_names:

            # get the experts for this layer
            task_vectors = [
                task_vectors_experts[expert.name][layer] for expert in experts
            ]

            task_vectors = torch.stack(task_vectors, dim=0)
            # get the redundant task vector
            merged_task_vector = self.get_optimized_task_vector(
                layer_name=layer,
                task_vectors=task_vectors,
                iter=self.config.iter,
                lr=self.config.lr,
            )

            # save the merged task vector in each layer
            task_merged_vectors[layer] = merged_task_vector / len(experts)

            # get the rank of the merged task vector
            rank = torch.linalg.matrix_rank(merged_task_vector)
            logger.info(
                f"Rank of the merged task vector for {layer} is {rank}, original rank is {merged_task_vector.shape[0]}"
            )
            rank_table.log(
                {
                    "layer": layer,
                    "rank": rank.item(),
                    "original_rank": merged_task_vector.shape[0],
                }
            )
        rank_table.log_final_table()
        return task_merged_vectors


@LibraryTransform.register("wudi_merge", WudiMergeConfig)
class WudiMerge(LibraryTransform):
    """
    implement the wudimerge in the paper https://arxiv.org/pdf/2503.08099v1
    """

    def __init__(self, config: WudiMergeConfig = None):
        super().__init__(config or WudiMergeConfig())

    def transform(self, library) -> Expert:
        device = "cuda" if torch.cuda.is_available() else "cpu"
        if type(library) == str:
            library = ExpertLibrary.get_expert_library(library)

        expert_names = list(library.keys())
        experts = [library[name] for name in expert_names]

        logger.info("Merging {} experts using WuDi merge before".format(len(experts)))

        base_expert = copy.deepcopy(experts[0])

        # Get all parameter keys that we want to merge
        keys = [key for key in base_expert.expert_weights.keys()]
        task_merged_vectors = {}
        for key in keys:
            # Stack all expert weights for this parameter
            values = torch.stack([expert.expert_weights[key] for expert in experts])

            values = values.to(device)

            # Initialize merged vector as sum of all vectors
            merging_vector = torch.nn.Parameter(
                torch.sum(values, dim=0), requires_grad=True
            )
            optimizer = torch.optim.Adam(
                [merging_vector], lr=self.config.lr, weight_decay=0
            )

            # Compute L2 norms
            l2_norms = torch.square(
                torch.norm(values.reshape(values.shape[0], -1), p=2, dim=-1)
            )

            # Optimize merging vector
            pbar = tqdm(range(self.config.iter), desc=f"Optimizing parameter {key}")
            prev_loss = float("inf")
            patience = 5  # Number of steps to wait for improvement
            no_improve_count = 0
            min_delta = 1e-4  # Minimum change in loss to be considered improvement

            for step in pbar:
                disturbing_vectors = merging_vector.unsqueeze(0) - values
                inner_product = torch.matmul(disturbing_vectors, values.transpose(1, 2))

                loss = torch.sum(
                    torch.square(inner_product) / l2_norms.unsqueeze(-1).unsqueeze(-1)
                )
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()

                # Check if loss improvement is significant
                if abs(prev_loss - loss.item()) < min_delta:
                    no_improve_count += 1
                else:
                    no_improve_count = 0

                # Early stopping if no significant improvement for patience steps
                if no_improve_count >= patience:
                    logger.info(
                        f"Early stopping at step {step} due to minimal loss change"
                    )
                    break

                prev_loss = loss.item()
                pbar.set_postfix({"loss": f"{loss.item():.4f}"})
            merging_vector = merging_vector / len(experts)
            task_merged_vectors[key] = merging_vector
        return task_merged_vectors


@dataclass
class WuDiMerge2Config(LibraryTransformConfig):
    iter: int = 300
    lr: float = 1e-4


@LibraryTransform.register("wudi_merge_2", WuDiMerge2Config)
class WuDiMerge2(WudiMergeAfter):
    """
    implement the wudimerge in the paper https://arxiv.org/pdf/2505.19892
    """

    def __init__(self, config: WuDiMerge2Config = None):
        super().__init__(config or WuDiMerge2Config())

    def get_optimized_task_vector(self, layer_name, task_vectors, iter=300, lr=1e-4):
        """
        get the optimized task vector for the layer
        min Σᵢ (1/||τᵢ,ₗ||²F) ||(τₘ,ₗ - τᵢ,ₗ)(τᵢ,ₗ)ᵀ||²F
        """
        original_dtype = task_vectors.dtype
        task_vectors = task_vectors.cuda()
        average_vector = task_vectors.mean(dim=0)
        low_rank_list = []
        taskvector_list = []
        for i in tqdm(
            range(task_vectors.shape[0]), desc=f"wudi merge 2 for {layer_name}"
        ):
            vector = task_vectors[i]
            u, s, v = torch.linalg.svd(vector, full_matrices=True)
            u2, s2, v2 = torch.linalg.svd(vector, full_matrices=False)
            reduced_index_s = int(s.shape[0] / task_vectors.shape[0])
            u2 = u2[:, :reduced_index_s]
            s2 = s2[:reduced_index_s]
            v2 = v2[:reduced_index_s, :]
            s_mask = torch.zeros_like(s)
            s_mask[:reduced_index_s] = 1
            s = s * s_mask
            v_mask = torch.zeros_like(v)
            v_mask[:reduced_index_s, :] = 1
            v = v * v_mask  # (n, n)
            S_matrix = torch.zeros(
                vector.shape[0], vector.shape[1], device=s.device
            )  # m x n
            min_dim = min(vector.shape)
            S_matrix[:min_dim, :min_dim] = torch.diag_embed(s)
            low_rank_list.append(S_matrix @ v)
            taskvector_list.append(u2 @ torch.diag_embed(s2) @ v2)
            # del u, s, v, u2, s2, v2, S_matrix, s_mask, v_mask
        low_rank = torch.stack(low_rank_list).to(original_dtype)
        taskvector = torch.stack(taskvector_list).to(original_dtype)

        merging_vector = torch.nn.Parameter(average_vector.to(original_dtype))
        # optimizer = torch.optim.SGD([merging_vector], lr=lr, momentum=0.9)
        optimizer = torch.optim.Adam([merging_vector], lr=lr, weight_decay=0)
        l2_norms = torch.square(
            torch.norm(taskvector.reshape(taskvector.shape[0], -1), p=2, dim=-1)
        ).to(original_dtype)

        pbar = tqdm(range(iter), desc=f"Optimizing {layer_name}", leave=False)
        prev_loss = float("inf")
        patience = 5  # Number of steps to wait for improvement
        no_improve_count = 0
        min_delta = 1e-4  # Minimum change in loss to be considered improvement

        for step in pbar:
            disturbing_vectors = merging_vector.unsqueeze(0) - taskvector
            inner_product = torch.matmul(disturbing_vectors, low_rank.transpose(1, 2))
            loss = torch.sum(
                torch.square(inner_product) / l2_norms.unsqueeze(-1).unsqueeze(-1)
            )
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            if abs(prev_loss - loss.item()) < min_delta:
                no_improve_count += 1
            else:
                no_improve_count = 0

            if no_improve_count >= patience:
                logger.info(f"Early stopping at step {step} due to minimal loss change")
                break

            prev_loss = loss.item()
            pbar.set_postfix({"loss": f"{loss.item():.4f}"})
        return merging_vector


@dataclass
class SVDMergeConfig(LibraryTransformConfig):
    path: str = "svd_ingredients.pt"


@LibraryTransform.register("svd_merge", SVDMergeConfig)
class SVDMerge(LibraryTransform):
    """
    merge the task vectors using svd
    """

    def __init__(self, config: SVDMergeConfig = None):
        super().__init__(config or SVDMergeConfig())

    def _get_task_vectors(self, expert, layer):

        lora_a = expert.expert_weights[f"{layer}.lora_a"]
        lora_b = expert.expert_weights[f"{layer}.lora_b"]
        task_vector = lora_a.data @ lora_b.data

        return task_vector

    def _merge_task_vectors(
        self, task_vectors, layer, device, original_dtype, sv_reduction
    ):

        sum_u = None
        sum_s = None
        sum_v = None

        # Process each task vector
        for i, vec in tqdm(
            enumerate(task_vectors), desc=f"SVD merging compute for {layer}"
        ):
            # Move parameter to GPU for computation
            vec = vec.to(device).float()

            # Compute SVD
            u, s, v = torch.linalg.svd(vec, full_matrices=False)

            # Compute reduced index
            reduced_index_s = int(s.shape[0] * sv_reduction)

            # Initialize storage for the first vector
            if i == 0:
                sum_u = torch.zeros_like(u, device=device)
                sum_s = torch.zeros_like(s, device=device)
                sum_v = torch.zeros_like(v, device=device)

            # Store important components
            sum_u[:, i * reduced_index_s : (i + 1) * reduced_index_s] = u[
                :, :reduced_index_s
            ]
            sum_s[i * reduced_index_s : (i + 1) * reduced_index_s] = s[:reduced_index_s]
            sum_v[i * reduced_index_s : (i + 1) * reduced_index_s, :] = v[
                :reduced_index_s, :
            ]
        # Compute final merged parameter
        u_u, s_u, v_u = torch.linalg.svd(sum_u, full_matrices=False)
        u_v, s_v, v_v = torch.linalg.svd(sum_v, full_matrices=False)

        # Compute merged result and move back to CPU
        merged_param = (
            torch.linalg.multi_dot([u_u, v_u, torch.diag(sum_s), u_v, v_v])
            .to(original_dtype)
            .cpu()
        )

        return merged_param

    @torch.no_grad()
    def transform(self, library) -> dict:
        # empty the cache
        torch.cuda.empty_cache()
        if type(library) == str:
            library = ExpertLibrary.get_expert_library(library)
        expert_names = list(library.keys())
        experts = [library[name] for name in expert_names]
        logger.info("Merging {} experts using SVD merge".format(len(experts)))

        one_expert = experts[0]
        # get the layer names from the model
        layer_names = [
            name.split(".lora_")[0] for name in one_expert.expert_weights.keys()
        ]
        layer_names = sorted(list(set(layer_names)))
        task_merged_vectors = {}
        if os.path.exists(self.config.path):
            logger.info(f"load task vectors from {self.config.path}")
            task_merged_vectors = torch.load(self.config.path)
        else:
            for layer in layer_names:
                logger.info(f"compute task vector for {layer}")
                # Get task vectors for this layer from all experts
                task_vectors = [
                    self._get_task_vectors(expert, layer) for expert in experts
                ]

                # Apply SVD merging for this layer
                sv_reduction = 1.0 / len(task_vectors)
                device = (
                    torch.device("cuda")
                    if torch.cuda.is_available()
                    else torch.device("cpu")
                )
                original_dtype = task_vectors[0].dtype

                task_merged_vectors[layer] = self._merge_task_vectors(
                    task_vectors, layer, device, original_dtype, sv_reduction
                )

        return task_merged_vectors


@dataclass
class AnalyticalWudiMergeConfig(LibraryTransformConfig):
    regularization: float = 1e-6
    pass


@LibraryTransform.register("analytical_wudi_merge", AnalyticalWudiMergeConfig)
class AnalyticalWudiMerge(LibraryTransform):
    """ """

    def __init__(self, config: AnalyticalWudiMergeConfig = None):
        super().__init__(config or AnalyticalWudiMergeConfig())
        self.merger = AnalyticalSolver(regularization_omega=self.config.regularization)

    def _get_task_vectors(self, expert):
        task_vectors = {}
        for key in expert.expert_weights.keys():
            base_layer_name = key.split(".lora_")[
                0
            ]  # Get base layer name by removing .lora_a or .lora_b
            if base_layer_name not in task_vectors:
                task_vectors[base_layer_name] = None

        for layer in task_vectors.keys():
            lora_a = expert.expert_weights[f"{layer}.lora_a"]
            lora_b = expert.expert_weights[f"{layer}.lora_b"]
            task_vectors[layer] = lora_a.data @ lora_b.data

        return task_vectors

    def transform(self, library) -> dict:
        if type(library) == str:
            library = ExpertLibrary.get_expert_library(library)
        expert_names = list(library.keys())
        experts = [library[name] for name in expert_names]
        logger.info(
            "Merging {} experts using WuDi analytical merge".format(len(experts))
        )

        # get the task vectors for each expert
        task_vectors_experts = {}
        for expert in experts:
            task_vectors = self._get_task_vectors(expert)
            task_vectors_experts[expert.name] = task_vectors
        task_merged_vectors = {}
        # merge the task vectors
        layer_names = list(task_vectors_experts[experts[0].name].keys())
        for layer in layer_names:
            task_vectors = [
                task_vectors_experts[expert.name][layer] for expert in experts
            ]

            # Get optimal merged matrix using analytical solution
            logger.info(f"Merging {layer} with {len(task_vectors)} task vectors")
            merged_matrix = self.merger.compute_analytical_solution(task_vectors)
            # Convert back to torch tensor with same device and dtype
            merged_task_vector = torch.tensor(
                merged_matrix,
                device=task_vectors[0].device,
                dtype=task_vectors[0].dtype,
            )
            # add the merged task vector to the model
            task_merged_vectors[layer] = merged_task_vector / len(experts)
        return task_merged_vectors


@dataclass
class WeightedLinearMergeConfig(LibraryTransformConfig):
    weights: dict = None


@LibraryTransform.register("weighted_linear_merge", WeightedLinearMergeConfig)
class WeightedLinearMerge(LibraryTransform):
    """
    Computes a uniform weight mixture across experts of a given library
    """

    def __init__(self, config: WeightedLinearMergeConfig = None):
        super().__init__(config or WeightedLinearMergeConfig())

    @torch.no_grad()
    def transform(self, library) -> Expert:
        if type(library) == str:
            library = ExpertLibrary.get_expert_library(library)

        expert_names = list(library.keys())
        experts = [library[name] for name in expert_names]

        logger.info("Averaging {} experts".format(len(experts)))

        base_expert = copy.deepcopy(experts[0])
        base_expert.name = "weighted_expert"

        if self.config.weights is not None:
            assert set(self.config.weights.keys()) == set(
                expert_names
            ), "Weights must have the same keys as the experts"
            if not (1 - 1e-6) <= sum(self.config.weights.values()) <= (1 + 1e-6):
                logger.warning(
                    "Weights do not sum to 1.0, please make sure this is intended"
                )

            # scale the base expert
            for k, v in base_expert.expert_weights.items():
                base_expert.expert_weights[k] *= self.config.weights[expert_names[0]]

        for _, expert in zip(expert_names[1:], experts[1:]):
            # Validate that the expert is compatible
            assert type(expert.expert_info.expert_config) == type(
                base_expert.expert_info.expert_config
            ), "Expert configs must be the same type"
            assert set(expert.expert_weights.keys()) == set(
                base_expert.expert_weights.keys()
            ), "Expert weights must have the same keys"

            weight = 1.0
            if self.config.weights is not None:
                weight = self.config.weights[expert.expert_info.expert_name]

            for k, v in expert.expert_weights.items():
                base_expert.expert_weights[k] += v * weight

        # Normalize the final expert
        if self.config.weights is None:
            for k, v in base_expert.expert_weights.items():
                base_expert.expert_weights[k] /= len(experts)

        # manually change the config of the expert to remove the tie_params
        base_expert.expert_config.tie_params = None

        return base_expert


@dataclass
class KnotMergeConfig(WeightedLinearMergeConfig):
    path: str = "knot_ingredients.pt"  # path to store SVD components


@LibraryTransform.register("weighted_knot_merge", KnotMergeConfig)
class KnotMerge(LibraryTransform):
    """
    Computes a weighted KnoT merge for LoRA experts as in https://arxiv.org/pdf/2410.19735
    """

    def __init__(self, config: KnotMergeConfig = None):
        super().__init__(config or KnotMergeConfig())
        self.ingredients = None

    def transform(self, library) -> Expert:
        if type(library) == str:
            library = ExpertLibrary.get_expert_library(library)
        # TODO: this should probably be stored in the library instead of the local path.
        # Current libary.add_auxiliary_data requires that aux data is associated with an expert, this is not associated with any expert.
        if not os.path.exists(self.config.path):
            U, task_Ss, task_sVs, UsV_dict = self.apply_svd(library)
            self.ingredients = {
                "U": U,
                "task_Ss": task_Ss,
                "task_sVs": task_sVs,  # premultiplied with s, cause Vs alone do not have the scale info.
                "UsV_dict": UsV_dict,
            }

            torch.save(self.ingredients, self.config.path)
        self.ingredients = torch.load(self.config.path)
        task_sVs = self.ingredients["task_sVs"]
        U = self.ingredients["U"]
        ties_mergert = TiesMerge()

        # Prepare for Ties merging of sVs
        expert_vectors = []
        for expert, params in enumerate(task_sVs):
            expert_vectors += [
                torch.nn.utils.parameters_to_vector(
                    list(params[k] for k in params.keys())
                )
            ]
        state_dict = {}
        expert_vectors = torch.stack(expert_vectors, dim=0)
        per_exp_th = expert_vectors.abs().quantile(
            1.0 - ties_mergert.config.top_k, dim=1
        )
        param_names = list(task_sVs[0].keys())
        for p_name in param_names:
            expert_weights = torch.stack([expert[p_name] for expert in task_sVs], dim=0)
            TH = per_exp_th.view(-1, *((1,) * (expert_weights.ndim - 1)))
            final_param, _, _ = ties_mergert.merge_param(TH, expert_weights)
            delta_W = U[p_name] @ final_param  # out_features, in_features
            state_dict[p_name] = delta_W

        return state_dict

    def apply_svd(self, library):
        """
        Reused from https://github.com/gstoica27/KnOTS/blob/main/task_merger.py
        """
        expert_names = list(library.keys())
        experts = [library[name] for name in expert_names]

        logger.info("Knotting {} experts".format(len(experts)))

        base_expert = copy.deepcopy(experts[0])
        base_expert.name = "weighted_expert"

        if self.config.weights is not None:
            assert set(self.config.weights.keys()) == set(
                expert_names
            ), "Weights must have the same keys as the experts"
            if not (1 - 1e-6) <= sum(self.config.weights.values()) <= (1 + 1e-6):
                logger.warning(
                    "Weights do not sum to 1.0, please make sure this is intended"
                )

        layers = set(
            [
                k.split(".lora")[0]
                for k in base_expert.expert_weights.keys()
                if ".lora" in k
            ]
        )
        d_in, d_out = (
            base_expert.expert_weights[f"{list(layers)[0]}.lora_a"].shape[0],
            base_expert.expert_weights[f"{list(layers)[0]}.lora_b"].shape[1],
        )

        UsV_dict = {}
        basis_dict = {}  # basis for reconstruction
        s_compositions_dict = [
            dict() for _ in range(len(experts))
        ]  # singular values composition information per task
        V_compositions_dict = [
            dict() for _ in range(len(experts))
        ]  # basis composition information per task

        for layer in layers:
            Ws = []
            logger.info(f"Computing KnoT merge for layer {layer}")
            # retreieve lora A and B from all experts
            # create W
            for _, expert in zip(expert_names, experts):
                # Validate that the expert is compatible
                assert (
                    type(expert.expert_info.expert_config) == LoRAConfig
                ), "Expert configs must be the same type"
                assert set(expert.expert_weights.keys()) == set(
                    base_expert.expert_weights.keys()
                ), "Expert weights must have the same keys"
                lora_a = expert.expert_weights[f"{layer}.lora_a"]
                lora_b = expert.expert_weights[f"{layer}.lora_b"]
                rank = expert.expert_config.lora_rank
                assert (
                    lora_b.shape[0] == lora_a.shape[1] == rank
                ), "lora_a and lora_a must have the same rank as the expert"
                W = (lora_a @ lora_b).T  # out_features, in_features
                Ws.append(W)

            # SVD
            device = "cuda" if torch.cuda.is_available() else "cpu"
            W_l = torch.cat(Ws, dim=1).to(device)
            U, s, Vt = torch.linalg.svd(W_l, full_matrices=False)
            U = U[:, s > 1e-5]
            Vt = Vt[s > 1e-5]
            s = s[s > 1e-5]
            UsV_dict[layer] = {"U": deepcopy(U), "s": deepcopy(s), "V": deepcopy(Vt)}
            # Set all s to be the same scale
            s[s <= 1e-5] = 0
            cat_hidden_dim = Vt.shape[1] // len(experts)

            basis_dict[layer] = U.cpu()
            sV_concat = Vt
            Vs = list(torch.split(sV_concat, cat_hidden_dim, dim=1))
            for idx, V in enumerate(Vs):
                V = (
                    torch.diag(s) @ V
                )  # WE use Ties merging hat relies on magnitde info, which is not present in Vs only. Comment from original code base: Simple and safe for all merging methods we use.
                s_model = s / s

                s_compositions_dict[idx][layer] = s_model.cpu()
                V_compositions_dict[idx][layer] = V.cpu()
        return basis_dict, s_compositions_dict, V_compositions_dict, UsV_dict


@dataclass
class TiesMergeConfig(LibraryTransformConfig):
    top_k: float = 0.2
    only_sparsify: bool = False


@LibraryTransform.register("ties_merge", TiesMergeConfig)
class TiesMerge(LibraryTransform):
    """
    Computes a uniform weight mixture across experts of a given library
    """

    def __init__(self, config: TiesMergeConfig = None):
        super().__init__(config or TiesMergeConfig())

        assert self.config.top_k > 0.0 and self.config.top_k <= 1.0

    @torch.no_grad()
    def merge_param(self, TH, expert_weights):
        # keep weights over the threshold
        keep_mask = expert_weights.abs() >= TH
        expert_weights = expert_weights * keep_mask
        used = 0

        if self.config.only_sparsify:
            final_param = expert_weights.mean(0)
            used += keep_mask.sum().item()
        else:
            # sign majority vote
            sign_per_dim = expert_weights.sign().sum(0, keepdim=True).sign()
            sign_per_dim = expert_weights.sum(0, keepdim=True).sign()

            # keep only weights whose sign agree with the majority
            use_for_avg = expert_weights.sign() == sign_per_dim

            deno = use_for_avg.sum(0).clamp(min=1.0)
            sum_param = (expert_weights * use_for_avg).sum(0)
            final_param = sum_param / deno
            used += (use_for_avg & (sign_per_dim != 0.0)).sum().item()
        return final_param, used, expert_weights

    @torch.no_grad()
    def transform(self, library) -> Expert:
        if type(library) == str:
            library = ExpertLibrary.get_expert_library(library)

        expert_names = list(library.keys())
        experts = [library[name] for name in expert_names]

        logger.info("Averaging {} experts".format(len(experts)))

        base_expert = copy.deepcopy(experts[0])
        base_expert.name = "ties_weighted_expert"

        state_dict_keys = list(base_expert.expert_weights.keys())

        # Build n_tasks x D experts
        # TODO: No need to build this matrix, can be done 1 expert at a time
        expert_vectors = []
        for expert in experts:
            expert_vectors += [
                torch.nn.utils.parameters_to_vector(
                    list(expert.expert_weights[k] for k in state_dict_keys)
                )
            ]

        expert_vectors = torch.stack(expert_vectors, dim=0)
        per_exp_th = expert_vectors.abs().quantile(1.0 - self.config.top_k, dim=1)
        keep_param = expert_vectors.abs() >= per_exp_th.view(-1, 1)

        mean_valid_per_task = keep_param.float().mean(1)
        assert torch.all((mean_valid_per_task - self.config.top_k).abs() < 1e-4)

        used, kept, total = 0, 0, 0

        for param_name in state_dict_keys:
            # stack the expert weights
            expert_weights = torch.stack(
                [expert.expert_weights[param_name] for expert in experts], dim=0
            )
            TH = per_exp_th.view(-1, *((1,) * (expert_weights.ndim - 1)))
            final_param, used_per_pa, expert_weights = self.merge_param(
                TH, expert_weights
            )

            used += used_per_pa
            kept += (expert_weights.abs() > TH).sum()
            total += expert_weights.numel()

            base_expert.expert_weights[param_name].data.copy_(final_param)

        logger.info(
            "Params not reset to 0 in TIES merge: {:.10f}%".format(100.0 * kept / total)
        )
        logger.info(
            "Params used to compute TIES mean: {:.10f}%".format(100.0 * used / total)
        )

        # manually change the config of the expert to remove the tie_params
        base_expert.expert_config.tie_params = None

        return base_expert


@dataclass
class HiddenStateComputerConfig(LibraryTransformConfig):
    use_base_model_only: bool = (
        False  # This computes sentence embeddings without the adapter
    )
    model: str = (
        None  # If `use_base_model_only`, can pass a specific model to compute embeddings with
    )
    max_samples_per_task: int = 10
    track: str = "each_layer"  # last layer, or each layer
    pool: str = "last"  # last, or mean


@LibraryTransform.register("hidden_state_computer", HiddenStateComputerConfig)
class HiddenStateComputer(LibraryTransform):
    """
    Encodes a dataset and computes the average embedding
    """

    def __init__(self, config: HiddenStateComputerConfig = None):
        super().__init__(config or HiddenStateComputerConfig())

    def _update_args(self, args, default_args):
        for k, v in vars(default_args).items():
            if not hasattr(args, k):
                setattr(args, k, v)

        # Also, overwrite the updated args even if already present
        for k, v in default_args.updated_kwargs.items():
            setattr(args, k, v)

        for arg_name in [
            "include_task_source",
        ]:
            value = getattr(default_args, arg_name, None)
            setattr(args, arg_name, value)

        for arg_name in [
            "include_task_source",
        ]:
            value = getattr(default_args, arg_name, None)
            setattr(args, arg_name, value)

    def _track_hidden_states(self, model, keys=None, device="cpu"):
        model.container = {}

        if model.model is None:
            raise ValueError("Model must have a model attribute")

        if self.config.track == "last_layer":
            # Add a hook to the last layer
            def fetch_input(module, input, output):
                model.container["last_layer"] = input[0].detach().to(device)

            model.model.get_output_embeddings().register_forward_hook(fetch_input)
        elif self.config.track == "each_layer":
            # add a hook for all the layers that an expert modifies
            def build_hook(name):
                def retrieve_input(module, input, output):
                    model.container[name] = input[0].detach().to(device)

                return retrieve_input

            for container in model.experts_containers:
                container.register_forward_hook(build_hook(container.layer_name))
        else:
            raise NotImplementedError()

    def _retrieve_hidden_states(self, model):
        keys = list(model.container.keys())
        values = [model.container[k] for k in keys]
        for key in keys:
            del model.container[key]

        return {k: v for k, v in zip(keys, values)}

    @classmethod
    @torch.no_grad()
    def fetch(cls, library: Union[str, ExpertLibrary], config_hash: str = None):
        if isinstance(library, str):
            library = ExpertLibrary.get_expert_library(library)

        config_hash = config_hash or HiddenStateComputerConfig().save_name

        # try to fetch auxiliary data
        output = library.get_auxiliary_data(data_type=config_hash)

        if len(output) > 0:
            logger.info("Found {} precomputed centroids".format(len(output)))
            return output

        raise ValueError(
            "Hidden states are missing or corrupted, please recompute them."
        )

    @torch.no_grad()
    def transform(
        self,
        library: ExpertLibrary,
        persist=False,
        recompute=False,
        default_args=None,
        device="cpu",
    ) -> Expert:
        from mttl.arguments import ExpertConfig
        from mttl.models.lightning.expert_module import ExpertModule, MultiExpertModule

        if isinstance(library, str):
            library = ExpertLibrary.get_expert_library(library)

        try:
            protos = self.fetch(library, self.config.save_name)

            if not recompute:
                logger.info("Found {} precomputed centroids".format(len(protos)))
                return protos
        except ValueError:
            pass

        logger.info("Computing centroids for {} experts".format(len(library)))
        output = {}

        for _, (expert_name, expert) in enumerate(library.items()):
            training_config = ExpertConfig.from_dict(expert.training_config)

            if default_args is not None:
                self._update_args(training_config, default_args)

            if self.config.use_base_model_only and self.config.model is not None:
                training_config.model = self.config.model

            model = MultiExpertModel(
                MultiExpertModelConfig(
                    base_model=training_config.model,
                ),
                device_map=training_config.device_map,
            )
            if not self.config.use_base_model_only:
                model.add_expert_instance(expert, is_default=True)

            self._track_hidden_states(model, device=device)

            training_config.dataset = expert.expert_info.dataset
            training_config.subsample_train = self.config.max_samples_per_task
            if expert.expert_info.expert_task_name:
                train_tasks = expert.expert_info.expert_task_name.split(",")
                training_config.finetune_task_name = ",".join(train_tasks)
                training_config.subsample_train *= len(train_tasks)
            else:
                train_tasks = None

            training_config.train_batch_size = (
                default_args.predict_batch_size if default_args is not None else 4
            )

            # get datamodule
            dm = get_datamodule(training_config)
            dataloader = dm.train_dataloader()

            centroid, count = defaultdict(lambda: 0.0), 0

            pbar = tqdm(enumerate(dataloader), total=len(dataloader))
            device_model = next(model.parameters()).device

            for _, batch in pbar:
                batch = transfer_batch_to_device(batch, device_model)
                model.forward(**batch)

                bs = batch["input_ids"].size(0)
                last_token_idx = batch["attention_mask"].sum(1).to(device) - 1
                hidden_states = self._retrieve_hidden_states(model)
                bs_idx = torch.arange(
                    bs, device=hidden_states[list(hidden_states.keys())[0]].device
                )

                for layer, hidden_state in hidden_states.items():
                    assert hidden_state.ndim == 3

                    if self.config.pool == "last":
                        centroid[layer] += hidden_state[bs_idx, last_token_idx].sum(0)
                    elif self.config.pool == "mean":
                        deno = batch["attention_mask"].sum(1, keepdim=True)
                        centroid[layer] += (
                            (hidden_state * batch["attention_mask"].unsqueeze(-1)).sum(
                                1
                            )
                            / deno
                        ).sum(0)
                    else:
                        raise NotImplementedError()

                count += bs

            # average over all batches
            for layer in centroid.keys():
                centroid[layer] /= count
                centroid[layer] = F.normalize(centroid[layer], p=2, dim=-1).cpu()

            # convert to regular dict
            centroids = {k: v for k, v in centroid.items()}
            output[expert_name] = centroids

            del model

        if persist:
            # add embeddings to the library
            with library.batched_commit():
                for expert_name, data in output.items():
                    library.add_auxiliary_data(
                        data_type=self.config.save_name,
                        expert_name=expert_name,
                        config=self.config.__dict__,
                        data=data,
                        force=True,  # make sure we overwrite
                    )
        return output


@dataclass
class PhatgooseTransformConfig(LibraryTransformConfig):
    n_steps: int = 100
    learning_rate: float = 1e-3
    warmup_ratio: float = 0.1
    micro_batch_size: int = 4
    batch_size: int = 4
    seed: int = 42


@LibraryTransform.register("phatgoose", PhatgooseTransformConfig)
class PhatgooseTransform(HiddenStateComputer):
    def __init__(self, config: PhatgooseTransformConfig = None):
        super().__init__(config or PhatgooseTransformConfig())

    @classmethod
    @torch.no_grad()
    def fetch(cls, library: Union[str, ExpertLibrary], config_hash: str):
        if isinstance(library, str):
            library = ExpertLibrary.get_expert_library(library)

        config_hash = config_hash or PhatgooseTransformConfig().save_name

        # try to fetch auxiliary data
        output = library.get_auxiliary_data(data_type=config_hash)

        if len(output) != len(library):
            logger.warning(
                "Found {} precomputed Phatgoose prototypes. Some experts might not have prototypes.".format(
                    len(output)
                )
            )

        return output

    def transform(
        self,
        library,
        persist: bool = True,
        recompute: bool = False,
        expert_names: list = None,
        default_args=None,
    ):
        from mttl.arguments import ExpertConfig
        from mttl.models.lightning.expert_module import MultiExpertModule

        if type(library) == str:
            library = ExpertLibrary.get_expert_library(library)

        outputs = {}
        expert_names = expert_names or list(library.keys())
        loaded_output = library.get_auxiliary_data(data_type=self.config.save_name)

        for expert_name in expert_names:
            logger.info(f"Computing PHATGOOSE gates for expert {expert_name}")
            expert: Expert = library[expert_name]
            logger.info("Phatgoose save name : {}".format(self.config.save_name))

            if not recompute and expert_name in loaded_output:
                logger.info("Loading precomputed gates for {}".format(expert_name))

                # format is dict[layer_name] = embedding, layer_name ends with selector.{task_name}.v
                outputs[expert_name] = loaded_output[expert_name]
                continue

            training_config: ExpertConfig = ExpertConfig.from_dict(
                expert.training_config
            )

            if default_args is not None:
                self._update_args(training_config, default_args)

            training_config.trainable_param_names = ".*selector.*"
            training_config.weight_decay = 0.0
            training_config.total_steps = self.config.n_steps
            training_config.learning_rate = self.config.learning_rate
            training_config.warmup_proportion = self.config.warmup_ratio
            training_config.train_batch_size = self.config.batch_size
            training_config.micro_batch_size = self.config.micro_batch_size
            training_config.dataset = expert.expert_info.dataset

            if expert.expert_info.expert_task_name:
                train_tasks = expert.expert_info.expert_task_name.split(",")
                training_config.finetune_task_name = ",".join(train_tasks)
            else:
                train_tasks = None

            dm = get_datamodule(training_config)

            logger.info("Training config: {}".format(vars(training_config)))

            model = MultiExpertModel(
                MultiExpertModelConfig(
                    base_model=training_config.model,
                    selector_config=PhatgooseTrainerSelectorConfig(
                        lora_merge_after=True,
                    ),
                ),
                precision=training_config.precision,
                device_map="cuda" if torch.cuda.is_available() else "cpu",
            )
            model.add_expert_instance(expert, is_default=True)

            # for checksum
            frozen_sum, unfrozen_sum = 0, 0
            for key, value in model.state_dict().items():
                if re.match(".*selector.gates.*.v", key):
                    assert torch.allclose(
                        value, torch.zeros_like(value)
                    ), "gate should be 0 init"
                    unfrozen_sum += value.sum()
                else:
                    frozen_sum += value.sum()
                    value.requires_grad = False

            train_model(training_config, model, dm)

            # for checksum
            frozen_sum_after, unfrozen_sum_after = 0, 0
            for key, value in model.state_dict().items():
                if re.match(".*selector.gates.*.v", key):
                    unfrozen_sum_after += value.sum()
                else:
                    frozen_sum_after += value.sum()

            assert (
                frozen_sum == frozen_sum_after
            ), "Frozen params changed during training"
            assert (
                unfrozen_sum != unfrozen_sum_after
            ), "Unfrozen params did not change during training"

            # extract prototypes
            prototypes = {}
            for name, module in model.model.named_modules():
                if isinstance(module, ExpertContainer) and hasattr(
                    module.selector, "get_prototypes"
                ):
                    # expand dict
                    prototypes_module = {}
                    for k, v in module.selector.get_prototypes().items():
                        prototypes_module[f"{name}.selector.{k}.v"] = v
                    prototypes = {**prototypes, **prototypes_module}

            outputs[expert_name] = prototypes

            if persist:
                with library.batched_commit():
                    for expert_name, data in outputs.items():
                        library.add_auxiliary_data(
                            data_type=self.config.save_name,
                            expert_name=expert_name,
                            config=self.config.__dict__,
                            data=data,
                            force=True,  # make sure we overwrite
                        )
            del model
        return outputs


@dataclass
class ArrowTransformConfig(LibraryTransformConfig):
    ab_only: bool = True
    scale: bool = False  # If True, scale by eigenvalue
    tie_params: str = (
        "default"  # If default, ties the same params as during training. If a regex, processed the same way as during training
    )
    tie_op: str = "concat"  # or "sum"


@LibraryTransform.register("arrow", ArrowTransformConfig)
class ArrowTransform(LibraryTransform):
    """
    Given a library of experts, extract the input direction most affected by the linear transforms
    """

    def __init__(self, config: ArrowTransformConfig = None):
        super().__init__(config or ArrowTransformConfig())

    def _maybe_scale(self, vectors, eigvals):
        """
        Post Processing of the retrieved outputs,
        scales the output by the eigenvalue if needed.
        """
        output = {}
        for expert_name, expert_data in vectors.items():
            output[expert_name] = {}
            for layer_name, vector in expert_data.items():
                if self.config.scale:
                    vector = vector * eigvals[expert_name][layer_name]
                output[expert_name][layer_name] = torch.from_numpy(vector)
        return output

    def _low_rank_svd(self, A, B):
        """Faster SVD computation for low rank matrices"""

        # Compute SVD of A
        U_A, Sigma_A, V_A = torch.svd(A)

        # Compute SVD of B.T (transpose of B)
        U_B, Sigma_B, V_B = torch.svd(B.T)

        # Compute product matrix C = Sigma_A * (V_A.T @ V_B) * Sigma_B
        # Since V_A and V_B are orthogonal, their product is also an orthogonal matrix
        C = Sigma_A.diag_embed() @ V_A.t() @ V_B @ Sigma_B.diag_embed()

        # Compute SVD of the product matrix C
        U_C, Sigma_C, V_C = torch.svd(C)

        # Construct the final SVD components of W
        U_W = U_A @ U_C
        V_W_T = V_C.t() @ U_B.t()

        diff_AB = (U_W.T @ U_A).abs().diag()
        if diff_AB[0] < 0.9:
            logger.debug("The first singular vector of U_A and U_AB are not aligned")

        return U_W, Sigma_C, V_W_T

    def _get_unique_parent_names(self, alist):
        """
        if adict.keys() = ['model.layer1.lora_a', 'model.layer.lora_b', 'model.layer2.lora_a']
        output will be {'model.layer1', 'model.layer2'}
        """
        dict_keys = sorted(list(set(".".join(k.split(".")[:-1]) for k in alist)))
        return dict_keys

    @classmethod
    @torch.no_grad()
    def fetch(cls, library: Union[str, ExpertLibrary], config_hash: str):
        """Fetch arrow prototypes from the library, raises ValueError if they are not computed.

        Args:
            library (Union[str, ExpertLibrary]): ExpertLibrary object or its name
            scale (bool): If True, scale the output by the eigenvalue
        """
        if not isinstance(library, ExpertLibrary):
            library = ExpertLibrary.get_expert_library(library)

        config_hash = config_hash or ArrowTransformConfig().save_name

        # try to fetch auxiliary data
        protos = library.get_auxiliary_data(data_type=config_hash + "_protos")
        return protos

    @torch.no_grad()
    def transform(
        self,
        library,
        persist=True,
        recompute=False,
    ) -> Expert:
        logger.info("Arrow save name : {}".format(self.config.save_name))

        if isinstance(library, str):
            library = ExpertLibrary.get_expert_library(library)

        base_model = None

        # Try to fetch the precomputed Arrow prototypes
        protos = self.fetch(library, self.config.save_name)
        already_computed = []

        vectors = {}
        eigvals = {}
        for expert_name, expert in library.items():
            if expert_name in protos and not recompute:
                logger.info(
                    "Found precomputed Arrow prototypes for expert {}".format(
                        expert_name
                    )
                )
                already_computed.append(expert_name)
                continue

            logger.info(f"Computing SVD for expert {expert_name}")
            vectors[expert_name] = {}
            eigvals[expert_name] = {}

            if base_model is None and not self.config.ab_only:
                training_config = expert.training_config
                training_config.model_modifier = None
                from mttl.models.lightning.expert_module import MultiExpertModule

                base_model = MultiExpertModule(**vars(training_config))

            # get parameters tied during training
            param_map = get_target_2_source_param_mapping(
                expert.expert_weights.items(),
                expert.expert_info.expert_config.tie_params,
            )
            if self.config.tie_params != "default":
                # get parameters we wish to tie for Arrow
                _tied_params = get_target_2_source_param_mapping(
                    expert.expert_weights.items(), self.config.tie_params
                )
                # Make sure that params tied during training are also tied for Arrow
                if any(key not in _tied_params for key in param_map):
                    logger.warning(
                        "Some parameters that are tied during training are not tied during Arrow computation."
                    )
                param_map = _tied_params

            tied_params = list(param_map.keys()) + list(param_map.values())
            assert all(
                "lora_b" not in param_name for param_name in tied_params
            ), "Support for tied B not available"
            assert all(
                "lora_a" in param_name for param_name in tied_params
            ), "Only support tied As for now"

            # Now that we know only A's are tied, we can proceed using only the parent names
            # e.g. 'model.layers.30.self_attn.q_proj' instead of 'model.layers.30.self_attn.q_proj.lora_a'
            tied_parents = self._get_unique_parent_names(tied_params)

            untied_parents = [
                parent
                for parent in self._get_unique_parent_names(
                    expert.expert_weights.keys()
                )
                if parent not in tied_parents
            ]

            # Build a mapping from source to target parameters
            # e.g. <name_of_parent_of_param> : [<list of all other params tied to it>]
            # NOTE: list will be empty if the param is not tied to anything
            tied_param_bins = defaultdict(list)

            for tgt_name, src_name in param_map.items():
                parent_src = ".".join(src_name.split(".")[:-1])
                parent_tgt = ".".join(tgt_name.split(".")[:-1])
                tied_param_bins[parent_src].append(parent_tgt)
            for parent in untied_parents:
                tied_param_bins[parent] = []

            for parent_name, dependents in tied_param_bins.items():
                logger.info(f"\tComputing SVD for parameter {parent_name}")

                parent_names = [parent_name]
                A_name, B_name = f"{parent_name}.lora_a", f"{parent_name}.lora_b"
                As = [expert.expert_weights[A_name]]
                Bs = [expert.expert_weights[B_name]]
                base_W = []

                for tied_module in dependents:
                    logger.info(f"\t\t\tTying Arrow with {tied_module}")
                    As += [expert.expert_weights[f"{tied_module}.lora_a"]]
                    Bs += [expert.expert_weights[f"{tied_module}.lora_b"]]
                    parent_names += [tied_module]

                    if not self.config.ab_only:
                        base_W += [
                            base_model.model.state_dict()[f"{tied_module}.weight"]
                        ]

                if len(As) > 1:
                    if self.config.tie_op == "concat":
                        # Mimicking phi-2 behavior
                        assert self.config.ab_only
                        assert all(
                            torch.allclose(A, As[0]) for A in As
                        ), "A should be the same for all tied parameters"
                        A = As[0]
                        B = torch.cat(Bs, dim=1)
                    elif self.config.tie_op == "sum":
                        # A1B1 + A2B2 == [A1 A2] [B1; B2].
                        # We do it this way to leverage the low-rank SVD
                        A = torch.cat(As, dim=1)
                        B = torch.cat(Bs, dim=0)
                    else:
                        raise NotImplementedError()
                else:
                    A, B = As[0], Bs[0]

                # Reshape As and Bs (needed for Poly / MHR weights)
                rank = expert.expert_config.lora_rank
                A = A.reshape(-1, rank).float()
                B = B.reshape(rank, -1).float()

                W = (A @ B).T  # out_features, in_features

                if self.config.ab_only:
                    U_W, Sigma_W, _ = self._low_rank_svd(A, B)
                    top_value = Sigma_W[0] ** 2
                    bottom_vector = U_W[:, -1]
                    top_vector = U_W[:, 0]
                else:
                    base_W += [
                        base_model.model.state_dict()[f"{parent_name}.weight"]
                    ].float()
                    base_W = torch.stack(base_W).sum(0)
                    W += base_W
                    U, E, Vt = torch.linalg.svd(W)
                    top_vector = Vt[0]
                    bottom_vector = Vt[-1]
                    top_value = E[0]

                # Check that top vector is indeed an eigenvector
                WTW = W.T @ W
                ratio = WTW @ top_vector / (top_vector * top_value)
                torch.allclose(ratio, torch.ones_like(ratio), atol=1e-3)

                # Check that top vector is indeed the top eigenvector
                assert (WTW @ top_vector).pow(2).sum() >= (WTW @ bottom_vector).pow(
                    2
                ).sum()

                # Save eigenvector and eigvenvalue
                for parent in parent_names:
                    assert parent not in vectors[expert_name]
                    vectors[expert_name][parent] = top_vector.real.cpu().numpy()
                    eigvals[expert_name][parent] = top_value.item()

        to_upload = [x for x in library.keys() if x not in already_computed]
        new_protos = self._maybe_scale(vectors, eigvals)

        if persist and len(to_upload) > 0:
            # add embeddings to the library
            with library.batched_commit():
                for expert_name in to_upload:
                    logger.info(
                        f"Uploading centroids to the library for expert {expert_name}"
                    )
                    for data_name, data in [
                        ("vectors", vectors),
                        ("eigvals", eigvals),
                        ("protos", new_protos),
                    ]:
                        library.add_auxiliary_data(
                            data_type=self.config.save_name + "_" + data_name,
                            expert_name=expert_name,
                            config=self.config.__dict__,
                            data=data[expert_name],
                            force=True,  # make sure we overwrite
                        )

        protos.update(new_protos)
        return protos


@dataclass
class ExpertProjectorConfig:
    granularity: str = (
        "finegrained"  # whether to use the same coefficients for all parameters or per `nn.Parameter` instance
    )
    project_over_all_experts: bool = (
        False  # whether to project over all experts or just the ones in the cluster
    )


@LibraryTransform.register("expert_projector", ExpertProjectorConfig)
class ExpertProjector(LibraryTransform):
    """
    Given a library of clustered experts, project each one onto the basis generated
    by the individual experts of each cluster.
    """

    def __init__(self, config: ExpertProjectorConfig = None):
        super().__init__(config or ExpertProjectorConfig())

    def _project(self, source_expert, expert_basis, granularity="coarsegrained"):
        source_sd = source_expert.expert_weights
        state_dict_keys = list(source_sd.keys())

        assert set(state_dict_keys) == set(
            expert_basis[0].expert_weights.keys()
        ), breakpoint()

        if granularity == "coarsegrained":
            # build a n_experts x D matrix of concatenated parameters
            basis_vectors = []
            for expert in expert_basis:
                basis_vectors += [
                    torch.nn.utils.parameters_to_vector(
                        list(expert.expert_weights[k] for k in state_dict_keys)
                    )
                ]
            basis_vector = torch.stack(basis_vectors)
            project_vector = torch.nn.utils.parameters_to_vector(
                list(source_sd[k] for k in state_dict_keys)
            )

            # Treat as a min-squares problem
            global_alpha = torch.linalg.lstsq(
                basis_vector.T, project_vector.view(-1, 1)
            ).solution
        else:
            assert granularity == "finegrained"

        projected_expert = copy.deepcopy(source_expert)
        for key in state_dict_keys:
            basis_vector = torch.stack(
                [expert.expert_weights[key].flatten() for expert in expert_basis]
            )

            if granularity == "coarsegrained":
                alpha = global_alpha
            else:
                alpha = torch.linalg.lstsq(
                    basis_vector.T, source_sd[key].view(-1, 1)
                ).solution

            # project the source expert onto the basis
            projected = (basis_vector.T @ alpha).view(source_sd[key].shape)
            projected_expert.expert_weights[key].data.copy_(projected)

        return projected_expert

    @torch.no_grad()
    def transform(self, expert_library, cluster_library) -> Expert:
        if isinstance(expert_library, str):
            expert_library = ExpertLibrary.get_expert_library(expert_library)

        if isinstance(cluster_library, str):
            cluster_library = ExpertLibrary.get_expert_library(cluster_library)

        output = {}
        for cluster_name, cluster_exp in cluster_library.items():
            logger.info(f"processing cluster {cluster_name}")
            if self.config.project_over_all_experts:
                task_experts = [
                    expert_library[expert_name] for expert_name in expert_library.keys()
                ]
            else:
                tasks = cluster_exp.expert_info.expert_task_name.split(",")
                task_experts = [expert_library[expert_name] for expert_name in tasks]
            projected_expert = self._project(
                cluster_exp, task_experts, granularity=self.config.granularity
            )
            output[cluster_name] = projected_expert

        return output


@dataclass
class CrossExpertNormComputerConfig:
    pass


@LibraryTransform.register("cross_expert_norm_computer", CrossExpertNormComputerConfig)
class CrossExpertNormComputer(HiddenStateComputer):
    """
    Given a library of experts, compute the norm of ABx for both in-dist and ood experts
    """

    def __init__(self, config: CrossExpertNormComputerConfig = None):
        super().__init__(config or CrossExpertNormComputerConfig())

    @torch.no_grad()
    def transform(self, library, default_args=None) -> Expert:
        if isinstance(library, str):
            library = ExpertLibrary.get_expert_library(library)

        expert_names = list(library.keys())
        an_expert = library[expert_names[0]]
        training_config = an_expert.training_config

        # overwrite required args
        training_config.library_id = library.repo_id
        training_config.router_selector = "task_selector"

        if default_args is not None:
            self._update_args(training_config, default_args)

        training_config.train_batch_size = (
            default_args.predict_batch_size if default_args is not None else 4
        )
        training_config.finetune_task_name = ",".join(
            [
                library[exp_name].training_config.finetune_task_name
                for exp_name in library.keys()
            ]
        )

        from mttl.models.containers import ExpertContainer
        from mttl.models.lightning.expert_module import ExpertModule, MoEModel

        model = MoEModel(**vars(training_config))

        # build a hook to forward across other (ood) experts
        def build_hook(layer_name, container, task_id_container):
            def retrieve_input(module, input, output):
                task_names = task_id_container["routing_infos"].task_names
                attn_mask = task_id_container["routing_infos"].attention_mask
                container[layer_name] = input[0].detach()

                # output (bs, seq_len, D) is the correctly routed outpu
                # let's generate the outputs for random task routing

                not_picked = np.array(
                    list(set(module.selector.expert_names) - set(task_names))
                )
                random_tasks = np.random.choice(
                    not_picked,
                    size=len(task_names),
                    replace=not_picked.size < len(task_names),
                )

                # Redo ExpertContainer forward
                selector_out = module.selector(input[0])
                selector_out.experts = random_tasks.tolist()
                random_out = module.route(input[0], selector_out)

                norm_correct = (output * attn_mask.unsqueeze(-1)).pow(2).sum(
                    -1
                ).sqrt().sum() / attn_mask.sum()
                norm_wrong = (random_out * attn_mask.unsqueeze(-1)).pow(2).sum(
                    -1
                ).sqrt().sum() / attn_mask.sum()

                container[layer_name] = (norm_correct, norm_wrong)

                return output

            return retrieve_input

        hooks = []
        container = {}
        for module_name, module in model.named_experts():
            if isinstance(module, ExpertContainer):
                hook = build_hook(module_name, container, model.model.task_id_container)
                module.register_forward_hook(hook)
                hooks += [hook]

        logger.info(f"set {len(hooks)} hooks")
        training_config.subsample_train = 2_000
        dm = get_datamodule(training_config)
        dataloader = dm.train_dataloader()

        pbar = tqdm(enumerate(dataloader), total=len(dataloader))
        device = next(model.parameters()).device

        total_avg_diff, total_rel_diff = [], []
        for num_batch, batch in pbar:
            batch = transfer_batch_to_device(batch, device)

            if isinstance(model, ExpertModule):
                model.forward(batch, reduction="none")
            else:
                model.forward(
                    input_ids=batch["input_ids"],
                    attention_mask=batch["attention_mask"],
                )

            avg_diff, rel_diff = 0, 0
            for layer, (correct, wrong) in container.items():
                avg_diff += (correct - wrong).item()
                rel_diff += (correct / wrong).item()

            avg_diff /= len(container)
            rel_diff /= len(container)

            total_avg_diff += [avg_diff]
            total_rel_diff += [rel_diff]

            print(
                f"avg_diff: {avg_diff / len(container)}, rel_diff: {rel_diff / len(container)}"
            )


@dataclass
class MBClusteringTransformConfig(SVDEmbeddingTransformConfig):
    random_state: int = 42
    k: int = 10


@LibraryTransform.register("mbc_with_cos_sim", MBClusteringTransformConfig)
class MBCWithCosSimTransform(LibraryTransform):
    """
    Computes clusters based on the embedding similarity of the experts.
    The input to KMeans is the cosine similarity matrix between the experts' embeddings.
    """

    def __init__(self, config: MBClusteringTransformConfig = None):
        super().__init__(config or MBClusteringTransformConfig())

    def transform(
        self,
        library: ExpertLibrary,
        persist: bool = False,
        recompute: bool = False,
    ) -> Dict[str, List[str]]:
        svd_config = SVDEmbeddingTransformConfig(
            name=self.config.name,
            n_components=self.config.n_components,
            sparsity_threshold=self.config.sparsity_threshold,
        )

        def create_embeddings():
            svd_embedder = SVDEmbeddingTransform(
                svd_config,
                random_state=self.config.random_state,
            )
            embeddings = svd_embedder.transform(library, persist=persist)
            del svd_embedder
            return embeddings

        embeddings = library.get_auxiliary_data(svd_config.save_name)

        if len(embeddings) != len(library) or recompute:
            logger.info("Recomputing embeddings for clustering.")
            embeddings = create_embeddings()

        # Extract the embeddings as a numpy array
        expert_names, embeddings = zip(*sorted(embeddings.items()))

        embeddings_array = np.stack(embeddings)
        cosine_sim_matrix = cosine_similarity(embeddings_array, embeddings_array)

        kmeans = KMeans(
            n_clusters=self.config.k,
            init="k-means++",
            n_init=10,
            random_state=self.config.random_state,
        )
        kmeans.fit(cosine_sim_matrix)
        cluster_labels = kmeans.labels_
        clusters = defaultdict(list)

        for key, label in zip(expert_names, cluster_labels):
            clusters[f"cluster_{label}"].append(key)
        return clusters
