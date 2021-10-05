from .utils import isqrt, eval_sh_bases, gen_morton, is_pow2, _get_c_extension
import torch
from torch import nn, autograd
import torch.nn.functional as F
from typing import Union, List, NamedTuple
from dataclasses import dataclass
from warnings import warn
from functools import reduce
from tqdm import tqdm
_C = _get_c_extension()

@dataclass
class RenderOptions:
    """
    Rendering options, see comments
    available:
    :param linear_interp: bool
    :param background_brightness: float
    :param step_epsilon: float
    :param step_size: float, step size for linear_interp = True only
                      (for estimating the integral; in nearest-neighbor case the integral is exactly computed)
    :param sigma_thresh: float
    :param stop_thresh: float
    """
    linear_interp : bool = True           # If true, enables trilinear interpolation (trilerp) during rendering;
                                          # else, uses nearest neighbor interp (like svox)
                                          # Note that trilerp is about 8-9x slower.

    background_brightness : float = 1.0   # [0, 1], the background color black-white

    step_epsilon : float = 1e-3           # Epsilon added to voxel steps, in normalized voxels
                                          #  (i.e. 1 = 1 voxel width, different from svox where 1 = grid width!)
                                          #  (needed in current traversal method for safety,
                                          #   set it to something like 1e-1 for fast rendering.
                                          #   Probably do not set it below 1e-4, increase if getting stuck)

    step_size : float = 0.5               # Step size, in normalized voxels; only used if linear_interp = True
                                          #  (i.e. 1 = 1 voxel width, different from svox where 1 = grid width!)

    sigma_thresh : float = 1e-10          # Voxels with sigmas < this are ignored, in [0, 1]
                                          #  make this higher for fast rendering

    stop_thresh : float = 1e-7            # Stops rendering if the remaining light intensity/termination, in [0, 1]
                                          #  probability is <= this much (forward only)
                                          #  make this higher for fast rendering

    def _to_cpp(self):
        """
        Generate object to pass to C++
        """
        opt = _C.RenderOptions()
        opt.background_brightness = self.background_brightness
        opt.step_epsilon = self.step_epsilon
        opt.step_size = self.step_size
        opt.sigma_thresh = self.sigma_thresh
        opt.stop_thresh = self.stop_thresh
        # Note that the linear_interp option is handled in Python
        return opt

@dataclass
class Rays:
    origins : torch.Tensor
    dirs : torch.Tensor

    def _to_cpp(self):
        """
        Generate object to pass to C++
        """
        spec = _C.RaysSpec()
        spec.origins = self.origins
        spec.dirs = self.dirs
        return spec

    @property
    def is_cuda(self):
        return self.origins.is_cuda and self.dirs.is_cuda


# BEGIN Differentiable CUDA functions with custom gradient
class _SampleGridAutogradFunction(autograd.Function):
    @staticmethod
    def forward(ctx, data : torch.Tensor, grid, points : torch.Tensor):
        assert not points.requires_grad, "Point gradient not supported"
        out = _C.sample_grid(grid, points)
        ctx.save_for_backward(points)
        ctx.grid = grid
        return out

    @staticmethod
    def backward(ctx, grad_out):
        points, = ctx.saved_tensors
        grad_grid = _C.sample_grid_backward(
            ctx.grid, points, grad_out.contiguous()
        )
        if not ctx.needs_input_grad[0]:
            grad_grid = None
        return grad_grid, None, None


