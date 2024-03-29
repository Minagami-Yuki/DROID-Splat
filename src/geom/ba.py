import ipdb
from typing import Optional

import torch
from einops import rearrange, einsum, reduce
from torch_scatter import scatter_sum

from .chol import schur_solve, schur_block_solve, CholeskySolver, LUSolver, show_matrix
from .projective_ops import projective_transform
from lietorch import SE3
import lietorch

"""
Bundle Adjustment for SLAM implemented in pure Python. 
This is supposed to replicate the CUDA kernel and allows prototyping new functions.

We distinguish the following cases here:
1. We solve the system eij = wi*|| target_ij - Proj(Gij * IProj(di) ||^2
    for every edge ij in the pose graph defined by edge set E and nodes V. 

i) Motion only Bundle Adjustment: We only optimize the poses in the window [t0, t1]
ii) Structure only Bundle Adjustment: We only optimize the disparities in the window [t0, t1]
iii) Full Bundle Adjustment: We optimize both poses and disparities in the window [t0, t1]

2. We solve the system eij = wi*|| target_ij - Proj(Gij * IProj(di) ||^2 + || di - di_prior ||^2
    where we have an additional regularization term for the structure given a prior. 
    Since the prior is only defined for frame i, we only have |V| regularization terms.

3. We have additional scale s and shift o parameters for the prior.
i) We optimize poses, disparities, scale and shift parameters together in [t0, t1]
ii) We optimize disparities, scale and shift parameters for given fixed poses in [t0, t1]

NOTE The prior objective is quite robust, but will still result in artifacts. Interestingly 
these will be occluded, i.e. the objective optimizes a consistent visible scene which matches 
the predicted optical flow. This is mostly stable when having scale vary between [0.1, 2.0] and some shifts. 
Values above/below that result in large drifts when used naively.

NOTE PyTorch 2.1.2 and Python3.11 somehow results in float16 output of matmul 
of two float32 inputs! :/
We had better precision using Python 3.9 and this version of torch. This the reason 
why all matmul / einsum operations are cast to float32 afterwards.

NOTE DO NOT USE THESE FUNCTIONS FOR LARGE-SCLAE SYSTEMS!
The dense Cholesky decomposition solver is numerically unstable for very sparse systems.
Example: These work fine in the frontend until we hit a loop-closure, which suddenly creates a very 
large window [t0, t1] with a lot of sparse entries. The dense solver will then result in nan's.

NOTE Hessian H needs to be positive definite, the Schur complement S should also be positive definite!
we observe not only here but also in the working examples from lietorch, that this is not the case :/
H is symmetric, but only the diagonal elements Hii and Hjj have positive real eigenvalues. This due to 
how we define the residuals! If you define them using two poses gi & gj, there will be off diagonal elements. 
DROID-SLAM uses the same formulation like DSO does, so this is normal

NOTE Introducing additional scale/shift parameters for a prior can destabilize the optimization when also optimizing the poses. 
This creats an ambiguity, where we could scale the disparities and poses with a different scale. This usually diverges!

NOTE we change tensors in place similar to the CUDA kernel API, so be cautious what you pass to these functions
"""


def safe_scatter_add_mat(A: torch.Tensor, ii, jj, n: int, m: int) -> torch.Tensor:
    """Turn a dense (B, N, D, D) matrix into a sparse (B, n*m, D, D) matrix by
    scattering with the indices ii and jj.

    Example:
        In Bundle Adjustment we compute dense Hessian blocks for N camera nodes that are part of the whole scene
        defined by ii, jj. We now need the Hessian for the whole Scene window.
        We thus scatter this depending on the indices into a bigger N*N matrix,
        where each indices ij is the dependency between camera i and camera j.
    """
    # Filter out any negative and out of bounds indices
    v = (ii >= 0) & (jj >= 0) & (ii < n) & (jj < m)
    return scatter_sum(A[:, v], ii[v] * m + jj[v], dim=1, dim_size=n * m)


def safe_scatter_add_vec(b, ii, n):
    """Turn a dense (B, k, D) vector into a sparse (B, n, D) vector by
    scattering with the indices ii, where n >> k
    """
    # Filter out any negative and out of bounds indices
    v = (ii >= 0) & (ii < n)
    return scatter_sum(b[:, v], ii[v], dim=1, dim_size=n)


def safe_scatter_add_mat_inplace(H, data, ii, jj, B, M, D):
    v = (ii >= 0) & (jj >= 0)
    H.scatter_add_(1, (ii[v] * M + jj[v]).view(1, -1, 1, 1).repeat(B, 1, D, D), data[:, v])


def safe_scatter_add_vec_inplace(b, data, ii, B, M, D):
    v = ii >= 0
    b.scatter_add_(1, ii[v].view(1, -1, 1).repeat(B, 1, D), data[:, v])


