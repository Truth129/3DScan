"""CAS-Net, the ImageCAS-X benchmark's coronary lumen model, and its inference.

The network is ImageCAS-X's port of Dong et al.'s CAS-Net (Medical Image Analysis 85, 2023),
kept module for module so the published weights load by name. Inference follows the
benchmark's released pipeline at commit dbc7343: linear resampling to 0.5 mm, HU -200 to
1000 scaled to 0 to 1, Gaussian-weighted tiles at half overlap, the mean of four passes
mirrored on the first two axes, then the class with the higher sigmoid after linear
resampling back to the source grid.

This module imports PyTorch, so only the worker child imports it.

The network code is adapted from ImageCAS-X (github.com/kitbransby/ImageCAS-X):

    MIT License

    Copyright (c) 2026 Kit M. Bransby

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in all
    copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
    SOFTWARE.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import SimpleITK as sitk
import torch
from torch import nn
import torch.nn.functional as F

SPACING = 0.5  # mm, on every axis
HU = (-200.0, 1000.0)
# Voxels along the network's x, y and z, which are the model axes in order.
PATCH = (128, 160, 160)
SIGMA_SCALE = 0.125
# Training flipped only x and y; a head-to-foot flip is not anatomy the network saw.
MIRRORS = ((), (0,), (1,), (0, 1))


class ResEncoder3d(nn.Module):
    def __init__(self, in_channels, out_channels):
        super().__init__()
        self.conv1 = nn.Conv3d(in_channels, out_channels, kernel_size=3, padding=1)
        self.bn1 = nn.BatchNorm3d(out_channels)
        self.conv2 = nn.Conv3d(out_channels, out_channels, kernel_size=3, padding=1)
        self.bn2 = nn.BatchNorm3d(out_channels)
        self.relu = nn.ReLU(inplace=False)
        self.conv1x1 = nn.Conv3d(in_channels, out_channels, kernel_size=1)

    def forward(self, x):
        residual = self.conv1x1(x)
        out = self.relu(self.bn1(self.conv1(x)))
        out = self.relu(self.bn2(self.conv2(out)))
        return self.relu(out + residual)


class Decoder3d(nn.Module):
    def __init__(self, in_channels, out_channels):
        super().__init__()
        self.conv = nn.Sequential(
            nn.Conv3d(in_channels, out_channels, kernel_size=3, padding=1),
            nn.BatchNorm3d(out_channels),
            nn.ReLU(inplace=False),
            nn.Conv3d(out_channels, out_channels, kernel_size=3, padding=1),
            nn.BatchNorm3d(out_channels),
            nn.ReLU(inplace=False),
        )

    def forward(self, x):
        return self.conv(x)


class AGFF(nn.Module):
    """Attention-guided feature fusion: channel attention gates the low-level features only."""

    def __init__(self, low_channels, high_channels, reduction=16):
        super().__init__()
        self.channel_reduce = nn.Conv3d(high_channels, low_channels, kernel_size=1, stride=1, padding=0)
        self.avg_pool = nn.AdaptiveAvgPool3d(1)
        self.max_pool = nn.AdaptiveMaxPool3d(1)
        self.mlp = nn.Sequential(
            nn.Linear(low_channels * 2, (low_channels * 2) // reduction, bias=False),
            nn.ReLU(inplace=False),
            nn.Linear((low_channels * 2) // reduction, low_channels * 2, bias=False),
        )
        self.sigmoid = nn.Sigmoid()

    def forward(self, low_feat, high_feat):
        high_feat = F.interpolate(high_feat, size=low_feat.shape[2:], mode="trilinear", align_corners=True)
        high_feat = self.channel_reduce(high_feat)
        fused = torch.cat([low_feat, high_feat], dim=1)
        b, c, _, _, _ = fused.size()
        avg_out = self.mlp(self.avg_pool(fused).view(b, c))
        max_out = self.mlp(self.max_pool(fused).view(b, c))
        att = self.sigmoid(avg_out + max_out).view(b, c, 1, 1, 1)
        return att[:, : low_feat.size(1), :, :, :] * low_feat + high_feat


class SAFE(nn.Module):
    """Scale-aware feature enhancement: dilated-conv gates per channel group, then non-local attention."""

    def __init__(self, in_channels, rates=(1, 2, 3, 5)):
        super().__init__()
        self.branches = nn.ModuleList(
            [
                nn.Sequential(
                    nn.Conv3d(in_channels // 4, in_channels // 4, kernel_size=3, padding=r, dilation=r),
                    nn.BatchNorm3d(in_channels // 4),
                    nn.Sigmoid(),
                )
                for r in rates
            ]
        )
        self.q_conv = nn.Conv3d(in_channels, in_channels, kernel_size=(3, 1, 1), padding=(1, 0, 0))
        self.k_conv = nn.Conv3d(in_channels, in_channels, kernel_size=(1, 3, 1), padding=(0, 1, 0))
        self.v_conv = nn.Conv3d(in_channels, in_channels, kernel_size=(1, 1, 3), padding=(0, 0, 1))
        self.softmax = nn.Softmax(dim=-1)

    def forward(self, x):
        groups = torch.chunk(x, 4, dim=1)
        multi_scale = torch.cat([branch(groups[i]) * groups[i] for i, branch in enumerate(self.branches)], dim=1)
        B, C, H, W, D = multi_scale.size()
        Q = self.q_conv(multi_scale).view(B, C, -1)
        K = self.k_conv(multi_scale).view(B, C, -1)
        V = self.v_conv(multi_scale).view(B, C, -1)
        att = self.softmax(torch.bmm(Q.permute(0, 2, 1), K) / (C**0.5))
        att_feat = torch.bmm(att, V.permute(0, 2, 1)).permute(0, 2, 1).view(B, C, H, W, D)
        return multi_scale + att_feat


class MSFA(nn.Module):
    """Multi-scale feature aggregation, smallest scale first, ending in two-class logits."""

    def __init__(self, in_channels_list):
        super().__init__()
        self.compress = nn.ModuleList([nn.Conv3d(c, 8, kernel_size=1) for c in in_channels_list])
        self.att_convs = nn.ModuleList([nn.Conv3d(16, 8, kernel_size=3, padding=1) for _ in range(len(in_channels_list) - 1)])
        self.sigmoid = nn.Sigmoid()
        self.out_conv = nn.Conv3d(8, 2, kernel_size=3, padding=1)

    def forward(self, features):
        compressed = [c(f) for c, f in zip(self.compress, features)]
        agg = compressed[0]
        for i in range(1, len(compressed)):
            up = F.interpolate(agg, size=compressed[i].shape[2:], mode="trilinear", align_corners=True)
            att = self.sigmoid(self.att_convs[i - 1](torch.cat([compressed[i], up], dim=1)))
            agg = att * compressed[i] + up
        return self.out_conv(agg)


class CASNet3D(nn.Module):
    """Five-level residual encoder, SAFE bottleneck, AGFF-fused decoder and MSFA head; returns logits."""

    def __init__(self):
        super().__init__()
        self.enc_input = ResEncoder3d(1, 16)
        self.encoder1 = ResEncoder3d(16, 32)
        self.encoder2 = ResEncoder3d(32, 64)
        self.encoder3 = ResEncoder3d(64, 128)
        self.encoder4 = ResEncoder3d(128, 256)
        self.downsample = nn.MaxPool3d(kernel_size=2, stride=2)
        self.safe = SAFE(256)
        self.agff4 = AGFF(low_channels=128, high_channels=256)
        self.decoder4 = Decoder3d(256, 128)
        self.deconv4 = nn.ConvTranspose3d(256, 128, kernel_size=2, stride=2)
        self.agff3 = AGFF(low_channels=64, high_channels=128)
        self.decoder3 = Decoder3d(128, 64)
        self.deconv3 = nn.ConvTranspose3d(128, 64, kernel_size=2, stride=2)
        self.agff2 = AGFF(low_channels=32, high_channels=64)
        self.decoder2 = Decoder3d(64, 32)
        self.deconv2 = nn.ConvTranspose3d(64, 32, kernel_size=2, stride=2)
        self.agff1 = AGFF(low_channels=16, high_channels=32)
        self.decoder1 = Decoder3d(32, 16)
        self.deconv1 = nn.ConvTranspose3d(32, 16, kernel_size=2, stride=2)
        self.msfa = MSFA([128, 64, 32, 16])

    def forward(self, x):
        e0 = self.enc_input(x)
        e1 = self.encoder1(self.downsample(e0))
        e2 = self.encoder2(self.downsample(e1))
        e3 = self.encoder3(self.downsample(e2))
        e4 = self.safe(self.encoder4(self.downsample(e3)))
        d4 = self.decoder4(torch.cat([self.agff4(e3, e4), self.deconv4(e4)], dim=1))
        d3 = self.decoder3(torch.cat([self.agff3(e2, d4), self.deconv3(d4)], dim=1))
        d2 = self.decoder2(torch.cat([self.agff2(e1, d3), self.deconv2(d3)], dim=1))
        d1 = self.decoder1(torch.cat([self.agff1(e0, d2), self.deconv1(d2)], dim=1))
        return self.msfa([d4, d3, d2, d1])


def network(path, device):
    """The pinned weights are a plain state dict, so they load without running any pickled code."""
    model = CASNet3D()
    model.load_state_dict(torch.load(path, map_location="cpu", weights_only=True))
    return model.to(device).eval()


def resampled(values, spacing, new_spacing, size):
    """Linear resampling between two grids that share their first voxel and their axes, as the benchmark's SimpleITK step.

    `values` is z, y, x; spacing and size are x, y, z.
    """
    image = sitk.GetImageFromArray(np.ascontiguousarray(values, dtype=np.float32))
    image.SetSpacing([float(s) for s in spacing])
    resampler = sitk.ResampleImageFilter()
    resampler.SetOutputSpacing([float(s) for s in new_spacing])
    resampler.SetSize([int(s) for s in size])
    resampler.SetInterpolator(sitk.sitkLinear)
    resampler.SetDefaultPixelValue(0.0)
    return sitk.GetArrayFromImage(resampler.Execute(image))


def tile_starts(size, patch, stride):
    if size <= patch:
        return [0]
    starts = list(range(0, size - patch + 1, stride))
    if starts[-1] != size - patch:
        starts.append(size - patch)
    return starts


def gaussian(patch):
    """nnU-Net's tile weighting, unnormalised, because stitching divides by the summed weights."""
    px, py, pz = patch
    sx, sy, sz = (max(p * SIGMA_SCALE, 1e-8) for p in patch)
    dx, dy, dz = torch.meshgrid(*(torch.arange(p, dtype=torch.float32) - (p - 1) / 2.0 for p in patch), indexing="ij")
    return torch.exp(-((dx / sx) ** 2 + (dy / sy) ** 2 + (dz / sz) ** 2) / 2)


