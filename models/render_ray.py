# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from collections import OrderedDict
import torch
import torch.nn.functional as F
from einops import rearrange, repeat
import torch.nn as nn

########################################################################################################################
# helper functions for nerf ray rendering
########################################################################################################################


def sample_pdf(bins, weights, N_samples, det=False):
    '''
    Args:
        bins: tensor of shape [batch, N_rays, M+1], M is the number of bins
        weights: tensor of shape [batch, N_rays, M]
        N_samples: number of samples along each ray
        det: if True, will perform deterministic sampling

    Returns: [batch, N_rays, N_samples]
    '''

    batch = bins.shape[0]
    M = weights.shape[-1]
    weights += 1e-5
    # Get pdf
    pdf = weights / torch.sum(weights, dim=-1, keepdim=True)    # [batch, N_rays, M]
    cdf = torch.cumsum(pdf, dim=-1)  # [batch, N_rays, M]
    cdf = torch.cat([torch.zeros_like(cdf[..., 0:1]), cdf], dim=-1) # [batch, N_rays, M+1]

    # Take uniform samples
    if det:
        u = torch.linspace(0., 1., N_samples, device=bins.device)
        u = u[None, None, :].repeat(bins.shape[:2] + (1,))       # [batch, N_rays, N_samples]
    else:
        u = torch.rand(batch, bins.shape[1], N_samples, device=bins.device)

    # Invert CDF
    above_inds = torch.zeros_like(u, dtype=torch.long)       # [batch, N_rays, N_samples]
    for i in range(M):
        above_inds += (u >= cdf[..., i:i+1]).long()

    # random sample inside each bin
    below_inds = torch.clamp(above_inds-1, min=0)
    inds_g = torch.stack((below_inds, above_inds), dim=-1)     # [batch, N_rays, N_samples, 2]

    cdf = cdf.unsqueeze(2).repeat(1, 1, N_samples, 1)  # [batch, N_rays, N_samples, M+1]
    cdf_g = torch.gather(input=cdf, dim=-1, index=inds_g)  # [batch, N_rays, N_samples, 2]

    bins = bins.unsqueeze(2).repeat(1, 1, N_samples, 1)  # [batch, N_rays, N_samples, M+1]
    bins_g = torch.gather(input=bins, dim=-1, index=inds_g)  # [batch, N_rays, N_samples, 2]

    denom = cdf_g[..., 1] - cdf_g[..., 0]      # [batch, N_rays, N_samples]
    denom = torch.where(denom < 1e-5, torch.ones_like(denom), denom)
    t = (u - cdf_g[..., 0]) / denom

    samples = bins_g[..., 0] + t * (bins_g[..., 1]-bins_g[..., 0])

    return samples

def sample_along_camera_ray(ray_o, ray_d, depth_range,
                            N_samples,
                            inv_uniform=False,
                            det=False):
    '''
    :param ray_o: origin of the ray in scene coordinate system; tensor of shape [N_rays, 3] or [Batch, N_rays, 3]
    :param ray_d: homogeneous ray direction vectors in scene coordinate system; tensor of shape [N_rays, 3] or [Batch, N_rays, 3]
    :param depth_range: [B, 2] (near_depth, far_depth)
    :param inv_uniform: if True, uniformly sampling inverse depth
    :param det: if True, will perform deterministic sampling
    :return: tensor of shape [Batch, N_rays, N_samples, 3]
    '''

    if ray_o.ndim == 2:
        ray_o = ray_o[None, :]
    if ray_d.ndim == 2:
        ray_d = ray_d[None, :]

    # will sample inside [near_depth, far_depth]
    # assume the nearest possible depth is at least (min_ratio * depth)
    near_depth_value = depth_range[:, 0]
    far_depth_value = depth_range[:, 1]
    assert torch.all(near_depth_value > 0) and torch.all(far_depth_value > 0) and torch.all(far_depth_value > near_depth_value)

    near_depth = near_depth_value[..., None] * torch.ones_like(ray_d[..., 0])

    far_depth = far_depth_value[..., None] * torch.ones_like(ray_d[..., 0])
    if inv_uniform:
        start = 1. / near_depth     # [Batch, N_rays,]
        step = (1. / far_depth - start) / (N_samples-1)
        inv_z_vals = torch.stack([start+i*step for i in range(N_samples)], dim=-1)  # [Batch, N_rays, N_samples]
        z_vals = 1. / inv_z_vals
    else:
        start = near_depth
        step = (far_depth - near_depth) / (N_samples-1)
        z_vals = torch.stack([start+i*step for i in range(N_samples)], dim=-1)  # [Batch, N_rays, N_samples]

    if not det:
        # get intervals between samples
        mids = .5 * (z_vals[..., 1:] + z_vals[..., :-1])
        upper = torch.cat([mids, z_vals[..., -1:]], dim=-1)
        lower = torch.cat([z_vals[..., 0:1], mids], dim=-1)
        # uniform samples in those intervals
        t_rand = torch.rand_like(z_vals)
        z_vals = lower + (upper - lower) * t_rand   # [N_rays, N_samples]

    ray_d = ray_d.unsqueeze(2).repeat(1, 1, N_samples, 1)  # [N_rays, N_samples, 3]
    ray_o = ray_o.unsqueeze(2).repeat(1, 1, N_samples, 1)
    pts = z_vals.unsqueeze(-1) * ray_d + ray_o       # [N_rays, N_samples, 3]
    return pts, z_vals