class _VolumeRenderFunction(autograd.Function):
    @staticmethod
    def forward(ctx,
                data : torch.Tensor,
                grid,
                rays,
                opt,
                linear_interp : bool):
        cu_fn = _C.volume_render_lerp if linear_interp else _C.volume_render_nn
        color = cu_fn(grid, rays, opt)
        ctx.save_for_backward(color)
        ctx.grid = grid
        ctx.rays = rays
        ctx.opt = opt
        ctx.linear_interp = linear_interp
        return color

    @staticmethod
    def backward(ctx, grad_out):
        color_cache, = ctx.saved_tensors
        cu_fn = _C.volume_render_lerp_backward if ctx.linear_interp else _C.volume_render_nn_backward
        grad_grid = cu_fn(
            ctx.grid, ctx.rays, ctx.opt,
            grad_out.contiguous(),
            color_cache,
        )
        ctx.grid = ctx.rays = ctx.opt = None
        if not ctx.needs_input_grad[0]:
            grad_grid = None
        return grad_grid, None, None, None, None
# END Differentiable CUDA functions with custom gradient


class SparseGrid(nn.Module):
    """
    Main sparse grid data structure.
    initially it will be a dense grid of resolution <reso>.
    Only float32 is supported.

    :param reso: int or List[int, int, int], resolution for resampled grid, as in the constructor
    :param radius: float or List[float, float, float], the 1/2 side length of the grid, optionally in each direction
    :param center: float or List[float, float, float], the center of the grid
    :param use_z_order: bool, if true, stores the data initially in a Z-order curve if possible
    :param device: torch.device, device to store the grid
    """
    def __init__(self,
            reso : Union[int, List[int]]=128,
            radius : Union[float, List[float]]=1.0,
            center : Union[float, List[float]]=[0.0, 0.0, 0.0],
            basis_dim : int = 9, # SH size; square number
            use_z_order=False,
            device : Union[torch.device, str]="cpu"):
        super().__init__()

        assert isqrt(basis_dim) is not None, "basis_dim (SH) must be a square number"
        assert basis_dim >= 1 and basis_dim <= 16, "basis_dim 1-16 supported"
        self.basis_dim = basis_dim

        if isinstance(reso, int):
            reso = [reso] * 3
        else:
            assert len(reso) == 3, \
                   "reso must be an integer or indexable object of 3 ints"
        self.capacity : int = reduce(lambda x,y: x*y, reso)

        if not (reso[0] == reso[1] and reso[0] == reso[2] and is_pow2(reso[0])):
            print("Morton code requires a cube grid of power-of-2 size, ignoring...")
            use_z_order = False
        if use_z_order:
            init_links = gen_morton(reso[0], device=device, dtype=torch.int32)
        else:
            init_links = torch.arange(self.capacity, device=device, dtype=torch.int32)

        self.register_buffer("links", init_links.view(reso))
        self.links : torch.Tensor
        self.data = nn.Parameter(torch.zeros(
            self.capacity, self.basis_dim * 3 + 1, dtype=torch.float32, device=device))

        if isinstance(radius, float) or isinstance(radius, int):
            radius = [radius] * 3
        if isinstance(radius, torch.Tensor):
            radius = radius.to(device='cpu', dtype=torch.float32)
        else:
            radius = torch.tensor(radius, dtype=torch.float32, device='cpu')
        if isinstance(center, torch.Tensor):
            center = center.to(device='cpu', dtype=torch.float32)
        else:
            center = torch.tensor(center, dtype=torch.float32, device='cpu')

        self.radius : torch.Tensor = radius  # CPU
        self.center : torch.Tensor = center  # CPU
        self._offset = 0.5 * (1.0 - self.center / self.radius)
        self._scaling = 0.5 / self.radius

        self.opt = RenderOptions()


    @property
    def data_dim(self):
        """
        Get the number of channels in the data (data.size(1))
        """
        return self.data.size(1)

    def _fetch_links(self, links):
        result = torch.empty((links.size(0), self.data.size(1)), device=links.device, dtype=torch.float32)
        mask = links >= 0
        result[mask] = self.data[links[mask].long()]
        result[~mask] = 0.0
        return result

    def sample(self, points : torch.Tensor, use_kernel : bool = True, grid_coords = False):
        """
        Grid sampling with trilinear interpolation.
        Behaves like torch.nn.functional.grid_sample
        with padding mode border and align_corners=False (better for multi-resolution).

        Any voxel with link < 0 (empty) is considered to have 0 values in all channels
        prior to interpolating.

        :param points: torch.Tensor, (N, 3)
        :param use_kernel: bool, if false uses pure PyTorch version even if on CUDA.
        """
        if use_kernel and self.links.is_cuda and _C is not None:
            assert points.is_cuda
            return _SampleGridAutogradFunction.apply(self.data,
                    self._to_cpp(grid_coords), points)
        else:
            if not grid_coords:
                points = self.world2grid(points)
            points.clamp_min_(0.0)
            for i in range(3):
                points[:, i].clamp_max_(self.links.size(i) - 1)
            l = points.to(torch.long)
            for i in range(3):
                l[:, i].clamp_max_(self.links.size(i) - 2)
            wb = points - l
            wa = 1.0 - wb

            lx, ly, lz = l.unbind(-1)
            links000 = self.links[lx, ly, lz]
            links001 = self.links[lx, ly, lz + 1]
            links010 = self.links[lx, ly + 1, lz]
            links011 = self.links[lx, ly + 1, lz + 1]
            links100 = self.links[lx + 1, ly, lz]
            links101 = self.links[lx + 1, ly, lz + 1]
            links110 = self.links[lx + 1, ly + 1, lz]
            links111 = self.links[lx + 1, ly + 1, lz + 1]

            rgba000 = self._fetch_links(links000)
            rgba001 = self._fetch_links(links001)
            rgba010 = self._fetch_links(links010)
            rgba011 = self._fetch_links(links011)
            rgba100 = self._fetch_links(links100)
            rgba101 = self._fetch_links(links101)
            rgba110 = self._fetch_links(links110)
            rgba111 = self._fetch_links(links111)

            c00 = rgba000 * wa[:, 2:] + rgba001 * wb[:, 2:]
            c01 = rgba010 * wa[:, 2:] + rgba011 * wb[:, 2:]
            c10 = rgba100 * wa[:, 2:] + rgba101 * wb[:, 2:]
            c11 = rgba110 * wa[:, 2:] + rgba111 * wb[:, 2:]

            c0 = c00 * wa[:, 1:2] + c01 * wb[:, 1:2]
            c1 = c10 * wa[:, 1:2] + c11 * wb[:, 1:2]

            samples = c0 * wa[:, :1] + c1 * wb[:, :1]
            return samples

    def forward(self, points : torch.Tensor, use_kernel : bool = True):
        return self.sample(points, use_kernel=use_kernel)

    def _volume_render_gradcheck_nn(self, rays: Rays):
        """
        nearest-neighbor gradcheck version
        """
        def intersect_aabb_unit(cen, invdir):
            """
            voxel aabb ray tracing step
            :param cen: torch.Tensor [B, 3] center
            :param invdir: torch.Tensor [B, 3] 1/dir
            :return: tmin torch.Tensor [B] at least 0;
                     tmax torch.Tensor [B]
            """
            t1 = -cen * invdir
            t2 = t1 + invdir
            tmax = torch.min(torch.max(t1, t2), dim=-1).values
            return tmax

        origins = self.world2grid(rays.origins)
        dirs = rays.dirs / torch.norm(rays.dirs, dim=-1, keepdim=True)
        viewdirs = dirs
        B = dirs.size(0)
        assert origins.size(0) == B
        gsz = self._grid_size()
        dirs = dirs * (self._scaling * gsz).to(device=dirs.device)
        delta_scale = 1.0 / dirs.norm(dim=1)
        dirs *= delta_scale.unsqueeze(-1)

        sh_mult = eval_sh_bases(self.basis_dim, viewdirs)
        invdirs = 1.0 / dirs
        invdirs[dirs == 0] = 1e9

        t1 = (1e-3 - origins) * invdirs
        t2 = (self._grid_size().to(device=dirs.device) - 1.0 - 1e-3 - origins) * invdirs
        t = torch.max(torch.min(t1, t2), dim=-1).values.clamp_min_(0.0)
        tmax = torch.min(torch.max(t1, t2), dim=-1).values

        log_light_intensity = torch.zeros(B, device=origins.device)
        out_rgb = torch.zeros((B, 3), device=origins.device)
        good_indices = torch.arange(B, device=origins.device)

        mask = t < tmax
        good_indices = good_indices[mask]
        origins = origins[mask]
        dirs = dirs[mask]
        invdirs = invdirs[mask]
        t = t[mask]
        sh_mult = sh_mult[mask]
        tmax = tmax[mask]


        while good_indices.numel() > 0:
            pos = origins + t[:, None] * dirs
            l = pos.to(torch.long)
            pos_t = pos - l

            links = self.links[l.unbind(-1)]
            rgba = self._fetch_links(links)
            del links

            subcube_tmax = intersect_aabb_unit(pos_t, invdirs)

            delta_t = subcube_tmax + self.opt.step_epsilon
            log_att = - delta_t * torch.relu(rgba[..., 0]) * delta_scale[good_indices]
            weight = torch.exp(log_light_intensity[good_indices]) * (
                        1.0 - torch.exp(log_att))
            rgb = rgba[:, 1:]
            # [B', 3, n_sh_coeffs]
            rgb_sh = rgb.reshape(-1, 3, self.basis_dim)
            rgb = torch.sigmoid(torch.sum(sh_mult.unsqueeze(-2) * rgb_sh, dim=-1))   # [B', 3]
            rgb = weight[:, None] * rgb[:, :3]

            out_rgb[good_indices] += rgb
            log_light_intensity[good_indices] += log_att
            t += delta_t

            mask = t < tmax
            good_indices = good_indices[mask]
            origins = origins[mask]
            dirs = dirs[mask]
            invdirs = invdirs[mask]
            t = t[mask]
            sh_mult = sh_mult[mask]
            tmax = tmax[mask]
        out_rgb += torch.exp(log_light_intensity).unsqueeze(-1) * \
                   self.opt.background_brightness
        return out_rgb

    def _volume_render_gradcheck_lerp(self, rays : Rays):
        """
        trilerp gradcheck version
        """
        origins = self.world2grid(rays.origins)
        dirs = rays.dirs / torch.norm(rays.dirs, dim=-1, keepdim=True)
        viewdirs = dirs
        B = dirs.size(0)
        assert origins.size(0) == B
        gsz = self._grid_size()
        dirs = dirs * (self._scaling * gsz).to(device=dirs.device)
        delta_scale = 1.0 / dirs.norm(dim=1)
        dirs *= delta_scale.unsqueeze(-1)

        sh_mult = eval_sh_bases(self.basis_dim, viewdirs)
        invdirs = 1.0 / dirs
        invdirs[dirs == 0] = 1e9

        t1 = (1e-3 - origins) * invdirs
        t2 = (self._grid_size().to(device=dirs.device) - 1.0 - 1e-3 - origins) * invdirs
        t = torch.max(torch.min(t1, t2), dim=-1).values.clamp_min_(0.0)
        tmax = torch.min(torch.max(t1, t2), dim=-1).values

        log_light_intensity = torch.zeros(B, device=origins.device)
        out_rgb = torch.zeros((B, 3), device=origins.device)
        good_indices = torch.arange(B, device=origins.device)

        mask = t < tmax
        good_indices = good_indices[mask]
        origins = origins[mask]
        dirs = dirs[mask]
        invdirs = invdirs[mask]
        t = t[mask]
        sh_mult = sh_mult[mask]
        tmax = tmax[mask]


        while good_indices.numel() > 0:
            pos = origins + t[:, None] * dirs
            l = pos.to(torch.long)
            pos -= l

            # BEGIN CRAZY TRILERP
            lx, ly, lz = l.unbind(-1)
            links000 = self.links[lx, ly, lz]
            links001 = self.links[lx, ly, lz + 1]
            links010 = self.links[lx, ly + 1, lz]
            links011 = self.links[lx, ly + 1, lz + 1]
            links100 = self.links[lx + 1, ly, lz]
            links101 = self.links[lx + 1, ly, lz + 1]
            links110 = self.links[lx + 1, ly + 1, lz]
            links111 = self.links[lx + 1, ly + 1, lz + 1]

            rgba000 = self._fetch_links(links000)
            rgba001 = self._fetch_links(links001)
            rgba010 = self._fetch_links(links010)
            rgba011 = self._fetch_links(links011)
            rgba100 = self._fetch_links(links100)
            rgba101 = self._fetch_links(links101)
            rgba110 = self._fetch_links(links110)
            rgba111 = self._fetch_links(links111)

            wa, wb = 1.0 - pos, pos
            c00 = rgba000 * wa[:, 2:] + rgba001 * wb[:, 2:]
            c01 = rgba010 * wa[:, 2:] + rgba011 * wb[:, 2:]
            c10 = rgba100 * wa[:, 2:] + rgba101 * wb[:, 2:]
            c11 = rgba110 * wa[:, 2:] + rgba111 * wb[:, 2:]

            c0 = c00 * wa[:, 1:2] + c01 * wb[:, 1:2]
            c1 = c10 * wa[:, 1:2] + c11 * wb[:, 1:2]

            rgba = c0 * wa[:, :1] + c1 * wb[:, :1]
            # END CRAZY TRILERP

            log_att = - self.opt.step_size * torch.relu(rgba[..., 0]) * delta_scale[good_indices]
            weight = torch.exp(log_light_intensity[good_indices]) * (
                        1.0 - torch.exp(log_att))
            rgb = rgba[:, 1:]
            # [B', 3, n_sh_coeffs]
            rgb_sh = rgb.reshape(-1, 3, self.basis_dim)
            rgb = torch.sigmoid(torch.sum(sh_mult.unsqueeze(-2) * rgb_sh, dim=-1))   # [B', 3]
            rgb = weight[:, None] * rgb[:, :3]

            out_rgb[good_indices] += rgb
            log_light_intensity[good_indices] += log_att
            t += self.opt.step_size

            mask = t < tmax
            good_indices = good_indices[mask]
            origins = origins[mask]
            dirs = dirs[mask]
            invdirs = invdirs[mask]
            t = t[mask]
            sh_mult = sh_mult[mask]
            tmax = tmax[mask]
        out_rgb += torch.exp(log_light_intensity).unsqueeze(-1) * \
                   self.opt.background_brightness
        return out_rgb

    def volume_render(self, rays : Rays,
                      use_kernel : bool = True):
        """
        Standard volume rendering. See grid.opt.* (RenderOptions) for configs.

        :param rays: Rays, (origins (N, 3), dirs (N, 3))
        :param use_kernel: bool, if false uses pure PyTorch version even if on CUDA.
        :return: (N, 3) RGB
        """
        if use_kernel and self.links.is_cuda and _C is not None:
            assert rays.is_cuda
            return _VolumeRenderFunction.apply(self.data, self._to_cpp(),
                                               rays._to_cpp(), self.opt._to_cpp(),
                                               self.opt.linear_interp)
        else:
            warn("Using slow volume rendering, should only be used for debugging")
            if self.opt.linear_interp:
                return self._volume_render_gradcheck_lerp(rays)
            else:
                return self._volume_render_gradcheck_nn(rays)

    def resample(self,
                 reso : Union[int, List[int]]=128,
                 sigma_thresh : float = 5.0,
                 dilate : bool = True):
        """
        Resample and sparsify the grid
        :param reso: int or List[int, int, int], resolution for resampled grid, as in the constructor
        :param sigma_thresh: float, threshold to apply on the sigma
        :param dilate: bool, if true applies dilation of size 1 to the 3D mask for nodes to keep in the grid
                             (keep neighbors in all 28 directions, including diagonals, of the desired nodes)
        """
        device = self.links.device
        if isinstance(reso, int):
            reso = [reso] * 3
        else:
            assert len(reso) == 3, \
                   "reso must be an integer or indexable object of 3 ints"
        self.capacity : int = reduce(lambda x,y: x*y, reso)
        curr_reso = self.links.shape
        dtype = torch.float32
        X = torch.linspace(0.0, curr_reso[0] - 1, reso[0], dtype=dtype)
        Y = torch.linspace(0.0, curr_reso[1] - 1, reso[1], dtype=dtype)
        Z = torch.linspace(0.0, curr_reso[2] - 1, reso[2], dtype=dtype)
        X, Y, Z = torch.meshgrid(X, Y, Z)
        points = torch.stack((X, Y, Z), dim=-1).view(-1, 3)

        batch_size = 720720
        all_sample_vals = []
        all_sample_vals_mask = []
        for i in tqdm(range(0, len(points), batch_size)):
            sample_vals = self.sample(points[i:i+batch_size].to(device=device), grid_coords=True)
            sample_vals_mask = sample_vals[:, 0] > sigma_thresh
            if dilate:
                sample_vals = sample_vals.cpu()
            else:
                sample_vals = sample_vals[sample_vals_mask].cpu()
                sample_vals_mask = sample_vals_mask.cpu()
            all_sample_vals.append(sample_vals)
            all_sample_vals_mask.append(sample_vals_mask)
        del self.data

        sample_vals_mask = torch.cat(all_sample_vals_mask, dim=0)
        del all_sample_vals_mask
        if dilate:
            sample_vals_mask = _C.dilate(sample_vals_mask.view(reso).cuda()).view(-1).cpu()
        sample_vals = torch.cat(all_sample_vals, dim=0)
        del all_sample_vals
        if dilate:
            sample_vals = sample_vals[sample_vals_mask]
        init_links = torch.cumsum(sample_vals_mask.to(torch.int32), dim=-1).int() - 1
        init_links[~sample_vals_mask] = -1

        self.capacity = sample_vals_mask.sum().item()
        del sample_vals_mask
        self.data = nn.Parameter(sample_vals.to(device=device))
        self.links = init_links.view(reso).to(device=device)

    def world2grid(self, points):
        """
        World coordinates to grid coordinates. Grid coordinates are
        normalized to [0, n_voxels] in each side

        :param points: (N, 3)
        :return: (N, 3)
        """
        gsz = self._grid_size()
        offset = self._offset * gsz - 0.5;
        scaling = self._scaling * gsz
        return torch.addcmul(offset.to(device=points.device),
                             points,
                             scaling.to(device=points.device))

    def grid2world(self, points):
        """
        Grid coordinates to world coordinates. Grid coordinates are
        normalized to [0, n_voxels] in each side

        :param points: (N, 3)
        :return: (N, 3)
        """
        gsz = self._grid_size()
        roffset = self.radius * (1.0 / gsz - 1.0) + self.center
        rscaling = 2.0 * self.radius / gsz
        return torch.addcmul(roffset.to(device=points.device),
                             points,
                             rscaling.to(device=points.device))

    def _to_cpp(self, grid_coords : bool = False):
        """
        Generate object to pass to C++
        """
        gspec = _C.SparseGridSpec()
        gspec.data = self.data
        gspec.links = self.links
        if grid_coords:
            gspec._offset = torch.zeros_like(self._offset)
            gspec._scaling = torch.ones_like(self._offset)
        else:
            gsz = self._grid_size()
            gspec._offset = self._offset * gsz - 0.5
            gspec._scaling = self._scaling * gsz
        gspec.basis_dim = self.basis_dim
        return gspec

    def _grid_size(self):
        return torch.tensor(self.links.shape, device='cpu', dtype=torch.float32)