def additive_retr(disps: torch.Tensor, dz: torch.Tensor, ii) -> torch.Tensor:
    """Apply addition operator to e.g. inv-depth maps where dz can be
    multiple updates (results from multiple constraints) which are scatter summed
    to update the key frames ii.

    d_k2 = d_k1 + dz
    """
    ii = ii.to(device=dz.device)
    return disps + scatter_sum(dz, ii, dim=1, dim_size=disps.shape[1])


def pose_retr(poses: SE3, dx: torch.Tensor, ii) -> SE3:
    """Apply retraction operator to poses, where dx are lie algebra
    updates which come from multiple constraints and are scatter summed
    to the key frame poses ii.

    g_k2 = exp^(dx) * g_k1
    """
    ii = ii.to(device=dx.device)
    return poses.retr(scatter_sum(dx, ii, dim=1, dim_size=poses.shape[1]))


def is_positive_definite(matrix: torch.Tensor, eps=2e-5) -> bool:
    """Check if a Matrix is positive definite by checking symmetry looking at the eigenvalues"""
    return bool((abs(matrix - matrix.mT) < eps).all() and (torch.linalg.eigvals(matrix).real >= 0).all())


def get_keyframe_window(poses, intrinsics, disps, t1, disps_sens, scales, shifts):
    disps_loc, poses_loc, intr_loc = disps[:, :t1], poses[:, :t1], intrinsics[:, :t1]
    to_return = [disps_loc, poses_loc, intr_loc]
    if disps_sens is not None:
        to_return.append(disps_sens[:, :t1])
    if scales is not None and shifts is not None:
        s_loc, o_loc = scales[:, :t1], shifts[:, :t1]
        to_return += [s_loc, o_loc]
    return to_return


def bundle_adjustment(
    target: torch.Tensor,
    weight: torch.Tensor,
    damping: torch.Tensor,
    poses: torch.Tensor,
    disps: torch.Tensor,
    intrinsics: torch.Tensor,
    ii: torch.Tensor,
    jj: torch.Tensor,
    t0: int,
    t1: int,
    disps_sens: Optional[torch.Tensor] = None,
    scales: Optional[torch.Tensor] = None,
    shifts: Optional[torch.Tensor] = None,
    iters: int = 4,
    lm: float = 1e-4,
    ep: float = 0.1,
    structure_only: bool = False,
    motion_only: bool = False,
    scale_prior: bool = False,
    alpha: float = 1.0,
) -> None:
    """Wrapper function around different bundle adjustment methods."""

    assert sum([structure_only, motion_only]) <= 1, "You can either optimize only motion or structure or both!"

    # Convert and batch up the tensors to work with the pure Python code
    Gs = lietorch.SE3(poses[None, ...])
    disps, weight = disps.unsqueeze(0), weight.unsqueeze(0)
    target, intrinsics = target.unsqueeze(0), intrinsics.unsqueeze(0)
    if disps_sens is not None:
        disps_sens = disps_sens.unsqueeze(0)
    if scale_prior:
        assert disps_sens is not None, "You need to provide prior disparities to optimize with scales!"
        scales, shifts = scales.unsqueeze(0), shifts.unsqueeze(0)

    # Prepare arguments for bundle adjustment
    args = (target, weight, Gs, disps, intrinsics, ii, jj, t0, t1)
    if motion_only:
        ba_function = MoBA
        skwargs = {}
    else:
        skwargs = {"all_disps_sens": disps_sens, "eta": damping, "alpha": alpha}
        if scale_prior:
            skwargs["all_scales"], skwargs["all_shifts"] = scales, shifts
            if structure_only:
                ba_function = BA_prior_no_motion
            else:
                ba_function = BA_prior
        else:
            skwargs["structure_only"] = structure_only
            ba_function = BA

    #### Bundle Adjustment Loop
    for i in range(iters):
        ba_function(*args, **skwargs, ep=ep, lm=lm)
        disps.clamp(min=0.001)  # Disparities should never be negative
    ####

    # Update data structure
    poses = Gs.data[0]
    disps = disps[0]  # Remove the batch dimension again
    if scale_prior:
        scales = scales[0]
        shifts = shifts[0]


