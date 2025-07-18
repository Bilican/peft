# Copyright 2025-present the HuggingFace Inc. team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import warnings
from typing import Any, List, Optional, Union

import torch
import torch.nn as nn
import torch.nn.functional as F
from transformers.pytorch_utils import Conv1D

from peft.tuners.tuners_utils import BaseTunerLayer, check_adapters_to_merge

import sys
import os


from .waverec2d import waverec2d


class WaveFTLayer(BaseTunerLayer):
    # All names of layers that may contain (trainable) adapter weights
    adapter_layer_names = ("waveft_spectrum",)
    # All names of other parameters that may contain adapter-related parameters
    other_param_names = ("waveft_n_frequency", "waveft_scaling", "waveft_random_loc_seed", "waveft_wavelet_family")

    # Reduction amounts for different wavelet families
    WAVELET_REDUCTIONS = {
        'db1': (0, 0), 'db2': (2, 2), 'db3': (4, 4), 'db4': (6, 6), 'db5': (8, 8), 
        'db6': (10, 10), 'db7': (12, 12), 'db8': (14, 14), 'db9': (16, 16), 'db10': (18, 18), 
        'db11': (20, 20), 'db12': (22, 22), 'db13': (24, 24), 'db14': (26, 26), 'db15': (28, 28), 
        'db16': (30, 30), 'db17': (32, 32), 'db18': (34, 34), 'db19': (36, 36), 'db20': (38, 38), 
        'db21': (40, 40), 'db22': (42, 42), 'db23': (44, 44), 'db24': (46, 46), 'db25': (48, 48), 
        'db26': (50, 50), 'db27': (52, 52), 'db28': (54, 54), 'db29': (56, 56), 'db30': (58, 58), 
        'db31': (60, 60), 'db32': (62, 62), 'db33': (64, 64), 'db34': (66, 66), 'db35': (68, 68), 
        'db36': (70, 70), 'db37': (72, 72), 'db38': (74, 74), 'sym2': (2, 2), 'sym3': (4, 4), 
        'sym4': (6, 6), 'sym5': (8, 8), 'sym6': (10, 10), 'sym7': (12, 12), 'sym8': (14, 14), 
        'sym9': (16, 16), 'sym10': (18, 18), 'sym11': (20, 20), 'sym12': (22, 22), 'sym13': (24, 24), 
        'sym14': (26, 26), 'sym15': (28, 28), 'sym16': (30, 30), 'sym17': (32, 32), 'sym18': (34, 34), 
        'sym19': (36, 36), 'sym20': (38, 38), 'coif1': (4, 4), 'coif2': (10, 10), 'coif3': (16, 16), 
        'coif4': (22, 22), 'coif5': (28, 28), 'coif6': (34, 34), 'coif7': (40, 40), 'coif8': (46, 46), 
        'coif9': (52, 52), 'coif10': (58, 58), 'coif11': (64, 64), 'coif12': (70, 70), 'coif13': (76, 76), 
        'coif14': (82, 82), 'coif15': (88, 88), 'coif16': (94, 94), 'coif17': (100, 100)
    }

    def __init__(self, base_layer: nn.Module, **kwargs) -> None:
        self.base_layer = base_layer
        self.waveft_n_frequency = {}
        self.waveft_scaling = {}
        self.waveft_spectrum = nn.ParameterDict({})
        self.waveft_wavelet_family = {}
        self.indices = {}
        self.waveft_random_loc_seed = {}
        # Mark the weight as unmerged
        self._disable_adapters = False
        self.merged_adapters = []
        self.kwargs = kwargs

        base_layer = self.get_base_layer()
        if isinstance(base_layer, nn.Linear):
            self.in_features, self.out_features = base_layer.in_features, base_layer.out_features
        elif isinstance(base_layer, Conv1D):
            self.in_features, self.out_features = (
                base_layer.weight.ds_shape if hasattr(base_layer.weight, "ds_shape") else base_layer.weight.shape
            )
        else:
            raise ValueError(f"Unsupported layer type {type(base_layer)}")

    def update_layer(self, adapter_name, n_frequency, scaling, init_weights, random_loc_seed, wavelet_family="db1"):
        if n_frequency <= 0:
            raise ValueError(f"`n_frequency` should be a positive integer value but the value passed is {n_frequency}")
        if n_frequency > self.in_features * self.out_features:
            raise ValueError(
                f"`n_frequency` should be less than or equal to the product of the input and output dimensions "
                f"but the value passed is {n_frequency} and the product is {self.in_features * self.out_features}"
            )
        if wavelet_family not in self.WAVELET_REDUCTIONS:
            supported_wavelets = list(self.WAVELET_REDUCTIONS.keys())
            raise ValueError(
                f"Unsupported wavelet family: {wavelet_family}. "
                f"Supported wavelet families: {supported_wavelets}"
            )
            
        self.waveft_n_frequency[adapter_name] = n_frequency
        self.waveft_random_loc_seed[adapter_name] = random_loc_seed
        self.waveft_wavelet_family[adapter_name] = wavelet_family
        
        # Get the expanded dimensions based on wavelet family
        reduction_rows, reduction_cols = self.WAVELET_REDUCTIONS[wavelet_family]
        
        # Generate random indices within the original dimensions
        # We handle padding separately in get_delta_weight
        generator = torch.Generator().manual_seed(self.waveft_random_loc_seed[adapter_name])
        indices = torch.randperm(self.out_features * self.in_features, generator=generator)[:n_frequency]
        
        # Convert to row, col format for the original dimensions
        self.indices[adapter_name] = torch.stack(
            [indices // self.in_features, indices % self.in_features], dim=0
        )
        
        self.waveft_scaling[adapter_name] = scaling
        
        # Actual trainable parameters
        # Initialize based on init_weights
        if init_weights:
            # Initialize with zeros later using reset_wave_parameters
            self.waveft_spectrum[adapter_name] = nn.Parameter(torch.empty(n_frequency), requires_grad=True)
            self.reset_wave_parameters(adapter_name) # Initialize to zeros now
        else:
            # Initialize with randn scaled by a small std dev to prevent explosion
            std_dev = 0.01  # Using a small std dev for initial random weights
            self.waveft_spectrum[adapter_name] = nn.Parameter(torch.randn(n_frequency) * std_dev, requires_grad=True)

        self._move_adapter_to_device_of_base_layer(adapter_name)
        self.set_adapter(self.active_adapters)

    @torch.no_grad()
    def reset_wave_parameters(self, adapter_name):
        if adapter_name in self.waveft_spectrum.keys():
            nn.init.zeros_(self.waveft_spectrum[adapter_name])

    def get_delta_weight(self, adapter) -> torch.Tensor:
        spectrum = self.waveft_spectrum[adapter]
        indices = self.indices[adapter].to(spectrum.device)
        wavelet_family = self.waveft_wavelet_family[adapter]
        
        # Choose whether to use IDWT or direct spectrum based on kwargs
        if self.kwargs.get("use_idwt", True):
            reduction_rows, reduction_cols = self.WAVELET_REDUCTIONS[wavelet_family]
            
            # Create a padded spectrum matrix with additional rows and columns
            # to account for the reduction during wavelet reconstruction
            padded_out_features = self.out_features + reduction_rows
            padded_in_features = self.in_features + reduction_cols
            
            # Make dimensions even if needed for wavelet processing
            if padded_out_features % 2 != 0:
                padded_out_features += 1
            if padded_in_features % 2 != 0:
                padded_in_features += 1
                
            # Create the padded dense spectrum matrix
            dense_spectrum = torch.zeros(padded_out_features, padded_in_features, 
                                        device=spectrum.device, dtype=spectrum.dtype)
            
            # Calculate padding offsets to center the original data in the padded matrix
            row_offset = (padded_out_features - self.out_features) // 2
            col_offset = (padded_in_features - self.in_features) // 2
            
            # Adjust indices to account for padding offsets
            padded_indices = indices.clone()
            padded_indices[0, :] += row_offset
            padded_indices[1, :] += col_offset
            
            # Place spectrum values in the padded matrix
            # Filter out any indices that would be out of bounds
            valid_mask = (padded_indices[0, :] < padded_out_features) & (padded_indices[1, :] < padded_in_features)
            valid_indices = padded_indices[:, valid_mask]
            valid_spectrum = spectrum[valid_mask]
            
            # Set the spectrum values in the padded matrix
            dense_spectrum[valid_indices[0, :], valid_indices[1, :]] = valid_spectrum
            
            # Split into four sub-bands
            H, W = dense_spectrum.shape
            H2, W2 = H // 2, W // 2
            cA = dense_spectrum[:H2, :W2]      # top-left
            cH = dense_spectrum[:H2, W2:]      # top-right
            cV = dense_spectrum[H2:, :W2]      # bottom-left
            cD = dense_spectrum[H2:, W2:]      # bottom-right

            # Construct wavelet-coefficient tuple
            coeffs = (cA, (cH, cV, cD))
            
            # Reconstruct with the specified wavelet family
            delta_weight = waverec2d(coeffs, wavelet_family) * self.waveft_scaling[adapter]
            
            # Ensure the delta weight has exactly the correct dimensions
            if delta_weight.shape[0] != self.out_features or delta_weight.shape[1] != self.in_features:
                # Calculate where to start slicing to get a centered crop
                start_row = (delta_weight.shape[0] - self.out_features) // 2
                start_col = (delta_weight.shape[1] - self.in_features) // 2
                
                # Slice to the exact output size needed
                delta_weight = delta_weight[
                    start_row:start_row + self.out_features, 
                    start_col:start_col + self.in_features
                ]
        else:
            # Simple direct use of spectrum without IDWT
            dense_spectrum = torch.zeros(self.out_features, self.in_features, device=spectrum.device, dtype=spectrum.dtype)
            dense_spectrum[indices[0, :], indices[1, :]] = spectrum
            delta_weight = dense_spectrum * self.waveft_scaling[adapter]
            
        return delta_weight


class WaveFTLinear(nn.Module, WaveFTLayer):
    # WaveFT implemented in a dense layer
    def __init__(
        self,
        base_layer,
        adapter_name: str,
        n_frequency: int = 1000,
        scaling: float = 150.0,
        fan_in_fan_out: bool = False,  # Set this to True if the layer to replace stores weight like (fan_in, fan_out)
        init_weights: Union[bool, str] = False,
        random_loc_seed: int = 777,
        wavelet_family: str = "db1",
        use_idwt: bool = True,
        **kwargs,
    ) -> None:
        super().__init__()
        # Pass use_idwt to kwargs so it's available in get_delta_weight
        kwargs["use_idwt"] = use_idwt
        WaveFTLayer.__init__(self, base_layer, **kwargs)
        self.fan_in_fan_out = fan_in_fan_out
        self._active_adapter = adapter_name
        self.update_layer(adapter_name, n_frequency, scaling, init_weights, random_loc_seed, wavelet_family)

    def merge(self, safe_merge: bool = False, adapter_names: Optional[List[str]] = None) -> None:
        """
        Merge the active adapter weights into the base weights

        Args:
            safe_merge (`bool`, *optional*):
                If True, the merge operation will be performed in a copy of the original weights and check for NaNs
                before merging the weights. This is useful if you want to check if the merge operation will produce
                NaNs. Defaults to `False`.
            adapter_names (`List[str]`, *optional*):
                The list of adapter names that should be merged. If None, all active adapters will be merged. Defaults
                to `None`.
        """
        adapter_names = check_adapters_to_merge(self, adapter_names)
        if not adapter_names:
            # no adapter to merge
            return

        for active_adapter in adapter_names:
            if active_adapter in self.waveft_spectrum.keys():
                base_layer = self.get_base_layer()
                if safe_merge:
                    # Note that safe_merge will be slower than the normal merge
                    # because of the copy operation.
                    orig_weights = base_layer.weight.data.clone()
                    orig_weights += self.get_delta_weight(active_adapter)

                    if not torch.isfinite(orig_weights).all():
                        raise ValueError(
                            f"NaNs detected in the merged weights. The adapter {active_adapter} seems to be broken"
                        )

                    base_layer.weight.data = orig_weights
                else:
                    base_layer.weight.data += self.get_delta_weight(active_adapter)
                self.merged_adapters.append(active_adapter)

    def unmerge(self) -> None:
        """
        This method unmerges all merged adapter layers from the base weights.
        """
        if not self.merged:
            warnings.warn("Already unmerged. Nothing to do.")
            return
        while len(self.merged_adapters) > 0:
            active_adapter = self.merged_adapters.pop()
            if active_adapter in self.waveft_spectrum.keys():
                self.get_base_layer().weight.data -= self.get_delta_weight(active_adapter)

    def get_delta_weight(self, adapter) -> torch.Tensor:
        return super().get_delta_weight(adapter)

    def forward(self, x: torch.Tensor, *args: Any, **kwargs: Any) -> torch.Tensor:
        previous_dtype = x.dtype

        if self.disable_adapters:
            if self.merged:
                self.unmerge()
            result = self.base_layer(x, *args, **kwargs)
        elif self.merged:
            result = self.base_layer(x, *args, **kwargs)
        else:
            result = self.base_layer(x, *args, **kwargs)
            for active_adapter in self.active_adapters:
                if active_adapter not in self.waveft_spectrum.keys():
                    continue

                delta_w = self.get_delta_weight(active_adapter)
                x = x.to(delta_w.dtype)
                result = result + F.linear(x, delta_w)

        result = result.to(previous_dtype)
        return result

    def __repr__(self) -> str:
        rep = super().__repr__()
        return "waveft." + rep