########################################################################################################################
# ray rendering of nerf
########################################################################################################################

def raw2outputs(raw, z_vals, white_bkgd=False):
    '''
    Args:
        raw: raw network output; tensor of shape [batch, N_rays, N_samples, 4]
        z_vals: depth of point samples along rays; tensor of shape [batch, N_rays, N_samples]
    Returns:
        {'rgb': [batch, N_rays, 3], 'depth': batch, [N_rays,], 'weights': [batch, N_rays,]}
    '''
    rgb = raw[..., :3]     # [batch, N_rays, N_samples, 3]
    sigma = raw[..., 3]    # [batch, N_rays, N_samples]

    # Changed to include dists to imitate pixelnerf
    sigma2alpha = lambda sigma, dists: 1. - torch.exp(-dists * torch.relu(sigma))

    # point samples are ordered with increasing depth
    # interval between samples
    dists = z_vals[..., 1:] - z_vals[..., :-1]
    dists = torch.cat([dists, dists[..., -1:]], dim=-1)  # [batch, N_rays, N_samples]

    alpha = sigma2alpha(sigma, dists)  # [batch, N_rays, N_samples]

    # Eq. (3): T
    T = torch.cumprod(1. - alpha + 1e-10, dim=-1)[..., :-1]   # [batch, N_rays, N_samples-1]
    T = torch.cat((torch.ones_like(T[..., 0:1]), T), dim=-1)  # [batch, N_rays, N_samples]

    # maths show weights, and summation of weights along a ray, are always inside [0, 1]
    weights = alpha * T     # [N_rays, N_samples]
    rgb_map = torch.sum(weights.unsqueeze(-1) * rgb, dim=2)  # [N_rays, 3]

    if white_bkgd:
        rgb_map = rgb_map + (1. - torch.sum(weights, dim=-1, keepdim=True))


    depth_map = torch.sum(weights * z_vals, dim=-1)     # [N_rays,]

    ret = OrderedDict([('rgb', rgb_map),
                       ('depth', depth_map),
                       ('weights', weights),                # used for importance sampling of fine samples
                       ('mask', torch.ones_like(rgb_map[..., 0])),
                       ('alpha', alpha),
                       ('z_vals', z_vals)
                       ])

    return ret


def sample_one_plane(coords2d, plane):
    sampled_features = nn.functional.grid_sample(plane, coords2d,
                                                mode='bilinear', padding_mode='border', align_corners=True)
    # sampled_features (1, 32, 512, 64)
    return sampled_features.permute(0, 2, 3, 1).contiguous()

# def sample_once

def sample_from_plane(sample_coordinates, feature_plane):

    xy_embed = sample_one_plane(sample_coordinates[..., 0:2], feature_plane[0])
    yz_embed = sample_one_plane(sample_coordinates[..., 1:3], feature_plane[1])
    xz_embed = sample_one_plane(sample_coordinates[..., :3:2], feature_plane[2])

    # mean
    features = torch.stack([xy_embed, yz_embed, xz_embed])
    features = features.mean(0)

    return features # (B, num_rays, pts_per_ray, feat_dim)

def sample_from_multi_plane(sample_coordinates, feature_plane):
    feature_plane0 = feature_plane[0]
    feature_plane1 = feature_plane[1]

    feature0 = sample_from_plane(sample_coordinates, feature_plane0)
    feature1 = sample_from_plane(sample_coordinates, feature_plane1)

    features = torch.cat([feature0, feature1], axis=-1)

    return features