def get_hessian_and_rhs(
    Jz: torch.Tensor,
    Ji: torch.Tensor,
    Jj: torch.Tensor,
    target: torch.Tensor,
    weight: torch.Tensor,
    coords: torch.Tensor,
    valid: torch.Tensor,
    with_structure: bool = True,
):
    """Get mixed terms of Jacobian and Hessian for constructing the linear system"""
    B, N, _, ht, wd = target.shape
    # Reshape to residuals vector
    r = rearrange(target, "b n xy h w -> b n h w xy") - coords
    # Filter out super large residuals
    valid *= (r.norm(dim=-1) < 250.0).float().unsqueeze(-1)
    r = rearrange(r.double(), "b n h w xy -> b n (h w xy)")
    w = 0.001 * (valid * rearrange(weight, "b n xy h w -> b n h w xy"))
    w = rearrange(w.double(), "b n h w xy -> b n (h w xy) 1")

    Ji = rearrange(Ji, "b n h w xy D -> b n (h w xy) D")
    Jj = rearrange(Jj, "b n h w xy D -> b n (h w xy) D")
    wJiT, wJjT = (w * Ji).mT, (w * Jj).mT

    # Each block is B x N x D x D
    Hii = einsum(wJiT, Ji, "b n i j , b n j k -> b n i k").double()
    Hij = einsum(wJiT, Jj, "b n i j , b n j k -> b n i k").double()
    Hji = einsum(wJjT, Ji, "b n i j , b n j k -> b n i k").double()
    Hjj = einsum(wJjT, Jj, "b n i j , b n j k -> b n i k").double()
    # Each rhs term is B x N x D x 1
    vi = einsum(-wJiT, r, "b n D hwxy, b n hwxy -> b n D").double()
    vj = einsum(-wJjT, r, "b n D hwxy, b n hwxy -> b n D").double()

    if not with_structure:
        return Hii, Hij, Hji, Hjj, vi, vj

    # Mixed term of camera and disp blocks
    # (BNHW x D x 2) x (BNHW x 2 x 1) -> (BNHW x D x 1)
    Jz = rearrange(Jz, "b n h w xy 1 -> b n (h w) xy 1")
    wJiT = rearrange(wJiT, "b n D (hw xy) -> b n hw D xy", hw=ht * wd, xy=2)
    wJjT = rearrange(wJjT, "b n D (hw xy) -> b n hw D xy", hw=ht * wd, xy=2)
    Eik = torch.matmul(wJiT, Jz).squeeze(-1).double()
    Ejk = torch.matmul(wJjT, Jz).squeeze(-1).double()

    # Sparse diagonal block of disparities only
    w = rearrange(w, "b n (hw xy) 1 -> b n hw xy", hw=ht * wd)
    r = rearrange(r, "b n (hw xy) -> b n hw xy", hw=ht * wd)
    wJzT = (w[..., None] * Jz).mT  # (B N HW 1 XY)
    wk = einsum(-wJzT.squeeze(-2), r, "b n hw xy, b n hw xy -> b n hw").double()
    Ck = einsum(wJzT.squeeze(-2), Jz.squeeze(-1), "b n hw xy, b n hw xy -> b n hw")
    Ck = Ck.double()

    return Hii, Hij, Hji, Hjj, Eik, Ejk, Ck, vi, vj, wk


def scatter_pose_structure(Hii, Hij, Hji, Hjj, Eik, Ejk, Ck, vi, vj, wk, ii, jj, kk, bs, m, n, d):
    """Scatter the pose and structure hessians.
    This creates a sparse system out of dense blocks.
    """
    # Scatter add all edges and assemble full Hessian
    # 4 x (B, N, 6, 6) -> (B, M x M, 6, 6)
    H = torch.zeros(bs, m * m, d, d, device=Hii.device, dtype=torch.float64)
    safe_scatter_add_mat_inplace(H, Hii, ii, ii, bs, m, d)
    safe_scatter_add_mat_inplace(H, Hij, ii, jj, bs, m, d)
    safe_scatter_add_mat_inplace(H, Hji, jj, ii, bs, m, d)
    safe_scatter_add_mat_inplace(H, Hjj, jj, jj, bs, m, d)
    H = H.reshape(bs, m, m, d, d)

    v = safe_scatter_add_vec(vi, ii, m) + safe_scatter_add_vec(vj, jj, m)
    v = rearrange(v, "b m d -> b m 1 d 1")

    E = safe_scatter_add_mat(Eik, ii, kk, m, n) + safe_scatter_add_mat(Ejk, jj, kk, m, n)
    E = rearrange(E, "b (m n) hw d -> b m (n hw) d 1", m=m, n=n)

    # Depth only appears if k = i, therefore the gradient is 0 for k != i
    # This reduces the number of elements to M defined by kk
    # kk basically scatters all edges ij onto i, i.e. we sum up all edges that are connected to i
    C = safe_scatter_add_vec(Ck, kk, n)
    w = safe_scatter_add_vec(wk, kk, n)
    return H, E, C, v, w