def stitched(model, volume, step_size, device):
    """Logits for one x, y, z volume: tiles one at a time, zero-padded past the edge, weighted and summed on the CPU."""
    X, Y, Z = volume.shape
    patch = PATCH
    stride = [max(1, int(round(p * step_size))) for p in patch]
    weight = gaussian(patch)
    total, count = None, torch.zeros(1, X, Y, Z)
    for x0 in tile_starts(X, patch[0], stride[0]):
        for y0 in tile_starts(Y, patch[1], stride[1]):
            for z0 in tile_starts(Z, patch[2], stride[2]):
                x1, y1, z1 = min(x0 + patch[0], X), min(y0 + patch[1], Y), min(z0 + patch[2], Z)
                tile = torch.zeros(1, 1, *patch)
                tile[0, 0, : x1 - x0, : y1 - y0, : z1 - z0] = volume[x0:x1, y0:y1, z0:z1]
                logits = model(tile.to(device))[0].float().cpu()
                if total is None:
                    total = torch.zeros(len(logits), X, Y, Z)
                w = weight[: x1 - x0, : y1 - y0, : z1 - z0]
                total[:, x0:x1, y0:y1, z0:z1] += logits[:, : x1 - x0, : y1 - y0, : z1 - z0] * w
                count[:, x0:x1, y0:y1, z0:z1] += w
    total /= count.clamp(min=1e-8)
    return total