def render_rays(ray_batch,
                model,
                featmaps,
                N_samples,
                inv_uniform=False,
                N_importance=0,
                det=False,
                white_bkgd=False):
    '''
    Args:
        ray_batch: {'ray_o': [batch, N_rays, 3] , 'ray_d': [batch, N_rays, 3], 'view_dir': [batch, N_rays, 2]}
        model:  {'net_coarse':  , 'net_fine': }
        featmaps: feature maps for inference [b, c, h, w] or [b, c, d, h, w]
        projector: projector object
        N_samples: samples along each ray (for both coarse and fine model)
        inv_uniform: if True, uniformly sample inverse depth for coarse model
        det: if True, will deterministicly sample depths
        white_bkgd: if True, assume background is white
    Return:
        {'outputs_coarse': {}, 'outputs_fine': {}}
    '''
    ret = {'outputs_coarse': None,
           'outputs_fine': None, 
           'tv': None}

    pts, z_vals = sample_along_camera_ray(ray_o=ray_batch['ray_o'],
                                          ray_d=ray_batch['ray_d'],
                                          depth_range=ray_batch['depth_range'],
                                          N_samples=N_samples, inv_uniform=inv_uniform, det=det)
    _, _, N_samples = pts.shape[:3] # (1, 512, 64, 3)

    features = sample_from_multi_plane(pts, featmaps)

    dir = repeat(ray_batch['ray_d'].unsqueeze(0), 'b nr c -> b nr ns c', ns=N_samples)

    b, n_rays, n_samples, _ = features.shape
    features = features.reshape(-1, features.shape[-1])
    dir = dir.reshape(-1, 3)
    enc_dir = model.direction_encoder(dir)
    coarse_features = torch.cat([features, enc_dir], dim=-1)
    coarse_features = nn.functional.pad(coarse_features, (0, 4), 'constant', 1)
    coarse_feats = model.net_coarse(coarse_features).to(dir)
    raw_coarse = torch.cat([model.sigmoid(coarse_feats[..., :-1]), model.softplus(coarse_feats[..., -1:])], -1).reshape(b, n_rays, n_samples, 4)

    outputs_coarse = raw2outputs(raw_coarse, z_vals, white_bkgd=white_bkgd)
    ret['outputs_coarse'] = outputs_coarse

    if N_importance > 0:
        assert model.net_fine is not None
        # detach since we would like to decouple the coarse and fine networks
        weights = outputs_coarse['weights'].clone().detach()            # [batch, N_rays, N_samples]
        if inv_uniform:
            inv_z_vals = 1. / z_vals
            inv_z_vals_mid = .5 * (inv_z_vals[..., 1:] + inv_z_vals[..., :-1])   # [batch, N_rays, N_samples-1]
            weights = weights[..., 1:-1]      # [batch, N_rays, N_samples-2]
            inv_z_vals = sample_pdf(bins=torch.flip(inv_z_vals_mid, dims=[-1]),
                                    weights=torch.flip(weights, dims=[-1]),
                                    N_samples=N_importance, det=det)  # [batch, N_rays, N_importance]
            z_samples = 1. / inv_z_vals
        else:
            # take mid-points of depth samples
            z_vals_mid = .5 * (z_vals[..., 1:] + z_vals[..., :-1])   # [batch, N_rays, N_samples-1]
            weights = weights[..., 1:-1]      # [N_rays, N_samples-2]
            z_samples = sample_pdf(bins=z_vals_mid, weights=weights,
                                   N_samples=N_importance, det=det)  # [batch, N_rays, N_importance]

        z_vals = torch.cat((z_vals, z_samples), dim=-1)  # [batch, N_rays, N_samples + N_importance]
        z_vals, _ = torch.sort(z_vals, dim=-1)
        N_total_samples = N_samples + N_importance     

        viewdirs = ray_batch['ray_d'].unsqueeze(0)[:, :, None].expand([-1, -1, N_total_samples, -1])
        ray_o = ray_batch['ray_o'].unsqueeze(0)[:, :, None].repeat(1, 1, N_total_samples, 1)        

        # pts: (1, 512, 128, 3)
        pts = z_vals.unsqueeze(-1) * viewdirs + ray_o  # [batch, N_rays, N_samples + N_importance, 3]

        features = sample_from_multi_plane(pts, featmaps)
        dir = repeat(ray_batch['ray_d'].unsqueeze(0), 'b nr c -> b nr ns c', ns=N_total_samples)
 
        b, n_rays, n_samples, _ = features.shape
        features = features.reshape(-1, features.shape[-1])
        dir = dir.reshape(-1, 3)
        enc_dir = model.direction_encoder(dir)
        fine_features = torch.cat([features, enc_dir], dim=-1)
        fine_features = nn.functional.pad(fine_features, (0, 4), 'constant', 1)
        fine_feats = model.net_fine(fine_features).to(dir)
        raw_fine = torch.cat([model.sigmoid(fine_feats[..., :-1]), model.softplus(fine_feats[..., -1:])], -1).reshape(b, n_rays, n_samples, 4)

        outputs_fine = raw2outputs(raw_fine, z_vals, white_bkgd=white_bkgd)
        ret['outputs_fine'] = outputs_fine

        tv = 0
        for featmap in featmaps:
            spatial_grids = [0, 1, 2]
            for grid_id in spatial_grids:
                tv += compute_plane_tv(featmap[grid_id])
        ret['tv'] = tv

    return ret


def compute_plane_tv(t):
    batch_size, c, h, w = t.shape
    count_h = batch_size * c * (h-1) * w
    count_w = batch_size * c * h * (w - 1)
    h_tv = torch.square(t[..., 1:, :] - t[..., :h-1, :]).sum()
    w_tv = torch.square(t[..., :, 1:] - t[..., :, :w-1]).sum()
    return 2 * (h_tv / count_h + w_tv / count_w)   