def MoBA(
    target: torch.Tensor,
    weight: torch.Tensor,
    all_poses: SE3,
    all_disps: torch.Tensor,
    all_intrinsics: torch.Tensor,
    ii: torch.Tensor,
    jj: torch.Tensor,
    t0: int,
    t1: int,
    ep: float = 0.1,
    lm: float = 1e-4,
    rig: int = 1,
) -> None:
    """Motion only bundle adjustment for optimizing pose nodes inside a window [t0, t1].
    The factor graph is defined by ii, jj.

    NOTE This always builds the system for poses 0:t1, but then excludes all poses as fixed before t0.
    """
    # Select keyframe window to optimize over!
    disps, poses, intrinsics = all_disps[:, :t1], all_poses[:, :t1], all_intrinsics[:, :t1]

    # Always fix at least the first keyframe!
    fixedp = max(t0, 1)

    bs, m, ht, wd = disps.shape  # M is a all cameras
    n = ii.shape[0]  # Number of edges for relative pose constraints
    d = poses.manifold_dim  # 6 for SE3, 7 for SIM3

    ### 1: compute jacobians and residuals ###
    coords, valid, (Ji, Jj, Jz) = projective_transform(poses, disps, intrinsics, ii, jj, jacobian=True)
    # NOTE normally this should be -Ji / -Jj and then vi / vj = -J^T @ r
    Ji, Jj, Jz = -Ji.double(), -Jj.double(), -Jz.double()
    ### 2: Construct linear system
    Hii, Hij, Hji, Hjj, vi, vj = get_hessian_and_rhs(Jz, Ji, Jj, target, weight, coords, valid, with_structure=False)

    # only optimize keyframe poses
    m = m - fixedp
    ii = ii / rig - fixedp
    jj = jj / rig - fixedp
    ii, jj = ii.to(torch.int64), jj.to(torch.int64)

    # Assemble larger sparse system for optimization window
    H = torch.zeros(bs, m * m, d, d, device=target.device, dtype=torch.float64)
    safe_scatter_add_mat_inplace(H, Hii, ii, ii, bs, m, d)
    safe_scatter_add_mat_inplace(H, Hij, ii, jj, bs, m, d)
    safe_scatter_add_mat_inplace(H, Hji, jj, ii, bs, m, d)
    safe_scatter_add_mat_inplace(H, Hjj, jj, jj, bs, m, d)
    H = H.reshape(bs, m, m, d, d)

    v = torch.zeros(bs, m, d, device=target.device, dtype=torch.float64)
    safe_scatter_add_vec_inplace(v, vi, ii, bs, m, d)
    safe_scatter_add_vec_inplace(v, vj, jj, bs, m, d)

    H = rearrange(H, "b n1 n2 d1 d2 -> b (n1 d1) (n2 d2)")
    H = H + (ep + lm * H) * torch.eye(H.shape[1], device=H.device)  # Damping
    v = rearrange(v, "b n d -> b (n d) 1")

    ### 3: solve the system + apply retraction ###
    solver = CholeskySolver
    dx = solver.apply(H, v)
    if torch.isnan(dx).any():
        print("Cholesky decomposition failed, trying LU decomposition")
        solver = LUSolver
        dx = solver.apply(H, v)
        if torch.isnan(dx).any():
            print("LU decomposition failed, using 0 update ...")
            dx = torch.zeros_like(dx)
    dx = rearrange(dx, "b (n1 d1) 1 -> b n1 d1", n1=m, d1=d).float()

    # Update only un-fixed poses
    poses1, poses2 = poses[:, :fixedp], poses[:, fixedp:]
    poses2 = poses2.retr(dx)
    poses = lietorch.cat([poses1, poses2], dim=1)
    # Update global poses
    all_poses[:, :t1] = poses