def predict(model, model_folder, image, spacing, device, step=lambda _: None):
    """The lumen on the model-axis grid of `image` (1, z, y, x), as 0 and 1. `spacing` is z, y, x."""
    selected = torch.device("cpu") if device == "cpu" else torch.device("cuda", int(device.partition(":")[2] or 0))
    step("preparing the scan")
    source = image[0]
    spacing_xyz = [float(s) for s in spacing[::-1]]
    size_xyz = source.shape[::-1]
    new_size = [int(round(size * s / SPACING)) for size, s in zip(size_xyz, spacing_xyz)]
    values = resampled(source, spacing_xyz, [SPACING] * 3, new_size).transpose(2, 1, 0)
    values = (np.clip(values, *HU) - HU[0]) / (HU[1] - HU[0])
    volume = torch.from_numpy(np.ascontiguousarray(values, dtype=np.float32))
    del values
    net = network(Path(model_folder) / model.checkpoint, selected)
    mean = None
    with torch.inference_mode():
        for number, axes in enumerate(MIRRORS, 1):
            step(f"pass {number} of {len(MIRRORS)}")
            logits = stitched(net, torch.flip(volume, axes) if axes else volume, model.tile_step_size, selected)
            if axes:
                logits = torch.flip(logits, [axis + 1 for axis in axes])
            mean = logits if mean is None else mean + logits
    del volume
    step("saving the masks")
    probabilities = torch.sigmoid(mean / len(MIRRORS))
    # Linear resampling commutes with the difference, so its sign is the benchmark's argmax of two resampled channels.
    lumen_ahead = (probabilities[1] - probabilities[0]).numpy().transpose(2, 1, 0)
    del mean, probabilities
    return (resampled(lumen_ahead, [SPACING] * 3, spacing_xyz, size_xyz) > 0).astype(np.uint8), torch.__version__