def BA(
    target: torch.Tensor,
    weight: torch.Tensor,
    all_poses: SE3,
    all_disps: torch.Tensor,
    all_intrinsics: torch.Tensor,
    ii: torch.Tensor,
    jj: torch.Tensor,
    t0: int,
    t1: int,
    eta: torch.Tensor,
    all_disps_sens: Optional[torch.Tensor] = None,
    alpha: float = 0.05,
    ep: float = 0.1,
    lm: float = 1e-4,
    structure_only: bool = False,
    rig: int = 1,
) -> None:
    """Bundle Adjustment for optimizing both poses and disparities.

    Shapes that should be expected:
        target / r: (B, N, 2, H, W) -> (BNHW x 2) = (M x 2), i.e. we have BNHW points and 2 (xy) coordinates
        Ji / Jj: (M x 2 x 6) since our function maps a 6D input to a 2D vector
        Jz: (M x 2 x 1) since for the structure we only optimize the 1D disparity
        H: J^T * J = (M x 6 x 6)
        Ji^T * r / Jj^T * r (v): (M x 6 x 2) * (M x 2 x 1) = (M x 6 x 1)
        Jz^T * r (w): (M x 1 x 2) x (M x 2 x 1) = (M x 1 x 1)

    args:
    ---
        target: Predicted reprojected coordinates from node ci -> cj of shape (|E|, 2, H, W)
        weight: Predicted confidence weights of "target" of shape (|E|, 2, H, W)
        eta: Levenberg-Marqhart damping factor on depth of shape (|V|, H, W)
        ii: Timesteps of outgoing edges of shape (|E|)
        jj: Timesteps of incoming edges of shape (|E|)
        t0, t1: Optimization window
    """
    # Select keyframe window to optimize over!
    disps, poses, intrinsics = all_disps[:, :t1], all_poses[:, :t1], all_intrinsics[:, :t1]
    if all_disps_sens is not None:
        disps_sens = all_disps_sens[:, :t1]
    else:
        disps_sens = None

    # Always fix the first pose and then fix all poses outside of optimization window
    fixedp = max(t0, 1)

    B, M, ht, wd = disps.shape  # M is a all cameras
    num_edges = ii.shape[0]  # Number of edges for relative pose constraints
    D = poses.manifold_dim  # 6 for SE3, 7 for SIM3

    ### 1: compute jacobians and residuals ###
    coords, valid, (Ji, Jj, Jz) = projective_transform(poses, disps, intrinsics, ii, jj, jacobian=True)
    Jz, Ji, Jj = -Jz.double(), -Ji.double(), -Jj.double()

    ### 2: Assemble linear system ###
    Hii, Hij, Hji, Hjj, Eik, Ejk, Ck, vi, vj, wk = get_hessian_and_rhs(Jz, Ji, Jj, target, weight, coords, valid)

    # Construct larger sparse system
    kx, kk = torch.unique(ii, return_inverse=True)
    N = len(kx)  # Actual unique key frame nodes to be updated
    # only optimize keyframe poses
    M = M - fixedp
    ii = ii // rig - fixedp
    jj = jj // rig - fixedp

    H, E, C, v, w = scatter_pose_structure(Hii, Hij, Hji, Hjj, Eik, Ejk, Ck, vi, vj, wk, ii, jj, kk, B, M, N, D)
    eta = rearrange(eta, "n h w -> 1 n (h w)")

    if disps_sens is not None:
        m = disps_sens[:, kx].view(B, -1, ht * wd) > 0
        m = m.int()
        # Add alpha only for where there is a prior, else only add normal damping
        C = C + alpha * m + (1 - m) * eta + 1e-7
        w = w - m * alpha * (disps[:, kx] - disps_sens[:, kx]).view(B, -1, ht * wd)
    else:
        C = C + eta + 1e-7  # Apply damping
    C = rearrange(C, "b n hw -> b (n hw) 1 1")
    w = rearrange(w, "b n hw -> b (n hw) 1 1")

    ### 3: solve the system ###
    if structure_only:
        dz = schur_block_solve(H, E, C, v, w, ep=ep, lm=lm, structure_only=True)
        dz = rearrange(dz, "b (n h w) 1 1 -> b n h w", n=N, h=ht, w=wd)
        ### 4: apply retraction ###
        all_disps[:, :t1] = additive_retr(disps, dz, kx)

    else:
        dx, dz = schur_block_solve(H, E, C, v, w, ep=ep, lm=lm)
        dz = rearrange(dz, "b (n h w) 1 1 -> b n h w", n=N, h=ht, w=wd)
        ### 4: apply retraction ###
        # Update only un-fixed poses
        poses1, poses2 = poses[:, :fixedp], poses[:, fixedp:]
        poses2 = poses2.retr(dx)
        poses = lietorch.cat([poses1, poses2], dim=1)
        # Update global poses and disparities
        all_poses[:, :t1] = poses
        all_disps[:, :t1] = additive_retr(disps, dz, kx)


def get_augmented_hessian_and_rhs_full(H, E, C, D, G, F, L, K, v, w, vs, vo):
    """Create augmented larger system with prior optimization variables
    for solving with single Schur complement.

    We now solve the system:

        B   0    0   | E     dxi      v
        0   D    L   | F  *  ds    =  vs
        0   L^T  G   | K     do       vo
        ----------
        E^T F^T  K^T | C     dz       w

    where H_aug consists of B, D, L, G;
    E_aug consists of E, F, K;
    and the Schur trick is still used on C,
    since we can easily invert it and it is by far the largest.
    """
    bs, m, m, d, d = H.shape
    bs, n, n = D.shape

    # There is no coupling between pose graph and scale, shift parameters -> We have to zero pad the augmented hessian
    zeros_pose_so = torch.zeros((bs, d * m, 2 * n), device=H.device, dtype=torch.float64)

    H = rearrange(H, "b m1 m2 d1 d2 -> b (m1 d1) (m2 d2)")
    H_top = torch.cat([H, zeros_pose_so], dim=2)

    DL = torch.cat([D, L], dim=2)
    LG = torch.cat([L.mT, G], dim=2)  # Since L is diagonal, L^T = L
    DLG = torch.cat([DL, LG], dim=1)
    H_bot = torch.cat([zeros_pose_so.mT, DLG], dim=2)
    H_aug = torch.cat([H_top, H_bot], dim=1)

    E = rearrange(E, "b m (n hw) d 1 -> b (d m) (n hw)", m=m, n=n)
    E_aug = torch.cat([E, F, K], dim=1)

    v = rearrange(v, "b m 1 d 1 -> b (m d) 1")
    v_aug = torch.cat([v, vs, vo], dim=1)

    return H_aug, E_aug, C, v_aug, w


def get_regularizor_hessians(
    Js: torch.Tensor, Jo: torch.Tensor, res: torch.Tensor, alpha: float, bs: int, n: int, ht: int, wd: int
):
    """Get Hessian blocks for the regularizor term
    res = || d_i - (s * dprior_i + o) ||^2."""
    F = alpha * Js.mT  # torch.matmul(Js.mT, Jd) # Multiplication with diagonal is the same
    K = alpha * Jo.mT  # torch.matmul(Jo.mT, Jd) # Multiplication with diagonal is the sam e
    D = alpha * torch.matmul(Js.mT, Js)
    G = alpha * torch.matmul(Jo.mT, Jo)
    L = alpha * torch.matmul(Js.mT, Jo)

    vs = torch.matmul(-alpha * Js.mT, res.view(bs, n * ht * wd, 1))
    vo = torch.matmul(-alpha * Jo.mT, res.view(bs, n * ht * wd, 1))

    return F, K, D, G, L, vs, vo


def get_regularizor_jacobians(disps_sens, bs, n, ht, wd):
    """Get Jacobians of second residual || d_i - (s * dprior_i + o) ||^2 w.r.t s and o."""

    def scatter_jacobian(A, ii, jj, n, m):
        B = safe_scatter_add_mat(A, ii.int(), jj.to(torch.int64), n, m)
        B = rearrange(B, "b (n1 n2) 1 1 -> b n1 n2", n1=n, n2=m)
        return B

    Jsi = -disps_sens.view(bs, n * ht * wd, 1, 1) * torch.ones(bs, n * ht * wd, 1, 1, device=disps_sens.device)
    Joi = -torch.ones(bs, n * ht * wd, 1, 1, device=disps_sens.device)

    # Scatter pattern for r2 Jacobians
    jsii = torch.arange(n, device=disps_sens.device).repeat_interleave(ht * wd)
    jsjj = torch.arange(0, ht * wd, device=disps_sens.device).repeat(n)
    jjk = ht * wd * torch.arange(n, device=disps_sens.device).repeat_interleave(ht * wd)
    jsjj = jsjj + jjk

    # Jacobians: (B x N x NHW) for Js, Jo and (B x NHW x NHW) for Jd
    Js = scatter_jacobian(Jsi, jsjj, jsii, n * ht * wd, n).double()
    Jo = scatter_jacobian(Joi, jsjj, jsii, n * ht * wd, n).double()
    return Js, Jo


# NOTE this is unstable and does not seem to work properly
# this might be because of a bug or because there is an ambiguity between poses and scales
# the same system works if we fix the poses, so I think this rules out a potential implementation bug
# TODO extend with kx_exp similar to CUDA kernel here and BA_prior_nomotion
def BA_prior(
    target: torch.Tensor,
    weight: torch.Tensor,
    all_poses: SE3,
    all_disps: torch.Tensor,
    all_intrinsics: torch.Tensor,
    ii: torch.Tensor,
    jj: torch.Tensor,
    t0: int,
    t1: int,
    eta: torch.Tensor,
    all_disps_sens: torch.Tensor,
    all_scales: torch.Tensor,
    all_shifts: torch.Tensor,
    ep: float = 0.1,
    lm: float = 1e-4,
    alpha: float = 0.001,
):
    """Bundle Adjustment for optimizing with a depth prior.
    Monocular depth can only be estimated up to an unknown global scale.
    Neural networks which predict depth from only a single image are notoriously bad
    at estimating consistent depth over a temporal video, i.e. the scale is not consistent over time.
    This function optimizes unknown scale and shift parameters of each key frame of the prior on top of
    camera pose graph and disparities.
    """
    # Select keyframe window to optimize over!
    disps, poses, intrinsics, disps_sens, scales, shifts = get_keyframe_window(
        all_poses, all_intrinsics, all_disps, t1, all_disps_sens, all_scales, all_shifts
    )
    # Always fix the first pose and then fix all poses outside of optimization window
    fixedp = max(t0, 1)
    bs, m, ht, wd = disps.shape  # M is a all cameras
    d = poses.manifold_dim  # 6 for SE3, 7 for SIM3

    ### 1: compute jacobians and residuals ###
    coords, valid, (Ji, Jj, Jz) = projective_transform(poses, disps, intrinsics, ii, jj, jacobian=True)
    Jz, Ji, Jj = -Jz.double(), -Ji.double(), -Jj.double()

    ### 2: Assemble linear system ###
    Hii, Hij, Hji, Hjj, Eik, Ejk, Ck, vi, vj, wk = get_hessian_and_rhs(Jz, Ji, Jj, target, weight, coords, valid)

    # Construct larger sparse system
    kx, kk = torch.unique(ii, return_inverse=True)
    n = len(kx)  # Actual unique key frame nodes to be updated
    # Always fix at least the first keyframe!
    m = m - fixedp
    ii = ii - fixedp
    jj = jj - fixedp

    H, E, C, v, w = scatter_pose_structure(Hii, Hij, Hji, Hjj, Eik, Ejk, Ck, vi, vj, wk, ii, jj, kk, bs, m, n, d)
    eta = rearrange(eta, "n h w -> 1 n (h w)")

    # C also needs a second term C2 added on top of it, which is J2d^T * J2d
    # Since J2d is just 1, we can simply add a 1 for every single entry here
    # NOTE the original code for RGBD mode adds the derivative term +1*alpha ONLY when a prior exists
    # else it applies damping.
    # We apply both damping and the second term derivative to stay true to the objective function
    C = C + alpha * 1.0 + eta
    C = rearrange(C, "b n hw -> b (n hw) 1 1")

    # Residuals for r2(disps, s, o) (B, N, HW)
    scaled_prior = disps_sens[:, kx].view(bs, -1, ht * wd) * scales[:, kx, None] + shifts[:, kx, None]
    # scaled_prior = scaled_prior.clamp(min=0.001)  # Prior should never be negative, i.e. clip this if s and o are diverging
    r2 = (disps[:, kx].view(bs, -1, ht * wd) - scaled_prior).double()
    w = w - alpha * r2
    w = rearrange(w, "b n hw -> b (n hw) 1 1")

    ### 3: Create augmented system
    ## Jacobians & Hessians of scales and shifts and mixed term with disparities
    Js, Jo = get_regularizor_jacobians(disps_sens[:, kx], bs, n, ht, wd)
    F, K, D, G, L, vs, vo = get_regularizor_hessians(Js, Jo, r2, alpha, bs, n, ht, wd)
    H_aug, E_aug, C, v_aug, w = get_augmented_hessian_and_rhs_full(H, E, C, D, G, F, L, K, v, w, vs, vo)

    ### 4: Solve whole system with dX, ds, do, dZ ###
    # NOTE this needs to be solve with LU decomposition since because of E,
    # the resulting Schur complement S is not positive definite
    dxso, dz = schur_solve(H_aug, E_aug, C, v_aug, w, ep=ep, lm=lm, solver="lu")
    dx, ds, do = dxso[:, : d * m], dxso[:, d * m : d * m + n], dxso[:, d * m + n :]
    dz = rearrange(dz, "b (n h w) -> b n h w", n=n, h=ht, w=wd)
    dx = rearrange(dx, "b (m d) 1 -> b m d", m=m, d=d)

    ### 4: apply retraction ###
    # Update only un-fixed poses
    poses1, poses2 = poses[:, :fixedp], poses[:, fixedp:]
    poses2 = poses2.retr(dx)
    poses = lietorch.cat([poses1, poses2], dim=1)

    # Update global poses, disparities, scales and shifts
    all_poses[:, :t1] = poses
    all_disps[:, :t1] = additive_retr(disps, dz, kx)
    all_scales[:, :t1] = additive_retr(scales, ds.squeeze(-1), kx)
    all_shifts[:, :t1] = additive_retr(shifts, do.squeeze(-1), kx)


# TODO add the overall indexing strategy to the other functions
# this is required to use the same damping parameters as the CUDA kernel
def BA_prior_no_motion(
    target: torch.Tensor,
    weight: torch.Tensor,
    all_poses: SE3,
    all_disps: torch.Tensor,
    all_intrinsics: torch.Tensor,
    ii: torch.Tensor,
    jj: torch.Tensor,
    t0: int,
    t1: int,
    eta: torch.Tensor,
    all_disps_sens: torch.Tensor,
    all_scales: torch.Tensor,
    all_shifts: torch.Tensor,
    ep: float = 0.1,
    lm: float = 1e-4,
    alpha: float = 0.05,
):

    disps, poses, intrinsics, disps_sens, scales, shifts = get_keyframe_window(
        all_poses, all_intrinsics, all_disps, t1, all_disps_sens, all_scales, all_shifts
    )
    # Always fix the first pose and then fix all poses outside of optimization window
    fixedp = max(t0, 1)
    bs, m, ht, wd = disps.shape  # M is a all cameras

    ### 1: compute jacobians and residuals ###
    coords, valid, (Ji, Jj, Jz) = projective_transform(poses, disps, intrinsics, ii, jj, jacobian=True)
    Jz, Ji, Jj = -Jz.double(), -Ji.double(), -Jj.double()

    ### 2: Assemble linear system ###
    _, _, _, _, _, _, Ck, _, _, wk = get_hessian_and_rhs(Jz, Ji, Jj, target, weight, coords, valid)

    ## Construct larger sparse system
    # What the CUDA kernel does:
    # C = accum_cuda(Cii, ii, kx_exp), where kx not only includes the values from ii, but also additional unique values from [t0, t1]
    # This changes the size of C to len(kx_exp) which would then also fit eta
    # What happens under the hood is that if a value in ii is not in kx_exp, then we will have a respective zero term in C since that node is not contributing to the total energy
    # This does not work with scatter sum code, so we need to pad the array in retrospect here instead of how its done in the CUDA code
    # NOTE we normally dont need to do this but we choose compatibility with the update operator that can also use the CUDA kernel
    kx, kk = torch.unique(ii, return_inverse=True)
    ts = torch.arange(t0, t1).long().to(ii.device)
    kx_exp, kk_exp = torch.unique(torch.cat([ts, ii], dim=0), return_inverse=True)

    n = len(kx)  # Actual unique key frame nodes to be updated
    n_exp = len(kx_exp)  # Expand with [t0, t1] to include all nodes in interval even if they dont contribute
    empty_nodes = n_exp - n
    # Always fix at least the first keyframe!
    m = m - fixedp
    ii = ii - fixedp
    jj = jj - fixedp

    C = safe_scatter_add_vec(Ck, kk, n)
    w = safe_scatter_add_vec(wk, kk, n)

    non_empty_nodes = torch.isin(kx_exp, kx)
    C_exp = torch.zeros((bs, n_exp, ht * wd), device=C.device, dtype=torch.float64)
    w_exp = torch.zeros((bs, n_exp, ht * wd), device=w.device, dtype=torch.float64)
    C_exp[:, non_empty_nodes] = C
    w_exp[:, non_empty_nodes] = w

    eta = rearrange(eta, "n h w -> 1 n (h w)")
    # C = C + alpha * 1.0 + eta
    # C = rearrange(C, "b n hw -> b (n hw) 1 1")
    C_exp = C_exp + alpha * 1.0 + eta
    C_exp = rearrange(C_exp, "b n hw -> b (n hw) 1 1")

    scaled_prior = disps_sens[:, kx_exp].view(bs, -1, ht * wd) * scales[:, kx_exp, None] + shifts[:, kx_exp, None]
    # scaled_prior = scaled_prior.clamp(min=0.001)  # Prior should never be negative, i.e. clip this if s and o are diverging
    r2 = (disps[:, kx_exp].view(bs, -1, ht * wd) - scaled_prior).double()
    w_exp[:, non_empty_nodes] = w_exp[:, non_empty_nodes] - alpha * r2[:, non_empty_nodes]
    w_exp = rearrange(w_exp, "b n hw -> b (n hw) 1 1")

    ### 3: Create augmented system
    Js_exp, Jo_exp = get_regularizor_jacobians(disps_sens[:, kx_exp], bs, n_exp, ht, wd)
    if empty_nodes > 0:
        Js_exp[:, :, ~non_empty_nodes] = torch.zeros(
            (bs, n_exp * ht * wd, empty_nodes), device=Js_exp.device, dtype=torch.float64
        )
        Jo_exp[:, :, ~non_empty_nodes] = torch.zeros(
            (bs, n_exp * ht * wd, empty_nodes), device=Jo_exp.device, dtype=torch.float64
        )
    F, K, D, G, L, vs, vo = get_regularizor_hessians(Js_exp, Jo_exp, r2, alpha, bs, n_exp, ht, wd)

    # Define new H and E block
    DL = torch.cat([D, L], dim=2)
    LG = torch.cat([L.mT, G], dim=2)  # Since L is diagonal, L^T = L
    H = torch.cat([DL, LG], dim=1)
    E = torch.cat([F, K], dim=1)
    v = torch.cat([vs, vo], dim=1)

    ### 4: Solve whole system with dX, ds, do, dZ ###
    dso, dz = schur_solve(H, E, C_exp, v, w_exp, ep=ep, lm=lm)
    ds, do = dso[:, :n_exp], dso[:, n_exp:]
    dz = rearrange(dz, "b (n h w) -> b n h w", n=n_exp, h=ht, w=wd)

    ### 4: apply retraction ###
    all_disps[:, :t1] = additive_retr(disps, dz, kx_exp)
    all_scales[:, :t1] = additive_retr(scales, ds.squeeze(-1), kx_exp)
    all_shifts[:, :t1] = additive_retr(shifts, do.squeeze(-1), kx_exp)
